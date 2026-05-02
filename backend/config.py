"""
Central settings — loaded once at import, fails fast on missing required keys.
"""
import os
from dataclasses import dataclass, field


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass
class Settings:
    # Core
    anthropic_api_key: str   = field(default_factory=lambda: _require("ANTHROPIC_API_KEY"))
    github_token: str        = field(default_factory=lambda: _require("GH_PAT"))
    github_default_repo: str = field(default_factory=lambda: _optional("GITHUB_DEFAULT_REPO", "ismaelloveexcel/ai-coworker-workspace"))
    model: str               = field(default_factory=lambda: _optional("CLAUDE_MODEL", "claude-sonnet-4-5-20251101"))
    watchdog_model: str      = field(default_factory=lambda: _optional("WATCHDOG_MODEL", _optional("CLAUDE_MODEL", "claude-sonnet-4-5-20251101")))
    environment: str         = field(default_factory=lambda: _optional("ENV", _optional("APP_ENV", "development")).lower())

    # Model routing / council mode
    model_router_enabled: bool = field(default_factory=lambda: _optional("MODEL_ROUTER_ENABLED", "false").lower() == "true")
    council_mode_enabled: bool = field(default_factory=lambda: _optional("COUNCIL_MODE_ENABLED", "false").lower() == "true")
    coding_model: str      = field(default_factory=lambda: _optional("CODING_MODEL", ""))
    research_model: str    = field(default_factory=lambda: _optional("RESEARCH_MODEL", ""))
    critic_model: str      = field(default_factory=lambda: _optional("CRITIC_MODEL", ""))
    summarizer_model: str  = field(default_factory=lambda: _optional("SUMMARIZER_MODEL", ""))
    judge_model: str       = field(default_factory=lambda: _optional("JUDGE_MODEL", ""))

    # Database
    db_path: str             = field(default_factory=lambda: _optional("DB_PATH", "data/agent.db"))

    # API authentication (F3/E1)
    # Set API_KEY env var to require Bearer token on all mutating endpoints.
    # Leave empty only for localhost dev — NEVER empty in any networked deployment.
    api_key: str             = field(default_factory=lambda: _optional("API_KEY", ""))

    # Agent behaviour
    max_steps: int           = field(default_factory=lambda: int(_optional("MAX_STEPS", "25")))
    step_timeout_seconds: int = field(default_factory=lambda: int(_optional("STEP_TIMEOUT_SECONDS", "300")))
    max_concurrent_tasks: int = field(default_factory=lambda: int(_optional("MAX_CONCURRENT_TASKS", "1")))
    task_create_rate_limit_enabled: bool = field(default_factory=lambda: _optional("TASK_CREATE_RATE_LIMIT_ENABLED", "true").lower() == "true")
    task_create_rate_limit_count: int = field(default_factory=lambda: int(_optional("TASK_CREATE_RATE_LIMIT_COUNT", "6")))
    task_create_rate_limit_window_seconds: int = field(default_factory=lambda: int(_optional("TASK_CREATE_RATE_LIMIT_WINDOW_SECONDS", "60")))
    task_request_max_bytes: int = field(default_factory=lambda: int(_optional("TASK_REQUEST_MAX_BYTES", "65536")))
    zombie_reaper_interval_seconds: int = field(default_factory=lambda: int(_optional("ZOMBIE_REAPER_INTERVAL_SECONDS", "60")))

    # Playwright
    playwright_enabled: bool  = field(default_factory=lambda: _optional("PLAYWRIGHT_ENABLED", "false").lower() == "true")
    whitelisted_domains: list = field(default_factory=lambda: [
        d.strip() for d in _optional("WHITELISTED_DOMAINS", "github.com").split(",") if d.strip()
    ])

    # Cost caps
    max_task_usd: float = field(default_factory=lambda: float(_optional("MAX_TASK_USD", "5.00")))
    watchdog_max_usd: float = field(default_factory=lambda: float(_optional("WATCHDOG_MAX_USD", "2.00")))
    daily_max_usd: float = field(default_factory=lambda: float(_optional("DAILY_MAX_USD", "20.00")))

    # SQLite backup rotation
    backup_enabled: bool = field(default_factory=lambda: _optional("DB_BACKUP_ENABLED", "true").lower() == "true")
    backup_interval_seconds: int = field(default_factory=lambda: int(_optional("DB_BACKUP_INTERVAL_SECONDS", "86400")))
    backup_retention_days: int = field(default_factory=lambda: int(_optional("DB_BACKUP_RETENTION_DAYS", "14")))

    # Logging (F52)
    log_level: str  = field(default_factory=lambda: _optional("LOG_LEVEL", "INFO"))
    log_json: bool  = field(default_factory=lambda: _optional("LOG_JSON", "false").lower() == "true")

    def __post_init__(self) -> None:
        import re as _re
        import logging as _logging
        _DATE_PIN_RE = _re.compile(r"claude-.+-\d{8}")
        for field_name in ("model", "watchdog_model"):
            value = getattr(self, field_name)
            if not value or not value.startswith("claude-"):
                raise RuntimeError(f"{field_name} must be a Claude model name, got {value!r}")
            if not _DATE_PIN_RE.search(value):
                _logging.getLogger(__name__).warning(
                    "%s=%r has no date pin — it may break if Anthropic retires the alias. "
                    "Consider using claude-sonnet-4-5-20251101.",
                    field_name,
                    value,
                )


settings = Settings()
