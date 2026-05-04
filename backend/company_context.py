"""Deterministic ARIE Finance company context pack for Supervisor planning (no LLM)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

_ARIE_CONTEXT: Dict[str, Any] = {
    "company_id": "arie_finance",
    "company_name": "ARIE Finance",
    "industry": "regulated financial services / finance",
    "operating_context": [
        "AI & Automation Lead is building internal tools, workflows, business agents, dashboards, and automation support.",
        "Work is finance-sector sensitive and needs compliance review.",
        "User is non-technical and needs plain-English outputs.",
    ],
    "departments": [
        "HR & People Operations",
        "Client Onboarding & Lifecycle",
        "Compliance Documentation & SOPs",
        "Internal Systems & Reporting",
        "Sales, Marketing & Partner Management",
        "Digital & Brand",
        "Paperless Initiative",
        "Finance",
        "Operations",
        "Technology",
        "Executive / Management",
    ],
    "compliance_assumptions": [
        "Data Protection Act 2017",
        "FIAMLA / AML-CFT",
        "FSC expectations",
        "CDD/KYC",
        "sanctions screening",
        "record retention",
        "audit trail",
        "access control",
        "consent/notice wording",
        "client/prospect/employee PII",
        "misleading or unsupported marketing claims",
        "human approval before outbound communication",
        "vendor/outsourcing risk",
        "AI use on sensitive data",
    ],
    "known_gaps": [
        {"id": "GA-046", "title": "CRM System — Client & Prospect Tracking", "keywords": ("crm", "client tracking", "prospect", "ga-046")},
        {"id": "GA-047", "title": "Sales Pipeline Visibility & Reporting", "keywords": ("pipeline", "forecast", "sales visibility", "ga-047")},
        {"id": "GA-048", "title": "Lead Capture & Qualification Process", "keywords": ("lead", "lead gen", "qualification", "capture", "ga-048")},
        {"id": "GA-049", "title": "Partner / Introducer Agreement Templates", "keywords": ("partner", "introducer", "agreement template", "ga-049")},
        {"id": "GA-050", "title": "Partner Onboarding Process", "keywords": ("partner onboarding", "introducer onboarding", "ga-050")},
        {"id": "GA-051", "title": "Partner Performance & Commission Tracking", "keywords": ("commission", "partner performance", "ga-051")},
        {"id": "GA-052", "title": "Marketing Materials Library", "keywords": ("marketing materials", "brand library", "ga-052")},
        {"id": "GA-053", "title": "Social Media Strategy & Content Calendar", "keywords": ("social media", "linkedin", "content calendar", "ga-053")},
        {"id": "GA-054", "title": "Email Marketing Capability", "keywords": ("email marketing", "newsletter", "ga-054")},
        {"id": "GA-016", "title": "Client Onboarding SOP", "keywords": ("onboarding sop", "client onboarding", "ga-016")},
        {"id": "GA-018", "title": "KYC Document Requirements Checklist", "keywords": ("kyc", "kyc checklist", "documents", "ga-018")},
        {"id": "GA-022", "title": "Complaints Handling Policy & Log", "keywords": ("complaints", "complaint log", "ga-022")},
        {"id": "GA-038", "title": "Management Information Dashboard", "keywords": ("mi dashboard", "management information", "ga-038")},
        {"id": "GA-039", "title": "Board Reporting Pack", "keywords": ("board pack", "board reporting", "ga-039")},
        {"id": "GA-043", "title": "Internal Knowledge Base / SOP Library", "keywords": ("knowledge base", "sop library", "ga-043")},
        {"id": "GA-042", "title": "Document Management System", "keywords": ("document management", "dms", "ga-042")},
        {"id": "GA-026", "title": "Data Protection Policy", "keywords": ("data protection", "dpa", "privacy policy", "ga-026")},
        {"id": "GA-027", "title": "IT & Cybersecurity Policy", "keywords": ("cybersecurity", "it policy", "ga-027")},
        {"id": "GA-033", "title": "Incident Response Plan", "keywords": ("incident response", "security incident", "ga-033")},
        {"id": "GA-034", "title": "Sanctions Screening Policy", "keywords": ("sanctions", "screening policy", "ga-034")},
        {"id": "GA-037", "title": "Regulatory Reporting Calendar", "keywords": ("regulatory reporting", "reporting calendar", "ga-037")},
    ],
    "planned_builds": [
        {"id": "BP-001", "title": "Digital HR System", "keywords": ("hr system", "digital hr", "bp-001")},
        {"id": "BP-002", "title": "Employee Onboarding & Offboarding Automation", "keywords": ("employee onboarding", "offboarding", "bp-002")},
        {"id": "BP-003", "title": "Performance Review & Training Tracker", "keywords": ("performance review", "training tracker", "bp-003")},
        {"id": "BP-004", "title": "Digital Client Onboarding Flow", "keywords": ("client onboarding flow", "digital onboarding", "bp-004")},
        {"id": "BP-005", "title": "KYC Document Collection Portal", "keywords": ("kyc portal", "document collection", "cdd portal", "bp-005")},
        {"id": "BP-006", "title": "Client Welcome & Communication Automation", "keywords": ("welcome email", "client communication", "bp-006")},
        {"id": "BP-007", "title": "Periodic CDD Review Reminder System", "keywords": ("cdd review", "periodic review", "bp-007")},
        {"id": "BP-008", "title": "Complaints Tracking System", "keywords": ("complaints tracking", "bp-008")},
        {"id": "BP-009", "title": "Management Information Dashboard", "keywords": ("management dashboard", "mi dashboard", "bp-009")},
        {"id": "BP-010", "title": "Board Reporting Pack Generator", "keywords": ("board pack", "board reporting generator", "bp-010")},
        {"id": "BP-011", "title": "AI-Powered Internal Knowledge Base", "keywords": ("knowledge base", "internal kb", "bp-011")},
        {"id": "BP-012", "title": "Document Management System", "keywords": ("document management", "dms", "bp-012")},
        {"id": "BP-013", "title": "CRM System", "keywords": ("crm", "customer relationship", "bp-013")},
        {"id": "BP-014", "title": "Sales Pipeline Tracker & Forecasting", "keywords": ("sales pipeline", "forecasting", "bp-014")},
        {"id": "BP-015", "title": "Partner Portal — Referrals, Commissions & Performance", "keywords": ("partner portal", "referrals", "bp-015")},
        {"id": "BP-016", "title": "Lead Capture & Enrichment Automation", "keywords": ("lead capture", "lead generation", "enrichment", "lead gen", "bp-016")},
        {"id": "BP-017", "title": "Email Sequence Automation", "keywords": ("email sequence", "drip", "bp-017")},
        {"id": "BP-018", "title": "Website Redesign", "keywords": ("website", "redesign", "bp-018")},
        {"id": "BP-019", "title": "Social Media Content System & Calendar", "keywords": ("social media", "content calendar", "bp-019")},
        {"id": "BP-020", "title": "Email Marketing Setup & Branded Templates", "keywords": ("email marketing", "templates", "bp-020")},
        {"id": "BP-021", "title": "Thought Leadership Content Pipeline", "keywords": ("thought leadership", "content pipeline", "bp-021")},
        {"id": "BP-022", "title": "Marketing Deck & Materials System", "keywords": ("marketing deck", "materials system", "bp-022")},
        {"id": "BP-023", "title": "DocuSign — All Agreements & Signatures", "keywords": ("docusign", "e-signature", "bp-023")},
        {"id": "BP-024", "title": "Digital Filing & Document Management", "keywords": ("digital filing", "paperless filing", "bp-024")},
        {"id": "BP-025", "title": "Paperless Client Onboarding", "keywords": ("paperless onboarding", "bp-025")},
    ],
}

_DEPARTMENT_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("HR & People Operations", ("hr ", " human resources", "employee", "payroll", "leave", "staff", "hiring", "termination")),
    ("Client Onboarding & Lifecycle", ("kyc", "cdd", "onboarding", "client lifecycle", "ubo", "due diligence")),
    ("Compliance Documentation & SOPs", ("sop", "policy", "compliance doc", "procedure", "control")),
    ("Internal Systems & Reporting", ("dashboard", "reporting", "mi ", "mis", "internal system", "workflow", "board pack", "generator")),
    ("Sales, Marketing & Partner Management", ("sales", "crm", "lead", "marketing", "partner", "introducer", "pipeline")),
    ("Digital & Brand", ("website", "linkedin", "social media", "brand", "content calendar")),
    ("Paperless Initiative", ("paperless", "digital filing", "e-sign")),
    ("Finance", ("invoice", "ledger", "budget", "financial control")),
    ("Operations", ("operations", "process improvement", "service delivery")),
    ("Technology", ("api", "integration", "software", "prototype", "build", "automation", "portal")),
    ("Executive / Management", ("board", "executive", "strategy", "management decision")),
]

_WORKFLOW_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "build_business_agent",
        ("agent", "chatbot", "assistant", "customer service", "hr agent", "support agent", "virtual agent"),
    ),
]


def get_company_context(company_id: str) -> Dict[str, Any]:
    if company_id != "arie_finance":
        raise KeyError(f"Unknown company_id: {company_id}")
    return dict(_ARIE_CONTEXT)


def list_company_contexts() -> List[Dict[str, Any]]:
    return [{"company_id": _ARIE_CONTEXT["company_id"], "company_name": _ARIE_CONTEXT["company_name"]}]


def _score_item(query_lower: str, item: Dict[str, Any]) -> int:
    score = 0
    qid = item.get("id") or ""
    if qid.lower() in query_lower:
        score += 5
    for kw in item.get("keywords") or ():
        if kw in query_lower:
            score += 2
    title = (item.get("title") or "").lower()
    for word in re.findall(r"[a-z0-9]{3,}", title):
        if len(word) >= 4 and word in query_lower:
            score += 1
    return score


def find_related_items(company_id: str, query: str, limit: int = 8) -> Dict[str, Any]:
    ctx = get_company_context(company_id)
    q = (query or "").lower()
    gap_scores = [( _score_item(q, g), g) for g in ctx["known_gaps"]]
    build_scores = [( _score_item(q, b), b) for b in ctx["planned_builds"]]
    gap_scores.sort(key=lambda x: x[0], reverse=True)
    build_scores.sort(key=lambda x: x[0], reverse=True)
    best_gap = gap_scores[0][0] if gap_scores else 0
    best_build = build_scores[0][0] if build_scores else 0
    threshold = 3
    is_ad_hoc = best_gap < threshold and best_build < threshold
    related_gaps = [{"id": g["id"], "title": g["title"], "score": s} for s, g in gap_scores if s >= threshold][:limit]
    related_builds = [{"id": b["id"], "title": b["title"], "score": s} for s, b in build_scores if s >= threshold][:limit]
    suggested_category = None
    suggested_next_action = None
    if is_ad_hoc:
        cats = classify_departments(company_id, query)
        suggested_category = cats[0] if cats else "Technology"
        suggested_next_action = (
            "Capture the idea in plain English, pick an output (research pack, blueprint, or prototype), "
            "and let the Supervisor route compliance and experts."
        )
    return {
        "related_gaps": related_gaps,
        "related_builds": related_builds,
        "is_ad_hoc": is_ad_hoc,
        "suggested_category": suggested_category,
        "suggested_next_action": suggested_next_action,
    }


def classify_departments(company_id: str, query: str) -> List[str]:
    _ = get_company_context(company_id)
    q = (query or "").lower()
    hits: List[Tuple[int, str]] = []
    for name, kws in _DEPARTMENT_KEYWORDS:
        score = sum(1 for k in kws if k in q)
        if score:
            hits.append((score, name))
    hits.sort(key=lambda x: x[0], reverse=True)
    out = [name for _, name in hits]
    if not out and ("board" in q or "kpi" in q or "reporting pack" in q):
        out.append("Internal Systems & Reporting")
    return out[:5]


def infer_workflow_type(query: str, output_mode: str) -> str:
    q = (query or "").lower()
    om = (output_mode or "").lower()
    for wf, kws in _WORKFLOW_KEYWORDS:
        if any(k in q for k in kws):
            return wf
    if "business agent" in om:
        return "build_business_agent"
    if "research" in om or "research pack" in om:
        return "market_research"
    if "prototype" in om:
        return "prototype"
    if "working tool" in om or om == "working tool":
        return "working_tool"
    if "blueprint" in om:
        return "blueprint"
    if "business agent" in om:
        return "build_business_agent"
    if any(x in q for x in ("research", "competitor", "market", "compare firms", "build vs buy")):
        return "market_research"
    if any(x in q for x in ("prototype", "mvp", "poc")):
        return "prototype"
    return "blueprint"
