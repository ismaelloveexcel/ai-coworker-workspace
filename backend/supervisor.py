"""Deterministic Supervisor skeleton for planning factory work."""
from typing import Dict, Optional

from backend.agents.registry import require_agents
from backend.arie_compliance import merge_compliance_path
from backend.recipes import normalize_workspace, select_recipe


def plan_task(prompt: str, workspace: Optional[str] = None, company: Optional[str] = None) -> Dict:
    normalized_workspace = normalize_workspace(workspace)
    recipe = select_recipe(prompt, normalized_workspace)
    agent_ids = merge_compliance_path(recipe.default_agents, prompt, normalized_workspace, company)
    agents = require_agents(agent_ids)
    return {
        "workspace": normalized_workspace,
        "company": company,
        "recipe": recipe.to_dict(),
        "agents": [agent.to_dict() for agent in agents],
        "agent_ids": agent_ids,
        "stages": recipe.stages,
        "approval_gates": recipe.approval_gates,
        "artifact_types": recipe.artifact_types,
        "success_criteria": recipe.success_criteria,
    }
