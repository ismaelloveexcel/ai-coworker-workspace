"""Policy decisions for local worker tool execution."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"

# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

STAGE_PLANNING = "planning"
STAGE_READ = "read"
STAGE_EDIT = "edit"
STAGE_VALIDATE = "validate"
STAGE_FINALIZE = "finalize"
STAGE_BROWSER = "browser"

# Static allowlist matrix: every known tool mapped to its stage/class.
# Tools absent from this matrix are unknown and denied by default when a
# stage context is active.
STAGE_TOOL_MATRIX: Dict[str, str] = {
    # planning / supervision
    "repo_snapshot": STAGE_PLANNING,
    "cost_status": STAGE_PLANNING,
    "humanize_error": STAGE_PLANNING,
    # read / inspection
    "filesystem_read": STAGE_READ,
    "filesystem_list": STAGE_READ,
    "github_read_file": STAGE_READ,
    "github_list_files": STAGE_READ,
    "github_compare_branch": STAGE_READ,
    "source_summarize": STAGE_READ,
    "research_compare": STAGE_READ,
    # edit / commit
    "filesystem_write": STAGE_EDIT,
    "github_create_branch": STAGE_EDIT,
    "github_commit_files": STAGE_EDIT,
    "secret_scan": STAGE_EDIT,
    # test / validation
    "run_tests": STAGE_VALIDATE,
    # PR / finalization
    "github_create_pr": STAGE_FINALIZE,
    # browser / network
    "playwright_browse": STAGE_BROWSER,
    "web_search": STAGE_BROWSER,
    "fetch_url": STAGE_BROWSER,
}

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


# ---------------------------------------------------------------------------
# Per-run stage context
# ---------------------------------------------------------------------------


class AgentStageContext:
    """Tracks per-run execution state for stage-aware policy enforcement.

    Create one instance per agent loop run and pass it to every
    ``evaluate_tool_call`` / ``execute_tool`` invocation.  The enforcer
    records gate-relevant side-effects (branch creation, validation) so that
    downstream capability checks (commit, PR) can be evaluated correctly.
    """

    def __init__(self, *, browser_enabled: bool = False) -> None:
        self._branch_created: bool = False
        self._validation_done: bool = False
        self._browser_enabled: bool = browser_enabled

    # -- state setters -------------------------------------------------------

    def record_tool_succeeded(self, tool_name: str) -> None:
        """Update gate state after a successful tool execution."""
        if tool_name == "github_create_branch":
            self._branch_created = True
        elif tool_name == "run_tests":
            self._validation_done = True

    def enable_browser(self) -> None:
        """Explicitly opt-in to browser/network tools for this run."""
        self._browser_enabled = True

    # -- state accessors -----------------------------------------------------

    @property
    def branch_created(self) -> bool:
        return self._branch_created

    @property
    def validation_done(self) -> bool:
        return self._validation_done

    @property
    def browser_enabled(self) -> bool:
        return self._browser_enabled


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


def _stage_context_policy(
    tool_name: str, context: "AgentStageContext"
) -> PolicyDecision | None:
    """Apply stage-context gates; return a denial decision or *None* to continue.

    When an ``AgentStageContext`` is active the caller opts in to *strict mode*:
    every tool must be listed in ``STAGE_TOOL_MATRIX``.  Tools known to the
    legacy ``_SAFE_NONLOCAL_TOOLS`` / ``_LOCAL_TOOL_CATEGORIES`` sets but absent
    from the matrix are therefore also denied – callers that require the legacy
    permissive behaviour should omit the context argument.

    Gates checked (in order):
    1. Tool must be present in STAGE_TOOL_MATRIX – deny unknown tools.
    2. browser/network tools require ``context.browser_enabled``.
    3. ``github_commit_files`` requires a branch to have been created first.
    4. ``github_create_pr`` requires validation (run_tests) to have completed.
    """
    stage = STAGE_TOOL_MATRIX.get(tool_name)
    if stage is None:
        return _decision(tool_name, "unknown", DENY, "Unknown tools are denied by policy")

    if stage == STAGE_BROWSER and not context.browser_enabled:
        return _decision(
            tool_name,
            STAGE_BROWSER,
            DENY,
            "Browser/network tools are disabled; call context.enable_browser() to opt in",
        )

    if tool_name == "github_commit_files" and not context.branch_created:
        return _decision(
            tool_name,
            STAGE_EDIT,
            DENY,
            "github_commit_files is gated behind branch creation; call github_create_branch first",
        )

    if tool_name == "github_create_pr" and not context.validation_done:
        return _decision(
            tool_name,
            STAGE_FINALIZE,
            DENY,
            "github_create_pr is gated behind validation; complete run_tests before creating a PR",
        )

    return None


def evaluate_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any] | None = None,
    context: "AgentStageContext | None" = None,
) -> PolicyDecision:
    """Return a policy decision before executing a tool.

    When *context* is supplied the call is evaluated against the static
    ``STAGE_TOOL_MATRIX`` and any active capability gates before the
    category-level path/suite checks are applied.
    """
    tool_input = tool_input or {}

    # Stage-context enforcement (new in PR-B2)
    if context is not None:
        stage_decision = _stage_context_policy(tool_name, context)
        if stage_decision is not None:
            return stage_decision

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
