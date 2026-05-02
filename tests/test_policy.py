"""Tests for local worker policy decisions and enforcement."""
from unittest.mock import Mock, patch

from backend.policy import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    STAGE_BROWSER,
    STAGE_EDIT,
    STAGE_FINALIZE,
    STAGE_PLANNING,
    STAGE_READ,
    STAGE_TOOL_MATRIX,
    STAGE_VALIDATE,
    AgentStageContext,
    clear_policy_audit_log,
    evaluate_tool_call,
    get_policy_audit_log,
)
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


# ---------------------------------------------------------------------------
# Stage tool matrix coverage
# ---------------------------------------------------------------------------


def test_stage_tool_matrix_covers_all_known_tools():
    """Every tool in _ALLOWED_TOOLS must appear in STAGE_TOOL_MATRIX."""
    from backend.tool_adapters import _ALLOWED_TOOLS

    missing = _ALLOWED_TOOLS - set(STAGE_TOOL_MATRIX)
    assert not missing, f"Tools missing from STAGE_TOOL_MATRIX: {sorted(missing)}"


def test_stage_tool_matrix_assigns_expected_stages():
    """Spot-check representative tools in each stage bucket."""
    assert STAGE_TOOL_MATRIX["repo_snapshot"] == STAGE_PLANNING
    assert STAGE_TOOL_MATRIX["cost_status"] == STAGE_PLANNING
    assert STAGE_TOOL_MATRIX["filesystem_read"] == STAGE_READ
    assert STAGE_TOOL_MATRIX["github_read_file"] == STAGE_READ
    assert STAGE_TOOL_MATRIX["filesystem_write"] == STAGE_EDIT
    assert STAGE_TOOL_MATRIX["github_create_branch"] == STAGE_EDIT
    assert STAGE_TOOL_MATRIX["github_commit_files"] == STAGE_EDIT
    assert STAGE_TOOL_MATRIX["run_tests"] == STAGE_VALIDATE
    assert STAGE_TOOL_MATRIX["github_create_pr"] == STAGE_FINALIZE
    assert STAGE_TOOL_MATRIX["playwright_browse"] == STAGE_BROWSER
    assert STAGE_TOOL_MATRIX["web_search"] == STAGE_BROWSER
    assert STAGE_TOOL_MATRIX["fetch_url"] == STAGE_BROWSER


# ---------------------------------------------------------------------------
# Stage context: unknown tool denial
# ---------------------------------------------------------------------------


def test_stage_context_denies_unknown_tool():
    ctx = AgentStageContext()
    decision = evaluate_tool_call("totally_unknown_tool", {}, context=ctx)

    assert decision.outcome == DENY
    assert decision.category == "unknown"


# ---------------------------------------------------------------------------
# Stage context: browser/network gating
# ---------------------------------------------------------------------------


def test_stage_context_denies_browser_when_not_enabled():
    ctx = AgentStageContext()
    for tool in ("playwright_browse", "web_search", "fetch_url"):
        decision = evaluate_tool_call(tool, {}, context=ctx)
        assert decision.outcome == DENY, f"{tool} should be denied without browser_enabled"
        assert decision.category == STAGE_BROWSER


def test_stage_context_allows_browser_when_explicitly_enabled():
    ctx = AgentStageContext(browser_enabled=True)
    # playwright_browse still hits REQUIRE_APPROVAL from the category check
    # (that's the correct layered behaviour after the stage gate passes)
    decision = evaluate_tool_call("playwright_browse", {"url": "https://example.com"}, context=ctx)
    assert decision.outcome != DENY or "browser_enabled" not in decision.reason

    # web_search and fetch_url are SAFE_NONLOCAL_TOOLS → ALLOW after stage gate passes
    for tool in ("web_search", "fetch_url"):
        decision = evaluate_tool_call(tool, {}, context=ctx)
        assert decision.outcome == ALLOW, f"{tool} should be allowed when browser is enabled"


def test_stage_context_browser_enable_mutates_context():
    ctx = AgentStageContext()
    assert not ctx.browser_enabled
    ctx.enable_browser()
    assert ctx.browser_enabled


def test_execute_tool_denies_browser_without_context_browser_enabled():
    """execute_tool with a stage context denies browser tools when not enabled."""
    sentinel = Mock(side_effect=AssertionError("underlying tool should not run"))
    ctx = AgentStageContext()
    with patch.dict("backend.tool_adapters._TOOL_MAP", {"web_search": sentinel}):
        result = execute_tool("web_search", {"query": "test"}, context=ctx)

    assert result["success"] is False
    assert result["policy_decision"]["outcome"] == DENY
    sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# Stage context: commit gating (branch must exist first)
# ---------------------------------------------------------------------------


