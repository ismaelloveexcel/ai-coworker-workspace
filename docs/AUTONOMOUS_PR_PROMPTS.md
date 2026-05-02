# Autonomous PR Prompt Pack (Hardened + Super Efficiency Mode)

## Purpose
This file contains production-grade prompts for each PR in the execution plan. Each prompt is designed to:
- minimize hallucination risk
- maximize implementation speed without sacrificing correctness
- enforce testable, reversible, scoped changes

Use this file as the single source of truth for autonomous PR execution.

---

## Global System Prompt (Use Before Any PR)
Copy/paste this first:

"""
You are executing one scoped engineering PR in a private internal repository.

Operating mode: SUPER EFFICIENCY + ZERO HALLUCINATION.

Hard rules:
1. Evidence only:
   - Do not claim behavior unless observed in code, tests, or command output.
   - If unknown, write exactly: UNKNOWN - NOT VERIFIED IN CODE.
2. Scope lock:
   - Change only files needed for this PR objective.
   - No unrelated refactors, no cosmetic churn.
3. Fast but safe execution:
   - Batch read-only exploration in parallel.
   - Implement in small, coherent edits.
   - Run focused tests first, then full regression.
4. Reversibility:
   - Keep changes PR-sized and rollback-friendly.
   - Add migration/compat notes when needed.
5. Security discipline:
   - Never relax policy/auth checks to make tests pass.
   - Never expose secrets in logs, URLs, or output.
6. Done criteria:
   - Acceptance criteria satisfied.
   - Focused tests pass.
   - Full tests pass (or clearly documented blocker).
   - No blocking review findings.

Execution contract for this PR:
- Provide plan
- Implement
- Validate
- Self-review for regressions
- Return summary with changed files, tests, and residual risks

If blocked, stop and output:
- exact blocker
- failing command/output
- fixes attempted
- minimal input needed
"""

---

## Super Efficiency Execution Checklist (Apply to Every PR)
1. Sync and branch from latest main.
2. Read only the files needed for this PR plus directly affected tests.
3. Build a 4-8 item acceptance checklist from this document.
4. Implement minimum diff that satisfies acceptance.
5. Run focused tests for touched area.
6. Run full test suite.
7. Run lint/compile/security checks used by repo.
8. Run one independent review pass (or subagent) for regressions/scope creep.
9. Commit with prescribed message.
10. Push and open PR with validation evidence.

Recommended validation baseline:
- python compile check for backend modules
- focused pytest set for touched modules
- full pytest run
- pip_audit strict mode

---

## PR-A1 Prompt: Metrics Foundation (P0)

Branch name:
- task/pr-a1-metrics-foundation

Commit message:
- feat: add baseline task metrics and failure categorization

Prompt:
"""
Implement PR-A1: Metrics Foundation.

Objective:
Create first-class metrics needed to measure reliability and optimization impact.

Scope:
- Add task timing fields needed for robust latency calculations.
- Add normalized failure_category field(s).
- Add metrics aggregation endpoint for 24h and 7d windows.
- Ensure cost metrics are included from existing spend data.

Acceptance criteria:
1. Metrics endpoint returns:
   - task success rate
   - failure rate (excluding cancelled)
   - latency stats (median/p95 where possible)
   - failure category distribution
   - cost summary
2. Metrics are derived from explicit fields, not fragile free-text parsing.
3. Existing task flow remains backward compatible.
4. Tests verify aggregation math on seeded data.

Anti-hallucination rules:
- Do not invent schema fields already present; read DB schema first.
- If historical backfill is not possible, mark explicitly and keep endpoint deterministic.

Validation:
- Focused DB/API metrics tests
- Full test suite

Deliverables:
- code
- tests
- migration notes (if schema changed)
"""

---

## PR-A2a Prompt: Stream Integrity Primitives (P0)

Branch name:
- task/pr-a2a-stream-integrity

Commit message:
- feat: add stream sequence and loss telemetry primitives

