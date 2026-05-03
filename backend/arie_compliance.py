"""ARIE Finance compliance routing and finalization gates (workspace=work).

Mauritius-facing regulatory lenses are encoded in agent personas; this module
enforces routing and *code-level* finalization gates so PRs / terminal success
cannot bypass recorded compliance approval for gated companies.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Company string accepted as ARIE Finance (case-insensitive, flexible spacing)
_ARIE_RE = re.compile(r"^\s*arie\s+finance\s*$", re.I)

# Ordered agent ids appended after recipe defaults (deduped, stable order)
COMPLIANCE_EXPERT_ID = "arie_compliance_expert"
HR_EXPERT_ID = "arie_hr_expert"
ONBOARDING_EXPERT_ID = "arie_client_onboarding_expert"
MARKETING_EXPERT_ID = "arie_marketing_expert"
SALES_EXPERT_ID = "arie_sales_expert"
REPORTING_EXPERT_ID = "arie_reporting_expert"

READINESS_DRAFT = "draft"
READINESS_NEEDS_INFORMATION = "needs_information"
READINESS_COMPLIANCE_REVIEW_REQUIRED = "compliance_review_required"
READINESS_COMPLIANCE_ISSUES = "compliance_issues_found"
READINESS_COMPLIANCE_PASSED = "compliance_passed"
READINESS_READY_MANAGEMENT = "ready_for_management_review"
READINESS_READY_BUILD = "ready_for_build"
READINESS_READY_FINAL = "ready_for_final_approval"

COMPLIANCE_STATUSES = frozenset({
    READINESS_DRAFT,
    READINESS_NEEDS_INFORMATION,
    READINESS_COMPLIANCE_REVIEW_REQUIRED,
    READINESS_COMPLIANCE_ISSUES,
    READINESS_COMPLIANCE_PASSED,
    READINESS_READY_MANAGEMENT,
    READINESS_READY_BUILD,
    READINESS_READY_FINAL,
})


def normalize_company(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def is_arie_finance_company(company: Optional[str]) -> bool:
    c = normalize_company(company)
    if not c:
        return False
    return bool(_ARIE_RE.match(c))


def is_arie_compliance_gated(*, workspace: str, company: Optional[str]) -> bool:
    """True when Mauritius ARIE Finance compliance gate applies.

    ``workspace=work`` with no ``company`` set is treated as the default ARIE
    internal stream (gated). A non-ARIE explicit ``company`` value disables the
    gate so non-ARIE work experiments remain possible.
    """
    if workspace != "work":
        return False
    c = normalize_company(company)
    if c is None:
        return True
    return is_arie_finance_company(c)


def default_company_for_workspace(workspace: str) -> Optional[str]:
    """Default company label for new tasks (ARIE work stream)."""
    if workspace == "work":
        return "ARIE Finance"
    return None


def _kw(prompt: str, *words: str) -> bool:
    h = prompt.lower()
    return any(w in h for w in words)


def route_arie_expert_ids(prompt: str) -> List[str]:
    """Return additional ARIE specialist agent ids for this prompt (excludes compliance)."""
    out: List[str] = []

    # HR / people
    if _kw(
        prompt,
        "hr ", " hr", "human resources", "employee", "payroll", "hiring",
        "termination", "performance review", "disciplinar",
    ):
        out.append(HR_EXPERT_ID)

    # KYC / onboarding
    if _kw(
        prompt,
        "kyc", "onboarding", "cdd", "due diligence", "client onboarding",
        "sanction", "pep", "ubo", "source of funds", "source of wealth",
    ):
        out.append(ONBOARDING_EXPERT_ID)

    lead_gen = _kw(
        prompt,
        "lead gen", "leadgen", "lead generation", "lead qualification",
        "prospect", "pipeline", "cadence", "sequence",
    )

    # Marketing / content
    if _kw(
        prompt,
        "marketing", "newsletter", "linkedin", "blog", "campaign",
        "content calendar", "seo", "landing page copy", "brand",
    ) or lead_gen:
        out.append(MARKETING_EXPERT_ID)

    # Sales / leads (lead-gen paths also pull marketing per ARIE routing policy)
    if _kw(
        prompt,
        "sales", "lead", "crm", "introducer",
        "partner", "referral", "outbound",
    ) or lead_gen:
        out.append(SALES_EXPERT_ID)

    # Reporting / board
    if _kw(
        prompt,
        "report", "dashboard", "kpi", "board pack", "investor",
        "management summary", "mi ", "mis", "regulatory report",
    ):
        out.append(REPORTING_EXPERT_ID)

    # Dedupe preserving order
    seen = set()
    deduped: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            deduped.append(x)
    return deduped


def merge_compliance_path(recipe_agent_ids: List[str], prompt: str, workspace: str, company: Optional[str]) -> List[str]:
    """Supervisor-visible agent path including mandatory compliance expert for ARIE work."""
    if not is_arie_compliance_gated(workspace=workspace, company=company):
        return list(recipe_agent_ids)

    extras = route_arie_expert_ids(prompt)
    merged: List[str] = []
    seen = set()
    for aid in list(recipe_agent_ids) + extras + [COMPLIANCE_EXPERT_ID]:
        if aid in seen:
            continue
        seen.add(aid)
        merged.append(aid)
    # Compliance expert must appear before final handoff — keep near end but after specialists
    if COMPLIANCE_EXPERT_ID in merged:
        merged = [a for a in merged if a != COMPLIANCE_EXPERT_ID]
        merged.append(COMPLIANCE_EXPERT_ID)
    return merged


def compliance_pr_blocked_payload(*, issue: str, risk_level: str, why_it_matters: str, recommended_fix: str, human_review_required: bool) -> Dict[str, Any]:
    return {
        "issue": issue,
        "risk_level": risk_level,
        "why_it_matters": why_it_matters,
        "recommended_fix": recommended_fix,
        "human_compliance_or_legal_review_required": human_review_required,
    }


def parse_pending_finalize(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def compliance_passed_flag(task: Dict[str, Any]) -> bool:
    v = task.get("compliance_passed")
    try:
        return int(v or 0) == 1
    except (TypeError, ValueError):
        return False


def assert_pr_allowed_for_arie(task: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """If returns (False, dict), PR creation must not proceed; dict is user-facing block."""
    ws = str(task.get("workspace") or "personal")
    company = normalize_company(task.get("company"))
    if not is_arie_compliance_gated(workspace=ws, company=company):
        return True, None
    if compliance_passed_flag(task):
        return True, None
    payload = compliance_pr_blocked_payload(
        issue="Compliance review has not been recorded as passed for this ARIE Finance work task.",
        risk_level="high",
        why_it_matters=(
            "Finance-sector outputs must not be finalised without an explicit compliance "
            "record covering Data Protection Act 2017, FIAMLA/AML-CFT, FSC expectations, "
            "CDD/KYC, sanctions, retention, audit trail, and outbound communications controls."
        ),
        recommended_fix=(
            "Submit a structured compliance review via POST /tasks/{id}/compliance-review. "
            "If issues were found, resolve them and re-submit with outcome passed only after "
            "human compliance or legal sign-off where required."
        ),
        human_review_required=True,
    )
    return False, payload


def assert_done_allowed_for_arie(task: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Terminal success (e.g. marking done) must not bypass compliance for gated tasks."""
    return assert_pr_allowed_for_arie(task)


