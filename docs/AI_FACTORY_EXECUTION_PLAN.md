# AI Coworker Factory Execution Plan

## Mission

Transform the current private AI coworker tool into a supervised, artifact-native, multi-agent mini factory for a solo user. The system must support personal app monetisation work and professional automation work, while remaining safe, recoverable, observable, and hard for future agents to derail.

The build must happen in two major movements:

1. Stabilize and harden the current system.
2. Upgrade into the AI Coworker Factory.

Do not skip stabilization. The current tool already has a working backend, SSE dashboard, GitHub task loop, watchdog, tests, Docker setup, and secret/protected-path controls. Preserve those strengths while fixing the gaps identified in the production-readiness audit.

## Product North Star

AI Coworker Factory is a private local-first agent command center where the user can say things like:

- "I need a tool for marketing."
- "Create a partner onboarding workflow."
- "Build a monetisable MVP for this idea."
- "Research competitors and create the best feature blueprint."
- "Turn this SOP into an internal workflow app."

The Supervisor agent should classify the request, select the workspace, choose agents, route work through a recipe, produce artifacts, run validation, request approval for risky actions, and package the result.

The user should see a live animated mini factory: Supervisor, specialist agents, task cards, artifacts, approvals, budget, and final handoff.

## Non-Negotiable Guardrails

These apply to every phase and every PR.

1. Stabilization before expansion.
2. Small PRs with one purpose each.
3. No direct commits to main.
4. No unrelated refactors.
5. Preserve existing tests unless intentionally updating behavior.
6. Add or update tests for every backend behavior change.
7. Never expose secrets in logs, SSE events, DB records, PR bodies, or screenshots.
8. Do not give agents unrestricted shell, browser, or filesystem access.
9. Sensitive files require explicit approval or must be blocked:
   - `.env*`
   - `.github/workflows/**`
   - `Dockerfile`
   - `docker-compose.yml`
   - `nginx.conf`
   - `CLAUDE.md`
   - auth/security/config files
10. Personal and Work contexts must be isolated. No cross-workspace memory or artifact leakage.
11. If a task is too large, split into sub-issues or sub-PRs.
12. If an agent loses context, it must re-read this plan and the relevant source files before acting.
13. If market research is needed, delegate to research subagents or run web fetch/search tools, then synthesize into a research artifact.
14. If implementation requires broad codebase exploration, delegate read-only exploration to subagents.
15. If a change touches security, auth, task execution, agent permissions, or local-worker capabilities, request an extra review pass from a security/reviewer subagent.

## Agent Delegation Policy

Future VS Code agents should delegate when useful.

Use an Explore/read-only subagent for:

- mapping unfamiliar modules,
- finding all references to task status/events/auth,
- checking test coverage,
- researching frontend patterns in the repo,
- validating whether a planned edit conflicts with existing behavior.

Use specialist subagents if available:

- Security reviewer for auth, tokens, local worker, browser, policy engine, protected files.
- Frontend/product designer for command center UI and animation.
- Backend architect for orchestrator, recipes, artifacts, events, DB migrations.
- Test engineer for validation strategy and flaky/hanging tests.
- Research agent for market comparison and feature extraction.

Subagents should return concise findings with file references and recommended next steps. The main agent remains responsible for final integration.

## PR Strategy

This is too large for one PR. Split work into independent PRs. Recommended sequence:

1. PR 1: Security/auth and dependency stabilization.
2. PR 2: DB-backed task locking, rate limits, and restart recovery.
3. PR 3: Event bus/SSE subscriber model and observability improvements.
4. PR 4: Agent registry, Supervisor skeleton, recipe model, and artifacts schema.
5. PR 5: Animated command center UI using existing backend events or new factory events.
6. PR 6: Research tools and market-research recipe.
7. PR 7: Local worker policy layer for laptop actions.
8. PR 8: Model router and council mode provider abstraction.
9. PR 9: Personal/Work workspace separation and memory boundaries.
10. PR 10: Artifact Studio, recipe library, and polish.

