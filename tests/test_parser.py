"""
Tests for backend.claude_wrapper — parse_action and _extract_json_object.
Covers F15 regression: INPUT regex must handle nested JSON objects.
"""
import pytest
from backend.claude_wrapper import _extract_json_object, parse_action


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------

def test_extract_simple():
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_nested():
    text = 'INPUT: {"files": [{"path": "a.py", "content": "if x: pass"}]}'
    result = _extract_json_object(text)
    import json
    parsed = json.loads(result)
    assert parsed["files"][0]["path"] == "a.py"


def test_extract_braces_in_string():
    text = '{"content": "if (x) { return; }"}'
    result = _extract_json_object(text)
    import json
    assert json.loads(result)["content"] == "if (x) { return; }"


def test_extract_leading_prose():
    text = 'Here is the object: {"key": "value"} and some trailing text'
    assert _extract_json_object(text) == '{"key": "value"}'


def test_extract_none_when_missing():
    assert _extract_json_object("no braces here") is None


# ---------------------------------------------------------------------------
# parse_action
# ---------------------------------------------------------------------------

VALID_RESPONSE = """
PLAN:
- Read the existing file
- Commit the change

ACTION: tool_call

TOOL: github_read_file

INPUT: {"path": "backend/main.py", "branch": "task/abc123"}

REASONING: Need to read the file before editing it.
"""

def test_parse_action_valid():
    result = parse_action(VALID_RESPONSE)
    assert result["action"] == "tool_call"
    assert result["tool"] == "github_read_file"
    assert result["input"]["path"] == "backend/main.py"


def test_parse_action_nested_input():
    text = """
PLAN:
- Commit files

ACTION: tool_call

TOOL: github_commit_files

INPUT: {"branch": "task/x", "message": "add files", "files": [{"path": "a.py", "content": "x=1"}]}

REASONING: Committing.
"""
    result = parse_action(text)
    assert result["tool"] == "github_commit_files"
    assert result["input"]["files"][0]["path"] == "a.py"


def test_parse_action_missing_plan():
    with pytest.raises(ValueError, match="PLAN"):
        parse_action("ACTION: tool_call\nTOOL: x\nINPUT: {}\nREASONING: r")


def test_parse_action_invalid_action():
    text = "PLAN:\n- step\nACTION: invalid_value\nTOOL: x\nINPUT: {}\nREASONING: r"
    with pytest.raises(ValueError, match="ACTION"):
        parse_action(text)


def test_parse_action_final_answer():
    text = """
PLAN:
- Done

ACTION: final_answer

TOOL:

INPUT: {}

REASONING: Task is complete.
"""
    result = parse_action(text)
    assert result["action"] == "final_answer"
