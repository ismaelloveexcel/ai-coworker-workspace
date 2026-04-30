"""
Tool adapters - replaces MCP entirely.
All GitHub ops via PyGithub. All outputs JSON-safe. Failures structured.

v2: Fixed @retry decorators (F4/E3).
    Previously all GithubException were caught inside the decorated function,
    so tenacity never saw a failure and never retried. Now the inner _gh_*
    helpers raise on failure; only the outer public function converts to _err().
    Unbounded _repo_cache replaced with bounded LRU (F11).
"""
import glob
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import sys
import time
from base64 import b64decode
from functools import lru_cache
from typing import Any, Dict, List, Optional

from github import Auth, Github, GithubException
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.config import settings

# PyGithub deprecated the positional-token constructor in 2.x and removes it
# in 3.x. Use the explicit Auth.Token form so we don't break on upgrade.
_gh = Github(auth=Auth.Token(settings.github_token))


def _is_transient(exc: Exception) -> bool:
    """Retry transient GitHub server and rate-limit errors."""
    if not isinstance(exc, GithubException):
        return False
    if exc.status >= 500 or exc.status == 429:
        return True
    if exc.status == 403:
        data = exc.data or {}
        message = (data.get("message") or "").lower()
        return "rate limit" in message or "secondary rate" in message or "abuse" in message
    return False


@lru_cache(maxsize=64)
def _get_repo(repo_full_name: str = None):
    """Bounded cache: max 64 distinct repos; LRU evicts stale entries (F11)."""
    name = repo_full_name or settings.github_default_repo
    return _gh.get_repo(name)


def _ok(data: Any) -> Dict:
    return {"success": True, "data": data}


def _err(msg: str) -> Dict:
    return {"success": False, "error": msg}


# -- Shared safety helpers ----------------------------------------------------

_SECRET_PATTERNS = [
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_assignment", re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([^\s'\"]{12,})")),
    ("generic_high_entropy", re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
]


def _redact(value: str) -> str:
    redacted = value
    for _, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: m.group(1) + "=[REDACTED]" if m.lastindex else "[REDACTED]", redacted)
    return redacted


def _secret_findings(content: str, path: str = "text", *, include_generic_entropy: bool = True) -> List[Dict]:
    findings = []
    for name, pattern in _SECRET_PATTERNS:
        if name == "generic_high_entropy" and not include_generic_entropy:
            continue
        for match in pattern.finditer(content or ""):
            start = max(0, match.start() - 12)
            end = min(len(content), match.end() + 12)
            findings.append({
                "path": path,
                "type": name,
                "start": match.start(),
                "end": match.end(),
                "preview": _redact(content[start:end]),
            })
    return findings


def _repo_root() -> str:
    return os.getcwd()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# -- Retry helpers (raise on failure so tenacity can retry) -------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_create_ref(repo, ref: str, sha: str) -> None:
    repo.create_git_ref(ref=ref, sha=sha)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_get_ref(repo, ref: str):
    return repo.get_git_ref(ref)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_create_file(repo, path: str, message: str, content: str, branch: str):
    repo.create_file(path, message, content, branch=branch)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_update_file(repo, path: str, message: str, content: str, sha: str, branch: str):
    repo.update_file(path, message, content, sha, branch=branch)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_create_pr(repo, title: str, body: str, head: str, base: str, draft: bool = False):
    return repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient))
def _gh_get_contents(repo, path: str, ref: str = None):
    kwargs = {"ref": ref} if ref else {}
    return repo.get_contents(path, **kwargs)


# -- GitHub Tools --------------------------------------------------------------

