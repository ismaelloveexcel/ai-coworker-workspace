"""
Tests for watchdog._parse_json — robust extraction (pending fix).
"""
import json
import pytest

# Import directly — watchdog.py reads env at import; provide dummies
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-dummy")
os.environ.setdefault("GH_PAT", "ghp-dummy")
os.environ.setdefault("GITHUB_REPO", "owner/repo")

from watchdog import _parse_json


def test_parse_plain_json():
    result = _parse_json('{"fix_type": "code_patch"}')
    assert result["fix_type"] == "code_patch"


def test_parse_json_fenced():
    text = '```json\n{"approved": true}\n```'
    assert _parse_json(text)["approved"] is True


def test_parse_json_with_leading_prose():
    text = 'Sure, here is the diagnosis:\n{"diagnosis": "null pointer"}\nEnd.'
    assert _parse_json(text)["diagnosis"] == "null pointer"


def test_parse_json_nested():
    text = '{"patches": [{"file": "a.py", "find": "x", "replace": "y"}]}'
    result = _parse_json(text)
    assert result["patches"][0]["file"] == "a.py"


def test_parse_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        _parse_json("no json here at all")


def test_parse_json_braces_in_string():
    text = '{"content": "if x: { pass }"}'
    result = _parse_json(text)
    assert result["content"] == "if x: { pass }"
