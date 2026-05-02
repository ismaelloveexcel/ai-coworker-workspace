import pytest

from backend.agents.registry import get_agent, list_agents, require_agents


def test_builtin_agents_are_serializable():
    agents = list_agents()

    assert any(agent["id"] == "supervisor" for agent in agents)
    assert all("display_name" in agent for agent in agents)
    assert all(isinstance(agent["allowed_tools"], list) for agent in agents)


def test_workspace_filter_hides_work_only_agents_from_personal():
    personal_agents = list_agents("personal")
    work_agents = list_agents("work")

    assert get_agent("crm_specialist").to_dict() in work_agents
    assert all(agent["id"] != "crm_specialist" for agent in personal_agents)


def test_require_agents_rejects_unknown_agent():
    with pytest.raises(ValueError, match="missing"):
        require_agents(["supervisor", "missing"])