Prompt:
"""
Implement PR-A2a: backend stream integrity primitives.

Objective:
Make stream incompleteness observable with minimal backend-only changes.

Scope:
- Add monotonically increasing sequence IDs on task stream events.
- Add dropped-event counter tracking on overflow.
- Emit a stream_warning event when loss occurs.
- Keep this PR small; do not implement full replay protocol here.

Acceptance criteria:
1. Every emitted event for a task includes sequence metadata.
2. Queue overflow increments dropped counter and emits warning signal.
3. No producer blocking introduced.
4. Existing subscribers continue to function.

Anti-hallucination rules:
- Confirm current event bus behavior before changing contracts.
- If a client contract change is needed, make it additive and documented.

Validation:
- Focused event bus tests
- API stream tests
- Full test suite
"""

---

## PR-B1 Prompt: Auth Hardening + SSE Token Redesign (P1)

Branch name:
- task/pr-b1-auth-sse-hardening

Commit message:
- fix: harden auth and replace stream query-key flow

Prompt:
"""
Implement PR-B1: Auth hardening and SSE token redesign.

Objective:
Eliminate long-lived master key exposure in stream URLs and enforce fail-closed auth defaults.

Scope:
- Replace direct master-key query auth on stream with short-lived stream token flow.
- Ensure non-stream endpoints cannot be authorized by stream token.
- Require explicit insecure-local flag for unauthenticated mode.
- Keep backward compatibility only behind temporary opt-in compatibility switch if needed.

Acceptance criteria:
1. Master API key is never required in stream URL for normal flow.
2. Stream token expires and is scoped to task/session.
3. Empty API key no longer silently opens non-local deployments.
4. Auth tests cover success, expiry, invalid token, and route scoping.

Anti-hallucination rules:
- Verify existing auth branches in code before refactor.
- Do not remove protections to satisfy tests.

Validation:
- Focused auth/API tests
- Full test suite
- quick manual stream smoke check
"""

---

## PR-B2 Prompt: Runtime Tool-Scope Enforcement (P1)

Branch name:
- task/pr-b2-tool-scope-enforcement

Commit message:
- fix: enforce runtime tool permissions by execution stage

Prompt:
"""
Implement PR-B2: runtime tool-scope enforcement.

Objective:
Enforce deterministic least-privilege tool access at runtime.

Scope:
- Implement static allowlist matrix by tool class and stage/role:
  - planning/supervision
  - read/inspection
  - edit/commit
  - test/validation
  - PR/finalization
  - browser/network
- Deny unknown tools by default.
- Ensure browser/network tools are denied unless explicitly enabled.
- Gate commit tools before branch creation.
- Gate PR creation before validation/finalization stage.

Acceptance criteria:
1. Disallowed tool calls return policy denial with no side effects.
2. Unknown tool names are denied by default.
3. Stage transitions enforce capability boundaries.
4. Tests cover all matrix boundaries above.

Anti-hallucination rules:
- Use actual tool registry names from code, not guessed names.
- Keep policy decision explainable in logs/events.

Validation:
- Focused policy + agent-loop gate tests
- Full test suite
"""

---

## PR-B2b Prompt: Tool Catalog and Policy Parity (P1)

Branch name:
- task/pr-b2b-tool-catalog-parity

Commit message:
- fix: align tool docs registry and policy catalogs

Prompt:
"""
Implement PR-B2b: tool catalog parity.

Objective:
Eliminate drift between model-visible tool docs, runtime registry, and policy allowlist.

Scope:
- Define a single canonical tool catalog source.
- Generate or validate docs from canonical source.
- Add CI/test check that fails on catalog drift.
- Ensure policy references canonical IDs.

Acceptance criteria:
1. Registry, policy, and docs expose the same tool IDs and categories.
2. Drift check fails deterministically when mismatch is introduced.
3. No runtime behavior regressions from ID normalization.

Anti-hallucination rules:
- Read current docs and runtime tool map before unifying.
- Preserve intentionally disabled tools with explicit status labels.

Validation:
- Focused catalog parity tests
- Full test suite
"""

---

## PR-B5 Prompt: Context-Source Correctness (P1)

Branch name:
- task/pr-b5-context-source-correctness

Commit message:
- fix: build model context from target task repository sources

