"""Catalog parity tests — fail deterministically when tool surfaces drift.

Surfaces checked:
  1. Runtime registry  (_TOOL_MAP keys in tool_adapters)
  2. Policy engine     (_LOCAL_TOOL_CATEGORIES + _SAFE_NONLOCAL_TOOLS in policy)
  3. Agent allowed_tools lists (builtin agent definitions)
  4. Model-visible docs (CLAUDE.md tool list)

All four must expose exactly the same tool IDs as the canonical catalog
defined in backend/tool_catalog.py.
"""
from __future__ import annotations

import os
import re

import pytest

from backend.tool_catalog import TOOL_CATALOG, TOOL_IDS
from backend.tool_adapters import _ALLOWED_TOOLS, _TOOL_MAP
from backend.policy import _LOCAL_TOOL_CATEGORIES, _SAFE_NONLOCAL_TOOLS
from backend.agents.builtin import BUILTIN_AGENTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CLAUDE_MD = os.path.join(_REPO_ROOT, "CLAUDE.md")

_TOOL_BULLET_RE = re.compile(r"^-\s+([\w_]+)\s*$", re.MULTILINE)


def _claude_md_tool_ids() -> set:
    """Extract tool IDs listed under the TOOL NAMES section of CLAUDE.md."""
    with open(_CLAUDE_MD, encoding="utf-8") as fh:
        text = fh.read()
    # Find the TOOL NAMES section and stop at the next ## heading
    section_match = re.search(
        r"##\s+TOOL NAMES.*?\n(.*?)(?=\n##|\Z)", text, re.DOTALL
    )
    if not section_match:
        return set()
    return set(_TOOL_BULLET_RE.findall(section_match.group(1)))


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------


def test_catalog_has_no_duplicate_ids():
    from collections import Counter
    ids = [entry.id for entry in TOOL_CATALOG]
    duplicates = [k for k, v in Counter(ids).items() if v > 1]
    assert not duplicates, f"Duplicate tool IDs in catalog: {duplicates}"


def test_catalog_ids_match_derived_set():
    assert TOOL_IDS == {entry.id for entry in TOOL_CATALOG}


# ---------------------------------------------------------------------------
# Registry parity
# ---------------------------------------------------------------------------


def test_tool_map_ids_match_catalog():
    """Every key in _TOOL_MAP must be in the canonical catalog."""
    registry_ids = set(_TOOL_MAP.keys())
    extra = registry_ids - TOOL_IDS
    missing = TOOL_IDS - registry_ids
    assert not extra, f"_TOOL_MAP has tools NOT in catalog: {sorted(extra)}"
    assert not missing, f"Catalog has tools NOT implemented in _TOOL_MAP: {sorted(missing)}"


def test_allowed_tools_ids_match_catalog():
    """_ALLOWED_TOOLS must equal the canonical catalog exactly."""
    extra = _ALLOWED_TOOLS - TOOL_IDS
    missing = TOOL_IDS - _ALLOWED_TOOLS
    assert not extra, f"_ALLOWED_TOOLS has extra IDs not in catalog: {sorted(extra)}"
    assert not missing, f"Catalog has IDs missing from _ALLOWED_TOOLS: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Policy parity
# ---------------------------------------------------------------------------


def test_policy_knows_all_catalog_tools():
    """Every catalog tool ID must be known to the policy engine."""
    policy_known = set(_LOCAL_TOOL_CATEGORIES) | set(_SAFE_NONLOCAL_TOOLS)
    extra = policy_known - TOOL_IDS
    missing = TOOL_IDS - policy_known
    assert not extra, f"Policy knows tools NOT in catalog: {sorted(extra)}"
    assert not missing, f"Catalog tools absent from policy: {sorted(missing)}"


def test_policy_local_categories_only_contain_catalog_ids():
    extra = set(_LOCAL_TOOL_CATEGORIES) - TOOL_IDS
    assert not extra, f"_LOCAL_TOOL_CATEGORIES references unknown IDs: {sorted(extra)}"


def test_policy_safe_nonlocal_only_contains_catalog_ids():
    extra = set(_SAFE_NONLOCAL_TOOLS) - TOOL_IDS
    assert not extra, f"_SAFE_NONLOCAL_TOOLS references unknown IDs: {sorted(extra)}"


def test_policy_no_overlap_between_local_and_nonlocal():
    overlap = set(_LOCAL_TOOL_CATEGORIES) & set(_SAFE_NONLOCAL_TOOLS)
    assert not overlap, f"Same tool ID appears in both local categories and safe-nonlocal: {sorted(overlap)}"


# ---------------------------------------------------------------------------
# Agent allowed_tools parity
# ---------------------------------------------------------------------------


def test_agent_allowed_tools_only_reference_catalog_ids():
    """No agent definition may reference a tool ID outside the canonical catalog."""
    violations: list[str] = []
    for agent in BUILTIN_AGENTS:
        unknown = [t for t in agent.allowed_tools if t not in TOOL_IDS]
        if unknown:
            violations.append(f"  agent {agent.id!r}: unknown tools {unknown}")
    assert not violations, "Agent definitions reference tools not in catalog:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Model-visible docs parity
# ---------------------------------------------------------------------------


def test_claude_md_lists_all_catalog_tools():
    """CLAUDE.md TOOL NAMES section must list every catalog tool ID."""
    doc_ids = _claude_md_tool_ids()
    missing = TOOL_IDS - doc_ids
    extra = doc_ids - TOOL_IDS
    assert not missing, f"CLAUDE.md is missing catalog tools: {sorted(missing)}"
    assert not extra, f"CLAUDE.md lists tools not in catalog: {sorted(extra)}"


# ---------------------------------------------------------------------------
# Drift-detection smoke test (proves tests fail when drift is introduced)
# ---------------------------------------------------------------------------


def test_drift_check_detects_phantom_tool_in_allowed_set():
    """Manually introduce drift and confirm the check catches it."""
    phantom = {"__phantom_tool__"}
    combined = _ALLOWED_TOOLS | phantom
    extra = combined - TOOL_IDS
    assert extra == phantom, "Drift check should have caught the phantom tool"


def test_drift_check_detects_missing_tool_from_allowed_set():
    """Manually remove a tool and confirm the check catches it."""
    if not _ALLOWED_TOOLS:
        pytest.skip("Empty allowed set — nothing to remove")
    shrunk = set(_ALLOWED_TOOLS) - {next(iter(_ALLOWED_TOOLS))}
    missing = TOOL_IDS - shrunk
    assert missing, "Drift check should have caught the missing tool"
