"""
Watchdog Agent - autonomous failure diagnosis and self-healing.

Architecture (v3):
  - Phase 1: Claude diagnoses failure from logs + relevant source files
  - Phase 2: Claude reviews its own patches against full file context
  - Static: py_compile + AST name-resolution check (via pyflakes if available)
  - SAFE landing: patches are committed to a PR branch, not main (F1/F2/E2)
    Humans or a merge-gate CI can review before landing. No more silent main writes.
  - CI gate: waits for CI to pass on the PR branch before marking done

Critical fixes in v3 (from audit report):
  F1/F2/E2: Watchdog NO LONGER writes to main directly. All patches go to
             branch watchdog/fix-<run_id> and a PR is opened for review.
             This eliminates the infinite re-trigger loop (F2) because pushing
             to a non-main branch does not trigger ci.yml (which only runs on main).
  Pending:  Robust JSON extraction (_parse_json), full tracebacks on exit,
             import builtins fix for AST checker.
"""
import ast
import builtins
import json
import os
import re
import sys
import traceback as _tb
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from github import Github, GithubException
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "ismaelloveexcel/ai-coworker-workspace")

if not ANTHROPIC_API_KEY:
    print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
    sys.exit(1)
if not GITHUB_TOKEN:
    print("ERROR: GH_PAT / GITHUB_TOKEN not set", file=sys.stderr)
    sys.exit(1)

WATCHDOG_ATTEMPT  = int(os.environ.get("WATCHDOG_ATTEMPT", "1"))
MAX_FIX_ATTEMPTS  = int(os.environ.get("WATCHDOG_MAX_RETRIES", "3"))
MODEL             = os.environ.get("WATCHDOG_MODEL", "claude-sonnet-4-5")
FAILED_RUN_ID     = os.environ.get("FAILED_RUN_ID", "")
FAILED_WORKFLOW   = os.environ.get("FAILED_WORKFLOW", "")

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_gh     = Github(GITHUB_TOKEN)
_repo   = _gh.get_repo(GITHUB_REPO)

# ---------------------------------------------------------------------------
# JSON extraction helper (robust: handles fences, leading prose, nesting)
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """
    Extract and parse the first JSON object from text, regardless of
    markdown fences, leading/trailing prose, or whitespace variants.
    Raises json.JSONDecodeError if nothing parseable is found.
    """
    text = text.strip()
    # Strip markdown code fence if present (handles trailing newline variants)
    fence = re.match(r"^```[a-z]*\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find balanced { ... } block
    start = text.find('{')
    if start == -1:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    depth, in_str, escape = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i+1])
    raise json.JSONDecodeError("Unbalanced JSON object", text, start)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def fetch_logs(run_id: str) -> str:
    """Download logs for a GitHub Actions run."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}/logs"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
        # Logs come as a zip; extract text naively
        if raw[:2] == b'PK':
            import io, zipfile
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                parts = []
                for name in sorted(z.namelist()):
                    if name.endswith('.txt'):
                        parts.append(z.read(name).decode('utf-8', errors='replace'))
            return '\n'.join(parts)[-20000:]  # last 20k chars
        return raw.decode('utf-8', errors='replace')[-20000:]
    except urllib.error.HTTPError as e:
        return f"[Could not fetch logs: HTTP {e.code} {e.reason}]"
    except Exception as e:
        return f"[Could not fetch logs: {e}]"


def get_file(path: str, ref: str = "main") -> Optional[str]:
    """Fetch file content from GitHub. Returns None on error."""
    try:
        contents = _repo.get_contents(path, ref=ref)
        import base64
        return base64.b64decode(contents.content).decode('utf-8', errors='replace')
    except Exception:
        return None


def upsert_file(path: str, content: str, message: str, branch: str) -> bool:
    """
    Upsert a file on the given branch (NOT main).
    Returns True on success.
    """
    try:
        existing = _repo.get_contents(path, ref=branch)
        _repo.update_file(path, message, content, existing.sha, branch=branch)
    except GithubException as e:
        if e.status == 404:
            _repo.create_file(path, message, content, branch=branch)
        else:
            raise
    return True


def create_watchdog_branch(run_id: str) -> str:
    """Create a branch for watchdog patches. Returns branch name."""
    branch_name = f"watchdog/fix-{run_id}-attempt-{WATCHDOG_ATTEMPT}"
    try:
        main_sha = _repo.get_git_ref("heads/main").object.sha
        _repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=main_sha)
        print(f"Created branch: {branch_name}")
    except GithubException as e:
        if e.status == 422:  # Already exists
            print(f"Branch already exists: {branch_name}")
        else:
            raise
    return branch_name


def open_watchdog_pr(branch: str, diagnosis: str, patches: List[Dict], applied: int, total: int) -> str:
    """Open a PR from the watchdog branch. Returns PR URL."""
    # Check for existing open PR
    existing = list(_repo.get_pulls(state="open", head=f"{_repo.owner.login}:{branch}"))
    if existing:
        print(f"PR already exists: {existing[0].html_url}")
        return existing[0].html_url

    patch_summary = "\n".join(
        f"- `{p.get('file', '?')}`: {p.get('description', 'patch')}"
        for p in patches[:10]
    )
    body = f"""## Watchdog Auto-Fix — Attempt {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS}