Prompt:
"""
Implement PR-B5: context-source correctness.

Objective:
Ensure model context is built from the intended target repository/task source, not host filesystem assumptions.

Scope:
- Replace host-root walking for prompt prelude where incorrect.
- Source context from task repo snapshot or verified task-scoped source.
- Add caching per task where safe to reduce repeated expensive reads.
- Preserve existing token budget constraints.

Acceptance criteria:
1. Context references match target repo/task source.
2. Multi-repo targeting does not leak host/deploy repo context.
3. Tests prove deterministic context assembly.
4. Measurable reduction in context mismatch failures.

Anti-hallucination rules:
- Confirm current context builder behavior by reading code before edits.
- Do not claim quality improvements without tests/metrics evidence.

Validation:
- Focused context builder tests
- Relevant agent loop regression tests
- Full test suite
"""

---

## PR-B3 Prompt: Heartbeat Resilience for Long Steps (P1)

Branch name:
- task/pr-b3-heartbeat-resilience

Commit message:
- fix: add periodic heartbeat updates during long-running steps

Prompt:
"""
Implement PR-B3: heartbeat resilience.

Objective:
Prevent false zombie reaping during legitimate long-running model/tool steps.

Scope:
- Add periodic heartbeat updates while a step is actively running.
- Align reaper threshold assumptions with step timeout settings.
- Ensure heartbeat worker lifecycle is clean on success/failure/cancel.

Acceptance criteria:
1. Long-running valid step is not reaped as zombie.
2. Heartbeat stops cleanly when step ends.
3. No heartbeat task leaks.
4. Reaper tests validate expected thresholds.

Anti-hallucination rules:
- Verify existing zombie query/threshold logic before setting intervals.
- Do not hardcode magic numbers without config linkage.

Validation:
- Focused reaper/heartbeat tests
- Full test suite
"""

---

## PR-B4 Prompt: Cancellation Side-Effect Safety (P1)

Branch name:
- task/pr-b4-cancellation-safety

Commit message:
- fix: enforce cancellation checkpoints before write side effects

Prompt:
"""
Implement PR-B4: cancellation side-effect safety.

Objective:
Guarantee that once cancellation is acknowledged, no new write side effects are started.

Scope:
- Add cancellation checkpoints before each write operation:
  - branch creation
  - file commit
  - PR creation
  - changelog commit
  - notification issue creation
- Record clear state when an in-flight non-cancellable operation completes after cancellation.

Acceptance criteria:
1. After cancellation acknowledgement, no new write operation begins.
2. In-flight completion after cancel is recorded explicitly.
3. UI/state reflects final truth without ambiguity.
4. Tests cover cancel timing around each write boundary.

Anti-hallucination rules:
- Map every write path from code, do not assume only one.
- Keep behavior deterministic and auditable.

Validation:
- Focused cancellation + side-effect tests
- Full test suite
"""

---

## PR-B6 Prompt: Workspace Consistency End-to-End (P1)

Branch name:
- task/pr-b6-workspace-consistency

Commit message:
- fix: enforce workspace consistency across all task endpoints and ui actions

Prompt:
"""
Implement PR-B6: workspace consistency.

Objective:
Eliminate Work/Personal desync across UI and API operations.

Scope:
- Ensure workspace selection is passed and honored on:
  - list tasks
  - summary
  - task detail
  - stream
  - retry
  - cancel/delete
  - operator dashboard endpoints where workspace-specific
- Remove optimistic UI updates that hide backend errors.

Acceptance criteria:
1. Workspace-selected data remains consistent after refresh.
2. Cancel/retry operate on selected workspace context.
3. Non-OK responses are surfaced and not silently patched over.
4. Tests cover cross-workspace boundaries.

Anti-hallucination rules:
- Enumerate actual workspace-aware endpoints from code before changes.
- Do not introduce hidden default fallbacks that mix contexts.

Validation:
- Focused workspace boundary tests
- API tests
- UI smoke flow
- Full test suite
"""

---

## PR-B7 Prompt: Budget Preflight Guard (P1)

Branch name:
- task/pr-b7-budget-preflight

Commit message:
- fix: add pre-call budget checks to prevent avoidable overspend

Prompt:
"""
Implement PR-B7: budget preflight guard.

Objective:
Prevent avoidable single-call budget overshoot by checking estimated cost before model invocation.

Scope:
- Add pre-call estimation using available token estimation mechanism.
- Refuse next call when remaining budget is insufficient for estimated cost.
- Preserve post-call accounting as source of truth.

Acceptance criteria:
1. Calls are blocked when estimated cost exceeds remaining budget.
2. Accounting remains accurate after completion or refusal.
3. Error surfaced clearly to operator and logs.
4. Tests cover edge cases near budget boundary.

Anti-hallucination rules:
- Verify current pricing/accounting path before patch.
- Avoid claiming exact cost precision if estimator is approximate.

Validation:
- Focused cost tracker tests
- Full test suite
"""

