# Cursor Full-Repo Audit Prompt

> Copy everything below the horizontal rule into a Cursor chat with the full repo open.

---

You are a senior full-stack engineer and product designer performing a thorough audit of the **AI Coworker Workspace** repo.
Your job is to: identify concrete gaps, recommend and implement fixes, and significantly raise the visual and UX quality of the product — especially `frontend/index.html`.

Do not give vague advice. Every recommendation must cite the specific file, line range, or function it applies to.
Do not hallucinate behaviour. Read each file before auditing it.

---

## CONTEXT

**What this is**: An autonomous coding agent platform. Users submit natural-language tasks in a browser UI. A FastAPI backend routes tasks to an agent loop (`backend/agent_loop.py`) that calls Claude, executes tools (GitHub branch creation, commits, PRs), and streams real-time updates to the frontend via SSE.

**Tech stack**:
- Backend: Python 3.12, FastAPI, asyncio, aiosqlite, structlog, Anthropic SDK, PyGithub
- Frontend: Single-file vanilla HTML/CSS/JS (`frontend/index.html`). No build step.
- Deployment: Docker + nginx, port 8765 dev

**Design system**: nocturnal-revival palette — `--bg:#0D0D1A`, `--amber:#F4B942`, `--teal:#7FDBDA`, `--border:#4A3F6B`. Fonts: MedievalSharp (headings), IM Fell English (body), Share Tech Mono (log).

**Current test count**: 472 passing (baseline on `main`). Do not regress this.

---

## AUDIT SCOPE — Work through all five areas in order

---

### AREA 1 — LANDING PAGE & VISUAL QUALITY (HIGHEST PRIORITY)

**Problem**: There is no landing/marketing page. The product goes straight to the operator dashboard. The agent sprites in the Tavern Floor are OS emoji rendered at 3.2rem — low resolution, OS-dependent rendering, no personality or consistency. The SVG background scene exists but is basic. Compared to products like Devin (devin.ai), GitHub Copilot Workspace, Cursor, and Linear — all of which have cinematic hero sections, crisp custom illustrations, and strong visual identity — this product looks like a prototype.

**Tasks**:

1. **Landing page** (`frontend/index.html` or a new `frontend/landing.html`):
   - Add a dedicated hero section shown before the operator dashboard (or as a separate route if you split files).
   - Hero must include: product name ("The Coworker Tavern"), one-line value proposition ("Deploy an AI coding team to your GitHub repo — from a single prompt"), a primary CTA button ("Enter the Tavern" → operator dashboard), and a feature highlight strip (3-4 items: "Autonomous PR creation", "Real-time agent stream", "Multi-agent orchestration", "Budget guardrails").
   - Style it to match the nocturnal-revival palette. Think: deep dark background, amber glows, teal accents, stone texture. Reference the visual quality of Linear's landing page (dark, polished, minimal) — not cartoon, not flat, but rich and confident.
   - The hero section should use a large SVG or CSS illustration — not emoji.

2. **Agent sprites** — replace OS emoji with purpose-built inline SVG characters:
   - Each of the four agents (Rex/Supervisor, Forge/Builder, Sage/Tester, Myra/Handoff) needs a distinct inline SVG sprite, ~64×80px viewBox, medieval-fantasy silhouette style matching the nocturnal-revival palette.
   - Rex: wizard/mage archetype. Amber robes, staff, pointed hat.
   - Forge: blacksmith/builder. Dark armour, crossed hammers, teal accent on the hammers.
   - Sage: scholar/tester. Long robes, book or scroll, teal glow.
   - Myra: elf/handoff courier. Lighter build, green cloak, amber highlight.
   - Sprites must: scale cleanly at 64–96px, respond to the existing `.idle / .active / .warning / .done` CSS state classes (use `currentColor` or CSS custom properties for glow colouring), and work at retina density.
   - Keep the existing `.char-sprite` container but replace the emoji text content with `<svg>` elements.

3. **Tavern scene background** — upgrade the SVG scene:
   - Current scene is a flat stone arch + mortar lines. It is functional but sparse.
   - Add: hanging lantern chains from the ceiling with a warm amber flame element, a second stone column on each side, at least one window cutout showing a starfield behind it, a wooden floor-plank texture strip at the bottom, and a teal rune glyph inlaid into the stone floor.
   - All additions must be inline SVG within the existing `<svg class="scene-bg">` element. No external images. Maintain the current viewBox `0 0 800 320`.

4. **Typography and hierarchy**:
   - The topbar brand title `The Coworker Tavern` is only 1rem–1.5rem. Increase to `clamp(1.2rem, 2.8vw, 1.8rem)` with stronger text-shadow glow.
   - Add a tagline below the brand subtitle that animates through 3 states: "Quests accepted 24/7" → "AI coworkers, no meetings" → "Ship while you sleep". Cycle every 3.5s with a CSS fade crossfade.

5. **Resolution / retina**:
   - All SVG elements must use vector units only — no pixel-rounded coordinates that blur at 2× DPI.
   - Verify CSS `font-size` values for panel titles are not below 0.8rem — anything smaller than 11px on retina looks muddy.
   - The `.char-sprite` `font-size: 3.2rem` emoji path renders at ~51px on 96dpi but at ~102px logical px on 192dpi — inconsistent across OS emoji fonts. The SVG replacement fixes this.

---

### AREA 2 — FRONTEND FEATURE GAPS

Read `frontend/index.html` in full before auditing. Identify and fix:

