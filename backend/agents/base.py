"""Serializable agent definition models for the AI Coworker Factory."""
from dataclasses import asdict, dataclass
from typing import Dict, List


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    display_name: str
    role: str
    expertise: List[str]
    workspace_scope: List[str]
    allowed_tools: List[str]
    can_commit: bool
    can_research_web: bool
    can_create_artifacts: bool
    default_model_policy: str
    approval_profile: str

    def to_dict(self) -> Dict:
        return asdict(self)
