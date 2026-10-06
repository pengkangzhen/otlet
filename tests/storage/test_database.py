from pathlib import Path

import pytest

from otlet.models.author import Author
from otlet.models.paper import Paper
from otlet.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_empty_database(db: Database):
    assert db.list_papers() == []
    assert db.list_tags() == []
    db.close()


def test_add_and_get_paper(db: Database):
    paper = Paper(
        id="p1",
        title="Test Paper",
        authors=[Author(name="Alice"), Author(name="Bob")],
        year=2024,
        doi="10.1234/test",
        abstract="A test paper abstract.",
        tags=["testing", "unit-test"],
    )
    db.add_paper(paper)

    retrieved = db.get_paper("p1")
    assert retrieved is not None
    assert retrieved.title == "Test Paper"
    assert len(retrieved.authors) == 2
    assert set(retrieved.tags) == {"testing", "unit-test"}
    db.close()


def test_get_paper_by_doi(db: Database):
    db.add_paper(Paper(id="p1", title="DOI Paper", doi="10.1234/doi-test"))
    result = db.get_paper_by_doi("10.1234/doi-test")
    assert result is not None
    assert result.title == "DOI Paper"
    db.close()


def test_tag_management(db: Database):
    tag = db.create_tag("machine-learning")
    assert tag.name == "machine-learning"

    # Unlinked tags are hidden from the default (manual) view…
    assert db.list_tags() == []
    # …but visible when everything is requested
    all_tags = db.list_tags(include_auto=True)
    assert len(all_tags) == 1
    assert all_tags[0].name == "machine-learning"

    # Linking the tag manually makes it visible by default
    db.add_paper(Paper(id="p1", title="Linked Paper"))
    db.add_tag_to_paper("p1", "machine-learning")
    visible = db.list_tags()
    assert len(visible) == 1
    assert visible[0].id == tag.id
    db.close()


def test_find_papers_by_tags_and_logic(db: Database):
    db.add_paper(Paper(id="p1", title="Paper A", tags=["ml", "nlp"]))
    db.add_paper(Paper(id="p2", title="Paper B", tags=["ml", "cv"]))
    db.add_paper(Paper(id="p3", title="Paper C", tags=["nlp", "cv"]))

    # AND: both ml and nlp → only Paper A
    results = db.find_papers_by_tags(["ml", "nlp"])
    assert len(results) == 1
    assert results[0].title == "Paper A"
    db.close()


def test_find_papers_by_tags_or_logic(db: Database):
    db.add_paper(Paper(id="p1", title="Paper A", tags=["ml"]))
    db.add_paper(Paper(id="p2", title="Paper B", tags=["cv"]))
    db.add_paper(Paper(id="p3", title="Paper C", tags=["nlp"]))

    # OR: ml or cv → Paper A and Paper B
    results = db.find_papers_by_tags(["ml", "cv"], match_all=False)
    assert len(results) == 2
    db.close()


def test_search_papers(db: Database):
    db.add_paper(
        Paper(id="p1", title="Deep Learning for NLP", abstract="Neural networks")
    )
    db.add_paper(
        Paper(
            id="p2",
            title="Reinforcement Learning",
            abstract="Policy gradient",
        )
    )

    results = db.search_papers("Deep Learning")
    assert len(results) == 1
    assert results[0].id == "p1"
    db.close()


def test_conversation_and_messages(db: Database):
    db.add_paper(Paper(id="p1", title="Chat Paper"))
    conv_id = db.create_conversation("p1")

    db.add_message(conv_id, "user", "What is this paper about?")
    db.add_message(conv_id, "assistant", "This paper discusses...")

    messages = db.get_messages(conv_id)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    db.close()


def test_tool_messages_roundtrip(db: Database):
    """The agent tool loop stores tool results with their call pairing."""
    db.add_paper(Paper(id="p1", title="Chat Paper"))
    conv_id = db.create_conversation("p1")

    db.add_message(conv_id, "user", "summarize page 3")
    db.add_message(conv_id, "assistant", "")
    db.add_message(
        conv_id,
        "tool",
        '{"pages": [3], "text": "..."}',
        tool_call_id="call_1",
        tool_name="read_pdf_pages",
    )

    messages = db.get_messages(conv_id)
    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["tool_name"] == "read_pdf_pages"

    # Roles outside the widened set are rejected
    with pytest.raises(ValueError):
        db.add_message(conv_id, "system", "nope")
    db.close()


