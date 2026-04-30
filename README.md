# AI Coworker Workspace

An autonomous coding agent platform powered by Claude. Users submit natural-language tasks; the agent executes them inside a GitHub repo — writing code, creating branches, committing, and opening PRs — with real-time SSE streaming and a self-healing Watchdog.

## Architecture

```
User (browser) ──POST /tasks──> FastAPI backend
                                    │
                              agent_loop.py (asyncio)
                                    │
                        claude_wrapper.py (Claude Sonnet)
                                    │
                        tool_adapters.py (PyGithub)
                                    │
                         GitHub repo (branch → PR)
```

**Watchdog**: monitors GitHub Actions for CI/agent failures, diagnoses via Claude, opens a fix PR on a `watchdog/fix-<run_id>` branch for human review.

## Quick Start

### 1. Prerequisites

- Python 3.12+
- Docker & Docker Compose
- GitHub Personal Access Token (repo scope)
- Anthropic API key

### 2. Clone and configure

```bash
git clone https://github.com/ismaelloveexcel/ai-coworker-workspace.git
cd ai-coworker-workspace
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY, GH_PAT, and API_KEY
```

### 3. Install Python deps (for bootstrap)

```bash
pip install python-dotenv PyGithub
python bootstrap_github.py   # sets repo secrets, creates agent-task label
```

### 4. Run with Docker Compose

```bash
docker compose up --build
```

Dashboard: http://localhost (nginx proxy)
API: http://localhost/api/health

### 5. Run locally (dev)

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

## GitHub Actions

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `CI` | push to main, PR | lint + syntax check |
| `Claude Autonomous Agent` | `workflow_dispatch`, issue labeled `agent-task` | Runs the agent against a task |
| `Watchdog Agent` | CI or Agent workflow fails/cancels | Diagnoses failure, opens fix PR |
| `Maintenance` | weekly (Monday 02:00 UTC) | pip-audit, env validation |

**Note:** The agent workflow is triggered manually (`workflow_dispatch`) or via the `agent-task` label — it does NOT run automatically on every push.

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `GH_PAT` | ✅ | GitHub Personal Access Token (repo scope) |
| `API_KEY` | ⚠️ | Bearer token for API auth — set in any networked deployment |
| `GITHUB_DEFAULT_REPO` | — | Default repo for agent tasks |
| `CLAUDE_MODEL` | — | Model name (default: `claude-sonnet-4-5`) |
| `MAX_STEPS` | — | Max agent steps per task (default: 25) |
| `WATCHDOG_DAILY_MAX` | — | Max watchdog invocations per UTC day (default: `10`) |

## Model

Default model: **`claude-sonnet-4-5`** (set via `CLAUDE_MODEL` env var).

## Security

- Set `API_KEY` in production — the API has no auth when this is empty
- `API_CORS_ORIGINS` defaults to localhost; set explicitly for any deployed frontend
- The Watchdog never writes code patches to `main` directly — all fixes go through a PR
- The Watchdog does write `.watchdog/state.json` to `main` to persist the daily invocation counter; CI is configured to ignore this path (`paths-ignore: .watchdog/**`) so the write does not trigger new CI runs or re-activate the watchdog loop

## Frontend

The deployed frontend is a single static page at `frontend/index.html`
(Tailwind via CDN + vanilla JS). It is served by the `nginx` service in
`docker-compose.yml` from `./frontend:/usr/share/nginx/html:ro` and requires
no build step.

## Development

```bash
# Run Python tests
pytest

# Syntax check
python -m py_compile backend/*.py watchdog.py

# Security audit
pip install pip-audit
pip-audit -r requirements.txt
```

## Self-Healing Watchdog

When a GitHub Actions workflow fails, the Watchdog:
1. Fetches the failure logs
2. Asks Claude to diagnose the root cause
3. Claude proposes patches; a second Claude pass reviews them
4. Static validation (syntax + name check)
5. Patches committed to `watchdog/fix-<run_id>` branch
6. PR opened for human review — CI runs on the branch to validate
7. Human merges if CI is green

The Watchdog never pushes to `main` automatically, with one exception: it reads and writes `.watchdog/state.json` on `main` to track the daily invocation count. This file contains only operational metadata (date + counter) and never carries code changes.

### Daily invocation ceiling

To prevent run-away invocations (e.g. a flapping CI that fires the watchdog hundreds of times a day), the watchdog enforces a per-UTC-day ceiling controlled by `WATCHDOG_DAILY_MAX` (default: 10). Persistent state is stored in `.watchdog/state.json` in the repository:

```json
{ "date": "2025-01-15", "invocations": 3 }
```

When the ceiling is reached for the current UTC day the watchdog prints a warning to stderr and exits 0 without opening a PR or creating an issue.
