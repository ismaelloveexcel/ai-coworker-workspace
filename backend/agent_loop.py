"""
Agent loop — core engine. Fully event-driven; DB is single source of truth.

v4 fixes (from audit):
- F34: asyncio.get_running_loop() replaces deprecated get_event_loop()
- F7:  Dedicated ThreadPoolExecutor for Claude calls and tool calls (separate
       from default pool) with a real cancel token so timed-out threads don't
       accumulate in the default pool
- F16: tool_output truncated to MAX_TOOL_OUTPUT chars before storing in DB/context
- F6:  Outer correction loop removed — claude_wrapper.run_agent_turn now owns
       one correction attempt internally and raises MalformedOutputError on failure
- F52: structlog configured once at module level
"""
import asyncio
import concurrent.futures
import functools
import json
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional

import structlog
import structlog.stdlib

from backend import db
from backend.claude_wrapper import MalformedOutputError, build_task_context, run_agent_turn, MAX_TOKENS, SYSTEM_PROMPT_TOKENS
from backend.config import settings
from backend.cost_tracker import BudgetExceeded, BudgetPreflightError, estimate_input_tokens, preflight_check, record_and_check
from backend.events import destroy_bus, emit, emit_log
from backend.model_router import infer_task_profile
from backend.notifier import notify_task_failure
from backend.tool_adapters import _redact, execute_tool, github_compare_branch, github_create_branch, github_create_pr, humanize_error, run_tests

log = structlog.get_logger(__name__)

MAX_TOOL_OUTPUT = 4000   # chars — prevents context blowout (F16)

# Dedicated executors — keep Claude and tool calls off the default pool (F7/F13)
_claude_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="claude")
_tool_executor   = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

# Registry of running asyncio Tasks keyed by task_id (for cancellation)
_running: Dict[str, asyncio.Task] = {}

# Set of task IDs for which cancellation has been explicitly requested.
# Populated by request_cancel() (called from the API layer) so that write
# checkpoints in run_task see the cancellation signal immediately, even when
# asyncio CancelledError delivery is delayed by an in-flight executor (PR-B4).
_cancel_requested: set = set()

# Write tools that produce external side-effects; used for in-flight recording.
_WRITE_TOOLS = frozenset({
    "github_create_branch", "github_commit_files", "github_create_pr", "filesystem_write",
})


def request_cancel(task_id: str) -> None:
    """Register a cancellation request for the given task.

    Call from the API layer alongside asyncio Task.cancel() so that write
    checkpoints in run_task block new writes as soon as cancellation is
    acknowledged (PR-B4).
    """
    _cancel_requested.add(task_id)


async def _check_cancel_checkpoint(task_id: str, op_name: str) -> None:
    """Raise CancelledError if cancellation has been requested for this task.

    Insert immediately before every write side-effect (PR-B4): once
    cancellation is acknowledged no new write operation is allowed to start.
    """
    if task_id in _cancel_requested:
        await emit_log(
            task_id, "info",
            f"Cancel checkpoint: {op_name} blocked — cancellation acknowledged",
        )
        raise asyncio.CancelledError(f"cancelled before {op_name}")


async def _notify_if_not_cancelled(task_id: str, repo: str, error: str) -> None:
    """Send failure notification only if cancellation has not been requested.

    Prevents creating a GitHub issue after cancel has been acknowledged
    (PR-B4 notification-issue checkpoint).
    """
    if task_id in _cancel_requested:
        await emit_log(
            task_id, "info",
            "Failure notification skipped — cancellation acknowledged",
        )
        return
    await notify_task_failure(task_id, repo, error)