def test_legacy_messages_table_migrated(tmp_path: Path):
    """A pre-v3 messages table (CHECK user/assistant only) is rebuilt
    to accept tool messages, with a backup written first."""
    import sqlite3

    path = tmp_path / "legacy-msg.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            paper_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO conversations (id, paper_id) VALUES ('c1', 'p1');
        INSERT INTO messages (id, conversation_id, role, content)
            VALUES ('m1', 'c1', 'user', 'old question');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    assert (tmp_path / "legacy-msg.db.pre-v3.bak").exists()
    messages = db.get_messages("c1")
    assert len(messages) == 1 and messages[0]["content"] == "old question"

    # Tool messages are now accepted and persist across reopen
    db.add_message("c1", "tool", "{}", tool_call_id="k", tool_name="t")
    db.close()
    db2 = Database(path)
    assert any(m["role"] == "tool" for m in db2.get_messages("c1"))
    db2.close()


def test_pdf_fingerprint_roundtrip(db: Database):
    db.add_paper(
        Paper(id="p1", title="Fingerprinted",
              pdf_fingerprint="a" * 64, pdf_path="/pdfs/p1.pdf")
    )
    hit = db.get_paper_by_fingerprint("a" * 64)
    assert hit is not None and hit.id == "p1"
    assert db.get_paper_by_fingerprint("b" * 64) is None

    # Soft-deleted papers leave the exact-key index
    db.delete_paper("p1")
    assert db.get_paper_by_fingerprint("a" * 64) is None
    db.close()


def test_doi_normalized_on_write_and_lookup(db: Database):
    # Resolver-URL and mixed-case forms collapse to one canonical value
    db.add_paper(Paper(id="p1", title="A", doi="https://doi.org/10.1287/opre.2020.1"))
    assert db.get_paper("p1").doi == "10.1287/opre.2020.1"
    assert db.get_paper_by_doi("HTTPS://DX.DOI.ORG/10.1287/opre.2020.1").id == "p1"
    assert db.get_paper_by_doi("10.1287/opre.2020.1").id == "p1"
    db.close()


