from pathlib import Path

import pytest

from agent_lit.models.author import Author
from agent_lit.models.paper import Paper
from agent_lit.storage.database import Database


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

    # Unlinked, ungrouped tags are hidden from the default (manual) view…
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

    # Default sidebar view hides auto-only ungrouped tags
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

    # Category rides along in list_tags and the perspective view
    db.add_paper(Paper(id="p1", title="X", tags=["column-generation"]))
    assert db.list_tags()[0].category == "algorithm"
    persp = db.create_perspective("Default")
    group = db.create_group(persp["id"], "空箱调运")
    db.add_tag_to_group(group["id"], tag.id)
    view = db.get_perspective_view(persp["id"])
    assert view[0]["tags"][0]["category"] == "algorithm"

    # Empty value clears the classification
    db.set_tag_category(tag.id, "")
    assert db.get_tag(tag.id).category is None

    # Invalid category is rejected
    import pytest as _pytest
    with _pytest.raises(ValueError):
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
