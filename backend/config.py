"""Central typed Settings — loads .env once, fails fast on missing keys."""
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: _require("ANTHROPIC_API_KEY"))
    github_token: str = field(default_factory=lambda: _require("GITHUB_TOKEN"))
    github_owner: str = field(default_factory=lambda: _require("GITHUB_OWNER"))
    github_default_repo: str = field(default_factory=lambda: _require("GITHUB_DEFAULT_REPO"))

    max_steps_per_task: int = field(default_factory=lambda: int(_optional("MAX_STEPS_PER_TASK", "30")))
    # Per-step timeout for a single Claude turn. Default is generous (5 min)
    # because Claude Sonnet calls with large contexts can legitimately take
    # 60-180s. Set lower in CI/workflow if you want faster fail-fast behavior.
    step_timeout_seconds: int = field(default_factory=lambda: int(_optional("STEP_TIMEOUT_SECONDS", "300")))

    allowed_tools: List[str] = field(default_factory=lambda: _optional("ALLOWED_TOOLS", "github,filesystem").split(","))
    playwright_enabled: bool = field(default_factory=lambda: _optional("PLAYWRIGHT_ENABLED", "false").lower() == "true")
    whitelisted_domains: List[str] = field(default_factory=lambda: _optional("WHITELISTED_DOMAINS", "github.com").split(","))

    model: str = field(default_factory=lambda: _optional("ANTHROPIC_MODEL", "claude-sonnet-4-5"))
    db_path: str = "data/agent.db"


settings = Settings()