def test_stage_context_denies_commit_before_branch_creation():
    ctx = AgentStageContext()
    decision = evaluate_tool_call("github_commit_files", {"files": []}, context=ctx)

    assert decision.outcome == DENY
    assert "github_create_branch" in decision.reason
    assert decision.category == STAGE_EDIT


def test_stage_context_allows_commit_after_branch_creation():
    ctx = AgentStageContext()
    ctx.record_tool_succeeded("github_create_branch")

    decision = evaluate_tool_call("github_commit_files", {"files": []}, context=ctx)

    # After branch created the stage gate passes; the tool proceeds to the
    # existing SAFE_NONLOCAL allow path.
    assert decision.outcome == ALLOW


def test_stage_context_branch_created_flag_tracks_state():
    ctx = AgentStageContext()
    assert not ctx.branch_created
    ctx.record_tool_succeeded("github_create_branch")
    assert ctx.branch_created


def test_execute_tool_denies_commit_without_branch():
    """execute_tool with a stage context denies commit before branch exists."""
    sentinel = Mock(side_effect=AssertionError("underlying tool should not run"))
    ctx = AgentStageContext()
    with patch.dict("backend.tool_adapters._TOOL_MAP", {"github_commit_files": sentinel}):
        result = execute_tool("github_commit_files", {"files": []}, context=ctx)

    assert result["success"] is False
    assert result["policy_decision"]["outcome"] == DENY
    sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# Stage context: PR gating (validation must complete first)
# ---------------------------------------------------------------------------


def test_stage_context_denies_pr_before_validation():
    ctx = AgentStageContext()
    decision = evaluate_tool_call("github_create_pr", {}, context=ctx)

    assert decision.outcome == DENY
    assert "run_tests" in decision.reason
    assert decision.category == STAGE_FINALIZE


def test_stage_context_allows_pr_after_validation():
    ctx = AgentStageContext()
    ctx.record_tool_succeeded("run_tests")

    decision = evaluate_tool_call("github_create_pr", {}, context=ctx)

    assert decision.outcome == ALLOW


def test_stage_context_validation_done_flag_tracks_state():
    ctx = AgentStageContext()
    assert not ctx.validation_done
    ctx.record_tool_succeeded("run_tests")
    assert ctx.validation_done


def test_execute_tool_denies_pr_without_validation():
    """execute_tool with a stage context denies PR before validation completes."""
    sentinel = Mock(side_effect=AssertionError("underlying tool should not run"))
    ctx = AgentStageContext()
    with patch.dict("backend.tool_adapters._TOOL_MAP", {"github_create_pr": sentinel}):
        result = execute_tool("github_create_pr", {}, context=ctx)

    assert result["success"] is False
    assert result["policy_decision"]["outcome"] == DENY
    sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# Stage context: full edit → validate → finalize transition
# ---------------------------------------------------------------------------


def test_stage_transition_edit_validate_finalize():
    """Walk the happy-path state machine: branch → commit → tests → PR."""
    ctx = AgentStageContext()

    # Before branch: commit is denied
    assert evaluate_tool_call("github_commit_files", {}, context=ctx).outcome == DENY

    # Simulate branch creation
    ctx.record_tool_succeeded("github_create_branch")
    assert ctx.branch_created

    # After branch: commit is allowed
    assert evaluate_tool_call("github_commit_files", {}, context=ctx).outcome == ALLOW

    # Before validation: PR is denied
    assert evaluate_tool_call("github_create_pr", {}, context=ctx).outcome == DENY

    # Simulate validation
    ctx.record_tool_succeeded("run_tests")
    assert ctx.validation_done

    # After validation: PR is allowed
    assert evaluate_tool_call("github_create_pr", {}, context=ctx).outcome == ALLOW


# ---------------------------------------------------------------------------
# Backwards compatibility: no-context behaviour unchanged
# ---------------------------------------------------------------------------


def test_no_context_browser_still_requires_approval():
    """Without a context the existing REQUIRE_APPROVAL behaviour is preserved."""
    decision = evaluate_tool_call("playwright_browse", {"url": "https://example.com"})
    assert decision.outcome == REQUIRE_APPROVAL


def test_no_context_unknown_tool_denied():
    """Without a context unknown tools are still denied."""
    decision = evaluate_tool_call("made_up_tool", {})
    assert decision.outcome == DENY


def test_no_context_commit_allowed_without_branch():
    """Without a context commit tools are not gated (old behaviour unchanged)."""
    decision = evaluate_tool_call("github_commit_files", {})
    assert decision.outcome == ALLOW


def test_no_context_pr_allowed_without_validation():
    """Without a context PR creation is not gated (old behaviour unchanged)."""
    decision = evaluate_tool_call("github_create_pr", {})
    assert decision.outcome == ALLOW