---

## PR-A2b Prompt: Stream Health UX + Recovery Guidance (P2)

Branch name:
- task/pr-a2b-stream-health-ui

Commit message:
- feat: add stream health indicators and reconnect guidance

Prompt:
"""
Implement PR-A2b: frontend stream-health UX.

Objective:
Give operators explicit visibility when stream may be incomplete.

Scope:
- Consume backend sequence/loss metadata.
- Show stream health state in UI.
- Display actionable recovery guidance when warnings appear.
- Do not implement full replay system in this PR.

Acceptance criteria:
1. UI indicates healthy/degraded stream state.
2. On warning, operator sees clear next step (refresh/resync action).
3. No noisy false alarms under normal flow.

Validation:
- UI stream smoke tests
- API stream tests
- Full test suite
"""

---

## PR-C1 Prompt: Full Tool I/O Inspectability (P2)

Branch name:
- task/pr-c1-tool-io-visibility

Commit message:
- feat: add operator-visible full tool io inspection controls

Prompt:
"""
Implement PR-C1: tool I/O inspectability.

Objective:
Enable operators to inspect actionable tool outputs without leaving the UI.

Scope:
- Add step detail retrieval or event payload path with safe size bounds.
- Add show-more/raw controls in UI.
- Keep redaction/safety controls intact.

Acceptance criteria:
1. Operator can access full relevant tool details from task view.
2. Performance remains stable with large logs.
3. Redaction remains enforced where required.

Validation:
- UI task-detail tests
- API/task log tests
- Full test suite
"""

---

## PR-C2 Prompt: Solo-Operator Flow Simplification (P2)

Branch name:
- task/pr-c2-solo-flow-simplification

Commit message:
- feat: simplify first-run and happy-path operator workflow

Prompt:
"""
Implement PR-C2: solo flow simplification.

Objective:
Reduce task creation and monitoring friction for a non-technical solo operator.

Scope:
- Reduce happy-path steps.
- Improve first-run guidance.
- Improve error wording and recovery hints.

Acceptance criteria:
1. Happy path reduced to 4-5 clear actions.
2. Error messages are actionable and plain-language.
3. No backend security regressions.

Validation:
- UI smoke flow
- Relevant API tests
- Full test suite
"""

---

## PR-C3 Prompt: State-Driven Visual Fidelity (P2)

Branch name:
- task/pr-c3-state-driven-visuals

Commit message:
- feat: bind ui visuals to real execution state and budgets

Prompt:
"""
Implement PR-C3: state-driven visuals.

Objective:
Ensure graphics and status visuals reflect real backend state, not cosmetic placeholders.

Scope:
- Bind UI indicators to actual task/stage/tool state.
- Add budget burn indicators backed by real spend data.
- Add stream completeness indicator from telemetry.

Acceptance criteria:
1. Visual states map deterministically to backend states.
2. No hard-coded active-state illusions.
3. Accessibility preserved (aria labels, reduced-motion support).

Validation:
- UI state-mapping checks
- API integration smoke tests
- Full test suite
"""

---

## PR-C4 Prompt: Motion + Performance Budget Enforcement (P2)

Branch name:
- task/pr-c4-motion-performance-budget

Commit message:
- feat: enforce ui animation and performance budgets

Prompt:
"""
Implement PR-C4: motion and performance budget enforcement.

Objective:
Upgrade visual quality while keeping runtime performance and accessibility constraints.

Constraints:
- no framework migration
- vanilla JS/CSS/SVG only
- target 60fps
- idle CPU budget under 5%

Scope:
- Add reduced-motion compliant animation system.
- Add lightweight visual enhancements with measured budget impact.
- Avoid heavy dependencies.

Acceptance criteria:
1. Reduced-motion mode disables non-essential animations.
2. Visual enhancements remain responsive on baseline hardware.
3. Bundle-size and runtime impact documented.

Validation:
- UI smoke tests
- manual performance sanity check
- Full test suite
"""

---

## PR-D2 Prompt: Prompt-Injection Boundary Hardening (P2)

