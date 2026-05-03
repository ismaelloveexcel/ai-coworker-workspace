"""Canonical tool catalog — single source of truth for all tool IDs and categories.

Every surface that exposes tool information (runtime registry, policy engine,
model-visible docs, and agent allowed_tools lists) MUST derive from this
module.  Adding, renaming, or removing a tool requires a change here first;
the parity tests will fail if any surface falls out of sync.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List

# ---------------------------------------------------------------------------
# Category constants — used by policy.py to route tool calls
# ---------------------------------------------------------------------------

CATEGORY_FILESYSTEM = "filesystem"
CATEGORY_BROWSER = "browser"
CATEGORY_SHELL_ALLOWLISTED = "shell_allowlisted"
CATEGORY_SHELL_SANDBOXED = "shell_sandboxed"
CATEGORY_REMOTE_OR_SUPPORT = "remote_or_support"


@dataclass(frozen=True)
class ToolEntry:
    id: str
    category: str
    description: str


# ---------------------------------------------------------------------------
# THE canonical catalog — one entry per tool, ordered alphabetically within
# each category for readability.
# ---------------------------------------------------------------------------

TOOL_CATALOG: List[ToolEntry] = [
    # GitHub / remote operations
    ToolEntry(
        id="github_create_branch",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Create a new git branch in a GitHub repository.",
    ),
    ToolEntry(
        id="github_commit_files",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Commit one or more files to a GitHub repository branch.",
    ),
    ToolEntry(
        id="github_create_pr",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Open a pull request in a GitHub repository.",
    ),
    ToolEntry(
        id="github_read_file",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Read a file from a GitHub repository.",
    ),
    ToolEntry(
        id="github_list_files",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="List files and directories in a GitHub repository path.",
    ),
    ToolEntry(
        id="github_compare_branch",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Compare two branches in a GitHub repository.",
    ),
    # Local filesystem (sandbox-scoped)
    ToolEntry(
        id="filesystem_read",
        category=CATEGORY_FILESYSTEM,
        description="Read a file from the local task sandbox.",
    ),
    ToolEntry(
        id="filesystem_write",
        category=CATEGORY_FILESYSTEM,
        description="Write a file to the local task sandbox.",
    ),
    ToolEntry(
        id="filesystem_list",
        category=CATEGORY_FILESYSTEM,
        description="List files in the local task sandbox.",
    ),
    # Browser / network (requires operator approval)
    ToolEntry(
        id="playwright_browse",
        category=CATEGORY_BROWSER,
        description="Browse a URL using a headless browser (requires operator approval).",
    ),
    # Support / meta
    ToolEntry(
        id="repo_snapshot",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Capture a snapshot of the target repository structure and key files.",
    ),
    # Test / validation
    ToolEntry(
        id="run_tests",
        category=CATEGORY_SHELL_ALLOWLISTED,
        description="Run an allowlisted test suite in the task sandbox.",
    ),
    # Security
    ToolEntry(
        id="secret_scan",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Scan content for hardcoded secrets before committing.",
    ),
    # Utility / observability
    ToolEntry(
        id="humanize_error",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Translate a raw error into plain English for the operator.",
    ),
    ToolEntry(
        id="cost_status",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Check the current task token spend and budget status.",
    ),
    # Web research (domain-allowlisted)
    ToolEntry(
        id="web_search",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Search the web for information (domain-allowlisted).",
    ),
    ToolEntry(
        id="fetch_url",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Fetch content from a URL (domain-allowlisted).",
    ),
    ToolEntry(
        id="source_summarize",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Summarize a web source for research (domain-allowlisted).",
    ),
    ToolEntry(
        id="research_compare",
        category=CATEGORY_REMOTE_OR_SUPPORT,
        description="Compare multiple web research sources side-by-side.",
    ),
]

# ---------------------------------------------------------------------------
# Derived structures — importable by policy.py, tool_adapters.py, and tests
# ---------------------------------------------------------------------------

#: Complete set of all canonical tool IDs.
TOOL_IDS: FrozenSet[str] = frozenset(entry.id for entry in TOOL_CATALOG)

#: Mapping from tool ID → category string, used by policy.py.
TOOL_CATEGORIES: Dict[str, str] = {entry.id: entry.category for entry in TOOL_CATALOG}

#: Tools that operate on the local filesystem (path policy applies).
LOCAL_FILESYSTEM_IDS: FrozenSet[str] = frozenset(
    entry.id for entry in TOOL_CATALOG if entry.category == CATEGORY_FILESYSTEM
)

#: Tools that open a browser or make outbound network requests.
BROWSER_IDS: FrozenSet[str] = frozenset(
    entry.id for entry in TOOL_CATALOG if entry.category == CATEGORY_BROWSER
)

#: Tools that run shell commands (suite allowlist applies).
SHELL_ALLOWLISTED_IDS: FrozenSet[str] = frozenset(
    entry.id for entry in TOOL_CATALOG if entry.category == CATEGORY_SHELL_ALLOWLISTED
)

#: Sandboxed argv-only subprocess tools (policy applies per tool).
SHELL_SANDBOXED_IDS: FrozenSet[str] = frozenset(
    entry.id for entry in TOOL_CATALOG if entry.category == CATEGORY_SHELL_SANDBOXED
)

#: Remote / support tools: no local side-effects; allowed by default.
REMOTE_OR_SUPPORT_IDS: FrozenSet[str] = frozenset(
    entry.id for entry in TOOL_CATALOG if entry.category == CATEGORY_REMOTE_OR_SUPPORT
)
