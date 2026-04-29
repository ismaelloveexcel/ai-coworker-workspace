"""
Claude wrapper — strict schema parsing, retry on malformed output.
System prompt cached at module load.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

with open("CLAUDE.md", "r") as f:
    _SYSTEM_PROMPT = f.read()

MAX_HISTORY_STEPS = 10
MAX_TOKENS = 4096


# ── Context builder ────────────────────────────────────────────────────────────

def build_task_context(task: Dict, steps: List[Dict]) -> str:
    recent = steps[-MAX_HISTORY_STEPS:]
    history = []
    for s in recent:
        history.append(
            f"[Step {s['step_num']}] Tool={s.get('tool_name','?')} "
            f"Status={s['status']}\n"
            f"  Input: {s.get('tool_input','')}\n"
            f"  Output: {s.get('tool_output','')}"
        )
    return (
        f"TASK ID: {task['id']}\n"
        f"TITLE: {task['title']}\n"
        f"PROMPT: {task['prompt']}\n"
        f"REPO: {task.get('repo_url','')}\n"
        f"BRANCH: {task.get('branch_name','')}\n"
        f"STATUS: {task['status']}\n\n"
        f"STEP HISTORY (last {len(recent)}):\n" + "\n".join(history)
    )


# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_action(text: str) -> Dict[str, Any]:
    """
    Parse strict format:
      PLAN:\n- ...\nACTION: ...\nTOOL: ...\nINPUT: {...}\nREASONING: ...
    Raises ValueError on malformed output.
    """
    result: Dict[str, Any] = {}

    # PLAN
    plan_match = re.search(r"PLAN:\s*\n((?:\s*-[^\n]+\n?)+)", text)
    if not plan_match:
        raise ValueError("Missing PLAN section")
    result["plan"] = plan_match.group(1).strip()

    # ACTION
    action_match = re.search(r"ACTION:\s*(tool_call|final_answer|error)", text)
    if not action_match:
        raise ValueError("Missing or invalid ACTION")
    result["action"] = action_match.group(1).strip()

    # TOOL
    tool_match = re.search(r"TOOL:\s*(\S+)", text)
    result["tool"] = tool_match.group(1).strip() if tool_match else None

    # INPUT
    input_match = re.search(r"INPUT:\s*(\{.*?\})", text, re.DOTALL)
    if input_match:
        try:
            result["input"] = json.loads(input_match.group(1))
        except json.JSONDecodeError:
            raise ValueError(f"INPUT is not valid JSON: {input_match.group(1)[:200]}")
    else:
        result["input"] = {}

    # REASONING
    reasoning_match = re.search(r"REASONING:\s*(.+?)(?:\n[A-Z]+:|$)", text, re.DOTALL)
    result["reasoning"] = reasoning_match.group(1).strip() if reasoning_match else ""

    return result


# ── Main agent turn ────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
def run_agent_turn(messages: List[Dict]) -> Tuple[str, Dict]:
    """
    Call Claude, parse response.
    Returns (raw_text, parsed_action).
    Retries up to 3x on API errors.
    """
    response = _client.messages.create(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )
    raw = response.content[0].text

    # Try parse — retry with correction if malformed (up to 2 attempts)
    for attempt in range(2):
        try:
            parsed = parse_action(raw)
            return raw, parsed
        except ValueError as e:
            if attempt == 0:
                # Send correction message
                correction_messages = messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            f"Your response was not in the required format. Error: {e}\n"
                            "Please respond EXACTLY in the required format with PLAN, ACTION, TOOL, INPUT, REASONING."
                        ),
                    },
                ]
                correction = _client.messages.create(
                    model=settings.model,
                    max_tokens=MAX_TOKENS,
                    system=_SYSTEM_PROMPT,
                    messages=correction_messages,
                )
                raw = correction.content[0].text
            else:
                raise ValueError(f"Agent produced malformed output after correction: {e}")

    raise ValueError("Failed to get valid output")
