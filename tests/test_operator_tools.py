"""Tests for operator-support tools exposed through backend.tool_adapters."""
from unittest.mock import patch

import pytest

from backend import db
from backend.tool_adapters import cost_status, github_commit_files, humanize_error, run_tests, secret_scan


def test_secret_scan_detects_common_tokens():
    text = "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz123456"

    result = secret_scan(text=text)

    assert result["success"] is True
    assert result["data"]["has_findings"] is True
    assert result["data"]["findings"][0]["type"] == "anthropic_api_key"
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz123456" not in result["data"]["findings"][0]["preview"]


def test_secret_scan_accepts_candidate_files():
    result = secret_scan(files=[{"path": "config.txt", "content": "token=ghp_abcdefghijklmnopqrstuvwxyz123456"}])

    assert result["success"] is True
    assert result["data"]["count"] >= 1
    assert result["data"]["findings"][0]["path"] == "config.txt"


def test_github_commit_files_blocks_protected_paths_before_network():
    result = github_commit_files(
        "task/test",
        [{"path": ".github/workflows/ci.yml", "content": "name: ci"}],
        "update workflow",
    )

    assert result["success"] is False
    assert "protected path" in result["error"]


def test_github_commit_files_blocks_secrets_before_network():
    result = github_commit_files(
        "task/test",
        [{"path": "backend/config.py", "content": "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz123456"}],
        "update config",
    )

    assert result["success"] is False
    assert "possible secret" in result["error"]
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz123456" not in result["error"]


def test_humanize_error_maps_github_422():
    result = humanize_error("GithubException: 422 Unprocessable Entity while creating pull request")

    assert result["success"] is True
    assert result["data"]["category"] == "empty_or_invalid_pr"
    assert "could not open the PR" in result["data"]["message"]


def test_humanize_error_maps_sqlite_lock():
    result = humanize_error("sqlite3.OperationalError: database is locked")

    assert result["data"]["category"] == "sqlite_locked"
    assert "database was busy" in result["data"]["message"]


@pytest.mark.asyncio
async def test_cost_status_returns_task_budget():
    task = await db.create_task("Cost", "check cost")
    await db.add_usd_spent(task["id"], 0.25)

    result = cost_status(task["id"])

    assert result["success"] is True
    assert result["data"]["task_id"] == task["id"]
    assert result["data"]["usd_spent"] == pytest.approx(0.25)
    assert result["data"]["remaining_usd"] >= 0


def test_run_tests_rejects_unknown_suite():
    result = run_tests(suite="shell please")

    assert result["success"] is False
    assert "Unknown test suite" in result["error"]


def test_run_tests_uses_allowlisted_suite():
    with patch("backend.tool_adapters._run_command", return_value={"success": True, "output": "ok", "exit_code": 0}) as run:
        result = run_tests(suite="python_compile")

    assert result["success"] is True
    assert result["data"]["success"] is True
    run.assert_called_once()

