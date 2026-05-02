"""
Tests for PR-B5: context-source correctness.

Verifies that build_task_context sources from the task's target repository
(via GitHub API) rather than from the host filesystem, that multi-repo
tasks do not leak host/deploy repo context, and that per-task caching
prevents repeated expensive reads.
"""
from unittest.mock import patch

import backend.claude_wrapper as cw
from backend.claude_wrapper import (
    _MAX_README_COMPACT_CHARS,
    _repo_slug,
    _get_task_prelude,
    _get_task_compact,
    build_task_context,
    _ctx_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id="t1", repo_url="https://github.com/owner/target-repo", branch=None, title="Test task", prompt="Do something"):
    t = {
        "id": task_id,
        "title": title,
        "prompt": prompt,
        "repo_url": repo_url,
        "status": "running",
    }
    if branch:
        t["branch"] = branch
    return t


_FAKE_TREE = {
    "success": True,
    "data": {
        "repo": "owner/target-repo",
        "branch": "main",
        "tree": [
            {"path": "README.md", "type": "file", "size": 100},
            {"path": "src/main.py", "type": "file", "size": 200},
        ],
        "files": {},
    },
}

_FAKE_README = {
    "success": True,
    "data": {"content": "# Target Repo README", "path": "README.md"},
}

_FAKE_CLAUDE_MD = {
    "success": True,
    "data": {"content": "# CLAUDE.md instructions", "path": "CLAUDE.md"},
}

_FAKE_NOT_FOUND = {"success": False, "error": "Not found"}


# ---------------------------------------------------------------------------
# _repo_slug normalisation
# ---------------------------------------------------------------------------

def test_repo_slug_strips_github_prefix():
    assert _repo_slug("https://github.com/owner/repo") == "owner/repo"


def test_repo_slug_strips_trailing_slash():
    assert _repo_slug("https://github.com/owner/repo/") == "owner/repo"


def test_repo_slug_passthrough_for_slug():
    assert _repo_slug("owner/repo") == "owner/repo"


def test_repo_slug_empty_string():
    assert _repo_slug("") == ""


def test_repo_slug_none():
    assert _repo_slug(None) == ""


# ---------------------------------------------------------------------------
# _build_repo_prelude_from_github — unit-level, GitHub calls mocked
# ---------------------------------------------------------------------------

def test_prelude_from_github_uses_target_repo(monkeypatch):
    """_build_repo_prelude_from_github should pass the target repo slug to GitHub."""
    captured = {}

    def fake_ghrf(path, repo=None, branch="main"):
        captured.setdefault("repos", set()).add(repo)
        if path == "README.md":
            return _FAKE_README
        return _FAKE_NOT_FOUND

    def fake_snap(repo=None, branch="main", max_files=300, max_file_chars=6000):
        captured["snap_repo"] = repo
        return _FAKE_TREE

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        result = cw._build_repo_prelude_from_github("owner/target-repo")

    assert "README.md" in result
    assert "# Target Repo README" in result
    assert captured["snap_repo"] == "owner/target-repo"
    assert all(r == "owner/target-repo" for r in captured["repos"])


def test_prelude_from_github_returns_empty_on_total_failure(monkeypatch):
    """When all GitHub calls fail, return empty string (triggers fallback)."""
    with patch("backend.tool_adapters.github_read_file", return_value=_FAKE_NOT_FOUND), \
         patch("backend.tool_adapters.repo_snapshot", return_value={"success": False, "error": "oops"}):
        result = cw._build_repo_prelude_from_github("owner/repo")

    assert result == ""


# ---------------------------------------------------------------------------
# _build_compact_context_from_github — unit-level
# ---------------------------------------------------------------------------

def test_compact_from_github_uses_target_repo():
    """_build_compact_context_from_github should only fetch README + tree."""
    fetched_paths = []

    def fake_ghrf(path, repo=None, branch="main"):
        fetched_paths.append(path)
        if path == "README.md":
            return _FAKE_README
        return _FAKE_NOT_FOUND

    def fake_snap(repo=None, branch="main", max_files=300, max_file_chars=6000):
        return _FAKE_TREE

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        result = cw._build_compact_context_from_github("owner/target-repo")

    assert "README.md (summary)" in result
    assert "# Target Repo README" in result
    assert "--- file tree ---" in result
    # Only README.md should be fetched for compact context
    assert fetched_paths == ["README.md"]


def test_compact_readme_truncated_to_500_chars():
    """README content in compact context must be ≤_MAX_README_COMPACT_CHARS chars."""
    long_content = "A" * (_MAX_README_COMPACT_CHARS * 2)

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": long_content}}
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        return _FAKE_TREE

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        result = cw._build_compact_context_from_github("owner/repo")

    # _MAX_README_COMPACT_CHARS A's followed by truncation marker
    assert "A" * _MAX_README_COMPACT_CHARS in result
    assert "[truncated]" in result


# ---------------------------------------------------------------------------
# Per-task caching
# ---------------------------------------------------------------------------

def test_prelude_cached_on_second_call(monkeypatch):
    """A second call for the same task_id must not re-invoke GitHub API."""
    _ctx_cache.clear()
    call_count = {"n": 0}

    def fake_ghrf(path, repo=None, branch="main"):
        call_count["n"] += 1
        if path == "README.md":
            return _FAKE_README
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        call_count["n"] += 1
        return _FAKE_TREE

    task = _make_task(task_id="cache-test-1")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        first = _get_task_prelude(task)
        calls_after_first = call_count["n"]
        second = _get_task_prelude(task)

    assert first == second
    assert call_count["n"] == calls_after_first  # no extra calls on second invocation


