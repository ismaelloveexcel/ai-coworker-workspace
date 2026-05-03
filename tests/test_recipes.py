import pytest

from backend.recipes import list_recipes, normalize_workspace, select_recipe
from backend.supervisor import plan_task


def test_select_recipe_matches_market_research_prompt():
    recipe = select_recipe("research similar products and compare competitors", "personal")

    assert recipe.id == "market_research"


def test_select_recipe_respects_work_workspace():
    recipe = select_recipe("clean up crm leads and enrich the pipeline", "work")

    assert recipe.id == "crm_cleanup_enrichment"


def test_plan_task_returns_recipe_agents_and_stages():
    plan = plan_task("turn this SOP into a workflow", "work", company="ARIE Finance")

    assert plan["workspace"] == "work"
    assert plan["recipe"]["id"] == "sop_to_workflow"
    assert any(agent["id"] == "sop_digitiser" for agent in plan["agents"])
    assert any(agent["id"] == "arie_compliance_expert" for agent in plan["agents"])
    assert plan["agent_ids"][-1] == "arie_compliance_expert"
    assert plan["stages"]


def test_plan_task_work_non_arie_skips_compliance_experts():
    plan = plan_task("turn this SOP into a workflow", "work", company="Independent Ltd")

    ids = [a["id"] for a in plan["agents"]]
    assert "arie_compliance_expert" not in ids


def test_plan_task_personal_has_no_arie_compliance_expert():
    plan = plan_task("research competitors", "personal")

    ids = [a["id"] for a in plan["agents"]]
    assert "arie_compliance_expert" not in ids


def test_list_recipes_filters_by_workspace():
    personal = list_recipes("personal")

    assert all("personal" in recipe["workspace_scope"] for recipe in personal)


def test_select_recipe_uses_workspace_fallback_when_no_keywords_match():
    personal = select_recipe("please handle this", "personal")
    work = select_recipe("please handle this", "work")

    assert personal.id == "build_tool"
    assert work.id == "sop_to_workflow"


def test_select_recipe_tie_breaks_by_catalog_order():
    recipe = select_recipe("build app", "personal")

    assert recipe.id == "build_monetisable_app"


def test_normalize_workspace_rejects_unknown_value():
    with pytest.raises(ValueError, match="personal"):
        normalize_workspace("shared")
