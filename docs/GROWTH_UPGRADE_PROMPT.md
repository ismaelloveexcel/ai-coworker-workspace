# Growth & Monetisation Upgrade Prompt

> Paste everything below the horizontal rule into a Cursor chat with the full repo open.

---

You are a senior product engineer, growth strategist, and full-stack developer. You are auditing the **AI Coworker Workspace** — an autonomous coding agent platform — with one specific goal:

**Upgrade it so the owner can use it to build and ship apps, attract clients, and generate up to $10,000/month in revenue from the Personal workspace.**

Do not give vague advice. Every recommendation must cite the specific file, line or function it applies to. Read each file before editing it. Do not break the 472 passing tests.

---

## WHAT THIS PRODUCT IS (read this before doing anything)

An operator-controlled platform where you submit a natural-language task in a browser UI → a FastAPI backend routes it to an AI agent loop (`backend/agent_loop.py`) → the agent calls Claude via the Anthropic API → it executes tools (GitHub branch creation, commits, PRs, file reads/writes) → and streams real-time progress back to the browser via SSE.

**Current stack:**
- Backend: Python 3.12, FastAPI, aiosqlite, Anthropic SDK, PyGithub. Port 8765.
- Frontend: Single-file vanilla HTML/CSS/JS — `frontend/index.html`. No build step.
- Design system: nocturnal-revival dark palette — `--bg:#0D0D1A`, `--amber:#F4B942`, `--teal:#7FDBDA`. Fonts: MedievalSharp (headings), IM Fell English (body).
- Auth: `API_KEY` env var via Bearer token.
- Tests: 472 passing (`python -m pytest tests/ -q`). Do not regress.

**Current workspaces:** Personal and Work (toggle in task intake panel). Workspace is sent as a parameter to the backend but currently has no distinct routing or persona.

---

## GOAL: $10,000/MONTH FROM THE PERSONAL WORKSPACE

The Personal workspace should become a **personal app factory** — a tool the owner uses to:
1. Build and launch micro-SaaS apps and tools (the agent writes the code, the owner ships it)
2. Take on freelance/contract work (the agent does the execution, the owner manages the client)
3. Create digital products (scripts, automations, templates, plugins) for sale
4. Potentially offer "AI-powered dev" as a productised service to clients

Work through the five upgrade areas below in order. Read all relevant files first.

---

## AREA 1 — PERSONAL WORKSPACE: PERSONA + PRODUCT BUILDER MODE

**Current state:** Personal and Work are just a label difference — same agent, same tools, no distinct behaviour.

**Goal:** Make the Personal workspace feel like a dedicated **product-building studio**:

1. **Product Projects** — add a concept of "Projects" to the Personal workspace. A project groups related tasks under a name (e.g. "SaaS Billing App", "Chrome Extension v1"). Store projects in a new `projects` table in `backend/db.py`. Add `project_id` as an optional FK on the `tasks` table. On the frontend, add a project selector above the task intake when workspace=personal. Allow creating a new project inline (name + one-line description).

2. **Personal agent persona** — when workspace=personal, inject a different system prompt prefix into `backend/agent_loop.py`. The personal persona should be:
   > "You are a senior full-stack engineer and product builder working for the owner of this platform. Your job is to build complete, shippable products — not prototypes. Prefer working code over explanations. Always create a GitHub branch, commit all changes, and open a PR. When a feature is complete, summarise what was built, what to test, and how to deploy it."
   Keep the Work persona as-is (professional, cautious).

3. **Revenue tracker** — add a `revenue_log` table to `db.py` with fields: `id`, `project_id`, `amount_usd`, `source` (e.g. "Gumroad", "client invoice", "stripe"), `note`, `created_at`. Add a `POST /revenue` and `GET /revenue` endpoint to `backend/main.py`. Add a "Revenue" section to the Personal workspace UI showing total revenue logged, broken down by source. Include a simple "Log revenue" form (amount + source + note).

4. **Deployment checklist** — when a task completes with a PR, show a "Ship it" checklist in the task detail panel (Personal workspace only): ☐ Tests passing ☐ PR reviewed ☐ Deployed ☐ Revenue logged. Each checkbox is clickable and persists in `localStorage`.

---

## AREA 2 — AGENT CAPABILITY UPGRADES

The agent currently creates branches, commits, and opens PRs. To be useful for building real products, it needs more tools:

1. **`web_search` tool** — add a `web_search` tool to `backend/tool_adapters.py` that calls the Brave Search API (env var `BRAVE_API_KEY`). The agent should use it to research libraries, check API docs, and find pricing. If `BRAVE_API_KEY` is not set, the tool should return a clear "not configured" message rather than failing silently. Add the tool to the tool registry in `backend/agents/builtin.py`.

2. **`run_shell` tool (sandboxed)** — add a `run_shell` tool that runs a command in a restricted subprocess (no network, no filesystem writes outside `/tmp`). Use Python `subprocess` with `timeout=30`, capture stdout/stderr. This lets the agent run `python script.py`, `npm test`, `curl localhost`, etc. Add a policy check in `backend/policy.py` that blocks any command containing `rm -rf`, `sudo`, `curl|wget` with pipe, etc.

