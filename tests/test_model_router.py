"""Tests for deterministic model routing and council scaffolding."""
from types import SimpleNamespace
from unittest.mock import patch

from backend.claude_wrapper import run_agent_turn
from backend.model_router import build_council_plan, infer_task_profile, route_model


DEFAULT_MODEL = "claude-sonnet-4-5-20251101"
RESEARCH_MODEL = "claude-research-4-5-20251101"
CRITIC_MODEL = "claude-critic-4-5-20251101"
SUMMARY_MODEL = "claude-summary-4-5-20251101"


def _settings(**overrides):
    values = {
        "model": DEFAULT_MODEL,
        "model_router_enabled": False,
        "council_mode_enabled": False,
        "coding_model": "",
        "research_model": "",
        "critic_model": "",
        "summarizer_model": "",
        "judge_model": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_router_disabled_uses_default_model():
    route = route_model("research", settings_obj=_settings(model_router_enabled=False, research_model=RESEARCH_MODEL))

    assert route.model == DEFAULT_MODEL
    assert route.reason == "router disabled; using default model"


def test_router_uses_task_specific_model_when_enabled():
    route = route_model("research", settings_obj=_settings(model_router_enabled=True, research_model=RESEARCH_MODEL))

    assert route.model == RESEARCH_MODEL
    assert route.task_type == "research"


def test_router_routes_high_risk_to_critic_model():
    route = route_model(
        "coding",
        risk="high",
        settings_obj=_settings(model_router_enabled=True, coding_model="claude-code-4-5-20251101", critic_model=CRITIC_MODEL),
    )

    assert route.model == CRITIC_MODEL
    assert "high-risk" in route.reason


def test_router_routes_cheap_profile_to_summarizer_model():
    route = route_model(
        "coding",
        cost_profile="cheap",
        settings_obj=_settings(model_router_enabled=True, coding_model="claude-code-4-5-20251101", summarizer_model=SUMMARY_MODEL),
    )

    assert route.model == SUMMARY_MODEL
    assert "summarizer" in route.reason


def test_router_miss_falls_back_to_default_model():
    route = route_model("judge", settings_obj=_settings(model_router_enabled=True))

    assert route.model == DEFAULT_MODEL
    assert "router miss" in route.reason


def test_infer_task_profile_detects_research_and_risk():
    profile = infer_task_profile({"title": "Research competitors", "prompt": "Compare pricing and auth risks"})

    assert profile["task_type"] == "research"
    assert profile["risk"] == "high"


def test_council_mode_degrades_when_disabled():
    plan = build_council_plan("Review this change", settings_obj=_settings(council_mode_enabled=False))

    assert plan["enabled"] is False
    assert plan["degraded"] is True
    assert plan["recommended_action"] == "Run a normal agent turn."


def test_council_mode_returns_scaffold_when_enabled():
    plan = build_council_plan(
        "Review this change",
        perspectives=["coding", "critic"],
        settings_obj=_settings(model_router_enabled=True, council_mode_enabled=True, coding_model="claude-code-4-5-20251101", critic_model=CRITIC_MODEL),
    )

    assert plan["enabled"] is True
    assert len(plan["perspectives"]) == 2
    assert {route["task_type"] for route in plan["perspectives"]} == {"coding", "critic"}


class _Usage:
    input_tokens = 11
    output_tokens = 7


class _Content:
    text = """PLAN:
- Step 1: finish
ACTION: final_answer
TOOL: none
INPUT: {}
REASONING: Done."""


class _Response:
    usage = _Usage()
    content = [_Content()]


def test_run_agent_turn_uses_routed_model_without_changing_parser_flow():
    routed = SimpleNamespace(model=RESEARCH_MODEL, to_dict=lambda: {"model": RESEARCH_MODEL, "task_type": "research"})
    with (
        patch("backend.claude_wrapper.route_model", return_value=routed),
        patch("backend.claude_wrapper._create_message", return_value=_Response()) as create_message,
    ):
        raw, parsed, usage = run_agent_turn([{"role": "user", "content": "Research competitors"}], {"task_type": "research"})

    assert "ACTION: final_answer" in raw
    assert parsed["action"] == "final_answer"
    assert usage["model"] == RESEARCH_MODEL
    create_message.assert_called_once()
    assert create_message.call_args.kwargs["model"] == RESEARCH_MODEL
