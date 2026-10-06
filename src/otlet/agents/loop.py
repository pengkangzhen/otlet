"""Tool-loop orchestration: chat → persist → execute tools → repeat.

The runner owns the model turns and the guardrails (max steps, stuck
detection, output truncation, cancellation); persistence is delegated
through the on_persist callback so the loop stays free of I/O and can
run against a fake LLM in tests. Discipline borrowed from LitBoard:
the assistant message is persisted BEFORE its tool calls execute, so a
crash never leaves an executed-but-unrecorded tool run.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from otlet.agents.core import (
    MAX_STEPS,
    ToolCall,
    call_signature,
    is_stuck,
    parse_tool_calls,
    truncate_output,
)
from otlet.agents.tools import ToolSpec, openai_schema
from otlet.llm.provider import LLMProvider

# End reasons surfaced to the UI
END_DONE = "done"
END_MAX_STEPS = "max_steps"
END_STUCK = "stuck"
END_STOPPED = "stopped"

_STOPPED_RESULT = "Stopped: the user cancelled this operation."
_STUCK_RESULT = (
    "This exact tool call was already made repeatedly without progress. "
    "Stop calling tools and answer with what you already have."
)


def run_turn(
    llm: LLMProvider,
    tools: dict[str, ToolSpec],
    system: str,
    history: list[dict],
    *,
    on_persist: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
):
    """Drive the model until it answers without requesting tools.

    Yields {"type": "delta", "text": str} as answer text streams, then a
    single final {"type": "result", "answer": str, "end_reason": str}.
    """
    messages = list(history)
    recent_signatures: list[str] = []

    for _ in range(MAX_STEPS):
        final: dict | None = None
        for event in llm.chat_tools_stream(
            messages, system=system, tools=openai_schema(tools)
        ):
            if event["type"] == "delta":
                yield event
            else:
                final = event
        if final is None:  # pragma: no cover - defensive
            yield {"type": "result", "answer": "", "end_reason": END_MAX_STEPS}
            return

        content = final["content"]
        raw_calls = final.get("tool_calls") or []
        calls = parse_tool_calls(raw_calls)

        # Persist the assistant turn before any tool runs (crash safety)
        if on_persist:
            on_persist({
                "kind": "assistant",
                "content": content,
                "tool_calls": raw_calls if calls else None,
            })
        messages.append({
            "role": "assistant",
            "content": content,
            **({"tool_calls": raw_calls} if calls else {}),
        })

        if not calls:
            yield {
                "type": "result",
                "answer": content,
                "end_reason": END_DONE,
            }
            return

        for call in calls:
            # surface the activity before the (possibly slow) execution
            yield {
                "type": "tool",
                "name": call.name,
                "detail": _brief(call.name, call.arguments),
            }
            result, end_reason = _execute_call(
                call, tools, recent_signatures, should_stop
            )
            result, _truncated = truncate_output(result)
            if on_persist:
                on_persist({
                    "kind": "tool",
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "content": result,
                })
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })
            if end_reason:
                yield {
                    "type": "result",
                    "answer": content,
                    "end_reason": end_reason,
                }
                return

    yield {"type": "result", "answer": "", "end_reason": END_MAX_STEPS}


def _brief(name: str, args: dict) -> str:
    """One-line human summary of a tool call for status displays."""
    if name == "read_pdf_pages":
        frm = args.get("from_page", 1)
        to = args.get("to_page")
        rng = f"第{frm}–{to}页" if to else f"第{frm}页起"
        if args.get("from_char"):
            rng += f" (from char {args['from_char']})"
        return rng
    if name == "find_literature":
        claims = args.get("claims") or []
        first = str(claims[0])[:40] if claims else "?"
        more = f" 等{len(claims)}条" if len(claims) > 1 else ""
        return f"“{first}”{more}"
    if name == "search_library":
        return f"“{str(args.get('query', ''))[:40]}”"
    return json.dumps(args, ensure_ascii=False)[:60]


def _execute_call(
    call: ToolCall,
    tools: dict[str, ToolSpec],
    recent_signatures: list[str],
    should_stop: Callable[[], bool] | None,
) -> tuple[str, str | None]:
    """Run one tool call. Returns (result_json, end_reason_or_None)."""
    recent_signatures.append(call_signature(call))

    if should_stop is not None and should_stop():
        # The cancelled call is still persisted as a tool result so the
        # tool_calls/tool pairing in the message history stays valid
        return _STOPPED_RESULT, END_STOPPED
    if is_stuck(recent_signatures):
        return _STUCK_RESULT, END_STUCK

    spec = tools.get(call.name)
    if spec is None:
        return json.dumps({"error": f"unknown tool: {call.name}"}), None
    try:
        return spec.execute(call.arguments), None
    except Exception as e:  # tool failures are answerable, not fatal
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), None
