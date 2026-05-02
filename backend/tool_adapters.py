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
import hashlib
import html
import ipaddress
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
from urllib.parse import urljoin, urlparse

import httpx
from github import Auth, Github, GithubException
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.config import settings
from backend.policy import ALLOW, evaluate_tool_call, record_policy_decision
from backend.tool_catalog import TOOL_IDS as _CATALOG_TOOL_IDS

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


# Patterns that are NOT secrets — never redact these even if they match
# the generic_high_entropy pattern.
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_GIT_SHA_RE = re.compile(r'^[0-9a-f]{40}$', re.I)


def _redact(value: str) -> str:
    redacted = value
    for name, pattern in _SECRET_PATTERNS:
        def _sub(m, _name=name):
            s = m.group(0)
            # Whitelist UUIDs (task IDs) and git SHAs — they are not secrets (A12)
            if _name == "generic_high_entropy":
                if _UUID_RE.fullmatch(s) or _GIT_SHA_RE.fullmatch(s):
                    return s
            return m.group(1) + "=[REDACTED]" if m.lastindex else "[REDACTED]"
        redacted = pattern.sub(_sub, redacted)
    return redacted


def _secret_findings(content: str, path: str = "text", *, include_generic_entropy: bool = True) -> List[Dict]:
    findings = []
    for name, pattern in _SECRET_PATTERNS:
        if name == "generic_high_entropy" and not include_generic_entropy:
            continue
        for match in pattern.finditer(content or ""):
            if name == "generic_high_entropy" and (_UUID_RE.fullmatch(match.group(0)) or _GIT_SHA_RE.fullmatch(match.group(0))):
                continue
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


def _domain_allowed(domain: str) -> bool:
    normalized = (domain or "").lower().strip(".")
    return any(
        normalized == d.lower().strip(".") or normalized.endswith("." + d.lower().strip("."))
        for d in settings.whitelisted_domains
    )


def _private_host_reason(host: str) -> str:
    normalized = (host or "").lower().strip(".")
    if not normalized:
        return "missing hostname"
    if normalized in {"localhost", "0", "0.0.0.0"} or normalized.endswith(".localhost"):
        return "local hostname"
    try:
        ip = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        return ""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return f"non-public IP address {ip}"
    return ""


def _validate_research_url(url: str) -> Dict:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return _err(f"Scheme not allowed: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        return _err("Credentials in URLs are not allowed")
    host = parsed.hostname or ""
    private_reason = _private_host_reason(host)
    if private_reason:
        return _err(f"Host not allowed: {private_reason}")
    if not _domain_allowed(host):
        return _err(f"Domain not whitelisted: {host!r}")
    return _ok({"parsed": parsed, "host": host})


def _clean_web_text(raw: str, limit: int = 50000) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return _truncate(_redact(text.strip()), limit)


def _source_id(url: str) -> str:
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


def _sentences(text: str, limit: int = 8) -> List[str]:
    chunks = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip())
    clean = [chunk.strip() for chunk in chunks if 30 <= len(chunk.strip()) <= 320]
    return clean[:limit]


def _keyword_lines(text: str, keywords: List[str], limit: int = 6) -> List[str]:
    lines = []
    for sentence in _sentences(text, 40):
        lower = sentence.lower()
        if any(keyword in lower for keyword in keywords):
            lines.append(sentence)
        if len(lines) >= limit:
            break
    return lines


# -- Retry helpers (raise on failure so tenacity can retry) -------------------


def _gh_before_sleep(retry_state) -> None:
    """Honour GitHub Retry-After header when present (A1)."""
    exc = retry_state.outcome.exception()
    if not isinstance(exc, GithubException):
        return
    try:
        headers = exc.headers or {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            extra = int(retry_after) + 2
            print(
                f"[GitHub] Retry-After header: sleeping {extra}s "
                f"(attempt {retry_state.attempt_number})",
                flush=True,
            )
            time.sleep(extra)
    except (AttributeError, ValueError, TypeError):
        pass


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
def _gh_create_ref(repo, ref: str, sha: str) -> None:
    repo.create_git_ref(ref=ref, sha=sha)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
def _gh_get_ref(repo, ref: str):
    return repo.get_git_ref(ref)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
def _gh_create_file(repo, path: str, message: str, content: str, branch: str):
    repo.create_file(path, message, content, branch=branch)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
def _gh_update_file(repo, path: str, message: str, content: str, sha: str, branch: str):
    repo.update_file(path, message, content, sha, branch=branch)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
def _gh_create_pr(repo, title: str, body: str, head: str, base: str, draft: bool = False):
    return repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=15, max=90), retry=retry_if_exception(_is_transient), before_sleep=_gh_before_sleep)
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


_PROTECTED_EXACT_PATHS = {".env", "Dockerfile", "docker-compose.yml", "nginx.conf", "CLAUDE.md"}
_PROTECTED_PREFIXES = (".github/workflows/",)


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_protected_repo_path(path: str) -> bool:
    normalized = _normalize_repo_path(path)
    segments = normalized.split("/")
    has_env_segment = any(segment == ".env" or segment.startswith(".env.") for segment in segments)
    return (
        normalized in _PROTECTED_EXACT_PATHS
        or has_env_segment
        or any(normalized.startswith(prefix) for prefix in _PROTECTED_PREFIXES)
    )


