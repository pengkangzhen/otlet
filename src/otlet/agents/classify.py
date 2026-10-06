"""Classify Agent — use LLM to suggest tags for a paper."""

from __future__ import annotations

from otlet.agents.base import AgentBase
from otlet.llm.provider import LLMProvider
from otlet.models.paper import Paper

_SYSTEM_PROMPT = """\
You are an academic paper classifier. Given a paper's title, abstract, and \
metadata, suggest 3-6 concise tags that describe its research topic, \
methodology, and domain.

Rules:
- Tags should be short (1-3 words each)
- Use lowercase with hyphens for multi-word tags (e.g., "computer-vision")
- Include both topic and method tags
- Return ONLY a JSON array of strings, nothing else
- Example: ["reinforcement-learning", "robotics", "policy-gradient", "simulation"]
"""


class ClassifyAgent(AgentBase):
    """Agent responsible for classifying and tagging papers using LLM."""

    name = "classify"

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def run(self, paper: Paper) -> list[str]:  # type: ignore[override]
        """Suggest tags/labels for a given paper.

        Args:
            paper: The paper to classify.

        Returns:
            List of suggested tags.
        """
        user_msg = self._build_user_message(paper)
        response = self._llm.chat(
            messages=[{"role": "user", "content": user_msg}],
            system=_SYSTEM_PROMPT,
            max_tokens=256,
            temperature=0.3,
        )
        return self._parse_tags(response)

    @staticmethod
    def _build_user_message(paper: Paper) -> str:
        parts = [f"Title: {paper.title}"]
        if paper.abstract:
            parts.append(f"Abstract: {paper.abstract}")
        if paper.venue:
            parts.append(f"Venue: {paper.venue}")
        if paper.year:
            parts.append(f"Year: {paper.year}")
        if paper.keywords:
            parts.append(f"Keywords: {', '.join(paper.keywords)}")
        return "\n".join(parts)

    @staticmethod
    def _parse_tags(response: str) -> list[str]:
        """Parse the LLM response into a list of tags."""
        import json

        response = response.strip()
        # Try to extract JSON array from the response
        try:
            tags = json.loads(response)
            if isinstance(tags, list):
                return [str(t).strip().lower() for t in tags if t]
        except json.JSONDecodeError:
            pass

        # Fallback: try to find array-like content
        start = response.find("[")
        end = response.rfind("]")
        if start != -1 and end != -1:
            try:
                tags = json.loads(response[start : end + 1])
                if isinstance(tags, list):
                    return [str(t).strip().lower() for t in tags if t]
            except json.JSONDecodeError:
                pass

        # Last resort: split by commas
        return [t.strip().strip('"').lower() for t in response.split(",") if t.strip()]
