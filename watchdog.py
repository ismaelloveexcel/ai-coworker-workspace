#!/usr/bin/env python3
"""
Watchdog Agent — autonomous failure detection and self-healing.

Flow:
  1. Fetch logs of the failed/cancelled run
  2. Ask Claude to diagnose the root cause
  3. Claude produces a structured fix plan (files to patch)
  4. Apply patches, commit to main
  5. Re-trigger the original workflow

Standalone — no backend/* imports.
"""
import io
import json
import os
import sys
import zipfile
import urllib.request
import urllib.error
from typing import Optional

# ── Deps check ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env vars set directly in GH Actions

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
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN       = os.environ["GITHUB_TOKEN"]          # injected by GH Actions
REPO_FULL          = os.environ["GITHUB_REPOSITORY"]     # owner/repo
FAILED_RUN_ID      = os.environ.get("FAILED_RUN_ID", "")
FAILED_WORKFLOW    = os.environ.get("FAILED_WORKFLOW", "")
MAX_LOG_CHARS      = 12_000   # keep prompt size sane
MAX_FIX_ATTEMPTS   = int(os.environ.get("WATCHDOG_MAX_RETRIES", "3"))
MODEL              = "claude-sonnet-4-6"

client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gh       = Github(auth=GHToken(GITHUB_TOKEN))
repo     = gh.get_repo(REPO_FULL)

# ── Helpers ───────────────────────────────────────────────────────────────────

