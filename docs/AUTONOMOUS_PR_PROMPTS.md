# Autonomous PR Execution Playbook (Internal Tool)

## Purpose

This playbook gives:

1. A master orchestrator prompt to execute all roadmap PRs sequentially.
2. Ten PR-specific implementation prompts (PR 1 through PR 10).
3. A mandatory independent subagent verification gate after each PR before commit/push.

This project is a private internal tool for one operator. It is not a SaaS product.

---

## Master Orchestrator Prompt (Copy/Paste)

Use this prompt to run all PRs in sequence autonomously:

"""
You are executing the AI Coworker roadmap as sequential PRs in this repository.

Hard constraints:
- This is an internal private tool for one operator. Not SaaS.
- No tenant management, billing, public onboarding, customer orgs, or marketplace features.
- Do not skip stabilization assumptions.
- One PR at a time, one branch per PR, one focused purpose per PR.
- Never commit directly to main.
- No unrelated refactors or formatting churn.
- If uncertain, read actual files and tests before coding.
- If you cannot verify a claim from code/tests, do not claim it.

Execution order:
1) PR 1
2) PR 2
3) PR 3
4) PR 4
5) PR 5
6) PR 6
7) PR 7
8) PR 8
9) PR 9
10) PR 10

For each PR:
1. Read docs/AI_FACTORY_EXECUTION_PLAN.md and CLAUDE.md.
2. Read the PR prompt section from docs/AUTONOMOUS_PR_PROMPTS.md.
3. Create branch from latest origin/main:
   - git fetch origin
   - git switch -c task/<pr-slug> origin/main
4. Implement only the scope defined in that PR section.
5. Run required validations.
6. Run independent verification subagent gate (MANDATORY) before commit.
7. If independent gate passes and local tests pass:
   - git add <scoped files>
   - git commit -m "<commit message from PR section>"
   - git push -u origin task/<pr-slug>
8. Output PR URL and validation summary.

Independent verification gate (MANDATORY):
- Use the Explore subagent in thorough mode.
- Give it the exact PR scope, intended files, acceptance criteria, and tests.
- Ask for findings ordered by severity with file references.
- Do not commit if any high/medium finding exists.
- Fix findings, rerun tests, rerun gate.

Required local validation baseline for each PR:
- $pyFiles = Get-ChildItem backend -Filter *.py | ForEach-Object { $_.FullName }
- & ".venv/Scripts/python.exe" -m py_compile @pyFiles watchdog.py
- & ".venv/Scripts/python.exe" -m pytest tests/ -q
- & ".venv/Scripts/python.exe" -m pip_audit -r requirements.txt --format=json --strict

If a command fails:
- Diagnose and fix root cause.
- Re-run validations.
- Do not continue to next PR until current PR is clean.

If a PR is already implemented/merged:
- Verify by diff/log/tests.
- Mark as completed with evidence.
- Do not re-implement.
- Move to next PR.

Final output after each PR:
- Branch name
- Commit SHA
- Changed files
- Validation results
- Independent subagent result summary
- PR URL
"""

---

## Independent Subagent Verification Prompt Template (Copy/Paste)

Use this before every commit/push:

"""
Use Explore subagent (thorough).

Review the current branch for PR <N>: <PR title>.

Expected scope:
- <list exact behavior changes>

Expected files (allowlist):
- <files expected to change>

Acceptance criteria:
- <criteria list>

Validation already run:
- py_compile
- focused tests: <list>
- full tests
- pip_audit

Tasks for subagent:
1. Detect scope creep, regressions, broken assumptions, and missing tests.
2. Detect security/secret exposure risks.
3. Detect API contract mismatches.
4. Detect risky operational behavior.
5. Return findings ordered by severity with file references.
6. If no blockers, explicitly say "No blocking findings".

Output format:
- Findings (High/Medium/Low)
- Open questions
- Residual risk
"""

