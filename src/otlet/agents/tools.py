"""Agent tools: OpenAI function schemas + paper-scoped executors.

read_pdf_pages gives the model bounded, page-addressed access to the
paper under discussion; search_library searches every indexed PDF in
the library. Both return JSON strings — the loop truncates oversized
output (agents/core.py) before it goes back to the model.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_store import PDFStore

MAX_PAGES_PER_CALL = 8   # pages served per read_pdf_pages invocation
READ_CHAR_BUDGET = 10_000  # total chars per invocation


@dataclass
class ToolSpec:
    """One tool: its schema plus the executor returning a JSON string."""

    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], str]


def openai_schema(tools: dict[str, ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools.values()
    ]


def build_tools(
    paper_id: str, db: Database, pdf_store: PDFStore
) -> dict[str, ToolSpec]:
    """The tool set for a conversation about one paper."""
    index = PDFIndex(db, pdf_store)

    def _j(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    def read_pdf_pages(args: dict) -> str:
        from_page = max(1, int(args.get("from_page", 1) or 1))
        to_page = args.get("to_page")
        from_char = max(0, int(args.get("from_char", 0) or 0))

        record = db.get_pdf_text(paper_id)
        if record is None:
            if index.build(paper_id) is None:
                return _j({
                    "error": (
                        "No PDF is available for this paper. Say so and "
                        "answer from the metadata only."
                    )
                })
            record = db.get_pdf_text(paper_id)
        total_pages, blob = record
        pages: list[str] = json.loads(zlib.decompress(blob))

        last = min(
            int(to_page) if to_page else total_pages,
            from_page + MAX_PAGES_PER_CALL - 1,
            total_pages,
        )
        if from_page > total_pages:
            return _j({
                "error": f"from_page {from_page} is beyond the last page "
                         f"({total_pages})."
            })

        budget = READ_CHAR_BUDGET
        out_pages: list[dict] = []
        read_pages: list[int] = []
        next_hint: dict | None = None
        for page_no in range(from_page, last + 1):
            text = pages[page_no - 1]
            start = from_char if page_no == from_page else 0
            if start >= len(text):
                continue  # continuation point past the page end
            remaining = text[start:]
            if len(remaining) <= budget:
                chunk, page_done = remaining, True
            else:
                chunk, page_done = remaining[:budget], False
            out_pages.append({
                "page": page_no,
                "char_offset": start,
                "char_total": len(text),
                "text": chunk,
            })
            read_pages.append(page_no)
            budget -= len(chunk)
            if budget <= 0:
                if not page_done:
                    next_hint = {
                        "from_page": page_no,
                        "from_char": start + len(chunk),
                    }
                elif page_no < last:
                    next_hint = {"from_page": page_no + 1}
                break
        else:
            if last < total_pages:
                next_hint = {"from_page": last + 1}

        if not read_pages:
            return _j({"error": "Nothing to read for this range."})
        first_read, last_read = read_pages[0], read_pages[-1]
        coverage = (
            f"Read pages {first_read}–{last_read} of {total_pages}. "
            + (
                "Not finished — call again with "
                + json.dumps(next_hint, ensure_ascii=False)
                if next_hint
                else "Range fully read."
            )
        )
        return _j({
            "pages": out_pages,
            "total_pages": total_pages,
            "read_pages": read_pages,
            "next": next_hint,
            "coverage_note": coverage,
        })

    def search_library(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return _j({"error": "query is required"})
        limit = min(20, max(1, int(args.get("limit", 5) or 5)))
        hits = index.search(query, limit=limit)
        titles = {p.id: p.title for p in db.list_papers()}
        return _j({
            "query": query,
            "hit_pages": len(hits),
            "hits": [
                {
                    "paper_id": h["paper_id"],
                    "title": titles.get(h["paper_id"], h["paper_id"]),
                    "page": h["page"],
                    "snippet": h["snippet"],
                }
                for h in hits
            ],
            "note": (
                "No matches. Try different wording (English terms for "
                "English papers), or 3+ characters for the trigram index."
                if not hits
                else "Use read_pdf_pages on this paper for the full context."
            ),
        })

    return {
        "read_pdf_pages": ToolSpec(
            name="read_pdf_pages",
            description=(
                "Read the text of specific pages of the paper under "
                "discussion. Each call serves at most 8 pages / 10000 "
                "chars; the response's coverage_note and next field tell "
                "you how to continue (same page with from_char, or the "
                "next from_page). Cite the pages you actually read."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from_page": {
                        "type": "integer",
                        "description": "First page to read (1-based)",
                    },
                    "to_page": {
                        "type": "integer",
                        "description": "Last page to read (optional)",
                    },
                    "from_char": {
                        "type": "integer",
                        "description": (
                            "Character offset to resume a long page from "
                            "(from a previous response's next field)"
                        ),
                    },
                },
                "required": [],
            },
            execute=read_pdf_pages,
        ),
        "search_library": ToolSpec(
            name="search_library",
            description=(
                "Search the full text of every indexed PDF in the "
                "library (not just the current paper). Returns matching "
                "pages with snippets — useful for 'which papers discuss "
                "X' questions. Use 3+ character queries for best results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to find"},
                    "limit": {
                        "type": "integer",
                        "description": "Max page hits (default 5)",
                    },
                },
                "required": ["query"],
            },
            execute=search_library,
        ),
    }
