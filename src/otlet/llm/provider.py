"""Unified LLM interface using litellm for provider-agnostic access."""

from __future__ import annotations

import os
from collections.abc import Generator

import litellm


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
        response = litellm.completion(
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
        response = litellm.completion(
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
