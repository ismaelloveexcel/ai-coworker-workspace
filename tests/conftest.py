"""Shared fixtures for the AI Coworker test suite."""
import os
import pytest

# Point all DB ops at a temp in-memory path during tests
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-dummy")
os.environ.setdefault("GH_PAT", "ghp-test-dummy")
os.environ.setdefault("API_KEY", "")
