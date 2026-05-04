"""Deterministic Supervisor skeleton for planning factory work."""
from typing import Dict, List, Optional

from backend.agents.registry import require_agents
from backend.arie_compliance import is_arie_compliance_gated, merge_compliance_path
from backend.recipes import get_recipe, normalize_workspace, select_recipe
from backend.supervisor_router import build_supervisor_plan


def _merge_recipe_with_router_experts(recipe_agent_ids: List[str], required_arie: List[str]) -> List[str]:
    """Keep non-ARIE recipe agents; append router-selected ARIE experts (compliance last)."""
    non_arie = [a for a in recipe_agent_ids if not a.startswith("arie_")]
    arie_ordered = [a for a in required_arie if a != "arie_compliance_expert"]
    has_compliance = "arie_compliance_expert" in required_arie
    seen = set()
    out: List[str] = []
    for a in non_arie + arie_ordered:
        if a not in seen:
            seen.add(a)
            out.append(a)
    if has_compliance:
        out = [a for a in out if a != "arie_compliance_expert"]
        out.append("arie_compliance_expert")
    return out


def plan_task(
    prompt: str,
    workspace: Optional[str] = None,
    company: Optional[str] = None,
    output_mode: Optional[str] = None,
) -> Dict:
    normalized_workspace = normalize_workspace(workspace)
    arie_plan: Optional[Dict] = None
    if is_arie_compliance_gated(workspace=normalized_workspace, company=company):
        arie_plan = build_supervisor_plan(
            prompt or "",
            normalized_workspace,
            company,
            output_mode or "blueprint",
        )
        rid = (arie_plan.get("suggested_recipe_id") or "").strip()
        alt = get_recipe(rid) if rid else None
        if alt is not None and normalized_workspace in alt.workspace_scope:
            recipe = alt
        else:
            recipe = select_recipe(prompt, normalized_workspace)
    else:
        recipe = select_recipe(prompt, normalized_workspace)

    if arie_plan and arie_plan.get("required_experts"):
        agent_ids = _merge_recipe_with_router_experts(recipe.default_agents, arie_plan["required_experts"])
    else:
        agent_ids = merge_compliance_path(recipe.default_agents, prompt, normalized_workspace, company)
    agents = require_agents(agent_ids)
    out: Dict = {
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
    if arie_plan is not None:
        out["arie_plan"] = arie_plan
    return out
