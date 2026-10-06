"""Pure mechanics of the agent tool loop — no I/O, no LLM, no database.

Everything here is trivially unit-testable; the orchestration lives in
agents/loop.py and persistence in agents/chat.py.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

# Guardrails (borrowed from the LitBoard agent loop, see
# docs/litboard-borrowing-plan.md P0-2)
MAX_STEPS = 12          # model turns per user message
TOOL_OUTPUT_LIMIT = 12_000   # chars per tool result fed back to the model
STUCK_WINDOW = 4        # tool calls examined for repetition
STUCK_REPEAT_LIMIT = 2  # identical calls within the window → stop


@dataclass
class ToolCall:
    """One parsed function call requested by the model."""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ""


def parse_tool_calls(raw_tool_calls: list | None) -> list[ToolCall]:
    """Defensively parse OpenAI-format tool_calls into ToolCall items.

    Fragmented or invalid JSON arguments degrade to an empty dict — a
    tool executor that needs the arguments reports the problem itself,
    which the model can then fix on the next turn.
    """
    calls: list[ToolCall] = []
    for tc in raw_tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args, raw_args = raw_args, json.dumps(raw_args)
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                args = {}
        else:
            raw_args, args = "", {}
        calls.append(
            ToolCall(
                id=tc.get("id") or uuid.uuid4().hex[:16],
                name=name,
                arguments=args,
                raw_arguments=raw_args,
            )
        )
    return calls


def call_signature(call: ToolCall) -> str:
    """Canonical identity of a call for repetition detection."""
    return (
        f"{call.name}:"
        + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    )


def is_stuck(recent_signatures: list[str]) -> bool:
    """True when any call signature repeats within the recent window —
    catches both re-issuing the same call and ping-pong loops (a,b,a,b)."""
    if len(recent_signatures) < STUCK_WINDOW:
        return False
    tail = recent_signatures[-STUCK_WINDOW:]
    return any(tail.count(s) >= STUCK_REPEAT_LIMIT for s in set(tail))


def truncate_output(text: str) -> tuple[str, bool]:
    """Cap a tool result; returns (text, truncated)."""
    if len(text) <= TOOL_OUTPUT_LIMIT:
        return text, False
    return (
        text[:TOOL_OUTPUT_LIMIT]
        + f"\n…[truncated at {TOOL_OUTPUT_LIMIT} chars — narrow the request]",
        True,
    )


def build_api_messages(rows: list[dict]) -> list[dict]:
    """Map database message rows to a valid OpenAI message sequence.

    - assistant rows carry their serialized tool_calls when present;
    - role='tool' rows become tool messages keyed by tool_call_id;
    - tool rows whose call is not pending (orphaned by earlier trimming
      or interrupted turns) are dropped so the sequence stays valid.
    """
    out: list[dict] = []
    pending_ids: set[str] = set()
    for r in rows:
        role = r.get("role")
        if role == "user":
            out.append({"role": "user", "content": r.get("content", "")})
        elif role == "assistant":
            msg = {"role": "assistant", "content": r.get("content") or ""}
            calls_json = r.get("tool_calls")
            if calls_json:
                try:
                    calls = json.loads(calls_json)
                except (json.JSONDecodeError, TypeError):
                    calls = []
                if calls:
                    msg["tool_calls"] = calls
                    pending_ids = {c.get("id") for c in calls if c.get("id")}
            out.append(msg)
        elif role == "tool":
            cid = r.get("tool_call_id")
            if cid and cid in pending_ids:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": r.get("content", ""),
                    }
                )
                pending_ids.discard(cid)
    return out