def github_create_branch(task_id: str, repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        sha = _gh_get_ref(r, "heads/main").object.sha
        branch_name = f"task/{task_id}"
        # Idempotent: don't fail if branch already exists
        try:
            _gh_get_ref(r, f"heads/{branch_name}")
        except GithubException:
            _gh_create_ref(r, f"refs/heads/{branch_name}", sha)
        return _ok({"branch": branch_name, "base_sha": sha})
    except RetryError as e:
        return _err(f"GitHub error creating branch after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error creating branch: {e.data}")


_PROTECTED_EXACT_PATHS = {".env", "Dockerfile", "docker-compose.yml", "nginx.conf"}
_PROTECTED_PREFIXES = (".env.", ".github/workflows/")


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_protected_repo_path(path: str) -> bool:
    normalized = _normalize_repo_path(path)
    return normalized in _PROTECTED_EXACT_PATHS or any(normalized.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def github_commit_files(branch: str, files: List[Dict], message: str, repo: str = None, allow_infra_edits: bool = False) -> Dict:
    """Commit multiple files. Each dict must have 'path' and 'content' keys."""
    try:
        normalized_files = []
        for file_info in files:
            path = _normalize_repo_path(file_info["path"])
            content = file_info["content"]
            if _is_protected_repo_path(path) and not allow_infra_edits:
                return _err(
                    f"Refusing to commit protected path {path!r}. "
                    "Set allow_infra_edits=true only for intentional infrastructure changes."
                )
            findings = _secret_findings(content, path, include_generic_entropy=False)
            if findings:
                return _err(
                    f"Refusing to commit {path!r}; possible secret detected: "
                    f"{findings[0]['type']} {findings[0]['preview']}"
                )
            normalized_files.append({"path": path, "content": content})

        r = _get_repo(repo)
        committed = []
        for file_info in normalized_files:
            path = file_info["path"]
            content = file_info["content"]
            try:
                existing = _gh_get_contents(r, path, ref=branch)
                _gh_update_file(r, path, _redact(message), content, existing.sha, branch)
            except GithubException:
                _gh_create_file(r, path, _redact(message), content, branch)
            committed.append(path)
        return _ok({"committed": committed, "branch": branch})
    except RetryError as e:
        return _err(f"GitHub error committing files after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error committing files: {e.data}")


def github_create_pr(branch: str, title: str, body: str = "", repo: str = None, draft: bool = False) -> Dict:
    try:
        r = _get_repo(repo)
        # Idempotent: return existing PR if one already exists for this branch
        existing = r.get_pulls(state="open", head=f"{r.owner.login}:{branch}")
        for pr in existing:
            return _ok({"pr_url": pr.html_url, "pr_number": pr.number, "existing": True})
        pr = _gh_create_pr(r, title=title, body=body, head=branch, base="main", draft=draft)
        return _ok({"pr_url": pr.html_url, "pr_number": pr.number, "draft": draft})
    except RetryError as e:
        return _err(f"GitHub error creating PR after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error creating PR: {e.data}")


def github_read_file(path: str, branch: str = "main", repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        contents = _gh_get_contents(r, path, ref=branch)
        raw = b64decode(contents.content).decode("utf-8", errors="replace")
        return _ok({"path": path, "content": raw, "sha": contents.sha})
    except RetryError as e:
        return _err(f"GitHub error reading file after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error reading file: {e.data}")


def github_list_files(path: str = "", branch: str = "main", repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        contents = _gh_get_contents(r, path or ".", ref=branch)
        if isinstance(contents, list):
            items = [{"name": c.name, "path": c.path, "type": c.type, "size": c.size} for c in contents]
        else:
            items = [{"name": contents.name, "path": contents.path, "type": contents.type, "size": contents.size}]
        return _ok({"files": items, "path": path})
    except RetryError as e:
        return _err(f"GitHub error listing files after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error listing files: {e.data}")


def github_compare_branch(branch: str, base: str = "main", repo: str = None) -> Dict:
    """Compare a working branch against base so the agent knows if PR creation is viable."""
    try:
        r = _get_repo(repo)
        comparison = r.compare(base, branch)
        files = []
        for file in getattr(comparison, "files", []) or []:
            files.append({
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "changes": file.changes,
            })
        commits = getattr(comparison, "commits", []) or []
        return _ok({
            "base": base,
            "branch": branch,
            "ahead_by": comparison.ahead_by,
            "behind_by": comparison.behind_by,
            "total_commits": comparison.total_commits,
            "has_changes": comparison.ahead_by > 0 or bool(files),
            "files": files,
            "commit_shas": [c.sha for c in commits[:20]],
        })
    except RetryError as e:
        return _err(f"GitHub error comparing branch after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error comparing branch: {e.data}")


def _collect_tree(repo_obj, path: str, branch: str, *, limit: int, out: List[Dict]) -> None:
    if len(out) >= limit:
        return
    try:
        contents = _gh_get_contents(repo_obj, path or ".", ref=branch)
    except Exception:
        return
    if not isinstance(contents, list):
        contents = [contents]
    for item in contents:
        if len(out) >= limit:
            return
        if item.name in {".git", "node_modules", ".next", "__pycache__", ".pytest_cache"}:
            continue
        out.append({"path": item.path, "type": item.type, "size": item.size})
        if item.type == "dir" and item.path.count("/") < 2:
            _collect_tree(repo_obj, item.path, branch, limit=limit, out=out)


def repo_snapshot(branch: str = "main", repo: str = None, max_files: int = 300, max_file_chars: int = 6000) -> Dict:
    """Return high-signal repo context in one tool call."""
    try:
        r = _get_repo(repo)
        tree: List[Dict] = []
        _collect_tree(r, "", branch, limit=max(1, min(max_files, 500)), out=tree)

        context_paths = [
            "README.md", "CLAUDE.md", "AGENTS.md", "requirements.txt",
            "pyproject.toml", "package.json", ".env.example",
            ".github/workflows/ci.yml", ".github/workflows/claude-agent.yml",
        ]
        files = {}
        for path in context_paths:
            content = github_read_file(path, branch=branch, repo=repo)
            if content.get("success"):
                raw = content["data"].get("content", "")
                files[path] = _truncate(_redact(raw), max_file_chars)
        return _ok({"repo": repo or settings.github_default_repo, "branch": branch, "tree": tree, "files": files})
    except Exception as e:
        return _err(f"Repo snapshot error: {e}")


def secret_scan(text: str = "", files: Optional[List[Dict]] = None) -> Dict:
    """Scan text and candidate file contents for common secret patterns."""
    findings = []
    targets = []
    if text:
        targets.append(("text", text))
    for file_info in files or []:
        targets.append((file_info.get("path", "<unknown>"), file_info.get("content", "")))

    for path, content in targets:
        findings.extend(_secret_findings(content or "", path))
    return _ok({"has_findings": bool(findings), "findings": findings[:100], "count": len(findings)})


def humanize_error(error: str) -> Dict:
    """Translate common raw implementation errors into operator-friendly text."""
    raw = str(error or "").strip()
    lower = raw.lower()
    message = "Something failed, but the system did not provide a specific reason."
    category = "unknown"
    recovery = "Open the task details and retry once. If it repeats, inspect the linked GitHub issue or workflow run."

    if "database is locked" in lower:
        category = "sqlite_locked"
        message = "The local task database was busy because another task was writing to it."
        recovery = "Wait a minute and retry the task. If it repeats, run only one task at a time until DB retries are added."
    elif "bad credentials" in lower or "401" in lower and "github" in lower:
        category = "github_auth"
        message = "GitHub rejected the saved token. The token may be revoked or expired."
        recovery = "Create a new GitHub PAT, update GH_PAT, and retry the task."
    elif "rate limit" in lower or "secondary rate" in lower or "429" in lower:
        category = "rate_limit"
        message = "GitHub or Anthropic temporarily rate-limited the automation."
        recovery = "Wait a few minutes and retry. Add automatic backoff if this happens often."
    elif "422" in lower and ("pull" in lower or "unprocessable" in lower):
        category = "empty_or_invalid_pr"
        message = "GitHub could not open the PR, often because the branch has no changes or the PR already exists."
        recovery = "Compare the task branch with main, then commit a real change or reuse the existing PR."
    elif "overloaded" in lower or "529" in lower:
        category = "anthropic_overloaded"
        message = "Anthropic was overloaded while the agent was thinking."
        recovery = "Retry the task after a short delay. This should be handled automatically with retries."
    elif "invalid x-api-key" in lower or "anthropic" in lower and "401" in lower:
        category = "anthropic_auth"
        message = "Anthropic rejected the API key."
        recovery = "Update ANTHROPIC_API_KEY and retry the task."
    elif "max steps" in lower:
        category = "max_steps"
        message = "The agent reached its step limit before finishing."
        recovery = "Retry with a smaller request, or open a partial PR from the task branch if it has useful commits."
    elif "timed out" in lower or "timeout" in lower:
        category = "timeout"
        message = "A model call or tool call took too long and was stopped."
        recovery = "Retry once. If it repeats, split the task into smaller pieces."

    return _ok({"category": category, "message": message, "recovery": recovery, "raw": _truncate(raw, 1000)})


def cost_status(task_id: str = "") -> Dict:
    """Return persisted spend for a task using a sync sqlite read for tool execution."""
    import sqlite3

    if not task_id:
        return _err("task_id is required")
    try:
        with sqlite3.connect(settings.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, title, status, usd_spent FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return _err(f"Task not found: {task_id}")
        spent = float(row["usd_spent"] or 0.0)
        cap = float(settings.max_task_usd)
        return _ok({
            "task_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "usd_spent": spent,
            "max_task_usd": cap,
            "remaining_usd": max(0.0, cap - spent),
            "budget_exceeded": spent > cap,
        })
    except Exception as e:
        return _err(f"Cost status error: {e}")


def _run_command(args: List[str], timeout_seconds: int) -> Dict:
    started = time.time()
    try:
        completed = subprocess.run(
            args,
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return {
            "command": args,
            "exit_code": completed.returncode,
            "duration_seconds": round(time.time() - started, 2),
            "output": _truncate(output, 12000),
            "success": completed.returncode == 0,
        }
    except FileNotFoundError:
        return {"command": args, "exit_code": None, "duration_seconds": round(time.time() - started, 2), "output": f"Command not found: {args[0]}", "success": False}
    except subprocess.TimeoutExpired as e:
        output = (_to_text(e.stdout) + _to_text(e.stderr)).strip()
        return {"command": args, "exit_code": None, "duration_seconds": timeout_seconds, "output": _truncate(output + "\nTimed out", 12000), "success": False}


def _python_files() -> List[str]:
    files = []
    for pattern in ("backend/*.py", "watchdog.py"):
        files.extend(glob.glob(os.path.join(_repo_root(), pattern)))
    return files


def run_tests(suite: str = "quick", timeout_seconds: int = 180) -> Dict:
    """Run allowlisted validation suites. This is intentionally not a general shell tool."""
    timeout_seconds = max(10, min(int(timeout_seconds), 600))
    npm = shutil.which("npm") or "npm"
    actionlint = shutil.which("actionlint") or os.path.join(_repo_root(), "actionlint")
    suites = {
        "python_compile": [[sys.executable, "-m", "py_compile", *_python_files()]],
        "pytest": [[sys.executable, "-m", "pytest", "tests/", "-q"]],
        "typecheck": [[npm, "run", "typecheck"]],
        "lint": [[npm, "run", "lint"]],
        "actionlint": [[actionlint, ".github/workflows/ci.yml", ".github/workflows/claude-agent.yml", ".github/workflows/watchdog.yml", ".github/workflows/maintenance.yml"]],
    }
    if suite == "quick":
        selected = ["python_compile", "pytest"]
    elif suite == "frontend":
        selected = ["typecheck", "lint"]
    elif suite == "all":
        selected = ["python_compile", "pytest", "typecheck", "lint", "actionlint"]
    elif suite in suites:
        selected = [suite]
    else:
        return _err(f"Unknown test suite: {suite!r}. Allowed: quick, frontend, all, {sorted(suites)}")

    results = []
    for name in selected:
        for command in suites[name]:
            result = _run_command(command, timeout_seconds)
            result["suite"] = name
            results.append(result)
            if not result["success"]:
                break
    return _ok({"suite": suite, "success": all(r["success"] for r in results), "results": results})


# -- Filesystem Tools (per-task ephemeral sandbox) ---------------------------
# F14: each task gets its own tmpdir under AGENT_WORKSPACE; concurrent tasks
#      cannot clobber each other's files.
# SECURITY: In production deployments set AGENT_WORKSPACE to a dedicated
#           directory that is NOT world-writable (e.g. owned by the service
#           user with mode 0700), so other OS users cannot read or inject
#           files into the agent sandbox.

_WORKSPACE_BASE = os.path.realpath(os.environ.get("AGENT_WORKSPACE", "/tmp/agent_workspace"))
_SAFE_ROOT = _WORKSPACE_BASE  # backward-compat alias used by tests


def _init_workspace() -> None:
    """Create _WORKSPACE_BASE with mode 0700 and validate it is not world-writable."""
    os.makedirs(_WORKSPACE_BASE, exist_ok=True)
    try:
        os.chmod(_WORKSPACE_BASE, 0o700)
    except PermissionError:
        pass  # directory is owned by another user (e.g. pre-created by root); check below
    st = os.stat(_WORKSPACE_BASE)
    if os.name != "nt" and st.st_mode & _stat.S_IWOTH:
        raise RuntimeError(
            f"AGENT_WORKSPACE ({_WORKSPACE_BASE!r}) is world-writable. "
            "Set it to a directory owned by the service user with mode 0700 "
            "to prevent other OS users from reading or injecting files into the agent sandbox."
        )


_init_workspace()


def _task_root(task_id: str) -> str:
    """Return (and create) a per-task sandbox directory."""
    root = os.path.join(_WORKSPACE_BASE, task_id)
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _sanitize_path(path: str, task_id: str = "") -> str:
    """Resolve path inside the task sandbox; reject traversal and symlinks."""
    if os.path.isabs(path):
        raise ValueError(f"Path traversal denied (absolute path): {path!r}")
    safe_root = _task_root(task_id) if task_id else _WORKSPACE_BASE
    candidate = os.path.normpath(os.path.join(safe_root, path))
    safe = os.path.realpath(candidate)
    root_with_sep = safe_root + os.sep
    if safe != safe_root and not safe.startswith(root_with_sep):
        raise ValueError(f"Path traversal denied: {path!r}")
    parts = safe[len(safe_root):].lstrip(os.sep).split(os.sep)
    check = safe_root
    for part in parts:
        check = os.path.join(check, part)
        if os.path.islink(check):
            raise ValueError(f"Symlink in path denied: {path!r}")
    return safe


def filesystem_read(path: str, task_id: str = "") -> Dict:
    try:
        safe_path = _sanitize_path(path, task_id)
        if not os.path.exists(safe_path):
            return _err(f"File not found: {path}")
        with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
            return _ok({"path": path, "content": f.read()})
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"OS error reading {path}: {e}")


def filesystem_write(path: str, content: str, task_id: str = "") -> Dict:
    try:
        safe_path = _sanitize_path(path, task_id)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return _ok({"path": path, "bytes_written": len(content)})
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"OS error writing {path}: {e}")


def filesystem_list(path: str = "", task_id: str = "") -> Dict:
    try:
        safe_path = _sanitize_path(path or ".", task_id)
        if not os.path.isdir(safe_path):
            return _err(f"Not a directory: {path}")
        entries = []
        for entry in os.scandir(safe_path):
            entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": entry.stat().st_size if entry.is_file() else 0})
        return _ok({"path": path, "entries": entries})
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"OS error listing {path}: {e}")


# -- Playwright (disabled by default) -----------------------------------------

def playwright_browse(url: str) -> Dict:
    if not settings.playwright_enabled:
        return _err("Playwright is disabled. Set PLAYWRIGHT_ENABLED=true to enable.")
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # Scheme check
        if parsed.scheme not in ("http", "https"):
            return _err(f"Scheme not allowed: {parsed.scheme!r}")
        # Domain whitelist: match on exact domain or .subdomain (F20 fix)
        domain = parsed.hostname or ""
        allowed = any(
            domain == d or domain.endswith("." + d)
            for d in settings.whitelisted_domains
        )
        if not allowed:
            return _err(f"Domain not whitelisted: {domain!r}")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            text = page.inner_text("body")
            browser.close()
        return _ok({"url": url, "content": text[:5000]})
    except Exception as e:
        return _err(f"Playwright error: {e}")


# -- Dispatcher ----------------------------------------------------------------

_ALLOWED_TOOLS = {
    "github_create_branch", "github_commit_files", "github_create_pr",
    "github_read_file", "github_list_files", "github_compare_branch",
    "filesystem_read", "filesystem_write", "filesystem_list",
    "playwright_browse", "repo_snapshot", "run_tests", "secret_scan",
    "humanize_error", "cost_status",
}

_TOOL_MAP = {
    "github_create_branch": github_create_branch,
    "github_commit_files": github_commit_files,
    "github_create_pr": github_create_pr,
    "github_read_file": github_read_file,
    "github_list_files": github_list_files,
    "github_compare_branch": github_compare_branch,
    "filesystem_read": filesystem_read,
    "filesystem_write": filesystem_write,
    "filesystem_list": filesystem_list,
    "playwright_browse": playwright_browse,
    "repo_snapshot": repo_snapshot,
    "run_tests": run_tests,
    "secret_scan": secret_scan,
    "humanize_error": humanize_error,
    "cost_status": cost_status,
}


def execute_tool(tool_name: str, tool_input: Dict, task_id: str = "") -> Dict:
    if tool_name not in _ALLOWED_TOOLS:
        return _err(f"Unknown tool: {tool_name!r}. Allowed: {sorted(_ALLOWED_TOOLS)}")
    fn = _TOOL_MAP[tool_name]
    # Inject task_id into task-scoped tools for per-task sandbox/cost lookup.
    _FS_TOOLS = {"filesystem_read", "filesystem_write", "filesystem_list"}
    if tool_name in _FS_TOOLS and task_id:
        tool_input = {**tool_input, "task_id": task_id}
    if tool_name == "cost_status" and task_id and not tool_input.get("task_id"):
        tool_input = {**tool_input, "task_id": task_id}
    try:
        return fn(**tool_input)
    except TypeError as e:
        return _err(f"Tool {tool_name!r} called with wrong arguments: {e}")
    except Exception as e:
        return _err(f"Unexpected error in {tool_name!r}: {e}")
