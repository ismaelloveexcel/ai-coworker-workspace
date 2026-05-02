"""Operator studio helpers for recipes, run history, and handoff summaries."""
from __future__ import annotations

from typing import Any, Dict, List


_RECIPES: List[Dict[str, Any]] = [
    {
        "id": "market-research",
        "name": "Market Research",
        "category": "general",
        "description": "Compare competitors, extract positioning, and produce a source-backed brief.",
        "stages": ["scope", "gather sources", "compare", "recommend"],
        "artifacts": ["research_brief", "comparison_matrix"],
        "approval_gates": ["external source review"],
        "editable": False,
    },
    {
        "id": "build-mvp",
        "name": "Build MVP",
        "category": "personal",
        "description": "Turn a product idea into a small tested implementation plan and first build.",
        "stages": ["requirements", "design", "build", "validate", "handoff"],
        "artifacts": ["code_diff", "handoff_summary"],
        "approval_gates": ["dependency install", "deployment"],
        "editable": False,
    },
    {
        "id": "lead-qualification",
        "name": "Lead Qualification Workflow",
        "category": "work",
        "description": "Prepare a repeatable workflow for qualifying and enriching leads.",
        "stages": ["input audit", "enrichment plan", "workflow build", "review"],
        "artifacts": ["workflow", "spreadsheet", "automation_blueprint"],
        "approval_gates": ["customer data access", "external system write"],
        "editable": False,
    },
    {
        "id": "fix-failure",
        "name": "Fix Failure",
        "category": "general",
        "description": "Diagnose a failing run, patch the root cause, and summarize the fix.",
        "stages": ["triage", "patch", "test", "handoff"],
        "artifacts": ["code_diff", "run_history", "handoff_summary"],
        "approval_gates": ["protected path edit"],
        "editable": False,
    },
]


def recipe_catalog(category: str = "") -> Dict[str, Any]:
    selected = [recipe for recipe in _RECIPES if not category or recipe["category"] == category]
    return {
        "recipes": selected,
        "count": len(selected),
        "categories": sorted({recipe["category"] for recipe in _RECIPES}),
        "policy": "Built-in recipes are preview-only in this slice; edits require a future policy-gated write path.",
    }


def run_history(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    for task in tasks:
        rows.append({
            "task_id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "branch": task.get("branch"),
            "pr_url": task.get("pr_url"),
            "current_step": task.get("current_step") or 0,
            "last_action": task.get("last_action"),
            "last_tool": task.get("last_tool"),
            "usd_spent": float(task.get("usd_spent") or 0),
            "updated_at": task.get("updated_at"),
        })
    return {"runs": rows, "count": len(rows)}


def artifact_index(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    artifacts = []
    for task in tasks:
        artifact_type = "handoff_summary" if task.get("status") in {"done", "failed", "cancelled"} else "run_snapshot"
        artifacts.append({
            "id": f"task:{task['id']}",
            "artifact_type": artifact_type,
            "title": task["title"],
            "task_id": task["id"],
            "status": task["status"],
            "trace": {
                "branch": task.get("branch"),
                "pr_url": task.get("pr_url"),
                "last_tool": task.get("last_tool"),
                "recovery_note": task.get("recovery_note"),
            },
        })
    return {"artifacts": artifacts, "count": len(artifacts), "source": "tasks"}


def handoff_summary(task: Dict[str, Any], steps: List[Dict[str, Any]], logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed_steps = [step for step in steps if step.get("status") == "done"]
    failed_steps = [step for step in steps if step.get("status") == "failed"]
    summary_lines = [
        f"Task: {task['title']}",
        f"Status: {task['status']}",
        f"Branch: {task.get('branch') or 'none'}",
        f"Pull request: {task.get('pr_url') or 'none'}",
        f"Steps: {len(steps)} total, {len(completed_steps)} completed, {len(failed_steps)} failed",
    ]
    if task.get("error"):
        summary_lines.append(f"Error: {task['error']}")
    if task.get("recovery_note"):
        summary_lines.append(f"Recovery: {task['recovery_note']}")
    return {
        "artifact_type": "handoff_summary",
        "task_id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "summary": "\n".join(summary_lines),
        "trace": {
            "branch": task.get("branch"),
            "pr_url": task.get("pr_url"),
            "last_action": task.get("last_action"),
            "last_tool": task.get("last_tool"),
            "log_count": len(logs),
        },
    }
