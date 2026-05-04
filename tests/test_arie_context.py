"""Tests for deterministic ARIE company context pack."""

from backend.company_context import (
    classify_departments,
    find_related_items,
    get_company_context,
    infer_workflow_type,
    list_company_contexts,
)


def test_arie_finance_context_exists():
    ctx = get_company_context("arie_finance")
    assert ctx["company_id"] == "arie_finance"
    assert ctx["company_name"] == "ARIE Finance"
    assert "regulated financial" in ctx["industry"].lower()
    assert len(ctx["known_gaps"]) >= 10
    assert len(ctx["planned_builds"]) >= 10
    assert any("Data Protection" in x for x in ctx["compliance_assumptions"])


def test_list_company_contexts():
    lst = list_company_contexts()
    assert any(x["company_id"] == "arie_finance" for x in lst)


def test_find_related_items_lead_generation():
    r = find_related_items("arie_finance", "lead generation tool for prospects", limit=8)
    ids = {x["id"] for x in r["related_builds"]} | {x["id"] for x in r["related_gaps"]}
    assert "BP-016" in ids or "GA-048" in ids


def test_find_related_items_crm():
    r = find_related_items("arie_finance", "we need a CRM for client tracking", limit=8)
    ids = {x["id"] for x in r["related_builds"]} | {x["id"] for x in r["related_gaps"]}
    assert "BP-013" in ids or "GA-046" in ids


def test_classify_departments_employee_onboarding():
    d = classify_departments("arie_finance", "employee onboarding checklist")
    assert "HR & People Operations" in d


def test_classify_departments_board_pack():
    d = classify_departments("arie_finance", "board pack generator for KPIs")
    assert "Internal Systems & Reporting" in d


def test_ad_hoc_unrelated_query():
    r = find_related_items(
        "arie_finance",
        "completely unrelated cosmic widget zzzqx99 nonsense corporate token",
        limit=8,
    )
    assert r["is_ad_hoc"] is True
    assert r.get("suggested_category")


def test_infer_workflow_type_business_agent():
    assert infer_workflow_type("build an HR chatbot for staff", "blueprint") == "build_business_agent"
