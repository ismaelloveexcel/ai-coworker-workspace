"""Shared fixtures for the AI Coworker test suite."""
import os
import pytest

# Set dummy credentials BEFORE any backend imports
os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy"
os.environ["GH_PAT"] = "ghp-test-dummy"
os.environ["API_KEY"] = ""
os.environ["LOG_JSON"] = "false"


@pytest.fixture(autouse=True)
async def isolated_db(tmp_path, monkeypatch):
    """Give each test its own SQLite file so connections share schema."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_file)

    # Patch the already-instantiated settings object
    from backend import config, db as db_module
    monkeypatch.setattr(config.settings, "db_path", db_file)
    monkeypatch.setattr(db_module.settings, "db_path", db_file)

    # Fresh schema for each test
    await db_module.init_db()
    yield
    # tmp_path is cleaned up automatically by pytest