Commit/push gate:
- If High or Medium findings exist: do not commit.
- If only Low or no findings: proceed after fixes and rerun validations.

---

## PR 1 Prompt: Security/Auth and Dependency Stabilization

Status target:
- Completed or verify-complete.

Scope:
- API token auth hardening (query token only for SSE stream).
- Dependency vulnerability remediation for pytest path.
- Protected-path bypass prevention.
- Secret scanning hardening at commit boundary.

Likely files:
- backend/main.py
- backend/tool_adapters.py
- requirements.txt
- tests/test_auth_read_endpoints.py
- tests/test_operator_tools.py
- CLAUDE.md (only if needed for guardrail wording)

Out of scope:
- New product features, UI redesign, non-security refactors.

Acceptance criteria:
- Query token does not authorize non-SSE endpoints.
- Bearer token works for protected endpoints.
- Protected infra paths cannot be bypassed by agent input flags.
- Secret scanning catches generic high-entropy leaks where configured.
- pip_audit does not report known pytest vulnerability path.

Focused tests:
- & ".venv/Scripts/python.exe" -m pytest tests/test_auth_read_endpoints.py tests/test_operator_tools.py -q

Commit message:
- fix: harden auth and commit security gates

---

## PR 2 Prompt: DB Locking, Rate Limits, Restart Recovery

Status target:
- Completed or verify-complete.

Scope:
- Task creation lock/race protections.
- Rate limiting and request size guardrails.
- Restart recovery for interrupted running tasks.
- Persist task progress markers for recovery context.

Likely files:
- backend/main.py
- backend/db.py
- backend/agent_loop.py
- backend/config.py
- nginx.conf
- tests/test_db.py
- tests/test_reaper.py
- tests/test_agent_loop_gates.py
- tests/test_api.py

Out of scope:
- New orchestration framework.

Acceptance criteria:
- No duplicate task launch race.
- Guardrails enforce request limits.
- On restart, orphan running tasks are reconciled safely.
- Branch/step/action/tool recovery metadata is preserved.

Focused tests:
- & ".venv/Scripts/python.exe" -m pytest tests/test_db.py tests/test_reaper.py tests/test_agent_loop_gates.py tests/test_api.py -q

Commit message:
- fix: add task guardrails and restart reconciliation

---

## PR 3 Prompt: Event Bus/SSE Subscriber Model and Observability

Status target:
- Completed or verify-complete.

Scope:
- Per-subscriber bounded queues for SSE fan-out.
- Non-blocking emit behavior with bounded memory.
- Correct subscribe/unsubscribe cleanup.
- Multi-subscriber stream reliability.

Likely files:
- backend/events.py
- backend/main.py
- tests/test_events.py
- tests/test_api.py
- tests/test_auth_read_endpoints.py

Acceptance criteria:
- Two concurrent subscribers both receive emitted events.
- Disconnecting one subscriber does not affect another.
- Emit with no subscribers is safe no-op.
- Queue overflow does not block producer.

Focused tests:
- & ".venv/Scripts/python.exe" -m pytest tests/test_events.py tests/test_api.py tests/test_auth_read_endpoints.py -q

Commit message:
- fix: support multi-subscriber task event streams

---

## PR 4 Prompt: Agent Registry, Supervisor Skeleton, Recipe Model, Artifacts Schema

Scope:
- Add foundational orchestration primitives without full behavior migration.
- Introduce explicit agent registry and role metadata.
- Add supervisor planner skeleton that maps request to recipe steps.
- Define artifact schema and storage metadata model.
- Keep existing task loop operational; do not replace all runtime flow yet.

Likely files:
- backend/ (new modules likely: supervisor.py, registry.py, recipes.py, artifacts.py)
- backend/db.py (schema migration for artifacts/recipe tracking)
- backend/main.py (minimal endpoints for inspecting registry/recipes/artifacts)
- tests/ (new focused tests for schema and planner skeleton)

Out of scope:
- Full multi-agent execution engine.
- UI animation work.

