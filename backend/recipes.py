"""Recipe catalog and prompt-to-recipe matching for the Supervisor."""
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


VALID_WORKSPACES = {"personal", "work"}


@dataclass(frozen=True)
class RecipeDefinition:
    id: str
    display_name: str
    workspace_scope: List[str]
    description: str
    trigger_keywords: List[str]
    stages: List[Dict]
    default_agents: List[str]
    allowed_tools: List[str]
    artifact_types: List[str]
    approval_gates: List[str]
    success_criteria: List[str]

    def to_dict(self) -> Dict:
        return asdict(self)


RECIPES = [
    RecipeDefinition(
        id="build_monetisable_app",
        display_name="Build Monetisable App",
        workspace_scope=["personal"],
        description="Plan and build a small app with a clear path to revenue.",
        trigger_keywords=["monetise", "monetize", "app", "mvp", "saas", "sell"],
        default_agents=["supervisor", "strategist", "architect", "builder", "tester", "launch_manager"],
        allowed_tools=["repo_snapshot", "github_read_file", "github_commit_files", "run_tests"],
        artifact_types=["app", "code_diff", "campaign_pack"],
        approval_gates=["before_commit", "before_external_publish"],
        success_criteria=["implementation plan exists", "tests pass", "handoff explains launch path"],
        stages=[
            {"id": "strategy", "agent": "strategist", "artifact": "document"},
            {"id": "architecture", "agent": "architect", "artifact": "automation_blueprint"},
            {"id": "build", "agent": "builder", "artifact": "code_diff"},
            {"id": "validate", "agent": "tester", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="market_research",
        display_name="Market Research",
        workspace_scope=["personal", "work"],
        description="Research competitors, alternatives, and opportunity gaps.",
        trigger_keywords=["research", "competitor", "market", "similar products", "compare"],
        default_agents=["supervisor", "researcher", "strategist", "critic"],
        allowed_tools=["github_read_file", "playwright_browse"],
        artifact_types=["research_brief", "document"],
        approval_gates=["before_web_research"],
        success_criteria=["sources summarized", "gaps identified", "recommendations included"],
        stages=[
            {"id": "research", "agent": "researcher", "artifact": "research_brief"},
            {"id": "synthesis", "agent": "strategist", "artifact": "document"},
            {"id": "review", "agent": "critic", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="build_tool",
        display_name="Build Tool",
        workspace_scope=["personal", "work"],
        description="Create or modify a practical internal or personal software tool.",
        trigger_keywords=["build", "create", "tool", "dashboard", "workflow app", "automation"],
        default_agents=["supervisor", "architect", "builder", "tester"],
        allowed_tools=["repo_snapshot", "github_read_file", "github_commit_files", "run_tests"],
        artifact_types=["app", "code_diff", "dashboard", "workflow"],
        approval_gates=["before_commit"],
        success_criteria=["scoped implementation", "tests pass", "operator handoff complete"],
        stages=[
            {"id": "design", "agent": "architect", "artifact": "automation_blueprint"},
            {"id": "implement", "agent": "builder", "artifact": "code_diff"},
            {"id": "verify", "agent": "tester", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="crm_cleanup_enrichment",
        display_name="CRM Cleanup/Enrichment",
        workspace_scope=["work"],
        description="Plan a guarded CRM cleanup, enrichment, or routing workflow.",
        trigger_keywords=["crm", "lead", "enrich", "qualification", "pipeline"],
        default_agents=["supervisor", "crm_specialist", "reporting_analyst", "critic"],
        allowed_tools=["github_read_file"],
        artifact_types=["workflow", "spreadsheet", "automation_blueprint"],
        approval_gates=["before_customer_data_access", "before_external_write"],
        success_criteria=["workspace is work", "data risks identified", "workflow is reversible"],
        stages=[
            {"id": "map", "agent": "crm_specialist", "artifact": "workflow"},
            {"id": "metrics", "agent": "reporting_analyst", "artifact": "dashboard"},
            {"id": "review", "agent": "critic", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="sop_to_workflow",
        display_name="SOP To Workflow",
        workspace_scope=["work"],
        description="Turn a documented operating procedure into a workflow blueprint.",
        trigger_keywords=["sop", "procedure", "process", "workflow", "checklist"],
        default_agents=["supervisor", "sop_digitiser", "architect", "tester"],
        allowed_tools=["github_read_file"],
        artifact_types=["workflow", "automation_blueprint", "document"],
        approval_gates=["before_external_write"],
        success_criteria=["steps are explicit", "owners are identified", "failure paths included"],
        stages=[
            {"id": "extract", "agent": "sop_digitiser", "artifact": "document"},
            {"id": "workflow", "agent": "architect", "artifact": "workflow"},
            {"id": "validate", "agent": "tester", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="review_code",
        display_name="Review Code",
        workspace_scope=["personal", "work"],
        description="Review code changes for regressions, security risks, and missing tests.",
        trigger_keywords=["review", "bug", "failure", "security", "test"],
        default_agents=["supervisor", "tester", "security_reviewer", "critic"],
        allowed_tools=["repo_snapshot", "github_read_file", "run_tests", "secret_scan"],
        artifact_types=["document", "code_diff"],
        approval_gates=["before_commit_if_fixing"],
        success_criteria=["findings are prioritized", "tests or gaps reported", "secrets not exposed"],
        stages=[
            {"id": "inspect", "agent": "tester", "artifact": "document"},
            {"id": "security", "agent": "security_reviewer", "artifact": "document"},
            {"id": "synthesis", "agent": "critic", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="arie_tool_blueprint",
        display_name="ARIE Tool Blueprint",
        workspace_scope=["work"],
        description="Research-led internal tool blueprint for ARIE Finance with compliance gates.",
        trigger_keywords=["blueprint", "internal tool", "arie", "gap item", "bp-", "workflow blueprint", "tool blueprint"],
        default_agents=["supervisor", "architect", "strategist", "builder", "tester"],
        allowed_tools=["repo_snapshot", "github_read_file", "github_commit_files", "run_tests"],
        artifact_types=[
            "tool_opportunity_brief",
            "market_research_pack",
            "competitor_comparison",
            "workflow_blueprint",
            "compliance_review",
            "build_plan",
        ],
        approval_gates=["before_commit", "before_external_publish"],
        success_criteria=[
            "workspace is work",
            "arie_compliance_expert included on agent path",
            "compliance review artefact planned before finalisation",
            "research or explicit skip documented",
        ],
        stages=[
            {"id": "discover", "agent": "strategist", "artifact": "tool_opportunity_brief"},
            {"id": "blueprint", "agent": "architect", "artifact": "workflow_blueprint"},
            {"id": "build", "agent": "builder", "artifact": "code_diff"},
            {"id": "verify", "agent": "tester", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="arie_work_tool_prototype",
        display_name="ARIE Work Tool Prototype",
        workspace_scope=["work"],
        description="Prototype or working slice of an internal ARIE tool with validation.",
        trigger_keywords=["prototype", "mvp", "poc", "working tool", "spike"],
        default_agents=["supervisor", "architect", "builder", "tester"],
        allowed_tools=["repo_snapshot", "github_read_file", "github_commit_files", "run_tests"],
        artifact_types=[
            "workflow_blueprint",
            "build_plan",
            "compliance_review",
            "code_diff",
        ],
        approval_gates=["before_commit"],
        success_criteria=[
            "arie_compliance_expert included on agent path",
            "compliance review included before handoff",
            "tests or checks recorded",
        ],
        stages=[
            {"id": "shape", "agent": "architect", "artifact": "workflow_blueprint"},
            {"id": "implement", "agent": "builder", "artifact": "code_diff"},
            {"id": "verify", "agent": "tester", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="arie_market_research",
        display_name="ARIE Market Research",
        workspace_scope=["work"],
        description="Competitor and market research pack for regulated ARIE decisions.",
        trigger_keywords=["market research pack", "competitor comparison", "compare firms", "build vs buy", "peer firms"],
        default_agents=["supervisor", "researcher", "strategist", "critic"],
        allowed_tools=["github_read_file", "playwright_browse"],
        artifact_types=[
            "market_research_pack",
            "competitor_comparison",
            "tool_opportunity_brief",
            "compliance_review",
        ],
        approval_gates=["before_web_research"],
        success_criteria=[
            "sources summarized",
            "arie_compliance_expert included on agent path",
            "compliance review planned for external claims",
        ],
        stages=[
            {"id": "scan", "agent": "researcher", "artifact": "market_research_pack"},
            {"id": "compare", "agent": "strategist", "artifact": "competitor_comparison"},
            {"id": "review", "agent": "critic", "artifact": "document"},
        ],
    ),
    RecipeDefinition(
        id="build_business_agent",
        display_name="Build Business Agent",
        workspace_scope=["work"],
        description="Design a business agent (HR, support, knowledge) with charters and escalation paths.",
        trigger_keywords=["business agent", "hr agent", "support agent", "customer service agent", "knowledge agent"],
        default_agents=["supervisor", "architect", "strategist", "builder", "tester"],
        allowed_tools=["github_read_file", "github_commit_files", "run_tests"],
        artifact_types=[
            "agent_charter",
            "escalation_matrix",
            "workflow_blueprint",
            "compliance_review",
            "build_plan",
        ],
        approval_gates=["before_commit", "before_external_publish"],
        success_criteria=[
            "arie_compliance_expert included on agent path",
            "compliance review included",
            "escalation and human handoff documented",
        ],
        stages=[
            {"id": "charter", "agent": "strategist", "artifact": "agent_charter"},
            {"id": "design", "agent": "architect", "artifact": "workflow_blueprint"},
            {"id": "build", "agent": "builder", "artifact": "code_diff"},
            {"id": "verify", "agent": "tester", "artifact": "document"},
        ],
    ),
]


def normalize_workspace(workspace: Optional[str]) -> str:
    value = (workspace or "personal").strip().lower()
    if value not in VALID_WORKSPACES:
        raise ValueError("workspace must be one of: personal, work")
    return value


def list_recipes(workspace: Optional[str] = None) -> List[Dict]:
    normalized = normalize_workspace(workspace) if workspace else None
    recipes = RECIPES
    if normalized:
        recipes = [recipe for recipe in recipes if normalized in recipe.workspace_scope]
    return [recipe.to_dict() for recipe in recipes]


def get_recipe(recipe_id: str) -> Optional[RecipeDefinition]:
    return next((recipe for recipe in RECIPES if recipe.id == recipe_id), None)


def select_recipe(prompt: str, workspace: Optional[str] = None) -> RecipeDefinition:
    normalized = normalize_workspace(workspace)
    haystack = prompt.lower()
    candidates = [recipe for recipe in RECIPES if normalized in recipe.workspace_scope]
    scored = []
    for recipe in candidates:
        score = sum(1 for keyword in recipe.trigger_keywords if keyword in haystack)
        scored.append((score, recipe))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    fallback = get_recipe("build_tool") if normalized == "personal" else get_recipe("sop_to_workflow")
    if fallback is None:
        raise RuntimeError("fallback recipe is missing")
    return fallback
