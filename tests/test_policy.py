"""Tests for local worker policy decisions and enforcement."""
from unittest.mock import Mock, patch

from backend.policy import ALLOW, DENY, REQUIRE_APPROVAL, clear_policy_audit_log, evaluate_tool_call, get_policy_audit_log
from backend.tool_adapters import execute_tool


def test_policy_allows_safe_filesystem_sandbox_path():
    decision = evaluate_tool_call("filesystem_read", {"path": "notes/result.txt"})

    assert decision.outcome == ALLOW
    assert decision.category == "filesystem"


def test_policy_denies_env_files():
    decision = evaluate_tool_call("filesystem_write", {"path": "nested/.env.production"})

    assert decision.outcome == DENY
    assert ".env" in decision.reason


def test_policy_denies_absolute_and_home_paths():
    assert evaluate_tool_call("filesystem_read", {"path": "/tmp/file"}).outcome == DENY
    assert evaluate_tool_call("filesystem_read", {"path": "~/secrets"}).outcome == DENY
    assert evaluate_tool_call("filesystem_read", {"path": "C:/Users/me/.ssh/id_rsa"}).outcome == DENY


def test_policy_requires_approval_for_browser_even_with_model_flag():
    decision = evaluate_tool_call("playwright_browse", {"url": "https://github.com", "approved": True})

    assert decision.outcome == REQUIRE_APPROVAL
    assert "approval" in decision.reason.lower()


def test_policy_allows_allowlisted_test_suite():
    decision = evaluate_tool_call("run_tests", {"suite": "python_compile"})

    assert decision.outcome == ALLOW


def test_policy_denies_non_allowlisted_test_suite_before_tool_runs():
    decision = evaluate_tool_call("run_tests", {"suite": "shell please"})

    assert decision.outcome == DENY
    assert "not allowlisted" in decision.reason


def test_execute_tool_denies_env_read_before_underlying_tool_runs():
    sentinel = Mock(side_effect=AssertionError("underlying tool should not run"))
    with patch.dict("backend.tool_adapters._TOOL_MAP", {"filesystem_read": sentinel}):
        result = execute_tool("filesystem_read", {"path": ".env"})

    assert result["success"] is False
    assert result["policy_decision"]["outcome"] == DENY
    sentinel.assert_not_called()


def test_execute_tool_requires_browser_approval_before_underlying_tool_runs():
    sentinel = Mock(side_effect=AssertionError("underlying tool should not run"))
    with patch.dict("backend.tool_adapters._TOOL_MAP", {"playwright_browse": sentinel}):
        result = execute_tool("playwright_browse", {"url": "https://github.com", "approved": True})

    assert result["success"] is False
    assert result["policy_decision"]["outcome"] == REQUIRE_APPROVAL
    sentinel.assert_not_called()


def test_policy_decisions_are_auditable():
    clear_policy_audit_log()

    execute_tool("filesystem_read", {"path": ".env"})
    audit_log = get_policy_audit_log()

    assert len(audit_log) == 1
    assert audit_log[0]["tool_name"] == "filesystem_read"
    assert audit_log[0]["outcome"] == DENY
    assert "timestamp" in audit_log[0]