Required implementation details:
- Agent registry model:
  - id, name, capability_tags, risk_level, enabled
- Recipe model:
  - id, name, intent_tags, ordered steps, required capabilities
- Artifact schema:
  - id, task_id, type, title, path_or_payload_ref, producer_agent, created_at, metadata_json
- Supervisor skeleton:
  - classify request intent
  - select recipe by intent
  - return execution plan object (without full execution yet)

Acceptance criteria:
- Registry/recipes/artifacts models exist and are tested.
- DB migration is backward-compatible.
- Existing task endpoints remain functional.

Focused tests:
- New tests for registry selection, recipe matching, artifact persistence.
- Existing API/task tests should remain green.

Commit message:
- feat: add supervisor registry and artifact schema foundations

---

## PR 5 Prompt: Animated Command Center UI (Using Existing/Factory Events)

Scope:
- Build command-center frontend for private operator.
- Show live agent/task/activity/artifact states.
- Keep mobile and desktop usable.
- Preserve existing backend API compatibility.

Likely files:
- frontend/index.html
- app/page.js or app/dashboard/page.js (if using existing app surface)
- scripts/ if build-time helpers are needed
- tests/smoke.spec.ts and related frontend smoke tests

Out of scope:
- Public marketing site and SaaS onboarding.

Required UI behaviors:
- Live event feed and task cards.
- Agent lane visualization (Supervisor + specialists).
- Artifact panel with status and links.
- Budget/spend and guardrail indicators.
- Explicit risk/approval banners for sensitive actions.

Design constraints:
- Internal command center, intentional visual language.
- Not generic SaaS dashboard look.
- Keep accessibility basics (contrast, keyboard navigation for core actions).

Acceptance criteria:
- UI reflects live task progress via SSE/events.
- Core operator information is visible without opening logs.
- Existing auth and API usage remain secure.

Focused tests:
- & ".venv/Scripts/python.exe" -m pytest tests/test_api.py -q
- frontend smoke tests in repo (Playwright if configured).

Commit message:
- feat: add internal command center UI for live supervision

---

## PR 6 Prompt: Research Tools and Market-Research Recipe

Scope:
- Add research recipe that can gather and synthesize competitor/product data.
- Integrate safe web fetch/search route through existing tool policy.
- Persist research artifacts (briefs, comparison matrices, recommendations).

Likely files:
- backend/tool_adapters.py
- backend/recipes.py
- backend/supervisor.py
- backend/artifacts.py
- tests/test_operator_tools.py and new research tests

Out of scope:
- Unrestricted browsing or autonomous credentialed actions.

Required behavior:
- Supervisor can select a research recipe from user intent.
- Research outputs become artifacts with provenance metadata.
- Prompts enforce citation of sources used.
- Tool usage remains bounded and policy-controlled.

Acceptance criteria:
- Research recipe path is test-covered.
- Artifact output includes source references.
- No secret leakage in stored research outputs.

Focused tests:
- Recipe-selection tests.
- Tool-policy tests for research calls.
- Artifact persistence tests.

Commit message:
- feat: add market research recipe and artifact outputs

---

## PR 7 Prompt: Local Worker Policy Layer for Laptop Actions

Scope:
- Introduce policy engine for local actions (filesystem, browser, shell classes).
- Add approval requirements for sensitive action categories.
- Enforce allowlist and denylist rules for local worker operations.

Likely files:
- backend/policy.py (new)
- backend/tool_adapters.py
- backend/main.py (approval/status endpoints if needed)
- tests for policy enforcement and approvals

Out of scope:
- Fully autonomous unrestricted local control.

Required behavior:
- Policy decision object: allow, deny, require_approval, reason.
- Sensitive categories default to require_approval.
- Policy decisions are logged/auditable.
- Denied actions never execute underlying tool.

Acceptance criteria:
- Clear enforcement path with test coverage.
- Existing safe operations continue working.
- No bypass through model-provided flags.

