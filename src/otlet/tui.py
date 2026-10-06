"""Otlet TUI — terminal interface built on textual.

Layout: paper list (left) + detail/chat pane (right) + command input.

Input grammar (one line, bottom):
- plain text        → filter the list by substring
- /text             → full-text grep across indexed PDFs
- :v <claim>        → verify whether literature supports the claim
- :c <message>      → chat with the selected paper (tool loop, streams)
- :n <text>         → add a reading note to the selected paper
- :e                → enrich the selected paper's metadata
- :o / :r           → open / reveal the selected paper's PDF
- :t tag1,tag2      → filter by tags (":t clear" resets)
- :q                → quit

Keys: ↑/↓ or j/k move, Enter select, o open, r reveal, q quit.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Markdown

from otlet import platform
from otlet.agents.chat import ChatAgent
from otlet.agents.find_literature import run_find_literature
from otlet.agents.openalex import OpenAlexClient
from otlet.agents.search import SearchAgent
from otlet.config.settings import Settings
from otlet.llm.provider import LLMProvider
from otlet.services.enrich import enrich_paper
from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_store import PDFStore

_VERDICT_STYLE = {
    "supports": "**✓ supports**",
    "partial": "**△ partial**",
    "none": "**✗ none**",
}


class OtletTUI(App):
    TITLE = "Otlet"
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #papers { width: 46%; border: solid $accent; }
    #detail { width: 1fr; border: solid $accent; }
    #cmd { dock: bottom; }
    #status { dock: bottom; height: 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("o", "open_pdf", "Open PDF"),
        Binding("r", "reveal_pdf", "Reveal PDF"),
    ]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._db = Database(settings.db_path)
        self._store = PDFStore(settings.pdf_dir)
        self._index = PDFIndex(self._db, self._store)
        self._openalex = OpenAlexClient(mailto=settings.openalex_email)
        self._s2 = SearchAgent(api_key=settings.s2_api_key)
        self._chat = ChatAgent(
            LLMProvider(
                model=settings.model,
                api_key=settings.api_key,
                api_base=settings.api_base,
            ),
            self._store,
            db=self._db,
            openalex=self._openalex,
            search_agent=self._s2,
        )
        self._papers: list = []
        self._selected_id: str | None = None
        self._chat_log: list[str] = []
        self._tag_filter: list[str] = []

    # ── layout & lifecycle ──────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield DataTable(id="papers")
            yield Markdown(id="detail", markdown="*选择左侧论文查看详情*")
        yield Input(placeholder="过滤… /全文  :v 声明  :c 提问  :n 笔记  "
                                ":e 补全  :t 标签  :o 打开  :q 退出",
                    id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#papers", DataTable)
        table.cursor_type = "row"
        table.add_columns("Title", "Year", "Venue")
        self.refresh_list()

    def on_unmount(self) -> None:
        self._db.close()

    def _status(self, text: str) -> None:
        self.sub_title = text

    # ── list & detail ───────────────────────────────────────

    def refresh_list(self, substring: str = "") -> None:
        if self._tag_filter:
            papers = self._db.find_papers_by_tags(
                self._tag_filter, match_all=False
            )
        else:
            papers = self._db.list_papers()
        if substring:
            sub = substring.lower()
            papers = [
                p for p in papers
                if sub in p.title.lower()
                or sub in (p.venue or "").lower()
                or sub in (p.abstract or "").lower()
            ]
        self._papers = papers
        table = self.query_one("#papers", DataTable)
        table.clear()
        for p in papers:
            table.add_row(
                p.title[:64], str(p.year or ""), (p.venue or "")[:24],
                key=p.id,
            )
        self._status(f"{len(papers)} papers")

    def show_detail(self) -> None:
        paper = self._selected()
        if paper is None:
            return
        notes = self._db.list_notes(paper.id)
        note_md = "\n".join(
            f"- *{n['created_at'][:10]}* {n['content']}" for n in notes
        ) or "*(无笔记)*"
        chat = "\n\n".join(self._chat_log) or ""
        md = (
            f"# {paper.title}\n\n"
            f"{paper.display_authors} · {paper.year or '?'} · "
            f"{paper.venue or '?'}\n\n"
            f"**DOI**: {paper.doi or '-'}  "
            f"**ID**: {paper.id}\n\n"
            f"## 摘要\n\n{paper.abstract or '*(缺失 — 试试 :e 补全)*'}\n\n"
            f"## 笔记\n\n{note_md}\n"
        )
        if chat:
            md += f"\n---\n\n{chat}\n"
        self.query_one("#detail", Markdown).update(md)

    def _selected(self):
        return next(
            (p for p in self._papers if p.id == self._selected_id), None
        )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is not None and event.row_key.value:
            self._selected_id = event.row_key.value
            self.show_detail()

    # ── command input ───────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        if not cmd:
            return
        if cmd == ":q":
            self.exit()
        elif cmd.startswith("/"):
            self._grep(cmd[1:].strip())
        elif cmd.startswith(":c "):
            self._chat_run(cmd[3:].strip())
        elif cmd.startswith(":v "):
            self._verify(cmd[3:].strip())
        elif cmd.startswith(":n "):
            self._add_note(cmd[3:].strip())
        elif cmd == ":e":
            self._enrich()
        elif cmd in (":o", ":r"):
            self._open_or_reveal(reveal=cmd == ":r")
        elif cmd.startswith(":t"):
            arg = cmd[2:].strip()
            self._tag_filter = (
                [] if arg in ("", "clear")
                else [t.strip() for t in arg.split(",") if t.strip()]
            )
            self.refresh_list()
            self._status(
                f"tags: {','.join(self._tag_filter) or '(全部)'}"
            )
        else:
            self.refresh_list(cmd)

    # ── actions ─────────────────────────────────────────────

    def _grep(self, query: str) -> None:
        if not query:
            return
        hits = self._index.search(query, limit=20)
        titles = {p.id: p.title for p in self._db.list_papers()}
        lines = [
            f"- **{titles.get(h['paper_id'], h['paper_id'])}** "
            f"p.{h['page']}: {h['snippet']}"
            for h in hits
        ]
        self._chat_log = []  # detail pane shows grep results
        md = (
            f"# 全文检索 “{query}”\n\n"
            + ("\n".join(lines) if lines else "*无命中 — "
              "3 字符以上走索引；先 `otlet index` 建索引*")
        )
        self.query_one("#detail", Markdown).update(md)
        self._status(f"grep “{query}”: {len(hits)} hits")

    def _add_note(self, text: str) -> None:
        paper = self._selected()
        if paper is None or not text:
            return
        self._db.add_note(paper.id, text)
        self.show_detail()
        self._status("note added")

    def _open_or_reveal(self, *, reveal: bool) -> None:
        paper = self._selected()
        if paper is None or not paper.pdf_path:
            self._status("no PDF for the selected paper")
            return
        path = Path(paper.pdf_path).expanduser()
        if not path.exists():
            self._status("PDF file missing on disk")
            return
        platform.reveal_path(path) if reveal else platform.open_path(path)

    def action_open_pdf(self) -> None:
        self._open_or_reveal(reveal=False)

    def action_reveal_pdf(self) -> None:
        self._open_or_reveal(reveal=True)

    def _enrich(self) -> None:
        paper = self._selected()
        if paper is None:
            return

        def work() -> None:
            result = enrich_paper(
                self._db, paper, openalex=self._openalex,
                search_agent=self._s2,
            )
            filled = result["filled"]
            msg = (
                ", ".join(f"{k}={str(v)[:30]}" for k, v in filled.items())
                if filled else "already complete"
            )
            self.call_from_thread(
                lambda: (self.show_detail(),
                         self._status(f"enrich: {msg}"))
            )

        self.run_worker(work, thread=True)

    def _verify(self, claim: str) -> None:
        self._status("verifying…")

        def work() -> None:
            result = run_find_literature(
                self._db, claims=[claim],
                openalex=self._openalex, search_agent=self._s2,
                pdf_index=self._index,
            )
            self.call_from_thread(lambda: self._show_verify(claim, result))

        self.run_worker(work, thread=True)

    def _show_verify(self, claim: str, result: dict) -> None:
        parts = [f"# 声明核查\n\n> {claim}\n"]
        for item in result["claims"]:
            verdict = _VERDICT_STYLE.get(item["verdict"], item["verdict"])
            parts.append(f"\n## {verdict}\n")
            for ev in item["evidence"][:4]:
                lib = " *(在库内)*" if ev["in_library"] else ""
                parts.append(
                    f"- **{(ev['title'] or '?')[:70]}**{lib}\n"
                    f"  > {(ev['evidence_text'] or '')[:220]}\n"
                )
            if not item["evidence"]:
                parts.append(
                    "\n*未找到支撑证据 — 未判定，不等于证伪*\n"
                )
        for note in result["notes"]:
            parts.append(f"\n*note: {note}*")
        self._chat_log = []
        self.query_one("#detail", Markdown).update("".join(parts))
        s = result["summary"]
        self._status(
            f"verify: ✓{s['supported']} △{s['partial']} ✗{s['not_found']}"
        )

    def _chat_run(self, message: str) -> None:
        paper = self._selected()
        if paper is None or not message:
            return
        self._status("thinking…")

        def work() -> None:
            answer: list[str] = []
            try:
                for event in self._chat.ask(paper.id, message):
                    if event["type"] == "delta":
                        answer.append(event["text"])
                        self.call_from_thread(
                            lambda t=event["text"]: self._live_chat(
                                message, "".join(answer)
                            )
                        )
                    elif event["type"] == "tool":
                        self.call_from_thread(
                            lambda e=event: self._status(
                                f"→ {e['name']}({e['detail']})"
                            )
                        )
                final = "".join(answer)
            except Exception as e:
                final = f"*(LLM call failed: {e})*"
            self.call_from_thread(
                lambda: self._finish_chat(message, final)
            )

        self.run_worker(work, thread=True)

    def _live_chat(self, question: str, partial: str) -> None:
        self._chat_log = [
            f"## 你\n\n{question}\n\n## otlet\n\n{partial}▌"
        ]
        self.show_detail()

    def _finish_chat(self, question: str, answer: str) -> None:
        self._chat_log = [f"## 你\n\n{question}\n\n## otlet\n\n{answer}"]
        self.show_detail()
        self._status("chat done")


def main() -> None:
    OtletTUI(Settings.load()).run()


if __name__ == "__main__":
    main()
