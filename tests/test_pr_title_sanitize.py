"""
Tests for backend.main._sanitize_title — PR-title redaction.
"""
import os

os.environ.setdefault("AGENT_WORKSPACE", "/tmp/test_agent_workspace")

from backend.main import _sanitize_title, _PR_TITLE_MAX


def test_plain_title_unchanged():
    """Short plain-text titles pass through unchanged."""
    assert _sanitize_title("Fix login bug") == "Fix login bug"


def test_title_truncated():
    """Titles longer than _PR_TITLE_MAX are truncated with ellipsis."""
    long_title = "A" * (_PR_TITLE_MAX + 10)
    result = _sanitize_title(long_title)
    assert len(result) <= _PR_TITLE_MAX
    assert result.endswith("…")


def test_title_at_exact_max_not_truncated():
    """A title of exactly _PR_TITLE_MAX chars is NOT truncated."""
    exact = "B" * _PR_TITLE_MAX
    result = _sanitize_title(exact)
    assert result == exact


def test_control_characters_stripped():
    """ASCII control characters are replaced with spaces."""
    result = _sanitize_title("Fix\x00bug\x1fhere")
    assert "\x00" not in result
    assert "\x1f" not in result
    assert "Fix" in result
    assert "bug" in result


def test_empty_title_returns_placeholder():
    """Empty (or whitespace-only) title returns '(untitled)'."""
    assert _sanitize_title("") == "(untitled)"
    assert _sanitize_title("   ") == "(untitled)"


def test_secret_like_token_redacted():
    """A string matching the secret heuristic (20+ chars, high diversity) is redacted."""
    # Simulate a user accidentally pasting an API key in the title
    secret = "sk-Ant1234567890abcdefXYZ"   # 24 chars, mixed case + digits + hyphen
    result = _sanitize_title(f"Task with key {secret}")
    assert "[REDACTED]" in result
    assert secret not in result


def test_plain_lowercase_word_not_redacted():
    """Long lowercase words (like a URL slug) are NOT redacted as secrets."""
    slug = "implementcompleteauthenticationworkflow"  # 38 chars, all lowercase, no digits
    result = _sanitize_title(slug)
    assert "[REDACTED]" not in result


def test_plain_uppercase_word_not_redacted():
    """Long uppercase words are NOT redacted."""
    word = "IMPLEMENTCOMPLETEAUTHENTICATIONFLOW"  # all uppercase, no digits
    result = _sanitize_title(word)
    assert "[REDACTED]" not in result


def test_whitespace_stripped():
    """Leading and trailing whitespace is stripped."""
    result = _sanitize_title("  fix bug  ")
    assert result == "fix bug"


def test_sanitize_title_via_request_model():
    """The field validator in CreateTaskRequest applies _sanitize_title."""
    from backend.main import CreateTaskRequest
    req = CreateTaskRequest(title="  hello world  ", prompt="do something")
    assert req.title == "hello world"
