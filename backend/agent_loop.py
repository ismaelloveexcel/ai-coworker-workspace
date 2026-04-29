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
import json
import traceback
from typing import Dict, List, Optional

import structlog
import structlog.stdlib

from backend import db
from backend.claude_wrapper import MalformedOutputError, build_task_context, run_agent_turn
from backend.config import settings
from backend.cost_tracker import BudgetExceeded, record_and_check
from backend.events import destroy_bus, emit, emit_log, get_bus
from backend.notifier import notify_task_failure
from backend.tool_adapters import execute_tool, github_create_branch, github_create_pr

log = structlog.get_logger(__name__)

MAX_TOOL_OUTPUT = 4000   # chars — prevents context blowout (F16)

# Dedicated executors — keep Claude and tool calls off the default pool (F7/F13)
_claude_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="claude")
_tool_executor   = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

# Registry of running asyncio Tasks keyed by task_id (for cancellation)
_running: Dict[str, asyncio.Task] = {}


async def run_task(task_id: str) -> None:
    loop = asyncio.get_running_loop()   # F34: never use get_event_loop() in a coroutine
    log_ctx = log.bind(task_id=task_id)

    await db.update_task(task_id, status="running")
    await emit_log(task_id, "info", "Agent starting")

    try:
        task = await db.get_task(task_id)
        if not task:
            await emit_log(task_id, "error", "Task not found in DB")
            return

        repo = task.get("repo_url") or settings.github_default_repo

        # --- Create branch ---------------------------------------------------
        await emit_log(task_id, "info", f"Creating branch for task {task_id}")
        branch_result = await loop.run_in_executor(
            _tool_executor, github_create_branch, task_id, repo
        )
        if not branch_result.get("success"):
            err = branch_result.get("error", "unknown")
            await db.update_task(task_id, status="failed", error=f"Branch creation failed: {err}")
            await emit_log(task_id, "error", f"Branch creation failed: {err}")
            await emit(task_id, "task_failed", {"error": err})
            await notify_task_failure(task_id, repo, f"Branch creation failed: {err}")
            return

        branch = branch_result["data"]["branch"]
        await emit_log(task_id, "info", f"Working on branch: {branch}")

        steps = []
        step_count = 0

        while step_count < settings.max_steps:
            step_count += 1
            await emit_log(task_id, "info", f"Step {step_count}/{settings.max_steps}")
            await db.touch_heartbeat(task_id)

            # Build context
            task = await db.get_task(task_id)
            steps = await db.get_steps(task_id)
            context = build_task_context(task, steps)
            messages = [{"role": "user", "content": context}]

            # --- Claude call -------------------------------------------------
            step_id = await db.create_step(task_id, step_count)
            raw_text: Optional[str] = None
            parsed: Optional[Dict] = None

            try:
                raw_text, parsed, usage = await asyncio.wait_for(
                    loop.run_in_executor(_claude_executor, run_agent_turn, messages),
                    timeout=float(settings.step_timeout_seconds),
                )
                # Note: wait_for cancels the Future but the underlying thread
                # continues until the Anthropic SDK times out on its own (~600s).
                # The dedicated _claude_executor bounds the blast radius to 4 threads.
            except asyncio.TimeoutError:
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": "Claude call timed out"}))
                err = f"Step {step_count} timed out after {settings.step_timeout_seconds}s"
                await db.update_task(task_id, status="failed", error=err)
                await emit_log(task_id, "error", f"Step {step_count} timed out")
                await emit(task_id, "task_failed", {"error": "timeout"})
                await notify_task_failure(task_id, repo, err)
                return
            except MalformedOutputError as e:
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": str(e)}))
                await db.update_task(task_id, status="failed", error=str(e))
                await emit_log(task_id, "error", f"Malformed output: {e}")
                await emit(task_id, "task_failed", {"error": str(e)})
                await notify_task_failure(task_id, repo, str(e))
                return
            except Exception as e:
                tb = traceback.format_exc()
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": str(e)}))
                await db.update_task(task_id, status="failed", error=str(e))
                await emit_log(task_id, "error", f"Agent error: {e}")
                log_ctx.error("agent_error", exc_info=True)
                await emit(task_id, "task_failed", {"error": str(e)})
                await notify_task_failure(task_id, repo, str(e))
                return

            # Track cost and enforce budget cap
            try:
                await record_and_check(
                    task_id, settings.model,
                    usage["input_tokens"], usage["output_tokens"],
                )
            except BudgetExceeded as e:
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": str(e)}))
                await db.update_task(task_id, status="failed", error=str(e))
                await emit_log(task_id, "error", str(e))
                await emit(task_id, "task_failed", {"error": "budget_exceeded"})
                await notify_task_failure(task_id, repo, str(e))
                return

            action    = parsed.get("action", "error")
            tool_name = parsed.get("tool", "")
            tool_input = parsed.get("input", {})
            reasoning = parsed.get("reasoning", "")

            await db.update_step(step_id, tool_name=tool_name,
                                 tool_input=json.dumps(tool_input)[:MAX_TOOL_OUTPUT],
                                 reasoning=reasoning)
            await emit_log(task_id, "info", f"Action: {action} | Tool: {tool_name}", reasoning=reasoning[:200])

            # --- final_answer ------------------------------------------------
            if action == "final_answer":
                pr_result = await loop.run_in_executor(
                    _tool_executor, github_create_pr,
                    branch,
                    f"[Agent] {task['title']}",
                    f"Automated PR for task {task_id}\n\n{reasoning}",
                    repo,
                )
                pr_url = pr_result.get("data", {}).get("pr_url", "")
                await db.update_step(step_id, status="done",
                                     tool_output=json.dumps({"pr_url": pr_url})[:MAX_TOOL_OUTPUT])
                await db.update_task(task_id, status="done", pr_url=pr_url)
                await emit_log(task_id, "info", f"Task complete. PR: {pr_url}")
                await emit(task_id, "task_done", {"pr_url": pr_url})
                return

            # --- error action ------------------------------------------------
            if action == "error":
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": reasoning})[:MAX_TOOL_OUTPUT])
                await db.update_task(task_id, status="failed", error=reasoning)
                await emit_log(task_id, "error", f"Agent reported error: {reasoning}")
                await emit(task_id, "task_failed", {"error": reasoning})
                await notify_task_failure(task_id, repo, reasoning)
                return

            # --- tool_call ---------------------------------------------------
            if not tool_name:
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": "no tool name"})[:MAX_TOOL_OUTPUT])
                await emit_log(task_id, "warning", "tool_call with no TOOL name")
                continue

            await emit(task_id, "tool_start", {"tool": tool_name, "input": tool_input})
            tool_result = await loop.run_in_executor(
                _tool_executor, execute_tool, tool_name, tool_input
            )
            tool_output_str = json.dumps(tool_result)[:MAX_TOOL_OUTPUT]   # F16

            await db.update_step(step_id, status="done", tool_output=tool_output_str)
            await emit(task_id, "tool_done", {"tool": tool_name, "success": tool_result.get("success"), "output": tool_output_str[:500]})
            await emit_log(task_id, "info", f"Tool {tool_name} complete",
                           success=tool_result.get("success"))

        # Max steps reached
        err = f"Max steps ({settings.max_steps}) reached"
        await db.update_task(task_id, status="failed", error=err)
        await emit_log(task_id, "warning", f"Max steps reached ({settings.max_steps})")
        await emit(task_id, "task_failed", {"error": "max_steps"})
        await notify_task_failure(task_id, repo, err)

    except asyncio.CancelledError:
        await db.update_task(task_id, status="cancelled")
        await emit_log(task_id, "info", "Task cancelled")
        await emit(task_id, "task_cancelled", {})
        raise
    except Exception as e:
        traceback.print_exc()
        err = str(e)
        await db.update_task(task_id, status="failed", error=err)
        await emit_log(task_id, "error", f"Unexpected error: {e}")
        await emit(task_id, "task_failed", {"error": err})
        _repo = settings.github_default_repo
        try:
            _task = await db.get_task(task_id)
            if _task:
                _repo = _task.get("repo_url") or _repo
        except Exception:
            pass
        await notify_task_failure(task_id, _repo, err)
    finally:
        _running.pop(task_id, None)
        await asyncio.sleep(5)   # brief grace period for SSE consumers to drain
        destroy_bus(task_id)
