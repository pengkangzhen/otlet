"""Chat Agent — converse with a paper using LLM + PDF context."""

from __future__ import annotations

from collections.abc import Generator

from agent_lit.agents.base import AgentBase
from agent_lit.llm.provider import LLMProvider
from agent_lit.storage.pdf_store import PDFStore

_SYSTEM_PROMPT = """\
You are a helpful research assistant. The user is reading an academic paper \
and wants to understand it better. Use the paper metadata, the user's own \
notes, and the content provided below to answer their questions.

Guidelines:
- Answer in the same language as the user's question
- Be specific and reference relevant sections/equations/figures
- If the question goes beyond the paper's scope, say so honestly
- Help the user learn the paper's methods and contributions
- Use markdown formatting for clarity
- The user's notes reflect their research interests — connect answers to \
them when relevant

--- PAPER METADATA ---
Title: {title}
Authors: {authors}
Year: {year}
Venue: {venue}
DOI: {doi}
--- END METADATA ---

--- USER NOTES ---
{notes}
--- END USER NOTES ---

--- PAPER CONTENT ---
{paper_text}
--- END PAPER CONTENT ---
"""


class ChatAgent(AgentBase):
    """Agent for conversing about a paper's content."""

    name = "chat"

    def __init__(self, llm: LLMProvider, pdf_store: PDFStore) -> None:
        self._llm = llm
        self._pdf_store = pdf_store
        self._db = None  # Optional: set externally for metadata access

    def run(  # type: ignore[override]
        self,
        paper_id: str,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a message about a paper and get a response."""
        system = self._build_system(paper_id)
        return self._llm.chat(
            messages=messages,
            system=system,
            max_tokens=2048,
            temperature=0.5,
        )

    def stream(
        self,
        paper_id: str,
        messages: list[dict[str, str]],
    ) -> Generator[str, None, None]:
        """Stream a response about a paper token by token."""
        system = self._build_system(paper_id)
        yield from self._llm.chat_stream(
            messages=messages,
            system=system,
            max_tokens=2048,
            temperature=0.5,
        )

    def _build_system(self, paper_id: str) -> str:
        """Build the system prompt with paper metadata + notes + content."""
        paper_text = self._pdf_store.extract_text(paper_id) or ""
        # Get metadata and notes from DB if available
        title = authors = year = venue = doi = ""
        notes = "(No notes yet)"
        if self._db:
            paper = self._db.get_paper(paper_id)
            if paper:
                title = paper.title
                authors = paper.display_authors
                year = str(paper.year or "")
                venue = paper.venue or ""
                doi = paper.doi or ""
            note_rows = self._db.list_notes(paper_id)
            if note_rows:
                notes = "\n\n".join(
                    f"[{r['created_at'][:10]}] {r['content']}" for r in note_rows
                )
        return _SYSTEM_PROMPT.format(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            notes=notes,
            paper_text=paper_text[:12000] if paper_text else "(No PDF available)",
        )
