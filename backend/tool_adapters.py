"""
Tool adapters — replaces MCP entirely.
All GitHub ops via PyGithub. All outputs JSON-safe. Failures structured.
"""
import json
import os
import re
from base64 import b64decode, b64encode
from typing import Any, Dict, List, Optional

from github import Github, GithubException
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import settings
from backend.events import emit_log

_gh = Github(settings.github_token)
_repo_cache: Dict[str, Any] = {}


def _get_repo(repo_full_name: str = None):
    name = repo_full_name or settings.github_default_repo
    if name not in _repo_cache:
        _repo_cache[name] = _gh.get_repo(name)
    return _repo_cache[name]


def _ok(data: Any) -> Dict:
    return {"success": True, "data": data}


def _err(msg: str) -> Dict:
    return {"success": False, "error": msg}


# ── GitHub Tools ───────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
def github_create_branch(task_id: str, repo: str = None) -> Dict:
    """Idempotent branch creation: task/{task_id}"""
    branch_name = f"task/{task_id}"
    try:
        r = _get_repo(repo)
        main_ref = r.get_git_ref("heads/main")
        sha = main_ref.object.sha

        # Check if branch already exists
        try:
            r.get_git_ref(f"heads/{branch_name}")
            return _ok({"branch": branch_name, "created": False, "note": "already exists"})
        except GithubException:
            pass  # Doesn't exist — create it

        r.create_git_ref(ref=f"refs/heads/{branch_name}", sha=sha)
        return _ok({"branch": branch_name, "created": True, "sha": sha})
    except GithubException as e:
        return _err(f"GitHub error creating branch: {e.data}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
def github_commit_files(branch_name: str, files: List[Dict], message: str, repo: str = None) -> Dict:
    """
    Upsert files to branch. files: [{path, content}]
    Idempotent: creates or updates each file.
    """
    try:
        r = _get_repo(repo)
        committed = []
        for f in files:
            path = f["path"]
            content = f["content"]
            encoded = b64encode(content.encode()).decode() if isinstance(content, str) else b64encode(content).decode()

            try:
                existing = r.get_contents(path, ref=branch_name)
                r.update_file(path, message, content, existing.sha, branch=branch_name)
                committed.append({"path": path, "action": "updated"})
            except GithubException:
                r.create_file(path, message, content, branch=branch_name)
                committed.append({"path": path, "action": "created"})

        return _ok({"committed": committed, "branch": branch_name})
    except GithubException as e:
        return _err(f"GitHub error committing files: {e.data}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
def github_create_pr(branch_name: str, title: str, body: str = "", repo: str = None) -> Dict:
    """Create PR — idempotent (returns existing PR if duplicate)."""
    try:
        r = _get_repo(repo)

        # Check for existing PR
        existing_prs = r.get_pulls(state="open", head=f"{settings.github_owner}:{branch_name}")
        for pr in existing_prs:
            return _ok({"pr_url": pr.html_url, "pr_number": pr.number, "created": False})

        pr = r.create_pull(title=title, body=body, head=branch_name, base="main")
        return _ok({"pr_url": pr.html_url, "pr_number": pr.number, "created": True})
    except GithubException as e:
        return _err(f"GitHub error creating PR: {e.data}")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=2))
def github_read_file(path: str, branch: str = "main", repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        content = r.get_contents(path, ref=branch)
        return _ok({"path": path, "content": b64decode(content.content).decode()})
    except GithubException as e:
        return _err(f"File not found: {path} on {branch}: {e}")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=2))
def github_list_files(path: str = "", branch: str = "main", repo: str = None) -> Dict:
    try:
        r = _get_repo(repo)
        contents = r.get_contents(path or "", ref=branch)
        files = [{"path": c.path, "type": c.type, "size": c.size} for c in contents]
        return _ok({"files": files, "path": path})
    except GithubException as e:
        return _err(f"Error listing files: {e}")


# ── Filesystem Tools ───────────────────────────────────────────────────────────

_SAFE_ROOT = "/tmp/agent_workspace"

def _sanitize_path(path: str) -> str:
    """Prevent path traversal."""
    safe = os.path.normpath(os.path.join(_SAFE_ROOT, path.lstrip("/")))
    if not safe.startswith(_SAFE_ROOT):
        raise ValueError(f"Path traversal denied: {path}")
    return safe


def filesystem_write(path: str, content: str) -> Dict:
    try:
        safe_path = _sanitize_path(path)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return _ok({"path": path, "bytes": len(content)})
    except Exception as e:
        return _err(f"Filesystem write error: {e}")


def filesystem_read(path: str) -> Dict:
    try:
        safe_path = _sanitize_path(path)
        with open(safe_path, "r", encoding="utf-8") as f:
            content = f.read()
        return _ok({"path": path, "content": content})
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(f"Filesystem read error: {e}")


# ── Playwright (disabled by default) ──────────────────────────────────────────

def playwright_browse(url: str) -> Dict:
    if not settings.playwright_enabled:
        return _err("Playwright is disabled (PLAYWRIGHT_ENABLED=false)")

    from urllib.parse import urlparse
    domain = urlparse(url).hostname or ""
    if not any(domain.endswith(d) for d in settings.whitelisted_domains):
        return _err(f"Domain not whitelisted: {domain}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000)
            text = page.inner_text("body")
            browser.close()
            return _ok({"url": url, "text": text[:5000]})
    except Exception as e:
        return _err(f"Playwright error: {e}")


# ── Dispatcher ─────────────────────────────────────────────────────────────────

TOOL_MAP = {
    "github_create_branch": lambda inp, task_id: github_create_branch(task_id, inp.get("repo")),
    "github_commit_files":  lambda inp, _: github_commit_files(
        inp["branch_name"], inp["files"], inp.get("message", "agent: commit"), inp.get("repo")
    ),
    "github_create_pr":     lambda inp, _: github_create_pr(
        inp["branch_name"], inp["title"], inp.get("body", ""), inp.get("repo")
    ),
    "github_read_file":     lambda inp, _: github_read_file(
        inp["path"], inp.get("branch", "main"), inp.get("repo")
    ),
    "github_list_files":    lambda inp, _: github_list_files(
        inp.get("path", ""), inp.get("branch", "main"), inp.get("repo")
    ),
    "filesystem_write":     lambda inp, _: filesystem_write(inp["path"], inp["content"]),
    "filesystem_read":      lambda inp, _: filesystem_read(inp["path"]),
    "playwright_browse":    lambda inp, _: playwright_browse(inp["url"]),
}


def execute_tool(tool_name: str, tool_input: Dict, task_id: str) -> Dict:
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        return _err(f"Unknown tool: {tool_name}")
    try:
        return fn(tool_input, task_id)
    except Exception as e:
        return _err(f"Tool execution exception: {e}")