3. **Task templates** — add 6 pre-built task templates the owner can click to pre-fill the prompt input (Personal workspace only):
   - "Build a REST API with FastAPI — [describe the endpoints]"
   - "Create a Chrome extension that [describe the feature]"
   - "Build a landing page for [product name] with [key features]"
   - "Write a Python script that automates [describe the task]"
   - "Set up a Stripe payment integration for [describe the product]"
   - "Debug and fix: [paste the error here]"
   Show these as clickable chips above the prompt textarea.

4. **Multi-step planning** — before executing a complex task, have the agent emit a `plan` SSE event with a numbered list of steps it will take. Display this plan in the task detail panel before execution starts, so the owner can see what's about to happen.

---

## AREA 3 — CLIENT & FREELANCE WORKFLOW (Work workspace)

The Work workspace should become a **client delivery machine**:

1. **Client profiles** — add a `clients` table: `id`, `name`, `github_org` (optional), `budget_usd`, `created_at`. Add CRUD endpoints to `backend/main.py`. On the frontend (Work workspace), add a "Client" dropdown above the task intake that sets the `client_id` on the task. Show client name in the task card.

2. **Deliverable export** — add a "Export deliverable" button on completed tasks (Work workspace). Clicking it generates a simple HTML report: task title, description, PR link, steps taken, files changed, time spent, cost. The report should be downloadable as a `.html` file (no server call — generate client-side from task data).

3. **Time & cost estimate** — before a task is submitted, add a "Estimate" button that calls a new `POST /tasks/estimate` endpoint. The endpoint uses the prompt text and a simple heuristic (word count × 0.4 = estimated minutes, $0.05/min = cost estimate) to return a rough estimate. Display it inline below the prompt box.

---

## AREA 4 — GROWTH & DISTRIBUTION

These changes make the product shareable and attract users/clients:

1. **Public task preview** — add a `public` boolean field to tasks. When `public=true`, add a `GET /tasks/{id}/public` endpoint that returns the task title, status, PR link, and step count — but no logs or prompt content. Add a "Share" button to completed tasks that copies a link like `https://yourapp.com/task/{id}` to the clipboard. This lets the owner showcase what the agent built.

2. **Showcase page** — create `frontend/showcase.html` — a standalone page (no auth required) that displays completed public tasks as a portfolio grid. Each card shows: task title, status badge, PR link, build time, cost. This page is what the owner sends to potential clients as social proof.

3. **Webhook on task complete** — add a `WEBHOOK_URL` env var. When a task reaches `done` or `failed`, `POST` to the webhook URL with the task summary JSON. This enables Zapier/Make integrations for: notifying Slack, logging to Notion, triggering invoices, etc.

4. **Usage analytics** — add a `GET /analytics` endpoint that returns: tasks per day (last 30 days), average cost per task, total PRs opened, success rate, most-used tools. Display this on a new "Analytics" tab in the Operator Studio. This is the data the owner uses to pitch their service to clients ("I shipped 47 features last month at $0.40 each").

---

## AREA 5 — QUALITY OF LIFE

1. **Dark/dim mode toggle** — add a CSS variable switcher that offers three modes: Nocturnal (current default), Dim (slightly lighter for daytime use), and High Contrast. Persist in `localStorage`. Add a toggle button in the topbar.

2. **Task notes** — add a `notes` text field to tasks. In the task detail panel, show a small editable textarea labelled "Owner notes" that auto-saves to `PATCH /tasks/{id}` on blur. Use this to record client context, deployment notes, revenue logged.

3. **Pinned tasks** — add a `pinned` boolean to tasks. Pinned tasks always appear at the top of the queue regardless of creation date. Add a pin button (📌) to each task card.

4. **Agent activity feed** — add a global activity feed panel (collapsible, bottom of page) that shows the last 20 events across all tasks: tool calls, completions, errors. This gives the owner a live pulse of what the agents are doing without selecting individual tasks.

---

## CONSTRAINTS

- Do not break any of the 472 passing tests. Run `python -m pytest tests/ -q` after each area.
- Do not add npm/build tooling. All frontend changes stay in `frontend/index.html` or new standalone `.html` files.
- Do not add external image dependencies. All graphics must be inline SVG or CSS-only.
- All new DB tables must use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.
- All new endpoints must require Bearer auth on mutating methods.
- Commit each area as a separate commit: `growth: Area N — <title>`.
- Do not push to `main`. Open a PR from branch `growth/personal-upgrade`.

---

## OUTPUT FORMAT

After each area, output:

```
## AREA <N> — <title>
Status: COMPLETE | PARTIAL | GAPS_FOUND

### Changes made
- <file:line> — <what changed and why>

### Gaps / deferred
- <item + estimated effort>
```

Then a final **UPGRADE SUMMARY**:
- Features shipped
- Estimated monthly revenue potential per feature
- Top 3 next actions toward $10k/month
- Any blockers that need the owner's input (API keys, accounts, decisions)

---

## START

Read `frontend/index.html`, `backend/agent_loop.py`, `backend/tool_adapters.py`, `backend/db.py`, and `backend/main.py` in full before starting. Then begin Area 1. Do not skip sections.
