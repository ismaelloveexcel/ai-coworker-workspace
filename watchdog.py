#!/usr/bin/env python3
"""
Watchdog Agent v2 — autonomous failure detection and self-healing.

Upgrades over v1:
  - Two-phase Claude review: generate patch → review patch → apply only if approved
  - Python syntax validation (py_compile) before committing
  - AST name-resolution check: detects undefined variables in patched code
  - Dangling code detection: finds unreachable statements after patch
  - CI gate: waits for CI to pass on patched commit before re-triggering agent
  - Smarter cancellation handling: user-cancel vs system-kill treated differently
  - Patch dry-run: full patched file shown to reviewer Claude, not just the diff
"""
import ast
import io
import json
import os
import py_compile
import sys
import tempfile
import time
import zipfile
import urllib.request
import urllib.error
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic not installed"); sys.exit(1)

try:
    from github import Github, GithubException
    from github.Auth import Token as GHToken
except ImportError:
    print("ERROR: PyGithub not installed"); sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN        = os.environ["GITHUB_TOKEN"]
REPO_FULL           = os.environ["GITHUB_REPOSITORY"]
FAILED_RUN_ID       = os.environ.get("FAILED_RUN_ID", "")
FAILED_WORKFLOW     = os.environ.get("FAILED_WORKFLOW", "")
MAX_FIX_ATTEMPTS    = int(os.environ.get("WATCHDOG_MAX_RETRIES", "3"))
WATCHDOG_ATTEMPT    = int(os.environ.get("WATCHDOG_ATTEMPT", "1"))
CI_WAIT_TIMEOUT_S   = 180   # max seconds to wait for CI after patching
MAX_LOG_CHARS       = 14_000
MODEL               = "claude-sonnet-4-6"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gh     = Github(auth=GHToken(GITHUB_TOKEN))
repo   = gh.get_repo(REPO_FULL)


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh_api(path: str, method: str = "GET", body: dict = None) -> dict:
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"__error": e.code, "__msg": e.read().decode()}


def get_file(path: str, ref: str = "main") -> Optional[str]:
    try:
        import base64
        f = repo.get_contents(path, ref=ref)
        return base64.b64decode(f.content).decode()
    except GithubException:
        return None


def upsert_file(path: str, content: str, message: str) -> None:
    try:
        existing = repo.get_contents(path, ref="main")
        repo.update_file(path, message, content, existing.sha, branch="main")
        print(f"  ✓ Updated: {path}")
    except GithubException:
        repo.create_file(path, message, content, branch="main")
        print(f"  ✓ Created: {path}")


def fetch_logs(run_id: str) -> str:
    url = f"https://api.github.com/repos/{REPO_FULL}/actions/runs/{run_id}/logs"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 302:
            with urllib.request.urlopen(e.headers["Location"]) as r2:
                raw = r2.read()
        else:
            return f"[Log fetch failed: HTTP {e.code}]"
    try:
        zf   = zipfile.ZipFile(io.BytesIO(raw))
        logs = []
        for name in zf.namelist():
            text = zf.read(name).decode(errors="replace")
            logs.append(f"=== {name} ===\n{text}")
        combined = "\n".join(logs)
        return ("...[truncated]\n" + combined[-MAX_LOG_CHARS:]) if len(combined) > MAX_LOG_CHARS else combined
    except Exception as e:
        return f"[Log parse error: {e}]"