def test_compact_cached_on_second_call(monkeypatch):
    """A second call for the same task_id (compact slot) must not re-invoke GitHub API."""
    _ctx_cache.clear()
    call_count = {"n": 0}

    def fake_ghrf(path, repo=None, branch="main"):
        call_count["n"] += 1
        return _FAKE_README if path == "README.md" else _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        call_count["n"] += 1
        return _FAKE_TREE

    task = _make_task(task_id="cache-test-2")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        first = _get_task_compact(task)
        calls_after_first = call_count["n"]
        second = _get_task_compact(task)

    assert first == second
    assert call_count["n"] == calls_after_first


def test_different_repos_have_separate_cache_entries():
    """Tasks targeting different repos must each get their own cache entry."""
    _ctx_cache.clear()
    repos_seen = []

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": f"# README for {repo}"}}
        return _FAKE_NOT_FOUND

    def fake_snap(repo=None, **_kw):
        repos_seen.append(repo)
        return {"success": True, "data": {"repo": repo, "branch": "main", "tree": [], "files": {}}}

    task_a = _make_task(task_id="repo-a", repo_url="https://github.com/owner/repo-a")
    task_b = _make_task(task_id="repo-b", repo_url="https://github.com/owner/repo-b")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        prelude_a = _get_task_prelude(task_a)
        prelude_b = _get_task_prelude(task_b)

    assert "repo-a" in prelude_a
    assert "repo-b" in prelude_b
    assert prelude_a != prelude_b


# ---------------------------------------------------------------------------
# Multi-repo isolation — host context must not leak
# ---------------------------------------------------------------------------

def test_no_host_context_leak_when_github_succeeds():
    """When the GitHub API succeeds, host-filesystem content must not appear."""
    _ctx_cache.clear()

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": "# Only-target-repo content"}}
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        return {"success": True, "data": {
            "repo": "owner/other", "branch": "main",
            "tree": [{"path": "target-only-file.py", "type": "file", "size": 1}],
            "files": {},
        }}

    task = _make_task(task_id="isolation-test", repo_url="https://github.com/owner/other")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        prelude = _get_task_prelude(task)

    # Target repo content is present
    assert "Only-target-repo content" in prelude
    assert "target-only-file.py" in prelude
    # Host-only files must not appear (agent_loop.py lives only in the deploy repo)
    assert "agent_loop.py" not in prelude


# ---------------------------------------------------------------------------
# build_task_context integration — step 0 and step >0
# ---------------------------------------------------------------------------

def test_build_task_context_step0_uses_github_repo():
    """At step 0, build_task_context must include GitHub-sourced repo snapshot."""
    _ctx_cache.clear()

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": "# Target README"}}
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        return {"success": True, "data": {
            "repo": "owner/target", "branch": "main",
            "tree": [{"path": "target-file.py", "type": "file", "size": 10}],
            "files": {},
        }}

    task = _make_task(task_id="ctx-step0", repo_url="https://github.com/owner/target")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        context = build_task_context(task, steps=[])

    assert "=== Repo Snapshot ===" in context
    assert "# Target README" in context
    assert "target-file.py" in context
    assert "Task ID: ctx-step0" in context
    assert "owner/target" in context


def test_build_task_context_stepN_uses_compact_github_context():
    """At step >0, build_task_context must include compact GitHub-sourced context."""
    _ctx_cache.clear()

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": "# Target README compact"}}
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        return {"success": True, "data": {
            "repo": "owner/target", "branch": "main",
            "tree": [{"path": "compact-file.py", "type": "file", "size": 10}],
            "files": {},
        }}

    task = _make_task(task_id="ctx-stepN", repo_url="https://github.com/owner/target")
    steps = [
        {"step_num": 1, "tool_name": "github_read_file", "status": "done",
         "tool_input": "{}", "tool_output": "{}"},
    ]

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        context = build_task_context(task, steps=steps)

    assert "=== Repo Context ===" in context
    assert "# Target README compact" in context
    assert "compact-file.py" in context


def test_build_task_context_fallback_to_local_on_api_failure(tmp_path, monkeypatch):
    """When GitHub API fails, build_task_context falls back to local filesystem."""
    _ctx_cache.clear()

    # Point _ROOT at a tmp dir with a README
    readme = tmp_path / "README.md"
    readme.write_text("# Local deploy README")

    monkeypatch.setattr(cw, "_ROOT", str(tmp_path))

    task = _make_task(task_id="ctx-fallback", repo_url="https://github.com/owner/repo")

    with patch("backend.tool_adapters.github_read_file", return_value=_FAKE_NOT_FOUND), \
         patch("backend.tool_adapters.repo_snapshot", return_value={"success": False, "error": "fail"}):
        context = build_task_context(task, steps=[])

    assert "=== Repo Snapshot ===" in context
    assert "# Local deploy README" in context


def test_build_task_context_deterministic_for_same_inputs():
    """Same task + steps must always produce the same context string (determinism)."""
    _ctx_cache.clear()

    def fake_ghrf(path, repo=None, branch="main"):
        if path == "README.md":
            return {"success": True, "data": {"content": "# Stable README"}}
        return _FAKE_NOT_FOUND

    def fake_snap(**_kw):
        return {"success": True, "data": {
            "repo": "owner/stable", "branch": "main",
            "tree": [{"path": "stable.py", "type": "file", "size": 5}],
            "files": {},
        }}

    task = _make_task(task_id="det-test", repo_url="https://github.com/owner/stable")

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        ctx1 = build_task_context(task, steps=[])

    with patch("backend.tool_adapters.github_read_file", side_effect=fake_ghrf), \
         patch("backend.tool_adapters.repo_snapshot", side_effect=fake_snap):
        ctx2 = build_task_context(task, steps=[])

    assert ctx1 == ctx2