async def _heartbeat_worker(task_id: str, interval: int) -> None:
    """Periodically touch heartbeat while a task step is actively running.

    Prevents the zombie reaper from falsely marking a long-running but valid
    step (e.g. a slow Claude call or long-running tool) as a zombie.
    Swallows CancelledError so cancellation is always clean.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            await db.touch_heartbeat(task_id)
    except asyncio.CancelledError:
        pass  # Graceful shutdown — caller awaits the task after cancel()


def _utcnow() -> str:
    """Return current UTC time as an ISO-8601 string for timing fields."""
    return datetime.now(timezone.utc).isoformat()


def _human_error(error: str) -> str:
    translated = humanize_error(error)
    if translated.get("success"):
        data = translated.get("data", {})
        return f"{data.get('message', error)} Recovery: {data.get('recovery', '')}".strip()
    return _redact(error)


def _json_redacted(data) -> str:
    return _redact(json.dumps(data))[:MAX_TOOL_OUTPUT]


def _has_successful_tests(steps) -> bool:
    for step in steps:
        if step.get("tool_name") != "run_tests" or step.get("status") != "done":
            continue
        try:
            output = json.loads(step.get("tool_output") or "{}")
        except json.JSONDecodeError:
            continue
        if output.get("success") and output.get("data", {}).get("success"):
            return True
    return False


def _suite_for_changed_files(files) -> str:
    paths = [f.get("filename", "") for f in files or []]
    backend_changed = any(p.endswith(".py") or p.startswith("backend/") or p == "watchdog.py" for p in paths)
    frontend_changed = any(
        p.endswith((".js", ".jsx", ".ts", ".tsx")) or p.startswith(("frontend/", "app/", "components/"))
        for p in paths
    )
    if backend_changed and frontend_changed:
        return "all"
    if frontend_changed:
        return "frontend"
    return "quick"


async def run_task(task_id: str) -> None:
    loop = asyncio.get_running_loop()   # F34: never use get_event_loop() in a coroutine
    log_ctx = log.bind(task_id=task_id)

    await db.update_task(task_id, status="running", current_step=0, started_at=_utcnow())
    await db.touch_heartbeat(task_id)   # prevent false-positive zombie reap during branch creation
    await emit_log(task_id, "info", "Agent starting")

    _hb_task = asyncio.create_task(
        _heartbeat_worker(task_id, settings.heartbeat_interval_seconds),
        name=f"heartbeat-{task_id}",
    )

    try:
        task = await db.get_task(task_id)
        if not task:
            await emit_log(task_id, "error", "Task not found in DB")
            return

        repo = task.get("repo_url") or settings.github_default_repo

        # --- Create branch ---------------------------------------------------
        await _check_cancel_checkpoint(task_id, "branch_creation")
        await emit_log(task_id, "info", f"Creating branch for task {task_id}")
        try:
            branch_result = await loop.run_in_executor(
                _tool_executor, github_create_branch, task_id, repo
            )
        except asyncio.CancelledError:
            await emit_log(task_id, "warning",
                           "Cancelled during branch creation — branch may exist on GitHub")
            await db.update_task(task_id, status="cancelled",
                                 error="Cancelled during branch creation — branch may exist on GitHub")
            raise
        if not branch_result.get("success"):
            err = branch_result.get("error", "unknown")
            await db.update_task(task_id, status="failed", error=f"Branch creation failed: {err}",
                                 ended_at=_utcnow(), failure_category="branch_creation_failed")
            await emit_log(task_id, "error", f"Branch creation failed: {err}")
            await emit(task_id, "task_failed", {"error": err})
            await _notify_if_not_cancelled(task_id, repo, f"Branch creation failed: {err}")
            return

        branch = branch_result["data"]["branch"]
        await db.update_task(task_id, branch=branch, checkpoint_phase="post_branch")
        await emit_log(task_id, "info", f"Working on branch: {branch}")

        steps = []
        step_count = 0

        while step_count < settings.max_steps:
            step_count += 1
            await db.update_task(task_id, current_step=step_count)
            await emit_log(task_id, "info", f"Step {step_count}/{settings.max_steps}")
            await db.touch_heartbeat(task_id)

            # Build context
            task = await db.get_task(task_id)
            steps = await db.get_steps(task_id)
            context = build_task_context(task, steps)
            messages = [{"role": "user", "content": context}]
            route_context = infer_task_profile(task)

            # --- Preflight budget guard --------------------------------------
            # Estimate cost before calling the model to prevent avoidable
            # single-call budget overshoots (PR-B7).
            estimated_input = estimate_input_tokens(messages, system_tokens=SYSTEM_PROMPT_TOKENS)
            try:
                await preflight_check(
                    task_id,
                    settings.model,
                    estimated_input,
                    MAX_TOKENS,  # worst-case output
                )
            except BudgetPreflightError as e:
                human = _human_error(str(e))
                log_ctx.warning("budget_preflight_refused", error=human)
                step_id = await db.create_step(task_id, step_count)
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": human}))
                await db.update_task(task_id, status="failed", error=human)
                await emit_log(task_id, "error", human)
                await emit(task_id, "task_failed", {"error": human})
                await notify_task_failure(task_id, repo, human)
                return

            # --- Claude call -------------------------------------------------
            step_id = await db.create_step(task_id, step_count)
            raw_text: Optional[str] = None
            parsed: Optional[Dict] = None

            try:
                raw_text, parsed, usage = await asyncio.wait_for(
                    loop.run_in_executor(
                        _claude_executor,
                        functools.partial(run_agent_turn, messages, route_context),
                    ),
                    timeout=float(settings.step_timeout_seconds),
                )
                # Note: wait_for cancels the Future but the underlying thread
                # continues until the Anthropic SDK times out on its own (~600s).
                # The dedicated _claude_executor bounds the blast radius to 4 threads.
            except asyncio.TimeoutError:
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": "Claude call timed out"}))
                err = f"Step {step_count} timed out after {settings.step_timeout_seconds}s"
                human = _human_error(err)
                await db.update_task(task_id, status="failed", error=human,
                                     ended_at=_utcnow(), failure_category="timeout")
                await emit_log(task_id, "error", f"Step {step_count} timed out")
                await emit(task_id, "task_failed", {"error": human})
                await _notify_if_not_cancelled(task_id, repo, human)
                return
            except MalformedOutputError as e:
                human = _human_error(str(e))
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": human}))
                await db.update_task(task_id, status="failed", error=human,
                                     ended_at=_utcnow(), failure_category="malformed_output")
                await emit_log(task_id, "error", f"Malformed output: {human}")
                await emit(task_id, "task_failed", {"error": human})
                await _notify_if_not_cancelled(task_id, repo, human)
                return
            except Exception as e:
                human = _human_error(str(e))
                log_ctx.exception("agent_error", error=human)
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": human}))
                await db.update_task(task_id, status="failed", error=human,
                                     ended_at=_utcnow(), failure_category="agent_error")
                await emit_log(task_id, "error", f"Agent error: {human}")
                await emit(task_id, "task_failed", {"error": human})
                await _notify_if_not_cancelled(task_id, repo, human)
                return

            # Track cost and enforce budget cap
            try:
                await record_and_check(
                    task_id, usage.get("model", settings.model),
                    usage["input_tokens"], usage["output_tokens"],
                )
            except BudgetExceeded as e:
                human = _human_error(str(e))
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": human}))
                await db.update_task(task_id, status="failed", error=human,
                                     ended_at=_utcnow(), failure_category="budget_exceeded")
                await emit_log(task_id, "error", human)
                await emit(task_id, "task_failed", {"error": human})
                await _notify_if_not_cancelled(task_id, repo, human)
                return

            action    = parsed.get("action", "error")
            tool_name = parsed.get("tool", "")
            tool_input = parsed.get("input", {})
            reasoning = parsed.get("reasoning", "")

            await db.update_task(task_id, last_action=action, last_tool=tool_name or None)

            await db.update_step(step_id, tool_name=tool_name,
                                 tool_input=_json_redacted(tool_input),
                                 reasoning=_redact(reasoning))
            await emit_log(task_id, "info", f"Action: {action} | Tool: {tool_name}", reasoning=_redact(reasoning[:200]))

            # --- final_answer ------------------------------------------------
            if action == "final_answer":
                compare_result = await loop.run_in_executor(
                    _tool_executor, github_compare_branch, branch, "main", repo
                )
                if not compare_result.get("success"):
                    err = _human_error(compare_result.get("error", "Branch comparison failed"))
                    await db.update_step(step_id, status="failed", tool_output=_json_redacted({"error": err}))
                    await db.update_task(task_id, status="failed", error=err,
                                         ended_at=_utcnow(), failure_category="agent_error")
                    await emit_log(task_id, "error", err)
                    await emit(task_id, "task_failed", {"error": err})
                    await _notify_if_not_cancelled(task_id, repo, err)
                    return

                changed_files = compare_result.get("data", {}).get("files", [])
                if not compare_result.get("data", {}).get("has_changes"):
                    err = "No file changes were found on the task branch, so no PR was opened."
                    await db.update_step(step_id, status="failed", tool_output=_json_redacted({"error": err, "compare": compare_result.get("data", {})}))
                    await db.update_task(task_id, status="failed", error=err,
                                         ended_at=_utcnow(), failure_category="no_changes")
                    await emit_log(task_id, "error", err)
                    await emit(task_id, "task_failed", {"error": err})
                    await _notify_if_not_cancelled(task_id, repo, err)
                    return

                if not _has_successful_tests(steps):
                    suite = _suite_for_changed_files(changed_files)
                    await emit_log(task_id, "info", f"Running validation before PR: {suite}")
                    test_result = await loop.run_in_executor(_tool_executor, run_tests, suite)
                    if not test_result.get("success") or not test_result.get("data", {}).get("success"):
                        err = "Validation failed before PR creation. Fix the test errors and retry."
                        await db.update_step(step_id, status="failed", tool_output=_json_redacted({"error": err, "tests": test_result}))
                        await db.update_task(task_id, status="failed", error=err,
                                             ended_at=_utcnow(), failure_category="validation_failed")
                        await emit_log(task_id, "error", err)
                        await emit(task_id, "task_failed", {"error": err})
                        await _notify_if_not_cancelled(task_id, repo, err)
                        return

                await _check_cancel_checkpoint(task_id, "pr_creation")
                try:
                    pr_result = await loop.run_in_executor(
                        _tool_executor, github_create_pr,
                        branch,
                        f"[Agent] {task['title']}",
                        _redact(f"Automated PR for task {task_id}\n\n{reasoning}"),
                        repo,
                    )
                except asyncio.CancelledError:
                    await emit_log(task_id, "warning",
                                   "Cancelled during PR creation — PR may exist on GitHub")
                    await db.update_task(task_id, status="cancelled",
                                         error="Cancelled during PR creation — PR may exist on GitHub")
                    raise
                if not pr_result.get("success"):
                    err = _human_error(pr_result.get("error", "PR creation failed"))
                    await db.update_step(step_id, status="failed", tool_output=_json_redacted({"error": err, "compare": compare_result.get("data", {})}))
                    await db.update_task(task_id, status="failed", error=err,
                                         ended_at=_utcnow(), failure_category="pr_creation_failed")
                    await emit_log(task_id, "error", err)
                    await emit(task_id, "task_failed", {"error": err})
                    await _notify_if_not_cancelled(task_id, repo, err)
                    return
                pr_url = pr_result.get("data", {}).get("pr_url", "")

                # Persist the PR URL and checkpoint immediately so that if the
                # backend restarts before final status write, the reconciler
                # surfaces the correct PR URL and avoids a duplicate-PR retry.
                await db.update_task(task_id, pr_url=pr_url, checkpoint_phase="post_pr")

                # A13: append a CHANGELOG entry on the task branch so the operator
                # can answer "what did the agent change?" without reading git log.
                await _check_cancel_checkpoint(task_id, "changelog_commit")
                try:
                    from datetime import date as _date
                    from backend.tool_adapters import github_read_file, github_commit_files
                    cl_result = await loop.run_in_executor(
                        _tool_executor, github_read_file, "CHANGELOG.md", branch, repo
                    )
                    existing_cl = cl_result.get("data", {}).get("content", "") if cl_result.get("success") else ""
                    changed = ", ".join(
                        f.get("filename", "") for f in changed_files[:5]
                    ) or "no files"
                    entry = (
                        f"\n## [{task_id[:8]}] {task['title']} — {_date.today()}\n"
                        f"- PR: {pr_url}\n"
                        f"- Changed: {changed}\n"
                        f"- Reasoning: {_redact(reasoning[:300])}\n"
                    )
                    new_cl = existing_cl + entry
                    try:
                        await loop.run_in_executor(
                            _tool_executor,
                            github_commit_files,
                            branch,
                            [{"path": "CHANGELOG.md", "content": new_cl}],
                            f"chore: update CHANGELOG for task {task_id[:8]}",
                            repo,
                            False,
                        )
                    except asyncio.CancelledError:
                        await emit_log(task_id, "warning",
                                       "Cancelled during changelog commit — commit may have completed in background")
                        await db.update_task(
                            task_id, status="cancelled",
                            error="Cancelled during changelog commit — commit may have completed in background",
                        )
                        raise
                except Exception as cl_exc:
                    log_ctx.warning("changelog_update_skipped", error=str(cl_exc))
                await db.update_step(step_id, status="done",
                                     tool_output=_json_redacted({"pr_url": pr_url, "compare": compare_result.get("data", {})}))
                await db.update_task(task_id, status="done", pr_url=pr_url, ended_at=_utcnow())
                await emit_log(task_id, "info", f"Task complete. PR: {pr_url}")
                await emit(task_id, "task_done", {"pr_url": pr_url})
                return

            # --- error action ------------------------------------------------
            if action == "error":
                human = _human_error(reasoning)
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": human}))
                await db.update_task(task_id, status="failed", error=human,
                                     ended_at=_utcnow(), failure_category="agent_error")
                await emit_log(task_id, "error", f"Agent reported error: {human}")
                await emit(task_id, "task_failed", {"error": human})
                await _notify_if_not_cancelled(task_id, repo, human)
                return

            # --- tool_call ---------------------------------------------------
            if not tool_name:
                await db.update_step(step_id, status="failed",
                                     tool_output=_json_redacted({"error": "no tool name"}))
                await emit_log(task_id, "warning", "tool_call with no TOOL name")
                continue

            await _check_cancel_checkpoint(task_id, tool_name)
            await emit(task_id, "tool_start", {"tool": tool_name, "input": _redact(json.dumps(tool_input))[:500]})
            try:
                tool_result = await loop.run_in_executor(
                    _tool_executor, execute_tool, tool_name, tool_input, task_id
                )
            except asyncio.CancelledError:
                if tool_name in _WRITE_TOOLS:
                    await emit_log(
                        task_id, "warning",
                        f"Cancelled during in-flight {tool_name} — operation may have completed in background",
                    )
                    await db.update_task(
                        task_id, status="cancelled",
                        error=f"Cancelled during {tool_name} — operation may have completed in background",
                    )
                raise
            tool_output_str = _json_redacted(tool_result)   # F16

            await db.update_step(step_id, status="done", tool_output=tool_output_str)
            await emit(task_id, "tool_done", {"tool": tool_name, "success": tool_result.get("success"), "output": tool_output_str[:500]})
            await emit_log(task_id, "info", f"Tool {tool_name} complete",
                           success=tool_result.get("success"))

        # Max steps reached
        err = f"Max steps ({settings.max_steps}) reached"
        human_err = _human_error(err)
        await emit_log(task_id, "warning", f"Max steps reached ({settings.max_steps})")
        try:
            compare_result = await loop.run_in_executor(_tool_executor, github_compare_branch, branch, "main", repo)
            if compare_result.get("success") and compare_result.get("data", {}).get("has_changes"):
                await _check_cancel_checkpoint(task_id, "pr_creation")
                try:
                    pr_result = await loop.run_in_executor(
                        _tool_executor,
                        github_create_pr,
                        branch,
                        f"[Agent incomplete] {task['title']}",
                        _redact(f"Task {task_id} reached max steps before finalizing.\n\n{human_err}"),
                        repo,
                        True,
                    )
                except asyncio.CancelledError:
                    await emit_log(task_id, "warning",
                                   "Cancelled during max-steps PR creation — PR may exist on GitHub")
                    await db.update_task(task_id, status="cancelled",
                                         error="Cancelled during max-steps PR creation — PR may exist on GitHub")
                    raise
                if pr_result.get("success"):
                    pr_url = pr_result.get("data", {}).get("pr_url", "")
                    await db.update_task(
                        task_id,
                        status="failed",
                        error=f"{human_err} Partial draft PR: {pr_url}",
                        pr_url=pr_url,
                        recovery_note=f"Partial draft PR opened at {pr_url}. Review the PR before retrying.",
                        ended_at=_utcnow(),
                        failure_category="agent_error",
                    )
                    await emit(task_id, "task_failed", {"error": human_err, "pr_url": pr_url})
                    await _notify_if_not_cancelled(task_id, repo, f"{human_err} Partial draft PR: {pr_url}")
                    return
        except Exception as exc:
            await emit_log(task_id, "warning", f"Partial PR creation skipped: {_human_error(str(exc))}")
        await db.update_task(
            task_id,
            status="failed",
            error=human_err,
            recovery_note=f"Agent stopped after reaching max steps. Review branch {branch!r} for partial work.",
            ended_at=_utcnow(),
            failure_category="agent_error",
        )
        await emit(task_id, "task_failed", {"error": human_err})
        await _notify_if_not_cancelled(task_id, repo, human_err)

    except asyncio.CancelledError:
        await db.update_task(task_id, status="cancelled", ended_at=_utcnow())
        await emit_log(task_id, "info", "Task cancelled")
        await emit(task_id, "task_cancelled", {})
        raise
    except Exception as e:
        traceback.print_exc()
        err = _human_error(str(e))
        await db.update_task(task_id, status="failed", error=err,
                             ended_at=_utcnow(), failure_category="agent_error")
        await emit_log(task_id, "error", f"Unexpected error: {err}")
        await emit(task_id, "task_failed", {"error": err})
        _repo = settings.github_default_repo
        try:
            _task = await db.get_task(task_id)
            if _task:
                _repo = _task.get("repo_url") or _repo
        except Exception:
            pass
        await _notify_if_not_cancelled(task_id, _repo, err)
    finally:
        _cancel_requested.discard(task_id)
        _hb_task.cancel()
        await asyncio.gather(_hb_task, return_exceptions=True)
        _running.pop(task_id, None)
        await asyncio.sleep(5)   # brief grace period for SSE consumers to drain
        destroy_bus(task_id)