def github_commit_files(branch: str, files: List[Dict], message: str, repo: str = None, allow_infra_edits: bool = False) -> Dict:
    """Commit multiple files. Each dict must have 'path' and 'content' keys."""
    try:
        normalized_files = []
        for file_info in files:
            path = _normalize_repo_path(file_info["path"])
            content = file_info["content"]
            if _is_protected_repo_path(path):
                return _err(
                    f"Refusing to commit protected path {path!r}. "
                    "Protected paths require a future explicit approval path."
                )
            findings = _secret_findings(content, path, include_generic_entropy=True)
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


# -- Research Tools -----------------------------------------------------------

_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)


def web_search(query: str, max_results: int = 5, provider: str = "disabled") -> Dict:
    """Search provider shim. It is intentionally disabled until configured."""
    query = (query or "").strip()
    if not query:
        return _err("query is required")
    max_results = max(1, min(int(max_results), 10))
    return _err(
        "web_search is not configured. Add an approved search provider before use; "
        f"requested provider={provider!r}, max_results={max_results}. Use fetch_url with known source URLs meanwhile."
    )


def fetch_url(url: str, max_bytes: int = 65536, timeout_seconds: float = 10.0) -> Dict:
    """Fetch public, whitelisted text content with bounded output and provenance."""
    max_bytes = max(1024, min(int(max_bytes), 262144))
    timeout_seconds = max(1.0, min(float(timeout_seconds), 20.0))
    current_url = url
    visited = []
    try:
        headers = {"User-Agent": "ai-coworker-research/1.0", "Accept-Encoding": "identity"}
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False, headers=headers) as client:
            for _ in range(4):
                validation = _validate_research_url(current_url)
                if not validation.get("success"):
                    return validation
                with client.stream("GET", current_url) as response:
                    visited.append(str(response.url))
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return _err("Redirect response missing Location header")
                        current_url = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        return _err(f"HTTP error fetching URL: {response.status_code}")
                    content_encoding = response.headers.get("content-encoding", "").lower().strip()
                    if content_encoding and content_encoding != "identity":
                        return _err(f"Compressed responses not allowed: {content_encoding!r}")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    if not content_type or not any(content_type.startswith(allowed) for allowed in _TEXT_CONTENT_TYPES):
                        return _err(f"Content type not allowed or missing: {content_type!r}")
                    chunks = []
                    total = 0
                    truncated = False
                    for chunk in response.iter_raw():
                        if not chunk:
                            continue
                        remaining = max_bytes - total
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(chunk) > remaining:
                            chunks.append(chunk[:remaining])
                            total += remaining
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    raw_bytes = b"".join(chunks)
                    text = raw_bytes.decode(response.encoding or "utf-8", errors="replace")
                    clean_text = _clean_web_text(text, limit=max_bytes)
                    final_url = str(response.url)
                    return _ok({
                        "url": final_url,
                        "requested_url": url,
                        "source_id": _source_id(final_url),
                        "status_code": response.status_code,
                        "content_type": content_type or "unknown",
                        "bytes_read": len(raw_bytes),
                        "truncated": truncated,
                        "redirect_chain": visited[:-1],
                        "content": clean_text,
                        "provenance": {"tool": "fetch_url", "url": final_url, "source_id": _source_id(final_url)},
                    })
            return _err("Too many redirects")
    except httpx.TimeoutException:
        return _err("Timed out fetching URL")
    except httpx.RequestError as e:
        return _err(f"Request error fetching URL: {e}")


def source_summarize(source: Dict = None, text: str = "", url: str = "", title: str = "", max_points: int = 8) -> Dict:
    source = source or {}
    content = _redact(text or source.get("content", ""))
    source_url = url or source.get("url") or source.get("requested_url") or ""
    source_title = title or source.get("title") or source_url or "source"
    max_points = max(1, min(int(max_points), 12))
    if not content.strip():
        return _err("source text is required")
    summary_points = _sentences(content, max_points)
    pricing_notes = _keyword_lines(content, ["price", "pricing", "plan", "free", "paid", "$"], 6)
    feature_notes = _keyword_lines(content, ["feature", "workflow", "agent", "browser", "automation", "research", "collabor", "integrat"], 8)
    warnings = [
        "Web content is untrusted and must be corroborated before acting on claims.",
    ]
    secret_count = len(_secret_findings(content, source_url or "source", include_generic_entropy=False))
    return _ok({
        "artifact_type": "source_summary",
        "source": {
            "source_id": source.get("source_id") or _source_id(source_url or source_title),
            "url": source_url,
            "title": _truncate(_redact(source_title), 200),
        },
        "summary_points": summary_points,
        "pricing_notes": pricing_notes,
        "feature_notes": feature_notes,
        "provenance": [source.get("provenance") or {"tool": "source_summarize", "url": source_url}],
        "warnings": warnings,
        "redaction": {"secret_findings_removed": secret_count},
    })


