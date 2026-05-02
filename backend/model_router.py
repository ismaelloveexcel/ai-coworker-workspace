"""Deterministic model routing and council-mode scaffolding."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List

from backend.config import settings


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    task_type: str
    risk: str
    cost_profile: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


_TASK_MODEL_FIELDS = {
    "coding": "coding_model",
    "research": "research_model",
    "critic": "critic_model",
    "summarizer": "summarizer_model",
    "judge": "judge_model",
}

_RISK_WORDS = {"auth", "token", "secret", "security", "schema", "migration", "deploy", "database", "payment"}
_RESEARCH_WORDS = {"research", "competitor", "market", "compare", "source", "pricing"}
_CRITIC_WORDS = {"review", "critic", "audit", "risk", "validate"}
_SUMMARY_WORDS = {"summarize", "summary", "brief", "recap"}
_JUDGE_WORDS = {"judge", "decide", "rank", "score", "select"}


def _normalize_choice(value: str, default: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized or default


def _configured_model(field_name: str, settings_obj: Any) -> str:
    return (getattr(settings_obj, field_name, "") or "").strip()


def route_model(task_type: str = "coding", risk: str = "normal", cost_profile: str = "standard", settings_obj: Any = None) -> ModelRoute:
    """Select a model deterministically, falling back to the default model."""
    settings_obj = settings_obj or settings
    selected_task_type = _normalize_choice(task_type, "coding")
    selected_risk = _normalize_choice(risk, "normal")
    selected_cost = _normalize_choice(cost_profile, "standard")
    default_model = settings_obj.model

    if not getattr(settings_obj, "model_router_enabled", False):
        return ModelRoute("anthropic", default_model, selected_task_type, selected_risk, selected_cost, "router disabled; using default model")

    if selected_risk == "high":
        configured = _configured_model("critic_model", settings_obj)
        if configured:
            return ModelRoute("anthropic", configured, selected_task_type, selected_risk, selected_cost, "high-risk task routed to critic model")

    if selected_cost == "cheap":
        configured = _configured_model("summarizer_model", settings_obj)
        if configured:
            return ModelRoute("anthropic", configured, selected_task_type, selected_risk, selected_cost, "cheap profile routed to summarizer model")

    field_name = _TASK_MODEL_FIELDS.get(selected_task_type, "coding_model")
    configured = _configured_model(field_name, settings_obj)
    if configured:
        return ModelRoute("anthropic", configured, selected_task_type, selected_risk, selected_cost, f"{selected_task_type} task routed by configured model")
    return ModelRoute("anthropic", default_model, selected_task_type, selected_risk, selected_cost, "router miss; using default model")


def infer_task_profile(task: Dict[str, Any]) -> Dict[str, str]:
    text = f"{task.get('title', '')} {task.get('prompt', '')}".lower()
    words = set(text.replace("/", " ").replace("-", " ").split())
    task_type = "coding"
    if words & _RESEARCH_WORDS:
        task_type = "research"
    elif words & _CRITIC_WORDS:
        task_type = "critic"
    elif words & _SUMMARY_WORDS:
        task_type = "summarizer"
    elif words & _JUDGE_WORDS:
        task_type = "judge"
    risk = "high" if words & _RISK_WORDS else "normal"
    cost_profile = "cheap" if task_type == "summarizer" else "standard"
    return {"task_type": task_type, "risk": risk, "cost_profile": cost_profile}


def council_mode_enabled(settings_obj: Any = None) -> bool:
    settings_obj = settings_obj or settings
    return bool(getattr(settings_obj, "council_mode_enabled", False))


def build_council_plan(prompt: str, perspectives: Iterable[str] | None = None, settings_obj: Any = None) -> Dict[str, Any]:
    """Return a deterministic council-mode scaffold without calling providers."""
    settings_obj = settings_obj or settings
    selected_perspectives = list(perspectives or ["coding", "critic", "judge"])
    routes: List[Dict[str, str]] = [
        route_model(task_type=perspective, risk="normal", cost_profile="standard", settings_obj=settings_obj).to_dict()
        for perspective in selected_perspectives
    ]
    if not council_mode_enabled(settings_obj):
        return {
            "enabled": False,
            "degraded": True,
            "prompt": prompt,
            "perspectives": routes[:1],
            "consensus": "Council mode is disabled; use the default routed model path.",
            "disagreements": [],
            "recommended_action": "Run a normal agent turn.",
            "confidence": "medium",
            "risk": "low",
        }
    return {
        "enabled": True,
        "degraded": len({route["model"] for route in routes}) <= 1,
        "prompt": prompt,
        "perspectives": routes,
        "consensus": "Council mode scaffold prepared; provider execution is not implemented in this slice.",
        "disagreements": [],
        "recommended_action": "Collect provider responses before taking action.",
        "confidence": "low",
        "risk": "medium",
    }