Each PR must include:

- clear scope,
- tests or stated reason tests cannot run,
- updated docs or `.env.example` if config changes,
- no unrelated formatting churn,
- summary of behavior changed,
- rollback notes if operationally risky.

## Phase 0: Baseline Verification

Goal: establish known current state before editing.

Tasks:

1. Run Python tests.
2. Run py_compile.
3. Run pip-audit.
4. Read current backend, frontend, watchdog, workflows, Docker/nginx, tests, and CLAUDE.md before making claims.
5. Capture known failures.

Commands:

```powershell
& ".venv/Scripts/python.exe" -m py_compile backend/*.py watchdog.py
& ".venv/Scripts/python.exe" -m pytest tests/ -q
& ".venv/Scripts/python.exe" -m pip_audit -r requirements.txt --format=json --strict
```

Known current audit result:

- `pip-audit` found `pytest 8.4.2` vulnerable to CVE-2025-71176, fixed in `9.0.3`.
- Current `requirements.txt` has `pytest>=8.3.0,<9.0.0`, which prevents the fixed version.

Done criteria:

- Current failures documented.
- No code changes yet.

## Phase 1: Stabilize Current System

### 1.1 Fix API Token Handling

Problem:

`backend.main.require_auth()` accepts `?token=` on all protected endpoints, even though query-token auth is only needed for SSE.

Files:

- `backend/main.py`
- `frontend/index.html`
- `tests/test_auth_read_endpoints.py`

Required fix:

- Allow query token only for `GET /tasks/{task_id}/stream`.
- Require Bearer header everywhere else.
- Prefer short-lived SSE ticket endpoint later; do not implement full ticket system in this first stabilization PR unless scope remains small.

Tests:

- GET `/tasks?token=valid` should return 401 when API_KEY is set.
- POST `/tasks?token=valid` should return 401 when API_KEY is set.
- GET `/tasks/{id}/stream?token=valid` should still work.
- Bearer token should still work for all protected endpoints.

Done criteria:

- Query-param token bypass eliminated outside SSE.
- Existing read endpoint auth tests updated.

### 1.2 Fix Vulnerable Dev Dependency Path

Problem:

`pytest` constraint blocks fixed version for CVE-2025-71176.

Files:

- `requirements.txt`
- possibly CI if dependency groups are split later

Required fix:

- For quickest stabilization, set `pytest>=9.0.3,<10.0.0` if compatible.
- If compatibility breaks, split dev requirements into `requirements-dev.txt` and keep runtime requirements separate.

Validation:

- `pytest tests/ -q`
- `pip-audit -r requirements.txt --strict`

Done criteria:

- pip-audit no longer reports the pytest vulnerability, or the dev dependency is isolated and documented.

### 1.3 Add Rate Limits And Request Size Limits

Problem:

Authenticated or leaked-key callers can spam task creation and exhaust spend, DB, disk, and workers.

Files:

- `nginx.conf`
- `backend/main.py`
- `backend/config.py`
- tests for task creation rate logic if app-side limiter is added

Required fix:

- Add nginx `limit_req_zone`, `limit_req`, and `client_max_body_size`.
- Add minimal app-side task creation throttle for private use. Suggested: in-memory per-IP/key token bucket as a first step, DB-backed later.
- Ensure `/health` remains unaffected.

Suggested nginx snippet:

```nginx
limit_req_zone $binary_remote_addr zone=api_tasks:10m rate=6r/m;
client_max_body_size 64k;

location /api/tasks {
    limit_req zone=api_tasks burst=5 nodelay;
    proxy_pass http://backend:8000/tasks;
}
```

Adjust to existing proxy layout carefully.

Done criteria:

- Task creation has basic abuse protection.
- Config documented in `.env.example` or README if app-side settings are added.

### 1.4 Make Protected Paths Non-Bypassable For Normal Agents

Problem:

`github_commit_files()` accepts `allow_infra_edits=true`, which Claude can provide.

Files:

- `backend/tool_adapters.py`
- `tests/test_operator_tools.py`
- `CLAUDE.md` if contract needs updating