**Failed run:** [{FAILED_RUN_ID}](https://github.com/{GITHUB_REPO}/actions/runs/{FAILED_RUN_ID})
**Workflow:** {FAILED_WORKFLOW}
**Patches applied:** {applied}/{total}

### Diagnosis
{diagnosis[:2000]}

### Patches
{patch_summary}

---
*Generated by Watchdog Agent. Review before merging.*
*Merging this PR will run CI on the patched code. If CI passes, the fix is validated.*
"""
    pr = _repo.create_pull(
        title=f"[Watchdog] Auto-fix for run {FAILED_RUN_ID} (attempt {WATCHDOG_ATTEMPT})",
        body=body,
        head=branch,
        base="main",
    )
    print(f"Opened PR: {pr.html_url}")
    return pr.html_url


def wait_for_ci(branch: str, timeout_seconds: int = 300) -> Optional[str]:
    """
    Poll for CI completion on the given branch.
    Returns 'success', 'failure', or None on timeout.
    Matches by workflow file path (not display name) to survive renames (F22 fix).
    """
    import time
    deadline = time.time() + timeout_seconds
    target_workflow = ".github/workflows/ci.yml"
    print(f"Waiting for CI on branch {branch!r} (timeout={timeout_seconds}s)...")
    while time.time() < deadline:
        time.sleep(12)
        try:
            runs = _repo.get_workflow_runs(branch=branch, event="push")
            matching = [
                r for r in runs
                if getattr(r, 'path', '') == target_workflow or r.name == "CI"
            ]
            if not matching:
                continue
            # Pick the most recent run
            latest = sorted(matching, key=lambda r: r.created_at, reverse=True)[0]
            if latest.status != "completed":
                continue
            print(f"CI completed: {latest.conclusion} (run {latest.id})")
            return latest.conclusion
        except Exception as e:
            print(f"CI poll error: {e}")
    print(f"CI timed out after {timeout_seconds}s")
    return None


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------

def validate_syntax(path: str, content: str) -> Tuple[bool, str]:
    """py_compile check on the patched content."""
    import py_compile, tempfile
    with tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        py_compile.compile(tmp, doraise=True)
        return True, ""
    except py_compile.PyCompileError as e:
        return False, str(e)
    finally:
        os.unlink(tmp)


def validate_names(path: str, content: str) -> Tuple[bool, str]:
    """
    Run pyflakes on patched content if available, else fall back to basic
    AST undefined-name scan. Uses import builtins to avoid __builtins__ dict/module
    ambiguity (F25 fix).
    """
    # Prefer pyflakes for accurate analysis
    try:
        from pyflakes import api as pf_api, reporter as pf_reporter
        import io
        out = io.StringIO()
        rep = pf_reporter.Reporter(out, out)
        warnings = pf_api.check(content, path, reporter=rep)
        if warnings > 0:
            msg = out.getvalue().strip()
            # Filter out known false positives for module-level patterns
            lines = [l for l in msg.splitlines() if 'imported but unused' not in l]
            if lines:
                return False, "\n".join(lines[:5])
        return True, ""
    except ImportError:
        pass

    # Fallback: basic AST name check
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    defined: set = set()
    safe: set = set(dir(builtins))  # import builtins, not __builtins__ (F25)
    safe |= {"__name__", "__file__", "__doc__", "TYPE_CHECKING", "annotations"}

    # Collect top-level definitions
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)

    # Check top-level Name loads
    undefined = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and node.id not in safe:
                undefined.append(node.id)

    if undefined:
        unique = list(dict.fromkeys(undefined))[:5]
        return False, f"Possibly undefined names: {unique}"
    return True, ""


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patches(patches: List[Dict], branch: str) -> Tuple[int, int]:
    """
    Apply patches to the given branch (NOT main).
    Returns (applied_count, total_count).
    Patches with 'find' that are not found in the current file are skipped
    with a warning (logged) — caller checks applied < total.
    """
    applied = 0
    total = len(patches)

    for patch in patches:
        file_path = patch.get("file", "")
        find_str  = patch.get("find", "")
        replace   = patch.get("replace", "")
        desc      = patch.get("description", "patch")

        if not file_path or not find_str:
            print(f"  SKIP: patch missing file or find: {desc!r}")
            total -= 1
            continue

        current = get_file(file_path, ref=branch)
        if current is None:
            print(f"  SKIP: file not found on branch: {file_path}")
            continue

        if find_str not in current:
            print(f"  SKIP: find string not present in {file_path}: {find_str[:80]!r}")
            continue

        patched = current.replace(find_str, replace, 1)

        # Validate before committing
        ok_syn, syn_err = validate_syntax(file_path, patched)
        if not ok_syn:
            print(f"  REJECT {file_path}: syntax error: {syn_err}")
            continue

        ok_names, names_err = validate_names(file_path, patched)
        if not ok_names:
            print(f"  REJECT {file_path}: undefined names: {names_err}")
            continue

        try:
            upsert_file(file_path, patched, f"watchdog: {desc}", branch)
            print(f"  APPLIED {file_path}: {desc}")
            applied += 1
        except Exception as e:
            print(f"  ERROR applying {file_path}: {e}")

    return applied, total


# ---------------------------------------------------------------------------
# Claude phases
# ---------------------------------------------------------------------------

_CONTEXT_FILES = [
    "backend/agent_loop.py",
    "backend/claude_wrapper.py",
    "backend/tool_adapters.py",
    "backend/db.py",
    "backend/main.py",
    "watchdog.py",
]


def _build_source_context() -> str:
    """Fetch all relevant source files for diagnosis context."""
    parts = []
    for path in _CONTEXT_FILES:
        content = get_file(path) or "(not found)"
        parts.append(f"--- {path} ---\n{content}\n")
    return "\n".join(parts)


def phase1_diagnose(logs: str, source_context: str) -> Dict:
    """
    Ask Claude to diagnose the failure and produce patches.
    Returns parsed JSON with keys: diagnosis, fix_type, patches, escalation_reason.
    """
    prompt = f"""You are a senior Python engineer diagnosing a GitHub Actions failure.

## Failed workflow
Run ID: {FAILED_RUN_ID}
Workflow: {FAILED_WORKFLOW}
Attempt: {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS}

## Logs (last 20k chars)
{logs[-15000:]}

## Source files
{source_context[-25000:]}

## Task
Analyse the logs and source files. Identify the root cause.
Respond with ONLY a JSON object (no markdown, no commentary):

{{
  "diagnosis": "clear description of root cause",
  "fix_type": "code_patch" | "config" | "transient" | "escalate",
  "patches": [
    {{
      "file": "path/to/file.py",
      "find": "exact string to replace (must be unique in file)",
      "replace": "replacement string",
      "description": "one-line description"
    }}
  ],
  "escalation_reason": "only if fix_type=escalate"
}}

Rules:
- fix_type=transient: flaky test / network hiccup, no patch needed
- fix_type=escalate: cannot diagnose or fix is too risky
- fix_type=code_patch: provide specific patches using exact strings from the source
- Each patch.find must appear EXACTLY ONCE in the target file
- Do not invent variable names or functions that don't exist
"""
    response = _client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        print(f"Phase 1 JSON parse error: {e}\nRaw: {raw[:500]}", file=sys.stderr)
        raise


def phase2_review(patches: List[Dict], source_context: str, diagnosis: str) -> Dict:
    """
    Ask Claude to review its own patches against full file context.
    Returns {"approved": bool, "corrected_patches": [...], "rejection_reason": "..."}
    """
    patches_json = json.dumps(patches, indent=2)
    prompt = f"""You are a code reviewer checking patches proposed by another Claude instance.

## Diagnosis
{diagnosis[:1000]}

## Proposed patches
{patches_json}

## Full source context
{source_context[-25000:]}

Review each patch carefully:
1. Does the find string appear exactly once in the target file?
2. Will the replacement compile (no syntax errors)?
3. Does it introduce undefined variables or imports?
4. Does it actually fix the diagnosed issue?

Respond with ONLY a JSON object:
{{
  "approved": true | false,
  "corrected_patches": [ ... ],
  "rejection_reason": "only if approved=false"
}}

If approved=true, corrected_patches may be the same as the input or improved.
If approved=false, corrected_patches should be empty and rejection_reason must be set.
"""
    response = _client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        print(f"Phase 2 JSON parse error: {e}\nRaw: {raw[:500]}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main():
    print(f"=== Watchdog v3 | run={FAILED_RUN_ID} | attempt={WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS} ===")

    # Attempt cap
    if WATCHDOG_ATTEMPT > MAX_FIX_ATTEMPTS:
        print(f"Max fix attempts ({MAX_FIX_ATTEMPTS}) reached. Opening escalation issue.")
        _repo.create_issue(
            title=f"[Watchdog] Escalation: exceeded {MAX_FIX_ATTEMPTS} fix attempts for run {FAILED_RUN_ID}",
            body=f"Watchdog could not auto-fix the failure in run {FAILED_RUN_ID} after {MAX_FIX_ATTEMPTS} attempts. Manual intervention required.",
            labels=["watchdog-escalation"],
        )
        sys.exit(0)

    # Fetch logs
    print("Fetching logs...")
    logs = fetch_logs(FAILED_RUN_ID) if FAILED_RUN_ID else "[No run ID provided]"
    print(f"Log length: {len(logs)} chars")

    # Fetch source context
    print("Fetching source context...")
    source_context = _build_source_context()

    # Phase 1: diagnose
    print("Phase 1: diagnosing...")
    try:
        result = phase1_diagnose(logs, source_context)
    except Exception as e:
        _tb.print_exc()
        print(f"Phase 1 failed: {e}", file=sys.stderr)
        sys.exit(1)

    fix_type = result.get("fix_type", "escalate")
    diagnosis = result.get("diagnosis", "(no diagnosis)")
    patches = result.get("patches", [])

    print(f"Diagnosis: {diagnosis[:200]}")
    print(f"Fix type: {fix_type} | Patches: {len(patches)}")

    if fix_type == "transient":
        print("Transient failure detected. No action needed.")
        sys.exit(0)

    if fix_type == "escalate" or not patches:
        reason = result.get("escalation_reason", "No patches proposed")
        print(f"Escalating: {reason}")
        _repo.create_issue(
            title=f"[Watchdog] Escalation: run {FAILED_RUN_ID}",
            body=f"**Diagnosis:** {diagnosis}\n\n**Reason:** {reason}\n\nRun: https://github.com/{GITHUB_REPO}/actions/runs/{FAILED_RUN_ID}",
            labels=["watchdog-escalation"],
        )
        sys.exit(0)

    # Phase 2: review
    print("Phase 2: reviewing patches...")
    try:
        review = phase2_review(patches, source_context, diagnosis)
    except Exception as e:
        _tb.print_exc()
        print(f"Phase 2 failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not review.get("approved", False):
        reason = review.get("rejection_reason", "Phase 2 rejected patches")
        print(f"Patches rejected: {reason}")
        _repo.create_issue(
            title=f"[Watchdog] Patches rejected for run {FAILED_RUN_ID}",
            body=f"**Diagnosis:** {diagnosis}\n\n**Rejection:** {reason}\n\nPatches:\n{json.dumps(patches, indent=2)[:2000]}",
            labels=["watchdog-escalation"],
        )
        sys.exit(0)

    final_patches = review.get("corrected_patches") or patches

    # Create a PR branch (NOT main) for the fix
    print("Creating watchdog branch...")
    try:
        branch = create_watchdog_branch(FAILED_RUN_ID)
    except Exception as e:
        _tb.print_exc()
        print(f"Failed to create branch: {e}", file=sys.stderr)
        sys.exit(1)

    # Apply patches to the branch
    print(f"Applying {len(final_patches)} patches to branch {branch!r}...")
    try:
        applied, total = apply_patches(final_patches, branch)
    except Exception as e:
        _tb.print_exc()
        print(f"Patch application error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Applied {applied}/{total} patches")

    if applied == 0:
        print("No patches applied successfully. Escalating.")
        _repo.create_issue(
            title=f"[Watchdog] All patches failed for run {FAILED_RUN_ID}",
            body=f"**Diagnosis:** {diagnosis}\n\nAll {total} patches failed validation. Review and apply manually.",
            labels=["watchdog-escalation"],
        )
        sys.exit(1)

    if applied < total:
        print(f"WARNING: only {applied}/{total} patches applied. PR will note partial application.")

    # Open a PR for review (key change: humans/CI gate reviews before merge)
    print("Opening PR...")
    try:
        pr_url = open_watchdog_pr(branch, diagnosis, final_patches, applied, total)
        print(f"PR opened: {pr_url}")
    except Exception as e:
        _tb.print_exc()
        print(f"Failed to open PR: {e}", file=sys.stderr)
        sys.exit(1)

    # Wait for CI on the PR branch to validate the fix
    print("Waiting for CI on PR branch...")
    try:
        ci_result = wait_for_ci(branch, timeout_seconds=300)
    except Exception as e:
        _tb.print_exc()
        print(f"CI polling error: {e}", file=sys.stderr)
        ci_result = None

    if ci_result == "success":
        print(f"CI passed on branch {branch!r}. Fix validated — review and merge the PR: {pr_url}")
    elif ci_result == "failure":
        print(f"CI failed on branch {branch!r}. The patch may be incomplete. Check: {pr_url}")
        # Don't exit 1 — the PR is open, CI failure on the branch doesn't trigger watchdog
        # (watchdog.yml only fires on main CI or agent workflow failures, not branch CI)
    else:
        print(f"CI result unknown/timeout. PR is open for manual review: {pr_url}")

    print("=== Watchdog v3 complete ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
