"""Tests for the agent tool loop (core/loop/tools/chat) and the
provider's streaming tool-call accumulation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from otlet.agents.chat import ChatAgent
from otlet.agents.core import (
    TOOL_OUTPUT_LIMIT,
    build_api_messages,
    is_stuck,
    parse_tool_calls,
    truncate_output,
)
from otlet.agents.loop import run_turn
from otlet.agents.tools import ToolSpec, build_tools
from otlet.llm.provider import LLMProvider
from otlet.models.paper import Paper
from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_store import PDFStore


# ── Fakes ───────────────────────────────────────────────────


class FakeLLM:
    """Scripted chat_tools_stream turns; records every request."""

    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def chat_tools_stream(self, messages, *, system=None, tools=None, **kw):
        self.requests.append(
            {"system": system, "messages": list(messages)}
        )
        turn = self.turns.pop(0)
        if turn.get("content"):
            yield {"type": "delta", "text": turn["content"]}
        yield {
            "type": "message",
            "content": turn.get("content", ""),
            "tool_calls": turn.get("tool_calls", []),
        }


def _call(cid: str, name: str, args: str) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _echo_tool(output: str | None = None) -> dict[str, ToolSpec]:
    def execute(args: dict) -> str:
        return output if output is not None else json.dumps(args)

    return {
        "echo": ToolSpec(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
    }


def _collect(gen):
    events = list(gen)
    return events


# ── core.py ─────────────────────────────────────────────────


def test_parse_tool_calls_defensive():
    calls = parse_tool_calls([
        _call("c1", "echo", '{"x": 1}'),
        _call("c2", "echo", "not-json{"),      # broken JSON → empty args
        {"id": "c3", "function": {"arguments": "{}"}},  # no name → dropped
    ])
    assert [c.name for c in calls] == ["echo", "echo"]
    assert calls[0].arguments == {"x": 1}
    assert calls[1].arguments == {}


def test_is_stuck_window():
    sigs = ["a", "a", "a"]
    assert not is_stuck(sigs)               # below window size
    assert is_stuck(sigs + ["a"])           # 4×a
    assert is_stuck(["a", "b", "a", "b"])   # ping-pong loop
    assert not is_stuck(["a", "b", "c", "d"])  # all distinct


def test_truncate_output():
    short, truncated = truncate_output("x" * 100)
    assert (short, truncated) == ("x" * 100, False)
    long_text, truncated = truncate_output("x" * (TOOL_OUTPUT_LIMIT + 500))
    assert truncated
    assert long_text.startswith("x" * TOOL_OUTPUT_LIMIT)
    assert "truncated" in long_text


def test_build_api_messages_pairs_and_drops_orphans():
    rows = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": json.dumps(
            [_call("c1", "echo", "{}"), _call("c2", "echo", "{}")]
        )},
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},
        {"role": "tool", "content": "r2", "tool_call_id": "c2"},
        {"role": "assistant", "content": "answer"},
        # orphan tool row (its assistant turn was trimmed away)
        {"role": "tool", "content": "ghost", "tool_call_id": "zz"},
        {"role": "user", "content": "next"},
    ]
    msgs = build_api_messages(rows)
    assert [m["role"] for m in msgs] == [
        "user", "assistant", "tool", "tool", "assistant", "user",
    ]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["tool_call_id"] == "c1"


# ── loop.py ─────────────────────────────────────────────────


def test_run_turn_tool_then_answer_persists_in_order():
    llm = FakeLLM([
        {"content": "", "tool_calls": [_call("c1", "echo", '{"x": 1}')]},
        {"content": "final answer"},
    ])
    persisted: list[dict] = []

    events = _collect(run_turn(
        llm, _echo_tool(), "sys", [{"role": "user", "content": "hi"}],
        on_persist=persisted.append,
    ))

    assert events[-1] == {
        "type": "result", "answer": "final answer", "end_reason": "done",
    }
    kinds = [(p["kind"], bool(p.get("tool_calls"))) for p in persisted]
    # assistant(tool_calls) persisted BEFORE the tool result
    assert kinds == [("assistant", True), ("tool", False),
                     ("assistant", False)]
    assert persisted[1]["tool_call_id"] == "c1"
    assert persisted[1]["tool_name"] == "echo"
    assert json.loads(persisted[1]["content"]) == {"x": 1}


def test_run_turn_max_steps():
    turns = [
        {"content": "", "tool_calls": [_call(f"c{i}", "echo", f'{{"i": {i}}}')]}
        for i in range(12)
    ]
    llm = FakeLLM(turns)
    events = _collect(run_turn(
        llm, _echo_tool(), "sys", [{"role": "user", "content": "go"}],
    ))
    assert events[-1]["end_reason"] == "max_steps"
    assert len(llm.requests) == 12


def test_run_turn_stuck_on_identical_calls():
    same = {"content": "", "tool_calls": [_call("c", "echo", "{}")]}
    llm = FakeLLM([same] * 5)
    persisted: list[dict] = []
    events = _collect(run_turn(
        llm, _echo_tool(), "sys", [{"role": "user", "content": "go"}],
        on_persist=persisted.append,
    ))
    assert events[-1]["end_reason"] == "stuck"
    # executed 3 times, the 4th was intercepted as the stuck result
    tool_results = [p for p in persisted if p["kind"] == "tool"]
    assert len(tool_results) == 4
    assert "repeatedly" in tool_results[-1]["content"]


def test_run_turn_stopped_persists_placeholder():
    llm = FakeLLM([
        {"content": "", "tool_calls": [_call("c1", "echo", "{}")]},
    ])
    persisted: list[dict] = []
    events = _collect(run_turn(
        llm, _echo_tool(), "sys", [{"role": "user", "content": "go"}],
        on_persist=persisted.append, should_stop=lambda: True,
    ))
    assert events[-1]["end_reason"] == "stopped"
    assert "cancelled" in persisted[-1]["content"]
    # pairing stays valid: assistant tool_calls + one tool result
    assert persisted[0]["tool_calls"][0]["id"] == "c1"
    assert persisted[-1]["tool_call_id"] == "c1"


def test_run_turn_unknown_tool_is_answerable():
    llm = FakeLLM([
        {"content": "", "tool_calls": [_call("c1", "nope", "{}")]},
        {"content": "recovered"},
    ])
    persisted: list[dict] = []
    events = _collect(run_turn(
        llm, _echo_tool(), "sys", [{"role": "user", "content": "go"}],
        on_persist=persisted.append,
    ))
    assert events[-1]["end_reason"] == "done"
    assert "unknown tool" in persisted[1]["content"]


def test_run_turn_truncates_huge_tool_output():
    llm = FakeLLM([
        {"content": "", "tool_calls": [_call("c1", "echo", "{}")]},
        {"content": "ok"},
    ])
    persisted: list[dict] = []
    _collect(run_turn(
        llm, _echo_tool(output="x" * (TOOL_OUTPUT_LIMIT + 999)), "sys",
        [{"role": "user", "content": "go"}], on_persist=persisted.append,
    ))
    assert len(persisted[1]["content"]) < TOOL_OUTPUT_LIMIT + 200
    assert "truncated" in persisted[1]["content"]


# ── provider.chat_tools_stream ──────────────────────────────


def _stream_chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc_delta(index, cid=None, name=None, args=None):
    return SimpleNamespace(
        index=index, id=cid,
        function=SimpleNamespace(name=name, arguments=args),
    )


def test_chat_tools_stream_accumulates_fragments(monkeypatch):
    import litellm

    def fake_completion(**kwargs):
        assert kwargs["stream"] is True
        assert kwargs["tools"], "tools must be passed through"
        return iter([
            _stream_chunk(content="Hel"),
            _stream_chunk(content="lo"),
            _stream_chunk(tool_calls=[_tc_delta(0, cid="c1", name="echo",
                                                args='{"x"')]),
            _stream_chunk(tool_calls=[_tc_delta(0, args=': 1}')]),
        ])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    provider = LLMProvider(model="gpt-4o-mini")
    events = list(provider.chat_tools_stream(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "echo"}}],
    ))
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    final = events[-1]
    assert deltas == ["Hel", "lo"]
    assert final["content"] == "Hello"
    assert final["tool_calls"] == [{
        "id": "c1", "type": "function",
        "function": {"name": "echo", "arguments": '{"x": 1}'},
    }]


def test_chat_tools_stream_falls_back_to_sync(monkeypatch):
    import litellm

    message = SimpleNamespace(
        content="sync answer",
        tool_calls=[SimpleNamespace(
            id="c9",
            function=SimpleNamespace(name="echo", arguments={"x": 2}),
        )],
    )

    def fake_completion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("provider rejects streaming tools")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    provider = LLMProvider(model="gpt-4o-mini")
    events = list(provider.chat_tools_stream(
        [{"role": "user", "content": "q"}], tools=[{"type": "function",
                                                    "function": {"name": "e"}}],
    ))
    assert [e for e in events if e["type"] == "delta"] == [
        {"type": "delta", "text": "sync answer"}
    ]
    assert events[-1]["tool_calls"][0]["function"]["arguments"] == '{"x": 2}'


# ── tools.py ────────────────────────────────────────────────


@pytest.fixture
def tool_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "t.db")
    yield db
    db.close()


def _indexed_paper(db: Database, tmp_path: Path, page_texts: list[str]):
    import pymupdf

    paper_id = "p1"
    db.add_paper(Paper(id=paper_id, title="Tool Paper"))
    doc = pymupdf.open()
    for text in page_texts:
        doc.new_page().insert_text((72, 72), text[:200])
    doc.save(tmp_path / "p1.pdf")
    doc.close()
    store = PDFStore(tmp_path / "pdfs")
    store.import_file(tmp_path / "p1.pdf", paper_id=paper_id)
    db.update_paper(paper_id, pdf_path=str(store.get_path(paper_id)))
    PDFIndex(db, store).build(paper_id)
    return paper_id, store


def test_read_pdf_pages_budget_and_continuation(
    tool_db: Database, tmp_path: Path
):
    # page 2 outlasts one budget → continuation via from_char
    import zlib

    pid, store = _indexed_paper(tool_db, tmp_path, ["x", "y", "z"])
    pages = ["A" * 300, "B" * 11_000, "C" * 100]
    blob = zlib.compress(json.dumps(pages).encode(), 1)
    tool_db.upsert_pdf_text(pid, 3, blob)
    tool_db.replace_fts_rows(pid, pages)

    tools = build_tools(pid, tool_db, store)
    first = json.loads(tools["read_pdf_pages"].execute({}))
    assert first["total_pages"] == 3
    assert first["read_pages"] == [1, 2]
    assert len(first["pages"][1]["text"]) == 10_000 - 300
    assert first["next"] == {"from_page": 2, "from_char": 10_000 - 300}
    assert "Not finished" in first["coverage_note"]

    second = json.loads(tools["read_pdf_pages"].execute(first["next"]))
    assert second["read_pages"] == [2, 3]
    assert second["next"] is None
    assert second["pages"][0]["char_offset"] == 10_000 - 300


def test_read_pdf_pages_no_pdf(tool_db: Database, tmp_path: Path):
    tool_db.add_paper(Paper(id="np", title="No PDF"))
    tools = build_tools("np", tool_db, PDFStore(tmp_path / "pdfs"))
    out = json.loads(tools["read_pdf_pages"].execute({}))
    assert "No PDF" in out["error"]


def test_search_library_tool(tool_db: Database, tmp_path: Path):
    pid, store = _indexed_paper(
        tool_db, tmp_path, ["robust supply chain network design"]
    )
    tools = build_tools(pid, tool_db, store)
    out = json.loads(tools["search_library"].execute(
        {"query": "network design"}
    ))
    assert out["hit_pages"] == 1
    assert out["hits"][0]["title"] == "Tool Paper"

    empty = json.loads(tools["search_library"].execute(
        {"query": "quantum zebra"}
    ))
    assert empty["hit_pages"] == 0


# ── ChatAgent.ask end-to-end ────────────────────────────────


def test_chat_agent_ask_persists_full_turn(tool_db: Database, tmp_path: Path):
    pid, store = _indexed_paper(
        tool_db, tmp_path, ["page one content about models", "page two"]
    )
    llm = FakeLLM([
        {"content": "", "tool_calls": [
            _call("c1", "read_pdf_pages", '{"from_page": 1, "to_page": 2}')
        ]},
        {"content": "第1–2页讲了模型。"},
    ])
    agent = ChatAgent(llm, store, db=tool_db)

    deltas = []
    done = None
    for event in agent.ask(pid, "这篇论文讲了什么？"):
        if event["type"] == "delta":
            deltas.append(event["text"])
        else:
            done = event

    assert done["end_reason"] == "done"
    assert "".join(deltas) == "第1–2页讲了模型。"

    conv_id = tool_db.get_conversation(pid)
    rows = tool_db.get_messages(conv_id)
    roles = [r["role"] for r in rows]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert json.loads(rows[1]["tool_calls"])[0]["function"]["name"] == (
        "read_pdf_pages"
    )
    assert rows[2]["tool_name"] == "read_pdf_pages"
    tool_payload = json.loads(rows[2]["content"])
    assert tool_payload["read_pages"] == [1, 2]

    # The system prompt exposes the page count, not a text dump
    system_used = llm.requests[0]["system"]
    assert "Total pages: 2" in system_used

    # A follow-up turn replays tool traffic as valid history
    llm2 = FakeLLM([{"content": "follow-up"}])
    agent2 = ChatAgent(llm2, store, db=tool_db)
    for _ in agent2.ask(pid, "再具体一点"):
        pass
    replay = llm2.requests[0]["messages"]
    assert [m["role"] for m in replay] == [
        "user", "assistant", "tool", "assistant", "user",
    ]
    assert replay[1]["tool_calls"][0]["id"] == "c1"
    assert replay[2]["tool_call_id"] == "c1"