Required fix:

- Normal agent commits must always reject protected paths.
- If infra edits are ever needed, implement a separate explicit admin-approved path later.
- Do not expose `allow_infra_edits` to model-controlled inputs.

Tests:

- Protected path rejected even when `allow_infra_edits=true` in tool input, unless using a clearly separate internal-only helper not reachable from `execute_tool()`.

Done criteria:

- Agent cannot modify infra/protected paths by setting a flag.

### 1.5 Improve Secret Scanning At Commit Boundary

Problem:

Commit boundary uses `_secret_findings(..., include_generic_entropy=False)`.

Files:

- `backend/tool_adapters.py`
- `tests/test_operator_tools.py`

Required fix:

- Enable generic high-entropy checks at commit boundary.
- Keep SHA/UUID allowlists.
- Add allowlist only for known test fixtures if false positives occur.

Tests:

- Standalone base64url/high-entropy token is blocked.
- UUID and git SHA are not blocked.

Done criteria:

- Secrets are blocked before GitHub writes.

### 1.6 Fix CI Timeouts And Mutable Action Tags

Problem:

CI lacks timeout; some workflows use mutable action tags.

Files:

- `.github/workflows/ci.yml`
- `.github/workflows/watchdog.yml`
- `.github/workflows/claude-agent.yml`
- `.github/workflows/maintenance.yml`

Required fix:

- Add `timeout-minutes` to CI job.
- Pin all GitHub Actions to commit SHAs.
- Keep permissions least-privilege.

Validation:

- `actionlint` if available.
- Existing `run_tests(suite="all")` includes actionlint.

Done criteria:

- CI cannot hang indefinitely.
- Actions are pinned consistently.

## Phase 2: Reliability And Recovery

### 2.1 DB-Backed Task Lock

Problem:

`MAX_CONCURRENT_TASKS` is enforced by in-memory `_running` without lock/DB lease.

Files:

- `backend/main.py`
- `backend/db.py`
- tests in `tests/test_api.py` or new `tests/test_task_lock.py`

Required fix:

- Add `asyncio.Lock` immediately around in-process creation path.
- Prefer DB-backed lease table for stronger correctness:
  - `task_leases(id, task_id, status, acquired_at)`
  - unique active lease or transactionally count running tasks.

Done criteria:

- Two simultaneous POSTs cannot both start when limit is 1.
- Test uses concurrent requests.

### 2.2 Restart Reconciliation

Problem:

Container restart/OOM abandons in-flight tasks until reaper marks failed.

Files:

- `backend/db.py`
- `backend/main.py`
- `backend/agent_loop.py`
- tests in `tests/test_reaper.py` or new test file

Required fix:

- Persist task branch in DB after branch creation.
- Persist phase/current_step/last_action where useful.
- On startup, reconcile tasks with status `running`:
  - if branch exists and PR exists, update task with PR/status;
  - if branch exists but no PR, mark failed with recovery info;
  - if no branch, mark failed with restart reason.

Done criteria:

- Restart produces clear task state and recovery detail.

### 2.3 Per-Subscriber SSE Bus

Problem:

Current queue is one queue per task, so multiple clients compete for events.

Files:

- `backend/events.py`
- `backend/main.py`
- `tests/test_events.py`
- `tests/test_auth_read_endpoints.py`

Required fix:

- Replace task queue with subscriber queues:
  - `subscribe(task_id) -> queue/subscription id`
  - `emit()` fans out to all subscribers
  - cleanup on disconnect
- Keep bounded queues and drop-oldest behavior.

Done criteria:

- Multiple subscribers each receive the same event.
- Disconnect removes subscriber.

### 2.4 Health, Metrics, And Operator Status

Files:

- `backend/main.py`
- `backend/db.py`
- `frontend/index.html`

Required fix:

- Add `/status` for UI:
  - DB health
  - task counts
  - running tasks
  - daily spend
  - backup status
  - automation paused flag
- Optional `/metrics` later with Prometheus.

