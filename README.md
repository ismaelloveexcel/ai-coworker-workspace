# AI Coworker System

Fully autonomous coding agent platform powered by Claude.

## What it does
1. User submits a natural-language task via dashboard
2. Claude agent executes the task inside a GitHub repo
3. Agent writes code, commits, branches, and opens PRs autonomously
4. User sees real-time execution logs via SSE streaming

## Architecture
- **Backend**: FastAPI (async) + uvloop
- **Agent**: Anthropic Claude (claude-sonnet-4-20250514)
- **DB**: SQLite WAL mode (aiosqlite)
- **GitHub**: PyGithub (no MCP)
- **Streaming**: SSE via asyncio.Queue
- **Frontend**: Single-page HTML + Tailwind
- **Deploy**: Docker Compose (backend + nginx)

## Quick Start
```bash
cp .env.example .env
# Fill in .env with your credentials

python bootstrap_github.py   # Validate setup

docker compose up --build
# Backend: http://localhost:8000
# Frontend: http://localhost:3000
```

## GitHub Actions — Autonomous Agent
Push to `main` → GitHub Actions runs `claude-agent.yml` automatically.
The agent can also be triggered manually via `workflow_dispatch`.

## API
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /tasks | Submit a task |
| GET  | /tasks | List all tasks |
| GET  | /tasks/{id} | Task detail + steps + logs |
| GET  | /tasks/{id}/stream | SSE live stream |
| DELETE | /tasks/{id} | Cancel task |
| GET  | /health | Health check |
