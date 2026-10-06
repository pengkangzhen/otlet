"""Polish-round tests: otlet verify CLI, streamed chat events pushed to
the GUI, and the textual TUI (headless mount)."""

import asyncio
import json
from pathlib import Path

import pytest

from otlet.config.settings import Settings
from otlet.models.paper import Paper
from otlet.storage.database import Database

# ── CLI: otlet verify ──────────────────────────────────────


def test_cmd_verify_prints_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from otlet import cli
    from otlet.agents import find_literature as fl

    def fake_run(db, claims=None, claims_en=None, claim_text=None, **kw):
        return {
            "claims": [{
                "claim": (claims or [claim_text])[0],
                "verdict": "supports",
                "evidence": [{
                    "title": "A Real Paper", "doi": "10.1/x",
                    "year": 2021, "venue": "OR",
                    "citation_count": 9, "in_library": True,
                    "verdict": "supports",
                    "evidence_text": "Redundant inventory improves "
                                     "supply chain resilience.",
                    "source_field": "abstract",
                    "polarity_mismatch": False, "note": "",
                }],
                "recalled_but_not_supporting": [],
            }],
            "summary": {"status": "supported", "supported": 1,
                        "partial": 0, "not_found": 0},
            "notes": ["probe note"],
            "legend": {},
        }

    monkeypatch.setattr(fl, "run_find_literature", fake_run)

    settings = Settings(data_dir=tmp_path)
    rc = cli.main(["verify", "冗余库存提高供应链韧性"], settings=settings)
    out = capsys.readouterr().out
    assert rc == 0
    assert "supports" in out and "A Real Paper" in out
    assert "在库内" in out
    assert "未判定" in out  # the not_found disclaimer


# ── GUI: streamed chat events via evaluate_js ───────────────


def test_send_chat_message_pushes_events(tmp_path: Path):
    from otlet.web.api import Api

    api = Api(Settings(data_dir=tmp_path))
    api._db.add_paper(Paper(id="p1", title="Stream Paper"))

    class FakeChatAgent:
        def ask(self, paper_id, message):
            yield {"type": "delta", "text": "正在"}
            yield {"type": "tool", "name": "read_pdf_pages",
                   "detail": "第1–2页"}
            yield {"type": "delta", "text": "读第1–2页…"}
            yield {"type": "done", "answer": "正在读第1–2页…",
                   "end_reason": "done"}

    api._chat_agent = FakeChatAgent()
    pushed: list[str] = []

    class FakeWindow:
        def evaluate_js(self, code):
            pushed.append(code)

    api._window = FakeWindow()
    result = json.loads(api.send_chat_message("p1", "讲了什么？"))

    assert result == {"response": "正在读第1–2页…",
                      "end_reason": "done"}
    # every event reached the page, in order, via onChatEvent
    assert len(pushed) == 4
    assert pushed[0].startswith("window.onChatEvent && onChatEvent(")
    assert "read_pdf_pages" in pushed[1] and "第1–2页" in pushed[1]
    json.loads(pushed[-1][len("window.onChatEvent && onChatEvent("):-1])
    api.close()


def test_send_chat_message_without_window_still_returns(
    tmp_path: Path,
):
    from otlet.web.api import Api

    api = Api(Settings(data_dir=tmp_path))
    api._db.add_paper(Paper(id="p1", title="Stream Paper"))

    class FakeChatAgent:
        def ask(self, paper_id, message):
            yield {"type": "delta", "text": "answer"}
            yield {"type": "done", "answer": "answer", "end_reason": "done"}

    api._chat_agent = FakeChatAgent()
    api._window = None  # headless / page not ready
    result = json.loads(api.send_chat_message("p1", "hi"))
    assert result["response"] == "answer"
    api.close()


# ── TUI: headless mount + list rendering ────────────────────


def test_tui_mounts_and_lists_papers(tmp_path: Path):
    from otlet.tui import OtletTUI

    db = Database(tmp_path / "library.db")
    db.add_paper(Paper(id="p1", title="Resilient Network Design",
                       year=2020, venue="OR"))
    db.add_paper(Paper(id="p2", title="Facility Location"))
    db.close()

    app = OtletTUI(Settings(data_dir=tmp_path))

    async def drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            table = app.query_one("#papers", DataTable)
            assert table.row_count == 2
            # filter narrows the list
            cmd = app.query_one("#cmd")
            from textual.widgets import Input

            assert isinstance(cmd, Input)
            app.refresh_list("resilient")
            await pilot.pause()
            assert table.row_count == 1

    asyncio.run(drive())
