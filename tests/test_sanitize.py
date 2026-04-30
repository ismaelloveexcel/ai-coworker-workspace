"""
Tests for backend.tool_adapters — path sanitization (F10) and domain whitelist (F20).
"""
import pytest
from unittest.mock import patch
import os

os.environ.setdefault("AGENT_WORKSPACE", "/tmp/test_agent_workspace")

from backend.tool_adapters import _sanitize_path, playwright_browse


# ---------------------------------------------------------------------------
# _sanitize_path
# ---------------------------------------------------------------------------

def test_sanitize_normal_path():
    result = _sanitize_path("subdir/file.py")
    assert "subdir/file.py" in result
    assert result.startswith("/tmp/test_agent_workspace")


def test_sanitize_rejects_dotdot():
    with pytest.raises(ValueError, match="traversal"):
        _sanitize_path("../../etc/passwd")


def test_sanitize_rejects_absolute_escape():
    with pytest.raises(ValueError, match="traversal"):
        _sanitize_path("/etc/passwd")


def test_sanitize_root_itself():
    # The root path should be allowed (returns _SAFE_ROOT)
    import os
    from backend.tool_adapters import _SAFE_ROOT
    result = _sanitize_path(".")
    assert os.path.realpath(result) == os.path.realpath(_SAFE_ROOT)


# ---------------------------------------------------------------------------
# Domain whitelist (F20 fix verification)
# ---------------------------------------------------------------------------

def test_domain_whitelist_rejects_substring_bypass():
    """evilgithub.com must NOT match whitelist entry github.com"""
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.playwright_enabled = True
        mock_settings.whitelisted_domains = ["github.com"]
        result = playwright_browse("https://evilgithub.com/path")
        assert not result["success"]
        assert "not whitelisted" in result["error"]


def test_domain_whitelist_allows_exact():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.playwright_enabled = True
        mock_settings.whitelisted_domains = ["github.com"]
        # Will fail at playwright import but should pass the whitelist check
        result = playwright_browse("https://github.com/path")
        # Either succeeds or fails at playwright — not at whitelist
        assert "not whitelisted" not in result.get("error", "")


def test_domain_whitelist_allows_subdomain():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.playwright_enabled = True
        mock_settings.whitelisted_domains = ["github.com"]
        result = playwright_browse("https://api.github.com/path")
        assert "not whitelisted" not in result.get("error", "")


def test_blocks_non_https_scheme():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.playwright_enabled = True
        mock_settings.whitelisted_domains = ["github.com"]
        result = playwright_browse("file:///etc/passwd")
        assert not result["success"]