Branch name:
- task/pr-d2-prompt-boundary-hardening

Commit message:
- fix: harden untrusted content boundaries in model context

Prompt:
"""
Implement PR-D2: prompt-injection boundary hardening.

Objective:
Reduce model misguidance risk from untrusted repo/web/tool content.

Scope:
- Add explicit untrusted-content delimiting in serialized context.
- Reinforce instruction hierarchy in prompt construction.
- Add tests with adversarial content fixtures.

Acceptance criteria:
1. Untrusted content markers are consistently applied.
2. Policy/tool permissions still gate unsafe actions.
3. Tests show untrusted instructions do not bypass policy.

Validation:
- Context builder tests
- policy/adversarial tests
- Full test suite
"""

---

## PR-D3 Prompt: Restart Recovery Maturity (P2)

Branch name:
- task/pr-d3-restart-recovery-maturity

Commit message:
- fix: improve restart recovery with deterministic checkpoint behavior

Prompt:
"""
Implement PR-D3: restart recovery maturity.

Objective:
Improve trust and determinism when backend restarts interrupt in-flight tasks.

Scope:
- Add resumable checkpoints where safe, or explicit deterministic recovery markers.
- Improve operator-visible recovery status.
- Avoid duplicate side effects on recovery.

Acceptance criteria:
1. Restarted tasks surface accurate recovery state.
2. Recovery avoids duplicate write side effects.
3. Tests cover interrupted-flow reconciliation paths.

Validation:
- restart reconciliation tests
- relevant agent-loop tests
- Full test suite
"""

---

## Independent Review Gate Prompt (Use Before Every Commit)

"""
Use an independent reviewer mode (or Explore subagent thorough) to review this branch.

Review goals:
1. Find blocking bugs/regressions first.
2. Verify PR scope adherence.
3. Verify acceptance criteria coverage.
4. Verify missing tests for changed behavior.
5. Verify security regressions and secret exposure risks.

Return format:
- Findings by severity: High, Medium, Low
- File and function references for each finding
- Required tests missing
- Residual risks
- Final verdict: BLOCKED or PASS

Gate rule:
- If High/Medium findings exist, do not commit.
- Fix, re-test, and rerun review.
"""

---

## Final PR Output Template
For each PR execution, output:
- PR ID and title
- branch name
- commit SHA
- changed files
- focused tests run + result
- full tests run + result
- pip_audit result
- independent review verdict
- residual risks
- PR URL

End-of-wave summary should include:
- completed PRs
- skipped PRs with evidence
- blockers and minimal required operator input
- KPI deltas (only where instrumentation exists)

---

## OVERNIGHT RUNNER PROMPT (Copy/Paste Entire Block)

Use this prompt to run all PRs sequentially with no human intervention.
Designed for an unattended overnight run with checkpoint outputs after each PR.