def research_compare(topic: str, sources: List[Dict], max_competitors: int = 8) -> Dict:
    topic = (topic or "").strip() or "market research"
    if not sources:
        return _err("sources are required")
    max_competitors = max(1, min(int(max_competitors), 12))
    competitors = []
    provenance = []
    for index, source in enumerate(sources[:max_competitors], start=1):
        source_info = source.get("source") if isinstance(source.get("source"), dict) else source
        source_url = source_info.get("url", "")
        title = source_info.get("title") or source.get("title") or source_url or f"Source {index}"
        summary_points = source.get("summary_points") or _sentences(source.get("content", ""), 5)
        feature_notes = source.get("feature_notes") or _keyword_lines(source.get("content", ""), ["feature", "workflow", "agent", "automation", "research"], 5)
        pricing_notes = source.get("pricing_notes") or _keyword_lines(source.get("content", ""), ["price", "pricing", "free", "paid", "$"], 3)
        source_id = source_info.get("source_id") or _source_id(source_url or title)
        competitors.append({
            "name": _truncate(_redact(title), 120),
            "source_id": source_id,
            "url": source_url,
            "positioning": summary_points[:2],
            "features": feature_notes[:5],
            "pricing_notes": pricing_notes[:3],
            "strengths_to_borrow": feature_notes[:3] or summary_points[:3],
            "weaknesses_to_avoid": ["Validate claims against primary sources before implementation."],
        })
        provenance.append({"source_id": source_id, "url": source_url, "title": _truncate(_redact(title), 120)})

    feature_names = ["Agent workflow", "Research workflow", "Artifact output", "Pricing clarity"]
    feature_matrix = []
    for competitor in competitors:
        joined = " ".join(competitor["features"] + competitor["pricing_notes"]).lower()
        feature_matrix.append({
            "competitor": competitor["name"],
            "source_id": competitor["source_id"],
            "features": {
                "Agent workflow": "agent" in joined or "automation" in joined,
                "Research workflow": "research" in joined,
                "Artifact output": "artifact" in joined or "export" in joined or "document" in joined,
                "Pricing clarity": "price" in joined or "pricing" in joined or "free" in joined or "paid" in joined or "$" in joined,
            },
        })
    return _ok({
        "artifact_type": "research_brief",
        "topic": _redact(topic),
        "competitor_list": competitors,
        "positioning_comparison": [{"competitor": c["name"], "positioning": c["positioning"], "source_id": c["source_id"]} for c in competitors],
        "feature_matrix": {"features": feature_names, "rows": feature_matrix},
        "pricing_notes": [{"competitor": c["name"], "notes": c["pricing_notes"], "source_id": c["source_id"]} for c in competitors],
        "strengths_to_borrow": [item for c in competitors for item in c["strengths_to_borrow"]][:10],
        "weaknesses_to_avoid": ["Do not trust uncorroborated marketing claims.", "Do not copy proprietary UX or private data."],
        "differentiation_strategy": [
            "Emphasize private local supervision, source-backed artifacts, and explicit operator controls.",
            "Keep research outputs tied to source IDs so later builder agents can audit assumptions.",
        ],
        "recommended_mvp": [
            "Policy-gated source fetching",
            "Deterministic source summaries",
            "Research brief artifact with provenance",
            "Human review before implementation tasks consume web claims",
        ],
        "provenance": provenance,
    })


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
        if not _domain_allowed(domain):
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Derived from the canonical tool catalog — do NOT edit inline.
_ALLOWED_TOOLS = _CATALOG_TOOL_IDS

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
    "web_search": web_search,
    "fetch_url": fetch_url,
    "source_summarize": source_summarize,
    "research_compare": research_compare,
}


def execute_tool(tool_name: str, tool_input: Dict, task_id: str = "") -> Dict:
    tool_input = tool_input or {}
    if tool_name not in _ALLOWED_TOOLS:
        decision = evaluate_tool_call(tool_name, tool_input)
        record_policy_decision(decision)
        result = _err(f"Unknown tool: {tool_name!r}. Allowed: {sorted(_ALLOWED_TOOLS)}")
        result["policy_decision"] = decision.to_dict()
        return result
    # Inject task_id into task-scoped tools for per-task sandbox/cost lookup.
    _FS_TOOLS = {"filesystem_read", "filesystem_write", "filesystem_list"}
    if tool_name in _FS_TOOLS and task_id:
        tool_input = {**tool_input, "task_id": task_id}
    if tool_name == "cost_status" and task_id and not tool_input.get("task_id"):
        tool_input = {**tool_input, "task_id": task_id}
    decision = evaluate_tool_call(tool_name, tool_input)
    record_policy_decision(decision)
    if decision.outcome != ALLOW:
        result = _err(decision.reason)
        result["policy_decision"] = decision.to_dict()
        return result
    fn = _TOOL_MAP[tool_name]
    try:
        return fn(**tool_input)
    except TypeError as e:
        return _err(f"Tool {tool_name!r} called with wrong arguments: {e}")
    except Exception as e:
        return _err(f"Unexpected error in {tool_name!r}: {e}")
