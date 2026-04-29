"""
Bootstrap script — sets GitHub repo secrets and creates the agent-task label.
Run once after cloning:  python bootstrap_github.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

GH_PAT            = os.environ.get("GH_PAT", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPO_NAME         = os.environ.get("GITHUB_DEFAULT_REPO", "")

if not GH_PAT:
    print("ERROR: GH_PAT not set in .env", file=sys.stderr)
    sys.exit(1)
if not ANTHROPIC_API_KEY:
    print("ERROR: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
    sys.exit(1)
if not REPO_NAME:
    REPO_NAME = input("Enter your GitHub repo (owner/repo): ").strip()
    if not REPO_NAME or "/" not in REPO_NAME:
        print("ERROR: invalid repo name", file=sys.stderr)
        sys.exit(1)

from github import Github, GithubException

g    = Github(GH_PAT)
repo = g.get_repo(REPO_NAME)

# Set secrets
secrets = {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY, "GH_PAT": GH_PAT}
for name, value in secrets.items():
    try:
        repo.create_secret(name, value)
        print(f"  Set secret: {name}")
    except Exception as e:
        print(f"  WARNING: could not set {name}: {e}")

# Create agent-task label
try:
    repo.create_label("agent-task", "0075ca", "Trigger the autonomous agent")
    print("  Created label: agent-task")
except GithubException as e:
    if e.status == 422:
        print("  Label agent-task already exists")
    else:
        print(f"  WARNING: could not create label: {e}")

try:
    repo.create_label("watchdog-escalation", "e4e669", "Watchdog needs human review")
    print("  Created label: watchdog-escalation")
except GithubException as e:
    if e.status == 422:
        print("  Label watchdog-escalation already exists")

print(f"\nBootstrap complete for {REPO_NAME}.")
print("Next: docker compose up --build")
