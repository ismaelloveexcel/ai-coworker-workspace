"""
Claude wrapper - strict schema parsing, single correction attempt.

v2: removed @retry from run_agent_turn to prevent nested retry storms (F5).
    Fixed INPUT regex to handle deeply-nested JSON objects (F15).
    One correction attempt per turn; caller decides what to do on failure.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from backend.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

with open("CLAUDE.md", "r") as f:
    _SYSTEM_PROMPT = f.read()

MAX_HISTORY_STEPS = 10
MAX_TOKENS = 4096


# -- Context builder -----------------------------------------------------------

def build_task_context(task: Dict, steps: List[Dict]) -> str:
    recent = steps[-MAX_HISTORY_STEPS:]
    history = []
    for s in recent:
        history.append(
            f"[Step {s['step_num']}] Tool={s.get('tool_name','?')} "
            f"Status={s['status']}\n"
            f"  Input: {s.get('tool_input','')[:200]}\n"
            f"  Output: {s.get('tool_output','')[:500]}"
        )
    return f"""Task ID: {task['id']}
Title: {task['title']}
Repo: {task.get('repo_url', settings.github_default_repo)}
Status: {task['status']}

=== Step History ===
{chr(10).join(history) if history else 'No steps yet.'}

=== Objective ===
{task['prompt']}

What should the agent do next?"""


# -- JSON extraction -----------------------------------------------------------

def _extract_json_object(text: str) -> Optional[str]:
    """
    Find the first balanced { ... } block in text, handling arbitrary nesting.
    Returns the raw JSON string, or None if not found.
    This replaces the naive r'{.*?}' regex which truncated at the first }.
    """
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


# -- Schema parser -------------------------------------------------------------

def parse_action(text: str) -> Dict[str, Any]:
    """
    Parse the strict PLAN/ACTION/TOOL/INPUT/REASONING format.
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

    # INPUT - use brace-balanced extractor instead of naive regex (fixes F15)
    input_section = re.search(r"INPUT:\s*", text)
    if input_section:
        candidate = _extract_json_object(text[input_section.end():])
        if candidate:
            try:
                result["input"] = json.loads(candidate)
            except json.JSONDecodeError:
                raise ValueError(f"INPUT is not valid JSON: {candidate[:200]}")
        else:
            result["input"] = {}
    else:
        result["input"] = {}

    # REASONING
    reasoning_match = re.search(r"REASONING:\s*(.+?)(?=\nPLAN:|\nACTION:|\nTOOL:|\nINPUT:|$)", text, re.DOTALL)
    result["reasoning"] = reasoning_match.group(1).strip() if reasoning_match else ""

    return result


# -- Agent turn ----------------------------------------------------------------
# NOTE: No @retry here. Tenacity wraps retries at the API-error level only.
# Correction logic is a single pass: if Claude's output is malformed, send one
# correction request. If that also fails, raise MalformedOutputError so the
# caller (agent_loop) can mark the step failed cleanly. This prevents the
# 18x Claude-call storm from nested @retry + correction loops (F5/F6).

class MalformedOutputError(ValueError):
    """Raised when Claude's output is malformed after one correction attempt."""


def run_agent_turn(messages: List[Dict]) -> Tuple[str, Dict, Dict]:
    """
    Call Claude, parse response. One correction attempt on malformed output.
    Returns (raw_text, parsed_action, usage) where usage has input_tokens /
    output_tokens (summed across both calls when a correction is made).
    Raises MalformedOutputError if still malformed after correction.
    Raises anthropic.APIError on API-level failures (let caller retry if needed).
    """
    response = _client.messages.create(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    raw = response.content[0].text

    try:
        parsed = parse_action(raw)
        return raw, parsed, usage
    except ValueError as first_err:
        # One correction attempt
        correction_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your response was not in the required format. Error: {first_err}\n\n"
                    "Please reformat your response strictly following the PLAN/ACTION/TOOL/INPUT/REASONING schema."
                ),
            },
        ]
        correction = _client.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=correction_messages,
        )
        # Accumulate token usage from the correction call
        usage["input_tokens"] += correction.usage.input_tokens
        usage["output_tokens"] += correction.usage.output_tokens
        raw = correction.content[0].text
        try:
            parsed = parse_action(raw)
            return raw, parsed, usage
        except ValueError as second_err:
            raise MalformedOutputError(
                f"Agent produced malformed output after correction attempt. "
                f"Original error: {first_err}. Correction error: {second_err}"
            ) from second_err