Done criteria:

- UI can show meaningful private-operator status.

## Phase 3: Policy Engine And Approval Gates

Goal: before giving agents more power, create deterministic policy enforcement.

New files suggested:

- `backend/policy.py`
- `backend/approvals.py`
- `tests/test_policy.py`
- DB migrations in `backend/db.py`

Core concepts:

- `ToolRequest`
- `PolicyDecision`: allow, deny, ask_approval
- `ApprovalRequest`
- `ApprovalStatus`

Rules:

- Protected path write: ask/deny depending workspace and mode.
- `.env*`: deny read/write by default; redact if metadata needed.
- Package install: ask.
- Arbitrary command: ask.
- Browser logged-in session: ask.
- External web domain: allow for research unless workspace policy requires ask.
- Personal/work cross-access: deny.
- High estimated spend: ask.

Done criteria:

- All tool calls pass through policy before execution.
- Tests cover allow, deny, ask.
- Existing tools still work for safe operations.

## Phase 4: Supervisor, Agent Registry, Recipes, And Artifacts

This is the first upgrade phase after stabilization.

### 4.1 Agent Registry

New files suggested:

- `backend/agents/registry.py`
- `backend/agents/base.py`
- `backend/agents/builtin.py`
- `tests/test_agent_registry.py`

Built-in agents:

- Supervisor
- Researcher
- Strategist
- Architect
- Builder
- Tester
- Security Reviewer
- Critic
- Launch Manager
- CRM Specialist
- Marketing Automation Specialist
- Reporting Analyst
- SOP Digitiser
- Finance Support Agent
- Knowledge Librarian

Each agent definition should include:

- id
- display_name
- role
- expertise
- workspace_scope
- allowed_tools
- can_commit
- can_research_web
- can_create_artifacts
- default_model_policy
- approval_profile

Done criteria:

- API can list built-in agents.
- Agent definitions are serializable.

### 4.2 Workspace Model

Workspaces:

- Personal
- Work
- Project-specific later

Files:

- `backend/db.py`
- `backend/main.py`
- frontend

Required fields:

- task.workspace
- artifact.workspace
- memory.workspace later

Done criteria:

- New tasks can specify workspace.
- Existing tasks default safely.
- UI shows workspace switcher.

### 4.3 Recipes

New files suggested:

- `backend/recipes.py`
- `tests/test_recipes.py`

Starter recipes:

Personal:

- Build Monetisable App
- Research App Idea
- Create Landing Page
- Build MVP
- Create Launch Campaign
- Compare Competitors

Work:

- Lead Qualification Workflow
- CRM Cleanup/Enrichment
- Meeting Prep Brief
- Partner Review Pack
- SOP To Workflow
- Management Dashboard
- Internal Knowledge Assistant

General:

- Market Research
- Build Tool
- Review Code
- Fix Failure
- Create Report
- Create Deck

Done criteria:

- Supervisor can map a prompt to a recipe.
- Recipes declare stages, agents, tools, artifacts, approval gates, success criteria.

### 4.4 Artifact System

New DB concepts:

- artifacts
- artifact_versions
- artifact_links

Artifact types:

- app
- document
- slide_deck
- spreadsheet
- research_brief
- campaign_pack
- workflow
- code_diff
- dashboard
- design_mockup
- automation_blueprint
- knowledge_base

Required API:

- create artifact
- list artifacts by task/workspace
- get artifact
- add version

Done criteria:

- Agents can create artifacts.
- UI can display artifacts in task detail.

## Phase 5: Animated Factory Dashboard

Goal: replace static dashboard feel with a real command center while preserving usability.

Files:

- `frontend/index.html` initially
- If app migrates to Next/app later, use `app/page.js` and `app/dashboard/page.js` deliberately.

Design requirements:

- First screen is the usable command center, not a marketing page.
- Show Supervisor control tower.
- Show agent stations.
- Show active task card moving through stages.
- Show live activity timeline.
- Show workspace switcher.
- Show budget/status.
- Show approvals panel.
- Show artifacts drawer.
- Command bar always available.