def test_legacy_dois_normalized_on_open(tmp_path: Path):
    """Existing rows with resolver-URL DOIs get rewritten at startup."""
    import sqlite3

    path = tmp_path / "legacy-doi.db"
    db = Database(path)
    db.add_paper(Paper(id="p1", title="A", doi="10.1/keep"))
    db.close()

    conn = sqlite3.connect(path)  # simulate a pre-normalization row
    conn.execute(
        "UPDATE papers SET doi = 'https://doi.org/10.1/keep' WHERE id = 'p1'"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_paper_by_doi("10.1/keep").id == "p1"
    assert db.get_paper("p1").doi == "10.1/keep"
    db.close()


def test_delete_paper(db: Database):
    db.add_paper(Paper(id="p1", title="To Delete", tags=["tmp"]))
    db.delete_paper("p1")

    # Soft delete: hidden from all live views…
    assert db.get_paper("p1") is None
    assert db.list_papers() == []
    # …but recoverable from the trash
    trash = db.list_trash()
    assert len(trash) == 1
    assert trash[0].id == "p1"
    assert trash[0].deleted_date is not None

    db.restore_paper("p1")
    assert db.get_paper("p1") is not None

    db.delete_paper("p1")
    assert db.empty_trash() == 1
    assert db.list_trash() == []
    db.close()


def test_add_remove_tag_from_paper(db: Database):
    db.add_paper(Paper(id="p1", title="Tagged Paper", tags=["ml"]))
    paper = db.get_paper("p1")
    assert "ml" in paper.tags

    db.remove_tag_from_paper("p1", "ml")
    paper = db.get_paper("p1")
    assert "ml" not in paper.tags
    db.close()


def test_concurrent_access_from_threads(db: Database):
    """pywebview bridge calls hit the shared connection from worker threads."""
    import threading

    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(10):
                db.add_paper(
                    Paper(id=f"w{n}-{i}", title=f"Paper {n}/{i}", tags=["concurrent"])
                )
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(20):
                db.list_papers()
                db.find_papers_by_tags(["concurrent"])
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(db.list_papers()) == 30
    db.close()


def test_auto_tags_separated_from_manual(db: Database):
    db.add_paper(Paper(
        id="p1", title="Auto Paper",
        tags=["manual-tag"], auto_tags=["auto-tag", "another"],
    ))
    paper = db.get_paper("p1")
    assert paper.tags == ["manual-tag"]
    assert set(paper.auto_tags) == {"auto-tag", "another"}

    # Default sidebar view hides auto-only tags
    names = {t.name for t in db.list_tags()}
    assert "manual-tag" in names
    assert "auto-tag" not in names
    assert "another" not in names
    # include_auto returns the full tag table
    all_names = {t.name for t in db.list_tags(include_auto=True)}
    assert {"manual-tag", "auto-tag", "another"} <= all_names
    db.close()


def test_author_name_split_stored_and_deduped(db: Database):
    db.add_paper(Paper(id="p1", title="X", authors=[
        Author(name="Alice Wang", first_name="Alice", last_name="Wang"),
        Author(name="Bob"),
    ]))
    # Same person on a second paper resolves to one author row
    db.add_paper(Paper(id="p2", title="Y", authors=[
        Author(name="Alice Wang", first_name="Alice", last_name="Wang"),
    ]))

    paper = db.get_paper("p1")
    assert paper.authors[0].first_name == "Alice"
    assert paper.authors[0].last_name == "Wang"

    total = db._conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    assert total == 2  # Alice (deduped across papers) + Bob
    db.close()


def test_mark_tag_source_reclassify(db: Database):
    # Simulate a pre-migration paper: auto tags merged into manual links
    db.add_paper(Paper(id="p1", title="Old Paper", tags=["keep", "noise", "x"]))
    assert db.get_paper("p1").tags == ["keep", "noise", "x"]

    changed = db.mark_tag_source("p1", ["noise", "x"], "auto")
    assert changed == 2
    paper = db.get_paper("p1")
    assert paper.tags == ["keep"]
    assert set(paper.auto_tags) == {"noise", "x"}

    # Unknown names change nothing
    assert db.mark_tag_source("p1", ["ghost"], "auto") == 0
    db.close()


def test_notes_crud(db: Database):
    db.add_paper(Paper(id="p1", title="Noted Paper"))
    assert db.list_notes("p1") == []

    n1 = db.add_note("p1", "Key insight: the relaxation is tight")
    n2 = db.add_note("p1", "Compare with column generation in ECR context")
    notes = db.list_notes("p1")
    assert len(notes) == 2
    assert notes[0]["content"].startswith("Key insight")

    db.update_note(n2["id"], "Updated comparison note")
    notes = {n["id"]: n for n in db.list_notes("p1")}
    assert notes[n2["id"]]["content"] == "Updated comparison note"
    assert notes[n2["id"]]["updated_at"] >= notes[n2["id"]]["created_at"]

    db.delete_note(n1["id"])
    assert len(db.list_notes("p1")) == 1

    # Purging the paper cascades notes away
    db.delete_paper("p1")
    db.purge_paper("p1")
    assert db.list_notes("p1") == []
    db.close()


def test_legacy_notes_column_migrated(tmp_path: Path):
    """Old libraries with papers.notes get folded into paper_notes."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, notes TEXT)"
    )
    conn.execute(
        "INSERT INTO papers VALUES ('p1', 'Old Paper', 'legacy note text')"
    )
    conn.execute(
        "INSERT INTO papers VALUES ('p2', 'Empty Notes', NULL)"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    notes = db.list_notes("p1")
    assert len(notes) == 1
    assert notes[0]["content"] == "legacy note text"
    assert db.list_notes("p2") == []
    # The legacy column is gone
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(papers)")]
    assert "notes" not in cols
    # Reopening is a no-op (no duplicate notes)
    db.close()
    db2 = Database(db_path)
    assert len(db2.list_notes("p1")) == 1
    db2.close()


def test_set_paper_authors(db: Database):
    db.add_paper(Paper(id="p1", title="X", authors=[Author(name="Old Author")]))
    db.set_paper_authors("p1", [Author(name="Alice Wang"), Author(name="Bob Li")])
    paper = db.get_paper("p1")
    assert [a.name for a in paper.authors] == ["Alice Wang", "Bob Li"]
    db.close()


def test_update_citation_fields(db: Database):
    db.add_paper(Paper(id="p1", title="Cited Paper"))
    db.update_paper(
        "p1", volume="31", issue="3", pages="120-130",
        publisher="Elsevier", language="English",
    )
    paper = db.get_paper("p1")
    assert paper.volume == "31"
    assert paper.issue == "3"
    assert paper.pages == "120-130"
    assert paper.publisher == "Elsevier"
    assert paper.language == "English"
    db.close()


def test_set_tag_category(db: Database):
    tag = db.create_tag("column-generation")
    assert tag.category is None

    db.set_tag_category(tag.id, "algorithm")
    assert db.get_tag(tag.id).category == "algorithm"

    # Category rides along in list_tags and the sidebar counts view
    db.add_paper(Paper(id="p1", title="X", tags=["column-generation"]))
    assert db.list_tags()[0].category == "algorithm"
    assert db.list_tags_with_counts()[0]["category"] == "algorithm"

    # Empty value clears the classification
    db.set_tag_category(tag.id, "")
    assert db.get_tag(tag.id).category is None

    # Invalid category is rejected
    with pytest.raises(ValueError):
        db.set_tag_category(tag.id, "venue")
    db.close()


def test_get_papers_with_dois(db: Database):
    db.add_paper(Paper(id="p1", title="A", doi="10.1/a"))
    db.add_paper(Paper(id="p2", title="B"))  # no DOI
    db.add_paper(Paper(id="p3", title="C", doi="10.1/c"))
    db.delete_paper("p3")  # trashed papers are skipped
    assert set(db.get_papers_with_dois()) == {("p1", "10.1/a")}
    db.close()


def test_set_paper_author_affiliations(db: Database):
    db.add_paper(
        Paper(id="p1", title="A", doi="10.1/a",
              authors=[Author(name="Alice"), Author(name="Bob")])
    )
    db.add_paper(
        Paper(id="p2", title="B", doi="10.1/b",
              authors=[Author(name="Carol")])  # same-name check across papers
    )
    db.add_paper(Paper(id="p3", title="C", authors=[Author(name="Dana")]))

    updated = db.set_paper_author_affiliations(
        "p1", {"Alice": "MIT", "Bob": "", "Dana": "ETH"}
    )
    assert updated == 1  # empty string skipped; Dana is not on p1

    p1 = db.get_paper("p1")
    by_name = {a.name: a.affiliation for a in p1.authors}
    assert by_name["Alice"] == "MIT"
    assert by_name["Bob"] is None

    # Dana (a different paper's author with NULL affiliation) must stay empty
    # — the UPDATE is scoped to p1's authors, not the whole table
    p3 = db.get_paper("p3")
    assert p3.authors[0].affiliation is None

    # Existing values are never overwritten
    db.set_paper_author_affiliations("p1", {"Alice": "Stanford"})
    assert db.get_paper("p1").authors[0].affiliation == "MIT"
    db.close()


def test_set_tag_top_and_sidebar_counts(db: Database):
    db.add_paper(Paper(id="p1", title="A", tags=["空箱调运", "共享经济"]))
    db.add_paper(Paper(id="p2", title="B", tags=["空箱调运"]))
    theme = db.get_tag_by_name("空箱调运")
    db.set_tag_top(theme.id, True)

    rows = db.list_tags_with_counts()
    # Themes first, then by paper count
    assert rows[0]["name"] == "空箱调运"
    assert rows[0]["is_top"] == 1
    assert rows[0]["paper_count"] == 2
    assert rows[1]["name"] == "共享经济"
    assert rows[1]["is_top"] == 0

    # Zero-count tags are omitted from the sidebar list
    db.create_tag("unused")
    names = {r["name"] for r in db.list_tags_with_counts()}
    assert "unused" not in names

    # Unmarking works
    db.set_tag_top(theme.id, False)
    assert db.get_tag(theme.id).is_top is False
    db.close()


def test_get_cooccurring_tags(db: Database):
    db.add_paper(Paper(id="p1", title="A", tags=["空箱调运", "共享经济", "风险"]))
    db.add_paper(Paper(id="p2", title="B", tags=["空箱调运", "共享经济"]))
    db.add_paper(Paper(id="p3", title="C", tags=["共享经济"]))  # not under the theme
    db.add_paper(
        Paper(id="p4", title="D", tags=["空箱调运"], auto_tags=["噪声"])
    )

    theme = db.get_tag_by_name("空箱调运")
    co = db.get_cooccurring_tags(theme.id)
    counts = {t["name"]: t["paper_count"] for t in co}
    # Auto-only co-occurrences are hidden by default; ordering is count desc
    assert counts == {"共享经济": 2, "风险": 1}
    assert co[0]["name"] == "共享经济"

    # Soft-deleted papers stop counting
    db.delete_paper("p2")
    counts = {t["name"]: t["paper_count"] for t in db.get_cooccurring_tags(theme.id)}
    assert counts == {"共享经济": 1, "风险": 1}

    # include_auto pulls the auto-only co-occurrence back in
    counts = {
        t["name"]: t["paper_count"]
        for t in db.get_cooccurring_tags(theme.id, include_auto=True)
    }
    assert counts["噪声"] == 1
    db.close()


def test_set_tag_parent_nesting(db: Database):
    db.add_paper(
        Paper(id="p1", title="A", tags=["空箱调运", "heuristic", "pso", "opt"])
    )
    theme = db.get_tag_by_name("空箱调运")
    heur = db.get_tag_by_name("heuristic")
    pso = db.get_tag_by_name("pso")
    opt = db.get_tag_by_name("opt")

    # Drag heuristic + pso onto opt: they nest under it and leave the
    # co-occurrence pool (visible under their parent only)
    db.set_tag_parent(heur.id, opt.id)
    db.set_tag_parent(pso.id, opt.id)
    assert db.get_tag(heur.id).parent_id == opt.id
    assert db.get_tag(pso.id).parent_id == opt.id
    co = {t["name"] for t in db.get_cooccurring_tags(theme.id)}
    assert co == {"opt"}
    rows = {r["name"]: r["parent_id"] for r in db.list_tags_with_counts()}
    assert rows["heuristic"] == opt.id and rows["pso"] == opt.id

    # Nesting a top-level theme clears its top flag; promoting it back
    # detaches it (the two hierarchy notions are exclusive)
    db.set_tag_top(theme.id, True)
    db.set_tag_parent(theme.id, opt.id)
    assert db.get_tag(theme.id).is_top is False
    db.set_tag_top(theme.id, True)
    assert db.get_tag(theme.id).parent_id is None

    # Cycle guard: opt is an ancestor of heur → rejected, as are self-nesting
    # and unknown ids
    with pytest.raises(ValueError):
        db.set_tag_parent(opt.id, heur.id)
    with pytest.raises(ValueError):
        db.set_tag_parent(heur.id, heur.id)
    with pytest.raises(ValueError):
        db.set_tag_parent(heur.id, "nonexistent")

    # Detach returns the tag to the co-occurrence pool
    db.set_tag_parent(pso.id, None)
    assert db.get_tag(pso.id).parent_id is None
    assert "pso" in {t["name"] for t in db.get_cooccurring_tags(theme.id)}

    # Deleting a parent lifts its children to the parent's parent
    db.set_tag_parent(heur.id, opt.id)
    db.set_tag_parent(opt.id, theme.id)
    db.delete_tag(opt.id)
    assert db.get_tag(heur.id).parent_id == theme.id
    db.close()


def test_tag_parent_survives_reopen(tmp_path: Path):
    path = tmp_path / "reopen.db"
    db = Database(path)
    db.add_paper(Paper(id="p1", title="A", tags=["a", "b"]))
    a, b = db.get_tag_by_name("a"), db.get_tag_by_name("b")
    db.set_tag_parent(b.id, a.id)
    db.close()

    # The startup migration must not misread the nesting column as legacy
    # hierarchy data and wipe it
    db = Database(path)
    assert db.get_tag(b.id).parent_id == a.id
    db.close()


def test_v2_database_upgrades_to_nesting(tmp_path: Path):
    """A v2 database (parent_id folded away, never user_version-stamped)
    must regain the nesting column on open. Regression: the parent index
    cannot be created before the ALTER has added the column."""
    import sqlite3

    path = tmp_path / "v2.db"
    db = Database(path)
    db.add_paper(Paper(id="p1", title="A", tags=["a"]))
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_tags_parent")  # a real v2 db never had it
    conn.execute("ALTER TABLE tags DROP COLUMN parent_id")
    conn.execute("PRAGMA user_version = 0")  # v2 databases were never stamped
    conn.commit()
    conn.close()

    db = Database(path)  # must not raise
    assert db.get_tag_by_name("a") is not None
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(tags)")]
    assert "parent_id" in cols
    db.close()


def _make_legacy_db(db_path: Path) -> None:
    """A pre-v2 database: parent_id hierarchy + perspectives/groups."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tags (
            id TEXT PRIMARY KEY, name TEXT UNIQUE, parent_id TEXT,
            color TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE papers (
            id TEXT PRIMARY KEY, title TEXT, doi TEXT,
            pdf_path TEXT, bibtex_key TEXT
        );
        CREATE TABLE paper_tags (
            paper_id TEXT, tag_id TEXT, source TEXT DEFAULT 'manual',
            PRIMARY KEY (paper_id, tag_id)
        );
        CREATE TABLE perspectives (
            id TEXT PRIMARY KEY, name TEXT, icon TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE tag_groups (
            id TEXT PRIMARY KEY, perspective_id TEXT, name TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE tag_group_members (
            group_id TEXT, tag_id TEXT, PRIMARY KEY (group_id, tag_id)
        );
        CREATE TABLE group_papers (
            group_id TEXT, paper_id TEXT, PRIMARY KEY (group_id, paper_id)
        );
        INSERT INTO papers (id, title) VALUES
            ('p1', 'Empty repositioning under uncertainty'),
            ('p2', 'Supply chain resilience review');
        INSERT INTO tags (id, name, parent_id) VALUES
            ('t1', '空箱调运', NULL),
            ('t2', '机会约束', 't1'),
            ('t3', '供应链风险', NULL);
        INSERT INTO paper_tags (paper_id, tag_id, source)
            VALUES ('p1', 't2', 'manual');
        INSERT INTO perspectives (id, name) VALUES ('ps1', 'Default');
        INSERT INTO tag_groups (id, perspective_id, name)
            VALUES ('g1', 'ps1', '供应链韧性');
        INSERT INTO group_papers (group_id, paper_id) VALUES ('g1', 'p2');
        """
    )
    conn.commit()
    conn.close()


def test_migrate_legacy_tag_model(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    db = Database(db_path)

    # Container tables dropped, parent_id column gone, backup written
    tables = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert not tables & {"perspectives", "tag_groups", "tag_group_members",
                         "group_papers"}
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(tags)")]
    # The legacy hierarchy was folded away; the parent_id column that exists
    # now is the manual drag-and-drop nesting column, starting empty
    assert "parent_id" in cols
    nested = db._conn.execute(
        "SELECT COUNT(*) FROM tags WHERE parent_id IS NOT NULL"
    ).fetchone()[0]
    assert nested == 0
    assert "is_top" in cols
    assert (tmp_path / "legacy.db.pre-v2.bak").exists()

    # Legacy parent_id theme and group name both became theme tags
    theme = db.get_tag_by_name("空箱调运")
    assert theme is not None and theme.is_top
    resilience = db.get_tag_by_name("供应链韧性")
    assert resilience is not None and resilience.is_top

    # p1 was back-filled with its parent theme; p2 got the group's tag
    p1 = db.get_paper("p1")
    assert set(p1.tags) == {"机会约束", "空箱调运"}
    assert set(db.get_paper("p2").tags) == {"供应链韧性"}

    # The derived tree under the theme still shows the old child tag
    co_names = {t["name"] for t in db.get_cooccurring_tags(theme.id)}
    assert "机会约束" in co_names

    # Reopening is a no-op (idempotent, no second backup needed)
    db.close()
    db2 = Database(db_path)
    assert set(db2.get_paper("p1").tags) == {"机会约束", "空箱调运"}
    db2.close()
