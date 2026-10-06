"""Chat Agent — converse with a paper via a tool-calling loop.

The model reads the PDF itself through read_pdf_pages (bounded pages
per call, coverage notes) and searches the library through
search_library, instead of receiving a truncated dump of the full
text. Honesty is enforced by the system prompt: the model must state
which pages it actually read and never claim to have read the whole
paper when it has not.
"""

from __future__ import annotations

import json
from collections.abc import Generator

from otlet.agents.base import AgentBase
from otlet.agents.core import build_api_messages
from otlet.agents.loop import (
    END_MAX_STEPS,
    END_STOPPED,
    END_STUCK,
    run_turn,
)
from otlet.agents.openalex import OpenAlexClient
from otlet.agents.tools import build_tools
from otlet.llm.provider import LLMProvider
from otlet.storage.database import Database
from otlet.storage.pdf_store import PDFStore

_SYSTEM_PROMPT = """\
You are a helpful research assistant. The user is reading an academic
paper and wants to understand it better.

Tools:
- read_pdf_pages — read specific pages of THIS paper (up to 8 pages /
  10000 chars per call; follow the returned next field to continue)
- search_library — search the full text of every indexed PDF in the
  library, returning pages with snippets
- find_literature — verify whether published literature supports
  given claims; verdicts (supports/partial/none) come from fixed code
  rules, not from you

Guidelines:
- Answer in the same language as the user's question
- For questions about the paper's content, read the relevant pages with
  read_pdf_pages FIRST, then answer citing what you read
- Always state the page range you actually read (e.g. "第3–5页" /
  "pages 3-5"); never claim to have read the whole paper unless you did
- Use search_library when the question involves other papers in the
  library, and name which papers matched
- When the user asks whether literature supports a claim ("有没有文献
  支持X"), you MUST call find_literature — never stitch search results
  yourself and claim support. Report the returned verdicts as they
  are: presenting partial/none results as "有文献支持" is fabricating
  evidence, absolutely forbidden. "not_found" means not determined,
  not disproved — say so. Pass claims_en (English versions) alongside
  Chinese claims: evidence matching is lexical and cannot cross
  languages
- If there is no PDF, say so instead of guessing from the title
- Be specific: reference sections, equations, figures, and tables by
  their numbers
- If the question goes beyond the paper's scope, say so honestly
- The user's notes reflect their research interests — connect answers
  to them when relevant
- Use markdown formatting for clarity

--- PAPER METADATA ---
Title: {title}
Authors: {authors}
Year: {year}
Venue: {venue}
DOI: {doi}
Total pages: {total_pages}
--- END METADATA ---

--- USER NOTES ---
{notes}
--- END USER NOTES ---
"""

_END_NOTES = {
    END_MAX_STEPS: "\n\n*(reached the tool-call step limit — ask a more "
                   "specific question)*",
    END_STUCK: "\n\n*(stopped: repeated identical tool calls)*",
    END_STOPPED: "\n\n*(stopped by the user)*",
}


class ChatAgent(AgentBase):
    """Agent for conversing about a paper's content."""

    name = "chat"

    def __init__(
        self,
        llm: LLMProvider,
        pdf_store: PDFStore,
        db: Database | None = None,
        *,
        openalex: OpenAlexClient | None = None,
        search_agent=None,
    ) -> None:
        self._llm = llm
        self._pdf_store = pdf_store
        self._db = db
        self._openalex = openalex
        self._search_agent = search_agent

    def run(self, paper_id: str, message: str) -> str:  # type: ignore[override]
        """One-shot turn without streaming: returns the final answer."""
        answer = ""
        for event in self.ask(paper_id, message):
            if event["type"] == "done":
                answer = event["answer"]
        return answer

    def ask(
        self, paper_id: str, message: str
    ) -> Generator[dict, None, None]:
        """Run one full user turn with the tool loop.

        Persists the user message, every assistant turn (with its
        tool_calls), every tool result, and yields streaming events:
          {"type": "delta", "text": str}   — answer text as it streams
          {"type": "done", "answer": str, "end_reason": str}
        """
        assert self._db is not None, "ChatAgent needs a database (db=...)"
        db = self._db

        conv_id = db.get_conversation(paper_id)
        if not conv_id:
            conv_id = db.create_conversation(paper_id)
        db.add_message(conv_id, "user", message)

        history = build_api_messages(db.get_messages(conv_id))
        tools = build_tools(
            paper_id,
            db,
            self._pdf_store,
            openalex=self._openalex,
            search_agent=self._search_agent,
        )

        def persist(event: dict) -> None:
            if event["kind"] == "assistant":
                db.add_message(
                    conv_id,
                    "assistant",
                    event["content"],
                    tool_calls=(
                        json.dumps(event["tool_calls"], ensure_ascii=False)
                        if event["tool_calls"]
                        else None
                    ),
                )
            else:
                db.add_message(
                    conv_id,
                    "tool",
                    event["content"],
                    tool_call_id=event["tool_call_id"],
                    tool_name=event["tool_name"],
                )

        try:
            answer, end_reason = "", "failed"
            for event in run_turn(
                self._llm,
                tools,
                system=self._build_system(paper_id),
                history=history,
                on_persist=persist,
            ):
                if event["type"] == "delta":
                    yield {"type": "delta", "text": event["text"]}
                else:
                    answer = event["answer"]
                    end_reason = event["end_reason"]
        except Exception as e:
            yield {
                "type": "done",
                "answer": f"(LLM call failed: {e})",
                "end_reason": "failed",
            }
            return

        yield {
            "type": "done",
            "answer": answer + _END_NOTES.get(end_reason, ""),
            "end_reason": end_reason,
        }

    def _build_system(self, paper_id: str) -> str:
        """System prompt: metadata + notes + page count (no text dump)."""
        title = authors = year = venue = doi = ""
        total_pages = "?"
        notes = "(No notes yet)"
        if self._db:
            paper = self._db.get_paper(paper_id)
            if paper:
                title = paper.title
                authors = paper.display_authors
                year = str(paper.year or "")
                venue = paper.venue or ""
                doi = paper.doi or ""
            record = self._db.get_pdf_text(paper_id)
            if record:
                total_pages = str(record[0])
            note_rows = self._db.list_notes(paper_id)
            if note_rows:
                notes = "\n\n".join(
                    f"[{r['created_at'][:10]}] {r['content']}"
                    for r in note_rows
                )
        return _SYSTEM_PROMPT.format(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            total_pages=total_pages,
            notes=notes,
        )