Frontend guardrails:

- No giant ornamental hero at the expense of utility.
- Avoid one-color palette.
- Stable dimensions for cards/nodes/buttons.
- No overlapping text.
- Use icons where appropriate.
- Dynamic content must use safe rendering.

Done criteria:

- Existing create/list/stream task workflows still work.
- New factory UI can display agents, stages, events, and artifacts.
- Tested on desktop and mobile viewport if Playwright/browser available.

## Phase 6: Research And Market Intelligence

Goal: make market research a first-class capability.

New tools:

- `web_search`
- `fetch_url`
- `research_compare`
- `source_summarize`

If no search API is available yet, implement `fetch_url` first and design search provider abstraction.

Market research recipe should produce:

- competitor list
- positioning comparison
- feature matrix
- pricing notes
- strengths to borrow
- weaknesses to avoid
- differentiation strategy
- recommended MVP
- artifact: `research_brief`

Research swarm design:

- Supervisor splits research into independent subtopics.
- Each worker gets fresh context.
- Supervisor synthesizes.
- Web content is always untrusted.

Done criteria:

- User can request "research similar products" and receive a structured artifact.
- Sources are tracked.

## Phase 7: Local Worker For Laptop Co-Work

Goal: agents can work on the laptop safely.

Do not start with uncontrolled shell.

Local worker tools:

- list files
- read file
- apply patch
- git diff/status
- run allowlisted tests
- run dev server
- take screenshot/open preview later
- install dependency only with approval
- arbitrary command only with approval

Policy:

- workspace root restriction
- deny home directory by default
- deny `.env` reads/writes
- deny SSH/browser secrets
- approval for destructive operations
- all actions logged

Done criteria:

- Builder can safely work in repo.
- Tester can run tests.
- Sensitive operations create approval requests.

## Phase 8: Model Router And Council Mode

Goal: stop manual ChatGPT/Grok/Claude hopping.

Provider abstraction:

- Anthropic provider first.
- OpenAI-compatible provider later.
- xAI/Grok provider later.
- Local model provider optional.

Router should support:

- coding model
- research model
- critic model
- cheap summarizer
- judge model

Council mode output:

- model perspectives
- consensus
- disagreements
- recommended action
- confidence/risk

Done criteria:

- One interface can call multiple providers when configured.
- If only Anthropic is configured, council mode degrades gracefully.

## Phase 9: Browser Operator

Goal: browser automation for research and web workflows, inspired by Manus Browser Operator, but private and approval-gated.

Start later, after local worker and policy engine.

Capabilities:

- open page
- read page
- click/type with approval depending domain/session
- extract public information
- screenshot page
- fill forms only with explicit approval

Rules:

- Never read passwords/cookies directly.
- Logged-in session use requires approval.
- Show active domain and action transcript.
- Stop button must work.

Done criteria:

- Researcher can inspect public sites.
- Operator cannot silently act in logged-in sessions.

## Phase 10: Memory And Opportunity Radar

Memory scopes:

- user preference memory
- personal workspace memory
- work workspace memory
- project memory
- agent memory

Rules:

- No cross-workspace memory leakage.
- User can view/delete memory.
- Sensitive/client data retention configurable.

Opportunity radar:

Personal:

- app ideas
- competitor movement
- AI tool trends
- monetisation opportunities

Work:

- automation opportunities
- CRM/reporting pain points
- partner/client workflow ideas
- finance/marketing operations improvements

Done criteria:

- Weekly opportunity artifact can be generated manually first.
- Scheduled automation later.

## Validation Matrix

Run relevant checks per PR.

Backend/auth/db/tool changes:

```powershell
& ".venv/Scripts/python.exe" -m py_compile backend/*.py watchdog.py
& ".venv/Scripts/python.exe" -m pytest tests/ -q
```

Dependency changes:

```powershell
& ".venv/Scripts/python.exe" -m pip_audit -r requirements.txt --format=json --strict
```

Workflow changes:

```powershell
./actionlint .github/workflows/ci.yml .github/workflows/claude-agent.yml .github/workflows/watchdog.yml .github/workflows/maintenance.yml
```

Frontend changes:

- If static HTML, open locally or via nginx and verify manually/browser screenshot.
- If using app routes, run available frontend/typecheck/lint scripts if package setup exists.

Security-sensitive changes:

- Add explicit tests.
- Run secret scanning on changed file contents/diffs.
- Ask reviewer/security subagent.

## Derailment Prevention Checklist

Before each PR:

- What exact phase is this PR for?
- What files are in scope?
- What files are out of scope?
- What tests prove it?
- Does this change current task execution behavior?
- Does it affect secrets/auth/policy/protected paths?
- Does it need docs or env updates?
- Can it be split smaller?

During implementation:

- Re-read relevant files before editing.
- Use existing patterns where possible.
- Do not rename broad concepts unless required.
- Do not mix UI redesign with backend policy changes in one PR.
- Do not add model providers before the provider abstraction exists.
- Do not add browser automation before policy/approval exists.
- Do not add local shell access before local worker restrictions exist.

Before finalizing:

- Run tests.
- Summarize exact changes.
- Summarize residual risk.
- Mention any skipped tests and why.
- Ensure no unrelated diffs.

## Suggested First Agent Prompt

Use this prompt to hand off to another VS Code agent:

```text
You are working in the ai-coworker-workspace repo. Read docs/AI_FACTORY_EXECUTION_PLAN.md first and follow it strictly.

Start with Phase 0 and Phase 1 only. Do not begin factory upgrades yet.

Your first implementation PR should fix:
1. Query-param token auth so it only works for SSE stream endpoints.
2. The vulnerable pytest dependency constraint or split dev dependencies if needed.
3. Protected-path bypass via allow_infra_edits for normal agent commits.
4. Commit-boundary generic secret scanning.

Before editing, read:
- backend/main.py
- backend/tool_adapters.py
- requirements.txt
- tests/test_auth_read_endpoints.py
- tests/test_operator_tools.py
- CLAUDE.md

Add/update tests for each behavior. Run py_compile, pytest, and pip-audit. If a test or audit cannot run, report why.

Do not redesign UI, add new agents, add model providers, or touch browser/local-worker features in this PR.
```

## Suggested Market Research Agent Prompt

Use this when delegating market research:

```text
Research comparable products for a private AI mini-factory: Genspark Claw, Claude Code, Cursor Cloud Agents, Replit Agent, Manus Wide Research, Manus Browser Operator, Perplexity Computer, ChatGPT, Notion AI, Gamma, Canva AI, Zapier AI, Airtable AI.

Return:
- product positioning
- strongest features
- weaknesses/gaps
- what we should borrow
- what we should avoid
- implications for AI Coworker Factory

Focus on agent orchestration, artifacts, browser/computer use, coding workflow, research workflow, model routing, local/private use, and non-technical solo-user UX.
```

## Suggested Supervisor Product Spec Prompt

Use after stabilization:

```text
Design the Supervisor Agent for AI Coworker Factory.

It must:
- classify user intent,
- choose workspace Personal vs Work,
- select recipe,
- assign agents,
- choose tools and models,
- enforce policy,
- watch budget,
- request approvals,
- evaluate output quality,
- produce final plain-English handoff.

Return DB schema changes, API endpoints, agent registry changes, event types, tests, and a staged implementation plan.
```

## Final Target

The final product should combine:

- Genspark Claw-style AI employee and memory.
- Claude-style artifacts and deep coding.
- Claude Code/Cursor-style local repo work, branches, diffs, tests, PRs.
- Replit-style creative prototyping and visual progress.
- Manus-style wide research and browser operation.
- Perplexity-style source-backed research.
- A unique Supervisor-led factory for personal monetisation and professional automation work.

The result should feel like a private AI operations room: flexible, artifact-native, supervised, safe, creative, and powerful enough to turn vague goals into finished apps, workflows, reports, dashboards, campaigns, and PRs.