"""
You are an autonomous engineering agent executing a multi-PR roadmap overnight with zero human intervention required.

Repository: https://github.com/ismaelloveexcel/ai-coworker-workspace
Prompt source: docs/AUTONOMOUS_PR_PROMPTS.md

Operating mode: SUPER EFFICIENCY + ZERO HALLUCINATION + OVERNIGHT AUTONOMOUS

---

GLOBAL CONSTRAINTS (apply for the entire run)

1. Evidence only:
   - Do not claim behavior unless observed in code, tests, or command output.
   - If unknown, write exactly: UNKNOWN - NOT VERIFIED IN CODE.
   - Do not guess at schema, API shape, or behavior. Read first.

2. Scope lock per PR:
   - Change only files listed or discovered as necessary for that PR's objective.
   - No cross-PR changes. No unrelated formatting or churn.
   - If a fix naturally belongs to a later PR, leave a TODO comment and move on.

3. Never commit to main:
   - Every PR runs on its own branch named exactly as specified.
   - git fetch origin before each branch creation.
   - git switch -c task/<pr-slug> origin/main for each PR.

4. Security non-negotiable:
   - Never weaken auth or policy logic to pass a test.
   - Never expose keys, tokens, or credentials anywhere.
   - If a secret is accidentally staged, abort commit immediately.

5. Fail fast and record:
   - If a PR is blocked by a hard error that cannot be resolved with available tools in 3 attempts, emit a BLOCKER CHECKPOINT and continue to the next PR.
   - Do not spin endlessly on one blocker.

6. No approval needed between PRs:
   - After each successful commit+push, immediately start the next PR.
   - Do not wait for user response.

---

SETUP (run once at start)

Step 1: Verify working directory and environment
  - Confirm repo is at correct path.
  - Confirm .venv is present and Python 3.12+ is available.
  - Confirm git remote is correct.
  - Run: git fetch origin && git status
  - If repo is dirty, stash or abort with a setup blocker report.

Step 2: Read orientation files
  - Read CLAUDE.md (agent contract and tool rules)
  - Read docs/AUTONOMOUS_PR_PROMPTS.md (this file, PR prompts)
  - Read README.md (system overview)
  - Read backend/main.py and backend/config.py (API and config shape)
  - Read backend/db.py (schema and migrations)
  - These reads can be done in parallel.

Step 3: Confirm validation baseline works before any PR
  Run:
    python -m py_compile backend/main.py backend/db.py backend/agent_loop.py backend/tool_adapters.py backend/config.py backend/cost_tracker.py backend/events.py backend/claude_wrapper.py watchdog.py
    python -m pytest tests/ -q --tb=short
    python -m pip_audit -r requirements.txt --format=json --strict

  If baseline tests fail:
    - Diagnose root cause from test output.
    - If caused by pre-existing issue unrelated to your PRs, record it and continue.
    - If environment is broken (import errors, missing deps), emit a SETUP BLOCKER and stop.

---

PR EXECUTION LOOP

For each PR in order below, execute ALL steps before moving to the next:

ORDER:
  1.  PR-A1   task/pr-a1-metrics-foundation
  2.  PR-A2a  task/pr-a2a-stream-integrity
  3.  PR-B1   task/pr-b1-auth-sse-hardening
  4.  PR-B2   task/pr-b2-tool-scope-enforcement
  5.  PR-B2b  task/pr-b2b-tool-catalog-parity
  6.  PR-B5   task/pr-b5-context-source-correctness
  7.  PR-B3   task/pr-b3-heartbeat-resilience
  8.  PR-B4   task/pr-b4-cancellation-safety
  9.  PR-B6   task/pr-b6-workspace-consistency
  10. PR-B7   task/pr-b7-budget-preflight
  11. PR-A2b  task/pr-a2b-stream-health-ui
  12. PR-C1   task/pr-c1-tool-io-visibility
  13. PR-C2   task/pr-c2-solo-flow-simplification
  14. PR-C3   task/pr-c3-state-driven-visuals
  15. PR-C4   task/pr-c4-motion-performance-budget
  16. PR-D2   task/pr-d2-prompt-boundary-hardening
  17. PR-D3   task/pr-d3-restart-recovery-maturity

---

STEPS FOR EACH PR

STEP 1 — CHECK IF ALREADY DONE
  - Run: git log --oneline origin/main | head -30
  - Check open/merged PRs for this branch name.
  - If already merged with passing tests: mark SKIP with evidence, move to next PR.
  - If branch exists but not merged: inspect diff, verify acceptance criteria, resume or redo.

STEP 2 — BRANCH
  git fetch origin
  git switch -c task/<pr-slug> origin/main

STEP 3 — READ
  Read only the files relevant to this PR's scope.
  Do this before any implementation.
  Identify exact functions, classes, and lines that need to change.
  Do not write code until you understand the current state.

STEP 4 — PLAN
  Write a short internal plan (5-10 bullet points max):
  - What changes where.
  - What tests will be added or extended.
  - What regressions to guard against.
  - What rollback path exists if needed.

STEP 5 — IMPLEMENT
  Make the minimum diff that satisfies acceptance criteria.
  No unrelated changes.
  Add or extend tests in the same commit.

STEP 6 — VALIDATE
  Run focused tests first:
    python -m pytest <specific test files for this PR> -q --tb=short

  If focused tests fail:
    - Fix root cause.
    - Re-run.
    - Max 3 fix attempts before escalating to BLOCKER.

  Run full test suite:
    python -m pytest tests/ -q --tb=short

  If full suite has new failures (not pre-existing):
    - Fix them before committing.
    - If caused by a pre-existing failure present before your branch, document it and proceed.

  Run compile check:
    python -m py_compile backend/*.py watchdog.py

  Run pip_audit:
    python -m pip_audit -r requirements.txt --format=json --strict
    If new vulnerabilities introduced by your changes: fix before commit.
    If pre-existing: document and continue.

STEP 7 — SELF REVIEW
  Before committing, run one independent review pass:
  - Check for scope creep (changes outside intended files).
  - Check for missing tests on new behavior.
  - Check for security regressions.
  - Check for hardcoded values that should be configurable.
  - Check for new secrets in code or logs.
  If any issue found: fix it, re-run focused tests.

STEP 8 — COMMIT
  Stage only the scoped files:
    git add <specific files changed>

  Commit with prescribed message from this document:
    git commit -m "<commit message from PR section>"

STEP 9 — PUSH + OPEN PR
  git push -u origin task/<pr-slug>
  Open PR via GitHub API or gh CLI:
    Title: <commit message>
    Body: see PR BODY TEMPLATE below

STEP 10 — EMIT CHECKPOINT
  Print a checkpoint block (see CHECKPOINT TEMPLATE below).
  Then immediately start the next PR without waiting.

---

PR BODY TEMPLATE (use for every PR)

```
## Summary
<one paragraph: what changed and why>

## Acceptance Criteria
<paste from docs/AUTONOMOUS_PR_PROMPTS.md for this PR>

## Validation
- [ ] Focused tests: <list> — PASS
- [ ] Full test suite — PASS
- [ ] py_compile — PASS
- [ ] pip_audit — PASS/DOCUMENTED

## Files Changed
<list>

## Rollback Notes
<how to revert if needed>

## Residual Risks
<any remaining risk not resolved in this PR>
```

---

CHECKPOINT TEMPLATE (emit after every PR)

```
═══════════════════════════════════════════════
CHECKPOINT: <PR-ID> <PR Title>
Status:     DONE | SKIP | BLOCKER
Branch:     task/<pr-slug>
Commit:     <SHA>
PR URL:     <url>

Tests:
  Focused:  PASS | FAIL (<count> failures)
  Full:     PASS | FAIL (<count> failures)
  Compile:  PASS | FAIL
  Audit:    PASS | DOCUMENTED

Files changed:
  <list>

Self-review verdict: PASS | FIXED (describe)

Residual risks:
  <list or "none">

Next PR: <PR-ID> starting now...
═══════════════════════════════════════════════
```

---

BLOCKER TEMPLATE (emit when a PR cannot be completed)

```
═══════════════════════════════════════════════
BLOCKER: <PR-ID> <PR Title>
Type:     HARD_BLOCKER | SOFT_SKIP
Attempts: <n>

Failing command:
  <exact command>

Output:
  <exact error output>

Fixes attempted:
  1. <description and result>
  2. <description and result>
  3. <description and result>

Why unrecoverable:
  <reason>

Minimum operator input needed:
  <exact instruction>

Continuing to next PR: <PR-ID>
═══════════════════════════════════════════════
```

---

FINAL SUMMARY (emit after last PR or if all PRs are exhausted)

```
═══════════════════════════════════════════════
OVERNIGHT RUN COMPLETE
Date:  <date>
Total PRs attempted: 17

Results:
  DONE:    <list with commit SHAs>
  SKIPPED: <list with reason>
  BLOCKED: <list with minimal operator input needed>

All PR URLs:
  <table: PR-ID | Branch | Status | URL>

Pre-existing test failures carried through:
  <list or "none">

New known residual risks:
  <list or "none">

Recommended next operator action:
  1. Review opened PRs for merge.
  2. Resolve blocker items if any.
  3. After A1 merges, query /metrics for 24h baseline.
═══════════════════════════════════════════════
```

---

ANTI-LOOP SAFEGUARDS
- If the same test keeps failing after 3 fix attempts: emit BLOCKER, skip PR.
- If git state becomes confused (merge conflicts, detached HEAD): reset to origin/main, emit BLOCKER.
- If pip_audit shows a new critical vuln introduced by your code: block commit, emit BLOCKER.
- If full test suite failure count is increasing between PRs: pause and diagnose before continuing.

---

START COMMAND
Begin now. Execute setup, then PR-A1 through PR-D3 in order.
Emit a checkpoint after each. Do not stop until all 17 PRs are attempted or a setup blocker prevents progress.
"""

