"""Unified LLM interface using litellm for provider-agnostic access."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from functools import cache


@cache
def _litellm():
    """Import litellm lazily, pinned to its local model cost map.

    Importing litellm fetches a remote model price list, which blocks
    ~20s and logs a warning when offline. otlet never reads that map
    (it only routes chat completions), so force the bundled backup via
    LITELLM_LOCAL_MODEL_COST_MAP before the import; deferring the import
    itself keeps every non-LLM command and UI startup free of it.
    """
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


class LLMProvider:
    """Thin wrapper around litellm for chat completions with streaming."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> None:
        self.model = model or os.getenv("OTLET_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.getenv("OTLET_API_KEY")
        self.api_base = api_base or os.getenv("OTLET_API_BASE")

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> str:
        """Send a chat completion request and return the full response."""
        full_messages = self._build_messages(messages, system)
        kwargs = self._base_kwargs()
        response = _litellm().completion(
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )
        return response.choices[0].message.content or ""

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Generator[str, None, None]:
        """Stream chat completion tokens one by one."""
        full_messages = self._build_messages(messages, system)
        kwargs = self._base_kwargs()
        response = _litellm().completion(
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            **kwargs,
        )
        for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def chat_tools_stream(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.5,
    ) -> Generator[dict, None, None]:
        """Streamed chat completion with function-calling support.

        Yields {"type": "delta", "text": str} as content arrives, then a
        single final {"type": "message", "content": str, "tool_calls":
        [...]} in OpenAI shape (tool_calls may be []). Providers that
        reject streaming with tools fall back to one non-streaming call
        (the whole content then arrives as one delta). A failure after
        text has already streamed is re-raised — the caller has shown
        partial output and must handle it.
        """
        full_messages = self._build_messages(messages, system)
        common = {"max_tokens": max_tokens, "temperature": temperature}
        common.update(self._base_kwargs())
        if tools:
            common["tools"] = tools

        streamed = False
        try:
            response = _litellm().completion(
                messages=full_messages, stream=True, **common
            )
            content: list[str] = []
            calls: dict[int, dict] = {}
            for chunk in response:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    streamed = True
                    content.append(piece)
                    yield {"type": "delta", "text": piece}
                for tc in getattr(delta, "tool_calls", None) or []:
                    streamed = True
                    slot = calls.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    fn = tc.function
                    if fn is not None:
                        if fn.name:
                            slot["name"] = slot["name"] or fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
            yield {
                "type": "message",
                "content": "".join(content),
                "tool_calls": [
                    {
                        "id": slot["id"] or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"],
                        },
                    }
                    for i, slot in sorted(calls.items())
                    if slot["name"]
                ],
            }
        except Exception:
            if streamed:
                raise
            yield from self._chat_tools_sync(full_messages, common)

    def _chat_tools_sync(
        self, full_messages: list[dict], common: dict
    ) -> Generator[dict, None, None]:
        """Non-streaming fallback for streaming-with-tools rejection."""
        response = _litellm().completion(
            messages=full_messages, stream=False, **common
        )
        message = response.choices[0].message
        text = message.content or ""
        raw_calls = getattr(message, "tool_calls", None) or []
        tool_calls = []
        for tc in raw_calls:
            fn = tc.function
            args = fn.arguments
            if not isinstance(args, str):
                args = json.dumps(args or {})
            tool_calls.append(
                {
                    "id": tc.id or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {"name": fn.name, "arguments": args},
                }
            )
        if text:
            yield {"type": "delta", "text": text}
        yield {"type": "message", "content": text, "tool_calls": tool_calls}

    def _build_messages(
        self,
        messages: list[dict[str, str]],
        system: str | None,
    ) -> list[dict[str, str]]:
        result = []
        if system:
            result.append({"role": "system", "content": system})
        result.extend(messages)
        return result

    def _base_kwargs(self) -> dict:
        model = self.model
        if self.api_base and "/" not in model:
            # litellm routes bare model names to a custom OpenAI-compatible
            # endpoint (Ollama, LM Studio, vLLM...) only with the openai/ prefix
            model = f"openai/{model}"
        kwargs: dict = {"model": model}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif self.api_base:
            kwargs["api_key"] = "not-needed"  # local endpoints don't check keys
        if self.api_base:
            kwargs["api_base"] = self.api_base
        return kwargs