1. **Task filtering and search**: The quest queue has no filter or search. Add a small search/filter bar above `.queue` that filters visible tasks by title substring (client-side, no API call). Should debounce at 200ms.

2. **Empty-state illustrations**: When the quest queue is empty, the `.empty` div shows plain italic text. Replace with an SVG "dusty wanted poster" illustration (inline SVG, 160×100px, styled to match the tavern theme) and the text "No quests on the board. Dispatch your first adventurer above."

3. **Keyboard shortcut discoverability**: The `Ctrl+Enter` hint on the dispatch button is small and hidden. Add a `?` icon button in the topbar that opens a modal overlay listing all keyboard shortcuts. Keyboard shortcuts to document: `Ctrl+Enter` dispatch, `Escape` cancel selection, `R` refresh.

4. **Task detail panel**: When no task is selected, the right rail shows placeholder content but does not prompt the user to select a task from the queue. Add a styled "Select a quest from the board" empty state with a directional arrow SVG pointing left.

5. **Responsive layout at 1280px**: At exactly 1280px wide (common laptop), the three-column layout is cramped. Verify `.layout` grid behaves correctly and the middle column (Tavern Floor + quest queue) does not collapse below 420px. Fix if needed.

---

### AREA 3 — BACKEND GAPS

Read each backend file before auditing. Focus on:

1. **`backend/main.py` — missing `/metrics` UI integration**: The `/metrics` endpoint exists but the frontend does not poll it or display per-task p95 latency or failure rate. Add a small "Performance" section to the Operator Studio panel that fetches `/metrics` on page load and displays: total tasks, average duration, p95 duration, failure rate, and top failure category. Use a simple bar representation in CSS (no canvas/chart libraries).

2. **`backend/config.py` — runtime config gaps**: Check whether `DB_PATH`, `LOG_JSON`, and `ENVIRONMENT` are documented in README and have sensible defaults. If not, fix `config.py` defaults and update README.

3. **`backend/db.py` — missing index**: Check whether `tasks` table has an index on `status` and `created_at` for the queue query (`GET /tasks?status=...`). If not, add a migration in the `init_db` function that creates it with `CREATE INDEX IF NOT EXISTS`.

4. **`backend/agent_loop.py` — task timeout is hardcoded**: Find the hardcoded timeout value. Move it to `settings` in `config.py` with a `TASK_TIMEOUT_SECONDS` env var, default 300. Update `agent_loop.py` to read from `settings`.

5. **Error handling on `POST /tasks`**: Verify that if `run_task` raises an unhandled exception after the task row is created but before it transitions to `running`, the task row is updated to `failed` rather than left as `pending` forever. Fix if there is a gap.

---

### AREA 4 — SECURITY & HARDENING (review only — do not regress existing fixes)

These were addressed in the recent PR wave. Verify each is actually present in current `main`:

- [ ] SSE uses short-lived stream token, not raw API_KEY in `?token=`
- [ ] All mutating endpoints require Bearer auth
- [ ] Tool execution checks `stage_matrix` (B2) before dispatching
- [ ] Cancellation checkpoints block writes after cancel acknowledgement (B4)
- [ ] Prompt injection boundary markers present in context builder (D2)
- [ ] `DB_PATH=:memory:` is never used in production (check `settings.environment`)

If any item above is missing, add a `# SECURITY GAP` comment inline with the relevant code and raise it in the audit output. Do not silently skip.

---

### AREA 5 — COMPARISON WITH COMPETITORS AND UPGRADE RECOMMENDATIONS

Research the following products and compare their UI/UX and feature set against this codebase. Do NOT hallucinate features — only state what you can observe from their public documentation and product pages, and compare to what exists in this codebase.

Products to compare:
- **Devin** (cognition.ai/devin) — autonomous coding agent
- **GitHub Copilot Workspace** — task-first coding environment
- **Cursor** (cursor.com) — AI-native editor
- **Linear** (linear.app) — issue tracking with excellent dark UI design
- **Replit Agent** (replit.com) — in-browser AI coding

For each, identify 1-2 features or design decisions this product is missing that would materially improve it.

Then produce a **Recommended Upgrade Backlog** table:

| Priority | Feature | Comparison product that does it well | Effort estimate | Notes |
|---|---|---|---|---|

Limit to 10 items max. Evidence-only — no speculative features.

---

## OUTPUT FORMAT

For each area, output:

```
## AREA <N> — <title>
Status: COMPLETE | PARTIAL | GAPS_FOUND

### Findings
- [FILE:LINE] <finding description>

### Changes made
- <file> — <what changed and why>

### Remaining gaps (not fixed in this pass)
- <item>
```

Then produce a final **AUDIT SUMMARY** with:
- Total gaps found
- Total fixes applied
- Remaining open items with estimated effort
- Top 3 recommended next actions

---

## CONSTRAINTS

- Do not break any of the 472 passing tests. Run `python -m pytest tests/ -q` before finishing.
- Do not add npm/build tooling. All frontend changes must stay in `frontend/index.html` or new standalone HTML files with no build step.
- Do not add external image dependencies. All new graphics must be inline SVG or CSS-only.
- Commit each area as a separate commit with message format: `audit: Area N — <title>`
- Do not push to `main` directly. Open a PR from branch `audit/full-repo-cursor`.

---

## START COMMAND

Begin with Area 1. Read `frontend/index.html` in full first. Then proceed through Areas 2–5 in order. Do not skip sections. Emit the output format after each area before moving to the next.