Focused tests:
- Policy allow/deny/approval matrix tests.
- Regression tests for protected path and secret safety.

Commit message:
- feat: add local worker policy and approval enforcement

---

## PR 8 Prompt: Model Router and Council Mode Provider Abstraction

Scope:
- Add provider/model abstraction for routing tasks by class.
- Add council mode orchestration scaffold for multi-model review.
- Keep current default model path fully functional.

Likely files:
- backend/claude_wrapper.py
- backend/model_router.py (new)
- backend/config.py
- backend/agent_loop.py
- tests for routing behavior

Out of scope:
- Massive multi-provider implementation if credentials are absent.

Required behavior:
- Route by task type/risk/cost profile.
- Fallback to default model on router miss.
- Council mode optional and explicitly enabled by config.

Acceptance criteria:
- No regression to existing single-model flow.
- Router logic test-covered and deterministic.

Focused tests:
- Routing unit tests.
- Agent loop regression tests.

Commit message:
- feat: add model routing and council mode scaffolding

---

## PR 9 Prompt: Personal/Work Workspace Separation and Memory Boundaries

Scope:
- Add explicit workspace partitioning for personal vs work contexts.
- Prevent cross-workspace memory/artifact leakage.
- Add workspace selection and persistence in task metadata.

Likely files:
- backend/db.py (workspace fields and queries)
- backend/main.py (workspace-aware endpoints)
- backend/supervisor.py
- backend/artifacts.py
- tests for isolation boundaries

Out of scope:
- Multi-user tenancy.

Required behavior:
- Every task assigned to workspace category.
- Artifacts and memory retrieval filtered by workspace.
- No implicit fallback that mixes contexts.

Acceptance criteria:
- Cross-workspace leakage tests pass.
- Existing single-operator flow remains simple.

Focused tests:
- Workspace boundary tests.
- API filtering tests.

Commit message:
- feat: enforce personal-work workspace isolation boundaries

---

## PR 10 Prompt: Artifact Studio, Recipe Library, and Product Polish

Scope:
- Add final internal-operator usability layer:
  - artifact browsing and filtering
  - recipe library management and previews
  - polished handoff summaries and run history
- Tighten docs and operational runbooks.

Likely files:
- frontend/
- app/
- backend endpoints supporting artifact/recipe UX
- docs and README
- smoke tests and API tests

Out of scope:
- SaaS monetization plumbing.

Required behavior:
- Operator can review outputs quickly without digging through logs.
- Recipes are reusable, visible, and editable within policy boundaries.
- Final handoff artifacts are clear and traceable.

Acceptance criteria:
- End-to-end operator workflow is smooth and test-covered.
- No regressions to existing task execution safety.

Focused tests:
- API artifact/recipe tests.
- frontend smoke path tests.

Commit message:
- feat: add artifact studio and recipe library polish

---

## PR Completion Checklist (Use for Every PR)

1. Branch from latest origin/main.
2. Scope-limited changes only.
3. Focused tests green.
4. Full tests green.
5. py_compile green.
6. pip_audit green (or documented blocker).
7. Independent Explore subagent gate reports no blocking findings.
8. Commit message matches PR purpose.
9. Push branch.
10. Open PR with:
   - scope
   - behavior changed
   - validation results
   - residual risk
   - rollback notes if needed

---

## Suggested PR Titles

- PR 1: fix: harden auth and dependency security gates
- PR 2: fix: add task guardrails and restart recovery
- PR 3: fix: support multi-subscriber SSE event fanout
- PR 4: feat: add supervisor registry recipe and artifact foundations
- PR 5: feat: build internal command center live UI
- PR 6: feat: add market research recipe and source-backed artifacts
- PR 7: feat: enforce local worker policy and approval gates
- PR 8: feat: add model router and council mode scaffolding
- PR 9: feat: enforce personal work isolation boundaries
- PR 10: feat: add artifact studio recipe library and final polish
