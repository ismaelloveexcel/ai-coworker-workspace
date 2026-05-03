"""ARIE Finance compliance routing and PR gate tests."""
import pytest

from backend.arie_compliance import (
    assert_pr_allowed_for_arie,
    merge_compliance_path,
    validate_compliance_review_outcome,
)
from backend.supervisor import plan_task


def test_arie_work_always_routes_compliance_expert_last():
    ids = merge_compliance_path(["supervisor", "builder"], "generic internal tool", "work", "ARIE Finance")
    assert "arie_compliance_expert" in ids
    assert ids[-1] == "arie_compliance_expert"


def test_lead_generation_includes_sales_marketing_compliance():
    ids = merge_compliance_path(
        ["supervisor"],
        "lead generation campaign for prospects in the pipeline",
        "work",
        "ARIE Finance",
    )
    assert "arie_sales_expert" in ids
    assert "arie_marketing_expert" in ids
    assert "arie_compliance_expert" in ids


def test_hr_request_routes_hr_and_compliance():
    ids = merge_compliance_path(
        ["supervisor"],
        "HR policy update for employee records and payroll",
        "work",
        "ARIE Finance",
    )
    assert "arie_hr_expert" in ids
    assert "arie_compliance_expert" in ids


def test_marketing_request_routes_marketing_and_compliance():
    ids = merge_compliance_path(
        ["supervisor"],
        "LinkedIn marketing posts for our newsletter campaign",
        "work",
        "ARIE Finance",
    )
    assert "arie_marketing_expert" in ids
    assert "arie_compliance_expert" in ids


def test_reporting_request_routes_reporting_and_compliance():
    ids = merge_compliance_path(
        ["supervisor"],
        "board pack and KPI dashboard for management reporting",
        "work",
        "ARIE Finance",
    )
    assert "arie_reporting_expert" in ids
    assert "arie_compliance_expert" in ids


def test_no_finalize_until_compliance_passed():
    task = {"workspace": "work", "company": "ARIE Finance", "compliance_passed": 0}
    ok, payload = assert_pr_allowed_for_arie(task)
    assert ok is False
    assert payload["risk_level"] == "high"
    assert "human_compliance_or_legal_review_required" in payload


def test_compliance_passed_allows_pr():
    task = {"workspace": "work", "company": "ARIE Finance", "compliance_passed": 1}
    ok, payload = assert_pr_allowed_for_arie(task)
    assert ok is True
    assert payload is None


def test_compliance_issues_found_includes_risk_and_fix():
    status, blob = validate_compliance_review_outcome(
        {
            "outcome": "issues_found",
            "risk_level": "medium",
            "summary": "Retention gap",
            "reviewer": "compliance@example.com",
            "issues": [
                {
                    "issue": "Missing retention schedule",
                    "risk_level": "medium",
                    "why_it_matters": "FIAMLA record-keeping expectations",
                    "recommended_fix": "Add a 5-year retention table",
                    "human_compliance_or_legal_review_required": True,
                }
            ],
        }
    )
    assert status == "compliance_issues_found"
    assert blob["issues"][0]["recommended_fix"]


def test_personal_tasks_not_gated():
    task = {"workspace": "personal", "company": None, "compliance_passed": 0}
    ok, payload = assert_pr_allowed_for_arie(task)
    assert ok is True
    assert payload is None


def test_plan_task_supervisor_includes_agent_ids_key():
    plan = plan_task("crm cleanup", "work", company="ARIE Finance")
    assert "agent_ids" in plan
    assert plan["agent_ids"][-1] == "arie_compliance_expert"


def test_validate_outcome_rejects_bad_outcome():
    with pytest.raises(ValueError, match="outcome"):
        validate_compliance_review_outcome({"outcome": "maybe"})