def gh_api(path: str, method: str = "GET", body: dict = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        return {"__error": e.code, "__msg": body_txt}


def fetch_run_logs(run_id: str) -> str:
    """Download log zip and extract text from failed steps."""
    url = f"https://api.github.com/repos/{REPO_FULL}/actions/runs/{run_id}/logs"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # May redirect — follow manually
        if e.code == 302:
            location = e.headers.get("Location")
            with urllib.request.urlopen(location) as r2:
                raw = r2.read()
        else:
            return f"[Could not fetch logs: HTTP {e.code}]"

    try:
        zf   = zipfile.ZipFile(io.BytesIO(raw))
        logs = []
        for name in zf.namelist():
            content = zf.read(name).decode(errors="replace")
            logs.append(f"=== {name} ===\n{content}")
        combined = "\n".join(logs)
        # Trim to last N chars (most relevant = tail of logs)
        if len(combined) > MAX_LOG_CHARS:
            combined = "...[truncated]...\n" + combined[-MAX_LOG_CHARS:]
        return combined
    except Exception as e:
        return f"[Log parse error: {e}]"


def get_repo_file(path: str, ref: str = "main") -> Optional[str]:
    try:
        content = repo.get_contents(path, ref=ref)
        import base64
        return base64.b64decode(content.content).decode()
    except GithubException:
        return None


def upsert_file(path: str, content: str, message: str) -> None:
    try:
        existing = repo.get_contents(path, ref="main")
        repo.update_file(path, message, content, existing.sha, branch="main")
        print(f"  ✓ Updated {path}")
    except GithubException:
        repo.create_file(path, message, content, branch="main")
        print(f"  ✓ Created {path}")


def trigger_workflow(workflow_file: str, inputs: dict = None) -> bool:
    result = gh_api(
        f"/repos/{REPO_FULL}/actions/workflows/{workflow_file}/dispatches",
        method="POST",
        body={"ref": "main", "inputs": inputs or {}},
    )
    if result.get("__error"):
        print(f"  ✗ Trigger failed: {result}")
        return False
    print(f"  ✓ Re-triggered: {workflow_file}")
    return True


# ── Claude Diagnosis ──────────────────────────────────────────────────────────

WATCHDOG_SYSTEM = """You are a DevOps self-healing agent. You receive failure logs from a GitHub Actions workflow
and must diagnose the root cause, then produce exact file patches to fix it.

You MUST respond in this exact JSON format (no other text):
{
  "diagnosis": "one sentence root cause",
  "severity": "critical|high|medium|low",
  "fix_type": "code_patch|config_change|env_update|no_action|escalate",
  "patches": [
    {
      "file": "relative/path/to/file",
      "description": "what changes and why",
      "find": "exact string to replace (verbatim, including whitespace)",
      "replace": "exact replacement string"
    }
  ],
  "retrigger": true,
  "notes": "any extra context"
}

Rules:
- patches can be empty [] if fix_type is no_action or escalate
- find/replace must be exact substring matches (not regex)
- if the model string is wrong: patch backend/config.py model field
- if a Python import fails: patch requirements.txt or the import
- if GitHub auth fails: set fix_type=escalate (cannot fix secrets)
- if it is a transient error (rate limit, network timeout): set patches=[], retrigger=true
- if max retries exceeded: set fix_type=escalate, retrigger=false
"""

def diagnose_and_fix(logs: str, run_meta: dict, attempt: int) -> dict:
    """Ask Claude to diagnose failure and produce patches."""
    user_msg = f"""GitHub Actions workflow FAILED.

Workflow: {run_meta.get('name', '?')}
Run ID:   {run_meta.get('id', '?')}
Attempt:  {attempt}/{MAX_FIX_ATTEMPTS}
Trigger:  {run_meta.get('event', '?')}
Commit:   {run_meta.get('head_commit', {}).get('message', '?')[:100]}

--- FAILURE LOGS ---
{logs}

--- CURRENT backend/config.py ---
{get_repo_file('backend/config.py') or '[not found]'}

--- CURRENT requirements.txt ---
{get_repo_file('requirements.txt') or '[not found]'}

Diagnose the failure and produce the fix JSON.
"""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=WATCHDOG_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(text)


# ── Apply Fix ─────────────────────────────────────────────────────────────────

def apply_patches(patches: list, diagnosis: str) -> int:
    """Apply find/replace patches to repo files. Returns count applied."""
    applied = 0
    for patch in patches:
        file_path = patch.get("file", "")
        find      = patch.get("find", "")
        replace   = patch.get("replace", "")
        desc      = patch.get("description", "")

        if not file_path or not find:
            print(f"  ⚠ Skipping empty patch for {file_path}")
            continue

        current = get_repo_file(file_path)
        if current is None:
            print(f"  ✗ File not found: {file_path}")
            continue

        if find not in current:
            print(f"  ⚠ String not found in {file_path}: {find[:80]!r}")
            continue

        new_content = current.replace(find, replace, 1)
        commit_msg  = (
            f"fix(watchdog): {desc[:60]}\n\n"
            f"Auto-patched by Watchdog Agent\n"
            f"Diagnosis: {diagnosis[:120]}\n"
            f"Run: {FAILED_RUN_ID}"
        )
        upsert_file(file_path, new_content, commit_msg)
        applied += 1

    return applied


# ── Escalation ────────────────────────────────────────────────────────────────

def escalate(diagnosis: str, logs_excerpt: str, run_url: str) -> None:
    """Open a GitHub issue for human review."""
    title = f"🚨 Watchdog: unresolvable failure — {diagnosis[:80]}"
    body  = (
        f"## Watchdog Agent — Escalation\n\n"
        f"**Diagnosis:** {diagnosis}\n\n"
        f"**Failed run:** {run_url}\n\n"
        f"**Max auto-fix attempts reached.** Manual intervention required.\n\n"
        f"<details><summary>Log excerpt</summary>\n\n```\n{logs_excerpt[-3000:]}\n```\n</details>"
    )
    try:
        issue = repo.create_issue(title=title, body=body, labels=["watchdog-escalation"])
        print(f"  ✓ Escalation issue: {issue.html_url}")
    except GithubException as e:
        print(f"  ✗ Could not create issue: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not FAILED_RUN_ID:
        print("ERROR: FAILED_RUN_ID not set"); sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Watchdog Agent — Run {FAILED_RUN_ID}")
    print(f"{'='*60}\n")

    # Get run metadata
    run_meta = gh_api(f"/repos/{REPO_FULL}/actions/runs/{FAILED_RUN_ID}")
    run_url  = run_meta.get("html_url", f"https://github.com/{REPO_FULL}/actions/runs/{FAILED_RUN_ID}")
    workflow_file = FAILED_WORKFLOW or run_meta.get("path", "").split("/")[-1] or "claude-agent.yml"

    print(f"Workflow: {run_meta.get('name','?')}")
    print(f"Status:   {run_meta.get('status','?')} / {run_meta.get('conclusion','?')}")
    print(f"URL:      {run_url}\n")

    # Check retry counter via run name annotation (simple)
    attempt = int(os.environ.get("WATCHDOG_ATTEMPT", "1"))
    if attempt > MAX_FIX_ATTEMPTS:
        print(f"Max fix attempts ({MAX_FIX_ATTEMPTS}) exceeded — escalating")
        logs_excerpt = fetch_run_logs(FAILED_RUN_ID)
        escalate("Max watchdog retries exceeded", logs_excerpt, run_url)
        sys.exit(0)

    # Fetch logs
    print("Fetching failure logs...")
    logs = fetch_run_logs(FAILED_RUN_ID)
    print(f"  Log size: {len(logs)} chars\n")

    # Diagnose with Claude
    print("Diagnosing with Claude...")
    try:
        fix = diagnose_and_fix(logs, run_meta, attempt)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  ✗ Claude response parse error: {e}")
        sys.exit(1)

    print(f"\n  Diagnosis:  {fix.get('diagnosis','?')}")
    print(f"  Severity:   {fix.get('severity','?')}")
    print(f"  Fix type:   {fix.get('fix_type','?')}")
    print(f"  Patches:    {len(fix.get('patches',[]))}")
    print(f"  Re-trigger: {fix.get('retrigger', False)}")
    if fix.get("notes"):
        print(f"  Notes:      {fix['notes']}")

    fix_type = fix.get("fix_type", "no_action")

    # Escalate immediately if Claude says so
    if fix_type == "escalate":
        print("\nClaude recommends escalation — opening issue")
        escalate(fix.get("diagnosis", "?"), logs, run_url)
        sys.exit(0)

    # Apply patches
    patches = fix.get("patches", [])
    if patches:
        print(f"\nApplying {len(patches)} patch(es)...")
        applied = apply_patches(patches, fix.get("diagnosis", ""))
        print(f"  {applied}/{len(patches)} patches applied")
    else:
        print("\nNo patches needed (transient error or no_action)")

    # Re-trigger
    if fix.get("retrigger", False):
        # Pass original inputs from failed run if available
        print(f"\nRe-triggering {workflow_file}...")
        original_inputs = {}
        if "inputs" in run_meta:
            original_inputs = run_meta.get("inputs") or {}
        trigger_workflow(workflow_file, original_inputs)
    else:
        print("\nRe-trigger skipped per diagnosis")

    print(f"\n✓ Watchdog complete (attempt {attempt}/{MAX_FIX_ATTEMPTS})")


if __name__ == "__main__":
    main()
