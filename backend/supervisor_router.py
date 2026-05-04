"""Supervisor planning router: ARIE context, experts, research, structured prompt (deterministic)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from backend.arie_compliance import default_company_for_workspace, is_arie_compliance_gated, normalize_company
from backend.company_context import (
    classify_departments,
    find_related_items,
    get_company_context,
    infer_workflow_type,
)

COMPLIANCE_EXPERT = "arie_compliance_expert"
TECH_EXPERT = "arie_technology_expert"
SALES = "arie_sales_expert"
MARKETING = "arie_marketing_expert"
HR = "arie_hr_expert"
ONBOARDING = "arie_client_onboarding_expert"
REPORTING = "arie_reporting_expert"


def _dedupe_preserve(order: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in order:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _normalize_output_mode(output_mode: Optional[str]) -> str:
    s = (output_mode or "blueprint").strip()
    return s or "blueprint"


def _research_required(prompt: str, output_mode: str) -> bool:
    q = (prompt or "").lower()
    if "skip research" in q or "no market research" in q or "skip competitor" in q:
        return False
    om = output_mode.lower()
    if "tiny" in om or "internal note" in om or "internal fix" in q:
        return False
    return True


def _missing_questions(prompt: str, related: Dict[str, Any], workflow_type: str) -> List[str]:
    q = (prompt or "").lower()
    out: List[str] = []
    if any(k in q for k in ("lead", "crm", "pipeline", "sales")) and workflow_type != "build_business_agent":
        if not any(k in q for k in ("website", "linkedin", "introducer", "manual import", "import")):
            out.append("Which source should version 1 focus on: website, introducers, LinkedIn, manual import, or all?")
        out.append("Should this be a research pack, blueprint, prototype, or working tool first?")
        if "integration" not in q and "crm" in q:
            out.append("Should CRM integration happen now or after the blueprint is approved?")
    if workflow_type == "build_business_agent" and "channel" not in q and "email" not in q and "slack" not in q:
        out.append("Which channels should the agent support first (e.g. email, web chat, internal only)?")
    if related.get("is_ad_hoc") and len(out) < 2:
        out.append("What outcome or deadline matters most for this idea?")
    return out[:5]


def _suggested_recipe_id(workflow_type: str, output_mode: str, query: str) -> str:
    q = (query or "").lower()
    om = output_mode.lower()
    if any(x in q for x in ("sop", "procedure", "process", "checklist", "operating procedure")):
        return "sop_to_workflow"
    if any(x in q for x in ("crm", "pipeline", "lead enrich", "clean up crm")) and "sop" not in q:
        return "crm_cleanup_enrichment"
    if workflow_type == "build_business_agent":
        return "build_business_agent"
    if workflow_type == "market_research" or "research" in om:
        return "arie_market_research"
    if workflow_type == "prototype" or "prototype" in om:
        return "arie_work_tool_prototype"
    if workflow_type == "working_tool" or "working tool" in om:
        return "arie_work_tool_prototype"
    return "arie_tool_blueprint"


def _compliance_categories(ctx: Dict[str, Any]) -> List[str]:
    return list(ctx.get("compliance_assumptions") or [])[:12]


def _structured_prompt(
    *,
    ctx: Dict[str, Any],
    user_prompt: str,
    company: str,
    output_mode: str,
    workflow_type: str,
    related: Dict[str, Any],
    experts: List[str],
    compliance_required: bool,
    research_required: bool,
    missing_questions: List[str],
    departments: List[str],
) -> str:
    lines = [
        "ARIE SUPERVISOR INTERNAL BRIEF (plain-English)",
        f"Company: {ctx['company_name']}",
        f"User request: {user_prompt.strip()}",
        f"Requested output: {output_mode}",
        f"Workflow type: {workflow_type}",
        "",
        "Context summary:",
        "; ".join(ctx.get("operating_context") or []),
        "",
        "Related roadmap / gap items (highest relevance first):",
    ]
    for g in related.get("related_gaps") or []:
        lines.append(f"- Gap {g.get('id')}: {g.get('title')}")
    for b in related.get("related_builds") or []:
        lines.append(f"- Build {b.get('id')}: {b.get('title')}")
    if related.get("is_ad_hoc"):
        lines.append(f"- Ad hoc idea (no strong catalogue match). Suggested area: {related.get('suggested_category') or 'General'}.")
        if related.get("suggested_next_action"):
            lines.append(f"  Suggested next action: {related['suggested_next_action']}")
    lines.extend(
        [
            "",
            "Departments implicated:",
            ", ".join(departments) if departments else "(none strongly scored)",
            "",
            "Required experts (do not ask the operator to pick manually):",
            ", ".join(experts),
            "",
            f"Compliance gate: {'required' if compliance_required else 'not required for this company/workspace'}.",
            f"Market / competitor research: {'required before blueprint/build' if research_required else 'skipped per operator request'}.",
        ]
    )
    if missing_questions:
        lines.append("")
        lines.append("Execution-critical questions to clarify (only if not already answered):")
        for mq in missing_questions:
            lines.append(f"- {mq}")
    lines.append("")
    lines.append("Deliver in plain English for a non-technical operator; include compliance review before finalisation.")
    return "\n".join(lines)


def build_supervisor_plan(
    prompt: str,
    workspace: str = "work",
    company: Optional[str] = None,
    output_mode: str = "blueprint",
) -> Dict[str, Any]:
    ws = (workspace or "personal").strip().lower()
    comp = normalize_company(company)
    if ws == "work" and comp is None:
        comp = default_company_for_workspace("work")

    om = _normalize_output_mode(output_mode)
    user_prompt = prompt or ""
    gated = is_arie_compliance_gated(workspace=ws, company=comp)

    if not gated:
        wf = infer_workflow_type(user_prompt, om)
        return {
            "workspace": ws,
            "company": comp,
            "company_id": None,
            "user_prompt": user_prompt,
            "output_mode": om,
            "workflow_type": wf,
            "detected_departments": classify_departments("arie_finance", user_prompt) if ws == "work" else [],
            "required_experts": [],
            "related_gaps": [],
            "related_builds": [],
            "is_ad_hoc": False,
            "compliance_required": False,
            "compliance_categories": [],
            "research_required": _research_required(user_prompt, om),
            "missing_questions": [],
            "recommended_next_action": "Use the standard workspace recipe and agent path.",
            "structured_prompt": user_prompt,
            "suggested_recipe_id": "",
        }

    ctx = get_company_context("arie_finance")
    related = find_related_items("arie_finance", user_prompt, limit=8)
    departments = classify_departments("arie_finance", user_prompt)
    wf = infer_workflow_type(user_prompt, om)
    q = user_prompt.lower()

    experts: List[str] = []
    # Rule 7 business agent — one primary domain expert + compliance + technology
    if wf == "build_business_agent":
        if any(k in q for k in ("hr", "employee", "payroll", "leave", "staff", "human resources")):
            experts = [HR, COMPLIANCE_EXPERT, TECH_EXPERT]
        elif any(k in q for k in ("customer service", "support agent", "service agent")):
            experts = [ONBOARDING, MARKETING, COMPLIANCE_EXPERT, TECH_EXPERT]
        else:
            experts = [MARKETING, COMPLIANCE_EXPERT, TECH_EXPERT]
    # Rule 2 CRM / lead / pipeline
    elif any(k in q for k in ("crm", "lead gen", "lead generation", "pipeline", "sales pipeline", "forecast")):
        experts = [SALES, MARKETING, COMPLIANCE_EXPERT, TECH_EXPERT]
    # Rule 3 HR / staff
    elif any(k in q for k in ("hr ", " hr", "human resources", "employee", "payroll", "leave", "staff onboarding")):
        experts = [HR, COMPLIANCE_EXPERT, TECH_EXPERT]
    # Rule 4 KYC / onboarding docs
    elif any(k in q for k in ("kyc", "cdd", "client onboarding", "document collection", "ubo", "sanction")):
        experts = [ONBOARDING, COMPLIANCE_EXPERT, TECH_EXPERT]
    # Rule 5 marketing / content / site
    elif any(k in q for k in ("marketing", "linkedin", "newsletter", "content calendar", "website", "seo", "campaign")):
        experts = [MARKETING, COMPLIANCE_EXPERT]
        if any(k in q for k in ("build", "tool", "portal", "prototype", "automation", "dashboard")):
            experts.append(TECH_EXPERT)
    # Rule 6 reporting / dashboard / board
    elif any(k in q for k in ("dashboard", "kpi", "board pack", "reporting", "mi ", "mis", "management information")):
        experts = [REPORTING, COMPLIANCE_EXPERT]
        if any(k in q for k in ("build", "tool", "portal", "prototype", "automation", "generator")):
            experts.append(TECH_EXPERT)
    else:
        experts = [COMPLIANCE_EXPERT, TECH_EXPERT]

    # Rule 1: always compliance
    if COMPLIANCE_EXPERT not in experts:
        experts.append(COMPLIANCE_EXPERT)
    experts = _dedupe_preserve(experts)
    # Compliance last for handoff consistency
    experts = [e for e in experts if e != COMPLIANCE_EXPERT]
    experts.append(COMPLIANCE_EXPERT)

    research_required = _research_required(user_prompt, om)
    missing = _missing_questions(user_prompt, related, wf)
    suggested_recipe = _suggested_recipe_id(wf, om, user_prompt)

    structured = _structured_prompt(
        ctx=ctx,
        user_prompt=user_prompt,
        company=comp or "",
        output_mode=om,
        workflow_type=wf,
        related=related,
        experts=experts,
        compliance_required=True,
        research_required=research_required,
        missing_questions=missing,
        departments=departments,
    )

    rec_action = "Proceed with the suggested recipe and recorded experts; answer missing questions if any."
    if related.get("is_ad_hoc"):
        rec_action = related.get("suggested_next_action") or rec_action

    return {
        "workspace": ws,
        "company": comp,
        "company_id": "arie_finance",
        "user_prompt": user_prompt,
        "output_mode": om,
        "workflow_type": wf,
        "detected_departments": departments,
        "required_experts": experts,
        "related_gaps": related.get("related_gaps") or [],
        "related_builds": related.get("related_builds") or [],
        "is_ad_hoc": bool(related.get("is_ad_hoc")),
        "compliance_required": True,
        "compliance_categories": _compliance_categories(ctx),
        "research_required": research_required,
        "missing_questions": missing,
        "recommended_next_action": rec_action,
        "structured_prompt": structured,
        "suggested_recipe_id": suggested_recipe,
    }