def validate_compliance_review_outcome(body: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Validate POST /compliance-review body; returns (compliance_status, normalized_json)."""
    outcome = str(body.get("outcome") or "").strip().lower()
    if outcome not in {"passed", "issues_found", "needs_information"}:
        raise ValueError("outcome must be one of: passed, issues_found, needs_information")

    risk_level = str(body.get("risk_level") or "unspecified").strip() or "unspecified"
    issues = body.get("issues")
    if issues is not None and not isinstance(issues, list):
        raise ValueError("issues must be a list when provided")

    normalized_issues: List[Dict[str, Any]] = []
    if isinstance(issues, list):
        for item in issues:
            if not isinstance(item, dict):
                continue
            normalized_issues.append({
                "issue": str(item.get("issue") or ""),
                "risk_level": str(item.get("risk_level") or risk_level),
                "why_it_matters": str(item.get("why_it_matters") or ""),
                "recommended_fix": str(item.get("recommended_fix") or ""),
                "human_compliance_or_legal_review_required": bool(
                    item.get("human_compliance_or_legal_review_required", False)
                ),
            })

    summary = str(body.get("summary") or "").strip()
    reviewer = str(body.get("reviewer") or "").strip()

    blob = {
        "outcome": outcome,
        "risk_level": risk_level,
        "summary": summary,
        "reviewer": reviewer,
        "issues": normalized_issues,
        "categories_reviewed": body.get("categories_reviewed") if isinstance(body.get("categories_reviewed"), list) else [],
    }

    if outcome == "passed":
        status = READINESS_COMPLIANCE_PASSED
    elif outcome == "issues_found":
        status = READINESS_COMPLIANCE_ISSUES
    else:
        status = READINESS_NEEDS_INFORMATION

    return status, blob
