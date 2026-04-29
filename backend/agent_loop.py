"""
Agent loop — core engine.
Fully event-driven. DB is single source of truth.

v2 fixes:
- Removed asyncio.wait_for wrapping run_in_executor (caused infinite retry loop
  when Claude API was slow: timeout fires, step_count decrements, retries forever)
- Let Anthropic client handle its own timeout (default 600s)
- Cleaned up message flow (context rebuilt from DB each step — no stale appends)
- Stricter correction attempt tracking per step, not globally

v3 fixes:
- Re-introduced a per-step asyncio.wait_for around the Claude call, but this
  time it does NOT decrement step_count and does NOT loop — on timeout the
  step is marked failed and the task fails fast. Without this, a single hung
  Claude call could burn the entire workflow's timeout-minutes budget.
"""
import asyncio
import json
import traceback
from typing import Dict, List

import structlog

from backend import db
from backend.claude_wrapper import build_task_context, run_agent_turn
from backend.config import settings
from backend.events import emit_log, emit_status, emit_step
from backend.tool_adapters import execute_tool, github_create_branch

log = structlog.get_logger()


async def run_task(task_id: str) -> None:
    """Main agent loop. Runs until done, failed, or max steps."""
    task = await db.get_task(task_id)
    if not task:
        return

    await db.update_task(task_id, status="running")
    await emit_status(task_id, "running")
    await emit_log(task_id, "info", f"Task started: {task['title']}")

    step_count = 0
    messages: List[Dict] = []

    try:
        # ── Step 0: Create branch ──────────────────────────────────────────────
        await emit_log(task_id, "info", "Creating branch...")
        loop = asyncio.get_event_loop()
        branch_result = await loop.run_in_executor(None, github_create_branch, task_id)

        if not branch_result["success"]:
            raise RuntimeError(f"Branch creation failed: {branch_result['error']}")

        branch_name = branch_result["data"]["branch"]
        await db.update_task(task_id, branch_name=branch_name)
        task = await db.get_task(task_id)
        await emit_log(task_id, "info", f"Branch ready: {branch_name}")

        # ── Agent loop ─────────────────────────────────────────────────────────
        while step_count < settings.max_steps_per_task:
            step_count += 1

            # Check for cancellation
            task = await db.get_task(task_id)
            if task["status"] == "cancelled":
                await emit_log(task_id, "warn", "Task cancelled")
                return

            await emit_log(task_id, "info", f"=== Step {step_count}/{settings.max_steps_per_task} ===")

            # Rebuild context from DB (source of truth)
            steps   = await db.get_steps(task_id)
            context = build_task_context(task, steps)
            messages = [{"role": "user", "content": context}]

            step_id = await db.create_step(task_id, step_count)
            correction_attempts = 0  # reset per step

            # ── Call Claude (allow up to 2 corrections per step) ──────────────
            raw_text, parsed = None, None
            while correction_attempts <= 2:
                try:
                    raw_text, parsed = await asyncio.wait_for(
                        loop.run_in_executor(None, run_agent_turn, messages),
                        timeout=settings.step_timeout_seconds,
                    )
                    break  # success
                except asyncio.TimeoutError:
                    # Single Claude turn exceeded its budget. Do NOT retry or
                    # decrement step_count — fail the step and let the task
                    # fail fast so the job doesn't hang for hours.
                    #
                    # Note: asyncio.wait_for cancels the awaitable but cannot
                    # actually kill the underlying executor thread; the HTTP
                    # call will continue until the Anthropic SDK's own timeout
                    # (default 600s) fires. That's acceptable here because the
                    # task is failing and the workflow process will exit
                    # shortly after.
                    raise RuntimeError(
                        f"Claude call timed out after {settings.step_timeout_seconds}s "
                        f"on step {step_count}"
                    )
                except ValueError as e:
                    correction_attempts += 1
                    await emit_log(task_id, "warn",
                                   f"Malformed output (correction {correction_attempts}/2): {e}")
                    if correction_attempts > 2:
                        raise RuntimeError(f"Agent failed to produce valid output after corrections: {e}")
                    # Feed correction back
                    messages = messages + [
                        {"role": "assistant", "content": raw_text or ""},
                        {"role": "user", "content": (
                            f"Your output was not in the required format. Error: {e}\n"
                            "Respond EXACTLY using PLAN / ACTION / TOOL / INPUT / REASONING format."
                        )},
                    ]
                except Exception as e:
                    raise RuntimeError(f"Claude API error: {e}") from e

            action    = parsed.get("action", "error")
            tool_name = parsed.get("tool")
            tool_input = parsed.get("input", {})
            plan      = parsed.get("plan", "")
            reasoning = parsed.get("reasoning", "")

            await db.update_step(step_id, plan=plan, tool_name=tool_name,
                                  tool_input=json.dumps(tool_input), reasoning=reasoning)
            await emit_step(task_id, step_count, tool_name or action, "running",
                            plan=plan, reasoning=reasoning)
            await emit_log(task_id, "info", f"Action={action} Tool={tool_name}")

            # ── Final answer ───────────────────────────────────────────────────
            if action == "final_answer":
                pr_url = tool_input.get("pr_url") or tool_input.get("url", "")
                await db.update_task(task_id, status="done", pr_url=pr_url)
                await db.update_step(step_id, status="done",
                                     tool_output=json.dumps({"pr_url": pr_url}))
                await emit_status(task_id, "done", pr_url=pr_url)
                await emit_log(task_id, "info", f"✓ Done. PR: {pr_url}")
                return

            # ── Agent error ────────────────────────────────────────────────────
            if action == "error":
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": reasoning}))
                raise RuntimeError(f"Agent reported error: {reasoning}")

            # ── Execute tool ───────────────────────────────────────────────────
            if not tool_name:
                await emit_log(task_id, "warn", "No tool name — skipping step")
                await db.update_step(step_id, status="failed",
                                     tool_output='{"error":"no tool name"}')
                continue

            await emit_log(task_id, "info", f"Executing: {tool_name}")
            tool_result = await loop.run_in_executor(
                None, execute_tool, tool_name, tool_input, task_id
            )
            tool_output_str = json.dumps(tool_result)

            step_ok = tool_result.get("success", False)
            await db.update_step(step_id,
                                  status="done" if step_ok else "failed",
                                  tool_output=tool_output_str)
            await emit_step(task_id, step_count, tool_name,
                            "done" if step_ok else "failed", output=tool_result)
            await db.add_log(task_id, "info" if step_ok else "warn",
                             f"Tool {tool_name}: {'ok' if step_ok else 'FAILED'}",
                             meta=tool_output_str[:500])

            if not step_ok:
                await emit_log(task_id, "warn",
                               f"Tool {tool_name} failed: {tool_result.get('error','?')}")

            # Update PR url if just created
            if tool_name == "github_create_pr" and step_ok:
                pr_url = tool_result["data"].get("pr_url", "")
                if pr_url:
                    await db.update_task(task_id, pr_url=pr_url)

        # Max steps hit
        raise RuntimeError(
            f"Max steps ({settings.max_steps_per_task}) reached without completion"
        )

    except Exception as e:
        err_msg = str(e)
        tb      = traceback.format_exc()
        await db.update_task(task_id, status="failed")
        await emit_status(task_id, "failed", error=err_msg)
        await emit_log(task_id, "error", f"Task failed: {err_msg}")
        await db.add_log(task_id, "error", f"FATAL: {err_msg}", meta=tb[:1000])
        log.error("task_failed", task_id=task_id, error=err_msg)
