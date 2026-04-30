"""
Tool adapters - replaces MCP entirely.
All GitHub ops via PyGithub. All outputs JSON-safe. Failures structured.

v2: Fixed @retry decorators (F4/E3).
    Previously all GithubException were caught inside the decorated function,
    so tenacity never saw a failure and never retried. Now the inner _gh_*
    helpers raise on failure; only the outer public function converts to _err().
    Unbounded _repo_cache replaced with bounded LRU (F11).
"""
import os
from base64 import b64decode
from functools import lru_cache
from typing import Any, Dict, List

from github import Github, GithubException
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.config import settings

_gh = Github(settings.github_token)


def _is_transient(exc: Exception) -> bool:
    """Only retry on transient GitHub server errors (5xx), not 4xx client errors."""
    return isinstance(exc, GithubException) and exc.status >= 500


@lru_cache(maxsize=64)
def _get_repo(repo_full_name: str = None):
    """Bounded cache: max 64 distinct repos; LRU evicts stale entries (F11)."""
    name = repo_full_name or settings.github_default_repo
    return _gh.get_repo(name)


def _ok(data: Any) -> Dict:
    return {"success": True, "data": data}


def _err(msg: str) -> Dict:
    return {"success": False, "error": msg}


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
def _gh_create_pr(repo, title: str, body: str, head: str, base: str):
    return repo.create_pull(title=title, body=body, head=head, base=base)


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


def github_commit_files(branch: str, files: List[Dict], message: str, repo: str = None) -> Dict:
    """Commit multiple files. Each dict must have 'path' and 'content' keys."""
    try:
        r = _get_repo(repo)
        committed = []
        for file_info in files:
            path = file_info["path"]
            content = file_info["content"]
            try:
                existing = _gh_get_contents(r, path, ref=branch)
                _gh_update_file(r, path, message, content, existing.sha, branch)
            except GithubException:
                _gh_create_file(r, path, message, content, branch)
            committed.append(path)
        return _ok({"committed": committed, "branch": branch})
    except RetryError as e:
        return _err(f"GitHub error committing files after retries: {e}")
    except GithubException as e:
        return _err(f"GitHub error committing files: {e.data}")


def github_create_pr(branch: str, title: str, body: str = "", repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        # Idempotent: return existing PR if one already exists for this branch
        existing = r.get_pulls(state="open", head=f"{r.owner.login}:{branch}")
        for pr in existing:
            return _ok({"pr_url": pr.html_url, "pr_number": pr.number, "existing": True})
        pr = _gh_create_pr(r, title=title, body=body, head=branch, base="main")
        return _ok({"pr_url": pr.html_url, "pr_number": pr.number})
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


# -- Filesystem Tools (per-task ephemeral sandbox) ---------------------------
# F14: each task gets its own tmpdir under AGENT_WORKSPACE; concurrent tasks
#      cannot clobber each other's files.
# SECURITY: In production deployments set AGENT_WORKSPACE to a dedicated
#           directory that is NOT world-writable (e.g. owned by the service
#           user with mode 0700), so other OS users cannot read or inject
#           files into the agent sandbox.

import stat as _stat

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
    if st.st_mode & _stat.S_IWOTH:
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
    "github_read_file", "github_list_files",
    "filesystem_read", "filesystem_write", "filesystem_list",
    "playwright_browse",
}

_TOOL_MAP = {
    "github_create_branch": github_create_branch,
    "github_commit_files": github_commit_files,
    "github_create_pr": github_create_pr,
    "github_read_file": github_read_file,
    "github_list_files": github_list_files,
    "filesystem_read": filesystem_read,
    "filesystem_write": filesystem_write,
    "filesystem_list": filesystem_list,
    "playwright_browse": playwright_browse,
}


def execute_tool(tool_name: str, tool_input: Dict, task_id: str = "") -> Dict:
    if tool_name not in _ALLOWED_TOOLS:
        return _err(f"Unknown tool: {tool_name!r}. Allowed: {sorted(_ALLOWED_TOOLS)}")
    fn = _TOOL_MAP[tool_name]
    # Inject task_id into filesystem tools for per-task sandbox isolation (F14)
    _FS_TOOLS = {"filesystem_read", "filesystem_write", "filesystem_list"}
    if tool_name in _FS_TOOLS and task_id:
        tool_input = {**tool_input, "task_id": task_id}
    try:
        return fn(**tool_input)
    except TypeError as e:
        return _err(f"Tool {tool_name!r} called with wrong arguments: {e}")
    except Exception as e:
        return _err(f"Unexpected error in {tool_name!r}: {e}")
