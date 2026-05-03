"""
Tests for the tool_adapters module — covering the surfaces identified as
untested in the code audit:

  * _redact against real-shape secrets (audit §3.6)
  * _is_protected_repo_path including bypass attempts (audit §3.2)
  * _sanitize_path traversal + symlink rejection (audit §3.2)
  * record_tool_succeeded gate only advances on inner data.success (audit §2.1)
  * env inheritance: _SAFE_SUBPROCESS_ENV does not contain secret keys (audit §1.2)
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from backend.tool_adapters import (
    _is_protected_repo_path,
    _redact,
    _sanitize_path,
    _SAFE_SUBPROCESS_ENV,
)
from backend.policy import AgentStageContext, ALLOW, DENY


# ---------------------------------------------------------------------------
# _redact — secrets must be obscured, safe tokens must pass through
# ---------------------------------------------------------------------------

class TestRedact:
    def test_anthropic_key(self):
        out = _redact("key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890")
        assert "sk-ant-" not in out

    def test_openai_key(self):
        out = _redact("Authorization: Bearer sk-" + "A" * 48)
        assert "sk-" + "A" * 48 not in out

    def test_github_pat(self):
        out = _redact("export GH_PAT=github_pat_11ABCDEFGHIJKLMNOPQRSTUVWX")
        assert "github_pat_11" not in out

    def test_github_token_ghu(self):
        out = _redact("token: ghu_abcdefghijklmnopqrstuvwxyz12345")
        assert "ghu_" not in out

    def test_aws_access_key(self):
        out = _redact("AKIA1234567890ABCDEF is the key")
        assert "AKIA1234567890ABCDEF" not in out

    def test_stripe_live_key(self):
        out = _redact("sk_live_" + "z" * 32 + " is a stripe key")
        assert "sk_live_" not in out

    def test_slack_webhook(self):
        # Construct URL at runtime so GitHub push-protection doesn't flag it
        fake_slack = "https://hooks." + "slack.com/services/T00000000/B00000000/" + "X" * 24
        out = _redact(fake_slack)
        assert "hooks." + "slack.com" not in out

    def test_jwt(self):
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = _redact(fake_jwt)
        assert fake_jwt not in out

    def test_uuid_not_redacted(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        assert _redact(uid) == uid

    def test_git_sha_not_redacted(self):
        sha = "a" * 40
        assert _redact(sha) == sha

    def test_plain_text_unchanged(self):
        txt = "Hello world, no secrets here."
        assert _redact(txt) == txt


# ---------------------------------------------------------------------------
# _is_protected_repo_path — protected paths and bypass attempts
# ---------------------------------------------------------------------------

class TestIsProtectedRepoPath:
    # Should be protected
    @pytest.mark.parametrize("path", [
        ".env",
        ".env.local",
        "backend/.env.production",
        "Dockerfile",
        "docker-compose.yml",
        "nginx.conf",
        "CLAUDE.md",
        "requirements.txt",
        "package.json",
        "pyproject.toml",
        ".gitignore",
        "bootstrap_github.py",
        "watchdog.py",
        "backend/policy.py",
        "backend/tool_adapters.py",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "tests/conftest.py",
        "tests/test_api.py",
        # backslash normalised to forward slash
        ".github\\workflows\\ci.yml",
    ])
    def test_protected(self, path):
        assert _is_protected_repo_path(path), f"Expected {path!r} to be protected"

    # Should NOT be protected (ordinary app code)
    @pytest.mark.parametrize("path", [
        "backend/main.py",
        "backend/db.py",
        "frontend/index.html",
        "docs/README.md",
        "app/page.js",
        "scripts/measure-cwv.js",
    ])
    def test_not_protected(self, path):
        assert not _is_protected_repo_path(path), f"Expected {path!r} to be unprotected"


# ---------------------------------------------------------------------------
# _sanitize_path — traversal and (on POSIX) symlink rejection
# ---------------------------------------------------------------------------

class TestSanitizePath:
    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="absolute"):
            _sanitize_path("/etc/passwd")

    def test_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="traversal"):
            _sanitize_path("../../etc/passwd", task_id="")

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
    def test_rejects_symlink_escape(self, tmp_path):
        # Create a task sandbox, plant a symlink inside it pointing outside
        task_dir = tmp_path / "task123"
        task_dir.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = task_dir / "escape"
        link.symlink_to(outside)

        with patch("backend.tool_adapters._WORKSPACE_BASE", str(tmp_path)):
            with pytest.raises(ValueError, match="traversal|symlink"):
                _sanitize_path("escape", task_id="task123")

    def test_allows_normal_path(self, tmp_path):
        task_dir = tmp_path / "task456"
        task_dir.mkdir()
        with patch("backend.tool_adapters._WORKSPACE_BASE", str(tmp_path)):
            result = _sanitize_path("file.txt", task_id="task456")
        assert result.endswith("file.txt")


# ---------------------------------------------------------------------------
# _SAFE_SUBPROCESS_ENV — must not contain secrets from the parent environment
# ---------------------------------------------------------------------------

class TestSafeSubprocessEnv:
    _SECRET_ENV_KEYS = {
        "ANTHROPIC_API_KEY", "GH_PAT", "API_KEY", "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY", "STRIPE_SECRET_KEY",
    }

    def test_no_secret_keys_present(self):
        leaked = self._SECRET_ENV_KEYS & set(_SAFE_SUBPROCESS_ENV.keys())
        assert not leaked, f"Secret env keys leaked into subprocess env: {leaked}"

    def test_path_is_present(self):
        # PATH must be forwarded so binaries can be found
        assert "PATH" in _SAFE_SUBPROCESS_ENV or sys.platform == "win32"


# ---------------------------------------------------------------------------
# AgentStageContext.record_tool_succeeded — gate only advances on inner success
# ---------------------------------------------------------------------------

class TestRecordToolSucceeded:
    def _make_context(self):
        return AgentStageContext()

    def test_run_tests_pass_advances_gate(self):
        ctx = self._make_context()
        assert not ctx.validation_done
        ctx.record_tool_succeeded("run_tests", {
            "success": True,
            "data": {"suite": "quick", "success": True, "results": []},
        })
        assert ctx.validation_done

    def test_run_tests_fail_does_not_advance_gate(self):
        ctx = self._make_context()
        ctx.record_tool_succeeded("run_tests", {
            "success": True,       # outer wrapper says "tool executed"
            "data": {"suite": "quick", "success": False, "results": []},  # but tests failed
        })
        assert not ctx.validation_done

    def test_run_tests_missing_data_does_not_advance_gate(self):
        ctx = self._make_context()
        ctx.record_tool_succeeded("run_tests", {"success": True})
        assert not ctx.validation_done

    def test_github_create_branch_advances_branch_gate(self):
        ctx = self._make_context()
        assert not ctx.branch_created
        ctx.record_tool_succeeded("github_create_branch", {"success": True, "data": {}})
        assert ctx.branch_created

    def test_unrelated_tool_changes_nothing(self):
        ctx = self._make_context()
        ctx.record_tool_succeeded("filesystem_read", {"success": True, "data": {}})
        assert not ctx.validation_done
        assert not ctx.branch_created
