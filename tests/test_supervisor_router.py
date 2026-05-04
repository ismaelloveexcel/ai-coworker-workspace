"""Tests for ARIE supervisor routing (deterministic)."""

from backend.supervisor import plan_task
from backend.supervisor_router import build_supervisor_plan


def test_lead_generation_plan_experts_and_flags():
    p = build_supervisor_plan(
        "I need a lead generation tool for ARIE Finance",
        workspace="work",
        company="ARIE Finance",
        output_mode="Blueprint",
    )
    assert p["compliance_required"] is True
    assert p["research_required"] is True
    ids = p["required_experts"]
    assert "arie_sales_expert" in ids
    assert "arie_marketing_expert" in ids
    assert "arie_compliance_expert" in ids
    assert "arie_technology_expert" in ids
    assert ids[-1] == "arie_compliance_expert"


def test_hr_agent_plan():
    p = build_supervisor_plan(
        "Create an HR agent for staff questions",
        workspace="work",
        company="ARIE Finance",
        output_mode="Business agent",
    )
    assert p["workflow_type"] == "build_business_agent"
    ids = p["required_experts"]
    assert "arie_hr_expert" in ids
    assert "arie_compliance_expert" in ids
    assert "arie_technology_expert" in ids


def test_kyc_portal_plan():
    p = build_supervisor_plan(
        "KYC document collection portal for new clients",
        workspace="work",
        company="ARIE Finance",
        output_mode="Prototype",
    )
    ids = p["required_experts"]
    assert "arie_client_onboarding_expert" in ids
    assert "arie_compliance_expert" in ids
    assert "arie_technology_expert" in ids


def test_marketing_content_plan():
    p = build_supervisor_plan(
        "LinkedIn content calendar and newsletter campaigns",
        workspace="work",
        company="ARIE Finance",
        output_mode="Blueprint",
    )
    ids = p["required_experts"]
    assert "arie_marketing_expert" in ids
    assert "arie_compliance_expert" in ids
    assert "arie_technology_expert" not in ids


def test_reporting_dashboard_plan():
    p = build_supervisor_plan(
        "Management information dashboard with KPIs for executives",
        workspace="work",
        company="ARIE Finance",
        output_mode="Blueprint",
    )
    ids = p["required_experts"]
    assert "arie_reporting_expert" in ids
    assert "arie_compliance_expert" in ids
    assert "arie_technology_expert" not in ids


def test_reporting_with_build_adds_technology():
    p = build_supervisor_plan(
        "Build a dashboard tool for board KPI reporting",
        workspace="work",
        company="ARIE Finance",
        output_mode="Prototype",
    )
    ids = p["required_experts"]
    assert "arie_reporting_expert" in ids
    assert "arie_technology_expert" in ids


def test_personal_workspace_router_not_arie():
    p = build_supervisor_plan("anything", workspace="personal", company=None, output_mode="Blueprint")
    assert p["compliance_required"] is False
    assert p["required_experts"] == []


def test_non_arie_work_company_router():
    p = build_supervisor_plan(
        "internal spreadsheet",
        workspace="work",
        company="Independent Ltd",
        output_mode="Blueprint",
    )
    assert p["compliance_required"] is False
    assert p["company_id"] is None


def test_skip_research_flag():
    p = build_supervisor_plan(
        "blueprint for internal tool — skip research",
        workspace="work",
        company="ARIE Finance",
        output_mode="Blueprint",
    )
    assert p["research_required"] is False


def test_plan_task_personal_has_no_arie_plan_compliance():
    plan = plan_task("research competitors", "personal")
    assert "arie_plan" not in plan
    ids = [a["id"] for a in plan["agents"]]
    assert "arie_compliance_expert" not in ids


def test_plan_task_arie_includes_arie_plan_envelope():
    plan = plan_task("crm cleanup", "work", company="ARIE Finance")
    assert "arie_plan" in plan
    assert plan["arie_plan"]["structured_prompt"]
    assert plan["arie_plan"]["compliance_required"] is True