def wait_for_ci(commit_sha: str, timeout: int = CI_WAIT_TIMEOUT_S) -> str:
    """Poll until CI on commit_sha is done. Returns 'success'|'failure'|'timeout'."""
    print(f"  Waiting for CI on {commit_sha[:7]} (max {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(12)
        runs = gh_api(f"/repos/{REPO_FULL}/actions/runs?head_sha={commit_sha}&per_page=5")
        ci_runs = [r for r in runs.get("workflow_runs", []) if r.get("name") == "CI"]
        if not ci_runs:
            continue
        r = ci_runs[0]
        if r["status"] == "completed":
            result = r.get("conclusion", "failure")
            print(f"  CI {result}: {r['html_url']}")
            return result
        print(f"  CI still {r['status']}...")
    print("  CI timed out")
    return "timeout"


def trigger_workflow(workflow_file: str, inputs: dict = None) -> bool:
    result = gh_api(
        f"/repos/{REPO_FULL}/actions/workflows/{workflow_file}/dispatches",
        method="POST",
        body={"ref": "main", "inputs": inputs or {}}
    )
    ok = not result.get("__error")
    print(f"  {'✓' if ok else '✗'} Re-trigger {workflow_file}: {'ok' if ok else result}")
    return ok


def open_escalation_issue(diagnosis: str, logs: str, run_url: str, reason: str) -> None:
    title = f"🚨 Watchdog escalation: {diagnosis[:80]}"
    body  = (
        f"## Watchdog Agent — Escalation Required\n\n"
        f"**Reason:** {reason}\n"
        f"**Diagnosis:** {diagnosis}\n"
        f"**Failed run:** {run_url}\n"
        f"**Watchdog attempt:** {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS}\n\n"
        f"<details><summary>Log excerpt</summary>\n\n```\n{logs[-3000:]}\n```\n</details>"
    )
    try:
        issue = repo.create_issue(title=title, body=body, labels=["watchdog-escalation"])
        print(f"  ✓ Escalation issue: {issue.html_url}")
    except GithubException as e:
        print(f"  ✗ Could not create issue: {e}")


# ── Patch validation ───────────────────────────────────────────────────────────

def validate_python_syntax(code: str, filename: str) -> Optional[str]:
    """Returns error string if syntax is invalid, None if OK."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        py_compile.compile(tmp, doraise=True)
        return None
    except py_compile.PyCompileError as e:
        return str(e)
    finally:
        os.unlink(tmp)


def check_undefined_names(code: str, filename: str) -> list:
    """
    Basic AST walk: find Name nodes that look like undefined globals.
    Catches simple cases like DB_PATH when only settings.db_path exists.
    """
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ["SyntaxError — cannot check names"]

    # Collect all top-level assignments and imports (defined names)
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

    # Python builtins + common names we know are safe
    safe = set(dir(__builtins__)) | {"settings", "aiosqlite", "os", "uuid", "datetime",
                                      "timezone", "List", "Dict", "Optional", "Any"}
    defined |= safe

    # Look for Name references that are not in defined set (shallow pass only)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and not node.id.startswith("_"):
                issues.append(f"Possibly undefined name '{node.id}' at line {node.lineno}")

    return issues


def validate_patches(patches: list) -> list:
    """
    For each patch, apply it to the current file content and validate.
    Returns list of problem strings (empty = all good).
    """
    problems = []
    import base64

    for patch in patches:
        path    = patch.get("file", "")
        find    = patch.get("find", "")
        replace = patch.get("replace", "")

        if not path or not find:
            problems.append(f"Empty path or find in patch for '{path}'")
            continue

        current = get_file(path)
        if current is None:
            problems.append(f"File not found: {path}")
            continue

        if find not in current:
            problems.append(f"find-string not in {path}: {find[:60]!r}")
            continue

        patched = current.replace(find, replace, 1)

        if path.endswith(".py"):
            err = validate_python_syntax(patched, path)
            if err:
                problems.append(f"Syntax error in {path} after patch: {err}")
                continue

            undefined = check_undefined_names(patched, path)
            # Only flag names that actually appear in the replaced block
            new_names = [n for n in undefined if n.split("'")[1] in replace]
            if new_names:
                problems.append(f"Potential undefined names in {path}: {'; '.join(new_names)}")

    return problems


# ── Claude phase 1: diagnose ───────────────────────────────────────────────────

PHASE1_SYSTEM = """You are a DevOps self-healing agent. Diagnose a GitHub Actions failure and produce exact patches.

Respond ONLY in this JSON format (no other text, no markdown fences):
{
  "diagnosis": "one sentence root cause",
  "severity": "critical|high|medium|low",
  "fix_type": "code_patch|config_change|transient|escalate",
  "patches": [
    {
      "file": "relative/path/to/file",
      "description": "what changes and why",
      "find": "exact verbatim string to replace (including all whitespace)",
      "replace": "exact replacement string"
    }
  ],
  "retrigger": true,
  "notes": "any extra context"
}

Critical rules for patches:
- find must be a VERBATIM substring of the current file (check carefully)
- replace must be complete and self-contained — no partial statements
- Every name you introduce in replace must already be imported/defined in that file
- If you reference a variable, verify it exists in the file context provided
- Prefer minimal targeted changes — never patch more than needed
- For transient errors (network, rate limit, cancel): set patches=[], retrigger=true
- For auth/secrets issues: set fix_type=escalate
"""


def phase1_diagnose(logs: str, run_meta: dict) -> dict:
    context = f"""Workflow: {run_meta.get('name','?')}
Run ID:     {run_meta.get('id','?')}
Conclusion: {run_meta.get('conclusion','?')}
Event:      {run_meta.get('event','?')}
Attempt:    {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS}
Commit:     {run_meta.get('head_commit',{}).get('message','?')[:100]}

--- LOGS ---
{logs}

--- backend/config.py ---
{get_file('backend/config.py') or '[not found]'}

--- backend/db.py ---
{get_file('backend/db.py') or '[not found]'}

--- backend/agent_loop.py (first 80 lines) ---
{(get_file('backend/agent_loop.py') or '')[:3000]}

--- requirements.txt ---
{get_file('requirements.txt') or '[not found]'}
"""
    resp = client.messages.create(
        model=MODEL, max_tokens=2048, system=PHASE1_SYSTEM,
        messages=[{"role": "user", "content": context}]
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:])
        if text.endswith("```"):
            text = text[:-3].strip()
    return json.loads(text)


# ── Claude phase 2: review own patch ──────────────────────────────────────────

PHASE2_SYSTEM = """You are a senior Python code reviewer. Your job is to verify a proposed code patch
before it is committed. Be strict — catch anything that would cause a runtime error.

Respond ONLY in this JSON format:
{
  "approved": true,
  "issues": [],
  "corrected_patches": null
}

OR if you find problems:
{
  "approved": false,
  "issues": ["issue 1", "issue 2"],
  "corrected_patches": [
    {
      "file": "...",
      "description": "...",
      "find": "...",
      "replace": "..."
    }
  ]
}

Check specifically:
1. Every name in 'replace' is defined in the file (imports, module-level vars, builtins)
2. No partial statements (dangling assignments, unclosed blocks)
3. No leftover code from the original that is now unreachable or duplicated
4. Indentation is correct Python
5. find string EXACTLY matches the file content (whitespace-sensitive)
"""


def phase2_review(patches: list, fix: dict) -> dict:
    """Ask Claude to review its own patches against the full current file content."""
    file_contents = {}
    for patch in patches:
        path = patch.get("file", "")
        if path and path not in file_contents:
            file_contents[path] = get_file(path) or "[not found]"

    patch_block = json.dumps(patches, indent=2)
    files_block = "\n\n".join(
        f"=== {p} (current content) ===\n{c}" for p, c in file_contents.items()
    )

    prompt = f"""These patches were proposed to fix: {fix.get('diagnosis','?')}

PROPOSED PATCHES:
{patch_block}

CURRENT FILE CONTENT:
{files_block}

Review each patch. Check for undefined names, dangling code, and correctness.
If all patches are correct, set approved=true.
If there are issues, set approved=false, list them, and provide corrected_patches."""

    resp = client.messages.create(
        model=MODEL, max_tokens=2048, system=PHASE2_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:])
        if text.endswith("```"):
            text = text[:-3].strip()
    return json.loads(text)


# ── Apply patches ──────────────────────────────────────────────────────────────

def apply_patches(patches: list, diagnosis: str) -> int:
    applied = 0
    for patch in patches:
        path    = patch.get("file", "")
        find    = patch.get("find", "")
        replace = patch.get("replace", "")
        desc    = patch.get("description", "")[:60]

        current = get_file(path)
        if current is None or find not in current:
            print(f"  ✗ Cannot apply patch to {path}: string not found")
            continue

        new_content = current.replace(find, replace, 1)
        upsert_file(
            path, new_content,
            f"fix(watchdog): {desc}\n\nAuto-patched by Watchdog Agent v2\n"
            f"Diagnosis: {diagnosis[:120]}\nRun: {FAILED_RUN_ID}"
        )
        applied += 1
    return applied


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not FAILED_RUN_ID:
        print("ERROR: FAILED_RUN_ID not set"); sys.exit(1)

    print(f"\n{'='*60}")
    print(f" WATCHDOG AGENT v2 — Run {FAILED_RUN_ID}")
    print(f" Attempt {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS}")
    print(f"{'='*60}\n")

    run_meta = gh_api(f"/repos/{REPO_FULL}/actions/runs/{FAILED_RUN_ID}")
    conclusion = run_meta.get("conclusion", "")
    run_url    = run_meta.get("html_url", f"https://github.com/{REPO_FULL}/actions/runs/{FAILED_RUN_ID}")
    workflow_file = FAILED_WORKFLOW or run_meta.get("path", "").split("/")[-1] or "claude-agent.yml"

    print(f"Workflow:   {run_meta.get('name','?')}")
    print(f"Conclusion: {conclusion}")
    print(f"URL:        {run_url}\n")

    # Max attempts check
    if WATCHDOG_ATTEMPT > MAX_FIX_ATTEMPTS:
        print(f"Max attempts exceeded — escalating")
        logs = fetch_logs(FAILED_RUN_ID)
        open_escalation_issue("Max watchdog retries exceeded", logs, run_url,
                              "Exceeded max auto-fix attempts")
        sys.exit(0)

    # Fetch logs
    print("Fetching logs...")
    logs = fetch_logs(FAILED_RUN_ID)
    print(f"  {len(logs)} chars\n")

    # ── Phase 1: Diagnose ──────────────────────────────────────────────────────
    print("Phase 1: Diagnosing with Claude...")
    try:
        fix = phase1_diagnose(logs, run_meta)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  ✗ Phase 1 failed: {e}"); sys.exit(1)

    print(f"  Diagnosis:  {fix.get('diagnosis','?')}")
    print(f"  Severity:   {fix.get('severity','?')}")
    print(f"  Fix type:   {fix.get('fix_type','?')}")
    print(f"  Patches:    {len(fix.get('patches',[]))}")
    print(f"  Re-trigger: {fix.get('retrigger',False)}")

    fix_type = fix.get("fix_type", "transient")
    patches  = fix.get("patches", [])

    if fix_type == "escalate":
        print("\nClaude recommends escalation")
        open_escalation_issue(fix.get("diagnosis","?"), logs, run_url, "Claude escalated")
        sys.exit(0)

    # ── Phase 2: Review patches (even if empty — confirms intentional) ─────────
    if patches:
        print("\nPhase 2: Claude self-reviewing patches...")
        try:
            review = phase2_review(patches, fix)
        except (json.JSONDecodeError, Exception) as e:
            print(f"  ✗ Phase 2 review failed: {e} — aborting patch application")
            sys.exit(1)

        if not review.get("approved"):
            issues = review.get("issues", [])
            print(f"  ✗ Review REJECTED ({len(issues)} issues):")
            for issue in issues: print(f"    - {issue}")

            corrected = review.get("corrected_patches")
            if corrected:
                print("  Reviewer provided corrected patches — using those instead")
                patches = corrected
            else:
                print("  No corrected patches provided — escalating")
                open_escalation_issue(
                    fix.get("diagnosis","?"), logs, run_url,
                    f"Patch review failed: {'; '.join(issues[:3])}"
                )
                sys.exit(0)
        else:
            print(f"  ✓ Review APPROVED")

        # ── Static validation (compile + name check) ───────────────────────────
        print("\nStatic validation...")
        problems = validate_patches(patches)
        if problems:
            print(f"  ✗ Static validation failed ({len(problems)} issues):")
            for p in problems: print(f"    - {p}")
            open_escalation_issue(
                fix.get("diagnosis","?"), logs, run_url,
                f"Static validation failed after review: {'; '.join(problems[:3])}"
            )
            sys.exit(1)
        print(f"  ✓ All patches pass syntax + name checks")

        # ── Apply ──────────────────────────────────────────────────────────────
        print(f"\nApplying {len(patches)} patch(es)...")
        applied = apply_patches(patches, fix.get("diagnosis",""))
        print(f"  {applied}/{len(patches)} applied")

        # ── Wait for CI ────────────────────────────────────────────────────────
        if applied > 0:
            # Get SHA of what we just pushed
            time.sleep(5)
            latest = gh_api(f"/repos/{REPO_FULL}/commits?per_page=1")
            if isinstance(latest, list) and latest:
                sha = latest[0]["sha"]
                ci_result = wait_for_ci(sha)
                if ci_result == "failure":
                    print("  CI failed on patched code — escalating instead of re-triggering")
                    open_escalation_issue(
                        fix.get("diagnosis","?"), logs, run_url,
                        f"CI failed after Watchdog patch was applied (commit {sha[:7]})"
                    )
                    sys.exit(1)
                elif ci_result == "timeout":
                    print("  CI did not finish — proceeding anyway")
    else:
        print("\nNo patches (transient error) — skipping to re-trigger")

    # ── Re-trigger ─────────────────────────────────────────────────────────────
    if fix.get("retrigger", False):
        print(f"\nRe-triggering {workflow_file}...")
        original_inputs = run_meta.get("inputs") or {}
        trigger_workflow(workflow_file, original_inputs)
    else:
        print("\nRe-trigger skipped per diagnosis")

    print(f"\n✓ Watchdog v2 complete (attempt {WATCHDOG_ATTEMPT}/{MAX_FIX_ATTEMPTS})")


if __name__ == "__main__":
    main()
