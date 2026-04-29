"""
Agent loop — core engine.
Fully event-driven. DB is single source of truth.
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
from backend.tool_adapters import execute_tool

log = structlog.get_logger()


async def run_task(task_id: str) -> None:
    """Main agent loop. Runs until done, failed, or max steps."""
    task = await db.get_task(task_id)
    if not task:
        return

    await db.update_task(task_id, status="running")
    await emit_status(task_id, "running")
    await emit_log(task_id, "info", f"Task started: {task['title']}")

    messages: List[Dict] = []
    step_count = 0
    correction_attempts = 0

    try:
        # Step 0: Create branch
        await emit_log(task_id, "info", "Creating branch...")
        from backend.tool_adapters import github_create_branch
        branch_result = github_create_branch(task_id)
        if not branch_result["success"]:
            raise RuntimeError(f"Branch creation failed: {branch_result['error']}")

        branch_name = branch_result["data"]["branch"]
        await db.update_task(task_id, branch_name=branch_name)
        task = await db.get_task(task_id)
        await emit_log(task_id, "info", f"Branch ready: {branch_name}")

        # Initial user message
        context = build_task_context(task, [])
        messages = [{"role": "user", "content": context}]

        while step_count < settings.max_steps_per_task:
            step_count += 1
            await emit_log(task_id, "info", f"=== Step {step_count} ===")

            # Refresh task from DB
            task = await db.get_task(task_id)
            if task["status"] == "cancelled":
                await emit_log(task_id, "warn", "Task cancelled")
                return

            # Rebuild context
            steps = await db.get_steps(task_id)
            context = build_task_context(task, steps)
            if step_count > 1:
                messages = [{"role": "user", "content": context}]

            # Create step record
            step_id = await db.create_step(task_id, step_count)

            try:
                # Call Claude (sync, run in executor to not block event loop)
                loop = asyncio.get_event_loop()
                raw_text, parsed = await asyncio.wait_for(
                    loop.run_in_executor(None, run_agent_turn, messages),
                    timeout=settings.step_timeout_seconds,
                )
                correction_attempts = 0
            except asyncio.TimeoutError:
                await emit_log(task_id, "error", f"Step {step_count} timed out")
                await db.update_step(step_id, status="failed", tool_output='{"error":"timeout"}')
                await db.add_log(task_id, "error", f"Step {step_count} timed out")
                step_count -= 1  # Don't count timeout as progress
                continue
            except ValueError as e:
                # Malformed output
                correction_attempts += 1
                await emit_log(task_id, "warn", f"Malformed output (attempt {correction_attempts}): {e}")
                if correction_attempts >= 3:
                    raise RuntimeError(f"Repeated malformed output: {e}")
                await db.update_step(step_id, status="failed", tool_output=json.dumps({"error": str(e)}))
                continue

            action = parsed.get("action", "error")
            tool_name = parsed.get("tool")
            tool_input = parsed.get("input", {})
            plan = parsed.get("plan", "")
            reasoning = parsed.get("reasoning", "")

            await db.update_step(
                step_id,
                plan=plan,
                tool_name=tool_name,
                tool_input=json.dumps(tool_input),
                reasoning=reasoning,
            )
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

            # ── Error from Claude ──────────────────────────────────────────────
            if action == "error":
                await emit_log(task_id, "error", f"Agent reported error: {reasoning}")
                await db.update_step(step_id, status="failed",
                                     tool_output=json.dumps({"error": reasoning}))
                raise RuntimeError(f"Agent error: {reasoning}")

            # ── Execute tool ───────────────────────────────────────────────────
            if not tool_name:
                await emit_log(task_id, "warn", "No tool name in action — skipping")
                continue

            await emit_log(task_id, "info", f"Executing: {tool_name}")
            tool_result = await asyncio.get_event_loop().run_in_executor(
                None, execute_tool, tool_name, tool_input, task_id
            )
            tool_output_str = json.dumps(tool_result)

            await db.update_step(
                step_id,
                status="done" if tool_result.get("success") else "failed",
                tool_output=tool_output_str,
            )
            await emit_step(task_id, step_count, tool_name,
                            "done" if tool_result.get("success") else "failed",
                            output=tool_result)
            await db.add_log(task_id, "info",
                             f"Tool {tool_name}: {'ok' if tool_result.get('success') else 'failed'}",
                             meta=tool_output_str[:500])

            # Append result to messages for next turn
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({
                "role": "user",
                "content": f"Tool result for {tool_name}:\n{tool_output_str}\n\nContinue."
            })

            # Check if PR created
            if tool_name == "github_create_pr" and tool_result.get("success"):
                pr_url = tool_result["data"].get("pr_url", "")
                await db.update_task(task_id, pr_url=pr_url)

        # Max steps reached
        raise RuntimeError(f"Max steps ({settings.max_steps_per_task}) reached without completion")

    except Exception as e:
        err_msg = str(e)
        tb = traceback.format_exc()
        await db.update_task(task_id, status="failed")
        await emit_status(task_id, "failed", error=err_msg)
        await emit_log(task_id, "error", f"Task failed: {err_msg}")
        await db.add_log(task_id, "error", f"FATAL: {err_msg}", meta=tb[:1000])
        log.error("task_failed", task_id=task_id, error=err_msg)
