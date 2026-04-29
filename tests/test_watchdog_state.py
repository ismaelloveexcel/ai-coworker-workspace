"""
Tests for watchdog daily-invocation ceiling and persistent state helpers.
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch, call

# Provide dummy env vars so watchdog can be imported without real credentials
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-dummy")
os.environ.setdefault("GH_PAT", "ghp-dummy")
os.environ.setdefault("GITHUB_REPO", "owner/repo")

from watchdog import _load_state, _save_state, _check_and_increment_daily, WATCHDOG_DAILY_MAX


# ---------------------------------------------------------------------------
# _load_state
# ---------------------------------------------------------------------------

def test_load_state_no_file():
    """Returns a fresh zero-count state when the file does not exist."""
    with patch("watchdog.get_file", return_value=None), \
         patch("watchdog._today", return_value="2099-01-01"):
        state = _load_state()
    assert state == {"date": "2099-01-01", "invocations": 0}


def test_load_state_same_day():
    """Returns the stored state when the date matches today."""
    stored = json.dumps({"date": "2099-01-01", "invocations": 4})
    with patch("watchdog.get_file", return_value=stored), \
         patch("watchdog._today", return_value="2099-01-01"):
        state = _load_state()
    assert state["invocations"] == 4


def test_load_state_stale_date():
    """Resets the counter when stored date is from a previous day."""
    stored = json.dumps({"date": "2099-01-01", "invocations": 9})
    with patch("watchdog.get_file", return_value=stored), \
         patch("watchdog._today", return_value="2099-01-02"):
        state = _load_state()
    assert state == {"date": "2099-01-02", "invocations": 0}


def test_load_state_corrupt_json():
    """Falls back to a fresh state when the file contains invalid JSON."""
    with patch("watchdog.get_file", return_value="not-json{{{"), \
         patch("watchdog._today", return_value="2099-06-15"):
        state = _load_state()
    assert state == {"date": "2099-06-15", "invocations": 0}


# ---------------------------------------------------------------------------
# _save_state — update path (file already exists)
# ---------------------------------------------------------------------------

def test_save_state_updates_existing_file():
    """Calls update_file when the state file already exists on main."""
    mock_contents = MagicMock()
    mock_contents.sha = "abc123"

    mock_repo = MagicMock()
    mock_repo.get_contents.return_value = mock_contents

    with patch("watchdog._get_repo", return_value=mock_repo):
        _save_state({"date": "2099-01-01", "invocations": 3})

    mock_repo.update_file.assert_called_once()
    args = mock_repo.update_file.call_args
    # First positional arg is the path
    assert args[0][0] == ".watchdog/state.json"
    # Content should be valid JSON
    payload = json.loads(args[0][2])
    assert payload["invocations"] == 3


def test_save_state_creates_new_file():
    """Calls create_file when the state file does not yet exist (404)."""
    from github import GithubException

    mock_repo = MagicMock()
    mock_repo.get_contents.side_effect = GithubException(404, {}, {})

    with patch("watchdog._get_repo", return_value=mock_repo):
        _save_state({"date": "2099-01-01", "invocations": 1})

    mock_repo.create_file.assert_called_once()
    args = mock_repo.create_file.call_args
    assert args[0][0] == ".watchdog/state.json"


# ---------------------------------------------------------------------------
# _check_and_increment_daily
# ---------------------------------------------------------------------------

def test_check_allows_first_invocation():
    """Allows the first invocation (count 0 → 1) and persists updated state."""
    initial_state = {"date": "2099-01-01", "invocations": 0}
    saved = {}

    def fake_save(s):
        saved.update(s)

    with patch("watchdog._load_state", return_value=initial_state), \
         patch("watchdog._save_state", side_effect=fake_save):
        result = _check_and_increment_daily()

    assert result is True
    assert saved["invocations"] == 1


def test_check_allows_up_to_ceiling():
    """Allows invocation when count is exactly one below the ceiling."""
    initial_state = {"date": "2099-01-01", "invocations": WATCHDOG_DAILY_MAX - 1}
    saved = {}

    def fake_save(s):
        saved.update(s)

    with patch("watchdog._load_state", return_value=initial_state), \
         patch("watchdog._save_state", side_effect=fake_save):
        result = _check_and_increment_daily()

    assert result is True
    assert saved["invocations"] == WATCHDOG_DAILY_MAX


def test_check_blocks_at_ceiling():
    """Returns False (and does NOT save) when the ceiling is already reached."""
    initial_state = {"date": "2099-01-01", "invocations": WATCHDOG_DAILY_MAX}

    with patch("watchdog._load_state", return_value=initial_state), \
         patch("watchdog._save_state") as mock_save:
        result = _check_and_increment_daily()

    assert result is False
    mock_save.assert_not_called()


def test_check_blocks_above_ceiling():
    """Returns False even when stored count exceeds the ceiling."""
    initial_state = {"date": "2099-01-01", "invocations": WATCHDOG_DAILY_MAX + 5}

    with patch("watchdog._load_state", return_value=initial_state), \
         patch("watchdog._save_state") as mock_save:
        result = _check_and_increment_daily()

    assert result is False
    mock_save.assert_not_called()
