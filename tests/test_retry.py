"""
Tests for backend.tool_adapters — retry decorators actually retry on GithubException.
This is the regression test for F4: before the fix, @retry was a no-op because
GithubException was caught inside the decorated function.
"""
from unittest.mock import patch, MagicMock
from github import GithubException


def _make_gh_exc(status=502):
    return GithubException(status, {"message": "Bad Gateway"}, {})


@patch("time.sleep")  # suppress wait_exponential delays so tests don't hang
@patch("backend.tool_adapters._get_repo")
def test_create_branch_retries_on_github_exception(mock_get_repo, mock_sleep):
    """_gh_get_ref should be retried up to 3 times on GithubException."""
    mock_repo = MagicMock()
    mock_repo.get_git_ref.side_effect = _make_gh_exc(502)
    mock_get_repo.return_value = mock_repo

    from backend.tool_adapters import github_create_branch
    result = github_create_branch("test-task-retry", "owner/repo")

    # Should fail after retries but NOT crash the process
    assert result["success"] is False
    assert "retries" in result["error"]
    # Should have been called 3 times (tenacity stop_after_attempt(3))
    assert mock_repo.get_git_ref.call_count >= 3


@patch("time.sleep")  # suppress wait_exponential delays so tests don't hang
@patch("backend.tool_adapters._get_repo")
def test_github_read_file_retries(mock_get_repo, mock_sleep):
    mock_repo = MagicMock()
    mock_repo.get_contents.side_effect = _make_gh_exc(503)
    mock_get_repo.return_value = mock_repo

    from backend.tool_adapters import github_read_file
    result = github_read_file("README.md", repo="owner/repo")

    assert result["success"] is False
    assert "retries" in result["error"]
    assert mock_repo.get_contents.call_count >= 3


@patch("time.sleep")  # suppress wait_exponential delays so tests don't hang
@patch("backend.tool_adapters._get_repo")
def test_github_create_branch_succeeds_on_retry(mock_get_repo, mock_sleep):
    """Should succeed if the second attempt works."""
    mock_repo = MagicMock()
    # Fail first call to get_git_ref for main SHA, succeed on second
    mock_repo.get_git_ref.side_effect = [
        _make_gh_exc(502),   # first attempt fails
        MagicMock(object=MagicMock(sha="abc123")),  # retry succeeds
        _make_gh_exc(404),   # branch doesn't exist yet (for idempotency check)
    ]
    mock_repo.create_git_ref.return_value = MagicMock()
    mock_get_repo.return_value = mock_repo

    from backend.tool_adapters import github_create_branch
    result = github_create_branch("my-task", "owner/repo")
    assert result["success"] is True

