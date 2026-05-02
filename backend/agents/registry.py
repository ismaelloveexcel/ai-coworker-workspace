"""Lookup helpers for built-in agent definitions."""
from typing import Dict, List, Optional

from backend.agents.base import AgentDefinition
from backend.agents.builtin import BUILTIN_AGENTS


_AGENTS_BY_ID: Dict[str, AgentDefinition] = {agent.id: agent for agent in BUILTIN_AGENTS}


def list_agents(workspace: Optional[str] = None) -> List[Dict]:
    agents = BUILTIN_AGENTS
    if workspace:
        agents = [agent for agent in agents if workspace in agent.workspace_scope]
    return [agent.to_dict() for agent in agents]


def get_agent(agent_id: str) -> Optional[AgentDefinition]:
    return _AGENTS_BY_ID.get(agent_id)


def require_agents(agent_ids: List[str]) -> List[AgentDefinition]:
    missing = [agent_id for agent_id in agent_ids if agent_id not in _AGENTS_BY_ID]
    if missing:
        raise ValueError(f"Unknown agent ids: {missing}")
    return [_AGENTS_BY_ID[agent_id] for agent_id in agent_ids]
