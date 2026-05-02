"""Policy decisions for local worker tool execution."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"

_LOCAL_TOOL_CATEGORIES = {
    "filesystem_read": "filesystem",
    "filesystem_write": "filesystem",
    "filesystem_list": "filesystem",
    "playwright_browse": "browser",
    "run_tests": "shell_allowlisted",
}

_SAFE_NONLOCAL_TOOLS = {
    "github_create_branch",
    "github_commit_files",
    "github_create_pr",
    "github_read_file",
    "github_list_files",
    "github_compare_branch",
    "repo_snapshot",
    "secret_scan",
    "humanize_error",
    "cost_status",
    "web_search",
    "fetch_url",
    "source_summarize",
    "research_compare",
}

_SENSITIVE_PATH_SEGMENTS = {".ssh", ".gnupg", ".aws", ".azure", ".config", "AppData"}
_AUDIT_LIMIT = 500
_POLICY_AUDIT_LOG: List[Dict[str, Any]] = []


@dataclass(frozen=True)
class PolicyDecision:
    tool_name: str
    category: str
    outcome: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def _decision(tool_name: str, category: str, outcome: str, reason: str) -> PolicyDecision:
    return PolicyDecision(tool_name=tool_name, category=category, outcome=outcome, reason=reason)


def _path_from_input(tool_input: Dict[str, Any]) -> str:
    return str(tool_input.get("path") or "")


def _path_segments(path: str) -> List[str]:
    return [segment for segment in path.replace("\\", "/").split("/") if segment and segment != "."]


def _path_policy(tool_name: str, tool_input: Dict[str, Any]) -> PolicyDecision:
    path = _path_from_input(tool_input)
    segments = _path_segments(path)
    lowered_segments = [segment.lower() for segment in segments]
    if any(segment == ".." for segment in segments):
        return _decision(tool_name, "filesystem", DENY, "Path traversal is denied by policy")
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        return _decision(tool_name, "filesystem", DENY, "Absolute and home-directory paths are denied by policy")
    if any(segment == ".env" or segment.startswith(".env.") for segment in lowered_segments):
        return _decision(tool_name, "filesystem", DENY, ".env files are denied by policy")
    if any(segment.lower() in {value.lower() for value in _SENSITIVE_PATH_SEGMENTS} for segment in segments):
        return _decision(tool_name, "filesystem", DENY, "Credential and secret storage paths are denied by policy")
    return _decision(tool_name, "filesystem", ALLOW, "Filesystem action is constrained to the task sandbox")


def evaluate_tool_call(tool_name: str, tool_input: Dict[str, Any] | None = None) -> PolicyDecision:
    """Return a policy decision before executing a tool."""
    tool_input = tool_input or {}
    if tool_name in _SAFE_NONLOCAL_TOOLS:
        return _decision(tool_name, "remote_or_support", ALLOW, "Tool is outside the local worker action surface")
    category = _LOCAL_TOOL_CATEGORIES.get(tool_name)
    if category is None:
        return _decision(tool_name, "unknown", DENY, "Unknown tools are denied by policy")
    if category == "filesystem":
        return _path_policy(tool_name, tool_input)
    if category == "shell_allowlisted":
        suite = str(tool_input.get("suite") or "quick")
        allowed_suites = {"quick", "frontend", "all", "python_compile", "pytest", "typecheck", "lint", "actionlint"}
        if suite not in allowed_suites:
            return _decision(tool_name, category, DENY, f"Test suite {suite!r} is not allowlisted")
        return _decision(tool_name, category, ALLOW, "Allowlisted test command")
    if category == "browser":
        return _decision(tool_name, category, REQUIRE_APPROVAL, "Browser actions require operator approval")
    return _decision(tool_name, category, REQUIRE_APPROVAL, "Sensitive local action requires operator approval")


def record_policy_decision(decision: PolicyDecision) -> None:
    entry = {"timestamp": time.time(), **decision.to_dict()}
    _POLICY_AUDIT_LOG.append(entry)
    if len(_POLICY_AUDIT_LOG) > _AUDIT_LIMIT:
        del _POLICY_AUDIT_LOG[: len(_POLICY_AUDIT_LOG) - _AUDIT_LIMIT]


def get_policy_audit_log(limit: int = 100) -> List[Dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), _AUDIT_LIMIT))
    return list(_POLICY_AUDIT_LOG[-bounded_limit:])


def clear_policy_audit_log() -> None:
    _POLICY_AUDIT_LOG.clear()
