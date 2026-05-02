"""Deterministic Supervisor skeleton for planning factory work."""
from typing import Dict, Optional

from backend.agents.registry import require_agents
from backend.recipes import normalize_workspace, select_recipe


def plan_task(prompt: str, workspace: Optional[str] = None) -> Dict:
    normalized_workspace = normalize_workspace(workspace)
    recipe = select_recipe(prompt, normalized_workspace)
    agents = require_agents(recipe.default_agents)
    return {
        "workspace": normalized_workspace,
        "recipe": recipe.to_dict(),
        "agents": [agent.to_dict() for agent in agents],
        "stages": recipe.stages,
        "approval_gates": recipe.approval_gates,
        "artifact_types": recipe.artifact_types,
        "success_criteria": recipe.success_criteria,
    }
