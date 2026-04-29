#!/usr/bin/env python3
"""
Bootstrap script — fully standalone.
NO imports from backend/*. Only: os, sys, dotenv, PyGithub.
"""
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: python-dotenv not installed. Run: pip install python-dotenv PyGithub")
    sys.exit(1)

try:
    from github import Github, GithubException
except ImportError:
    print("ERROR: PyGithub not installed. Run: pip install PyGithub")
    sys.exit(1)

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
REPO_NAME = "ai-coworker-workspace"

# ── Validate ──────────────────────────────────────────────────────────────────
errors = []
if not GITHUB_TOKEN:
    errors.append("GITHUB_TOKEN is missing from .env")
if not GITHUB_OWNER:
    errors.append("GITHUB_OWNER is missing from .env")

if errors:
    for e in errors:
        print(f"ERROR: {e}")
    sys.exit(1)

print(f"✓ Credentials loaded (owner={GITHUB_OWNER})")

# ── GitHub Operations ─────────────────────────────────────────────────────────
try:
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    print(f"✓ Authenticated as: {user.login}")
except GithubException as e:
    print(f"ERROR: GitHub auth failed — {e}")
    sys.exit(1)

# Check / create repo
try:
    repo = g.get_repo(f"{GITHUB_OWNER}/{REPO_NAME}")
    print(f"✓ Repo exists: {repo.html_url}")
except GithubException:
    print(f"  Repo not found — creating {GITHUB_OWNER}/{REPO_NAME}...")
    try:
        repo = user.create_repo(
            name=REPO_NAME,
            description="AI Coworker System — autonomous coding agent",
            private=False,
            auto_init=True,
        )
        print(f"✓ Repo created: {repo.html_url}")
    except GithubException as e:
        print(f"ERROR: Could not create repo — {e}")
        sys.exit(1)

# Write env update
env_line = f"GITHUB_DEFAULT_REPO={GITHUB_OWNER}/{REPO_NAME}\n"
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        content = f.read()
    if "GITHUB_DEFAULT_REPO=" not in content:
        with open(env_path, "a") as f:
            f.write(env_line)
        print("✓ .env updated with GITHUB_DEFAULT_REPO")
    else:
        print("✓ GITHUB_DEFAULT_REPO already in .env")

print(f"\n✓ Repo ready: {repo.html_url}")
sys.exit(0)
