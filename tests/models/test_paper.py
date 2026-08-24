from agent_lit.models.author import Author
from agent_lit.models.paper import Paper


def test_paper_creation():
    paper = Paper(
        id="abc123",
        title="Attention Is All You Need",
        authors=[
            Author(name="Ashish Vaswani"),
            Author(name="Noam Shazeer"),
        ],
        year=2017,
        venue="NeurIPS",
        keywords=["transformer", "attention"],
    )
    assert paper.title == "Attention Is All You Need"
    assert len(paper.authors) == 2
    assert paper.year == 2017
    assert "transformer" in paper.keywords


def test_paper_display_authors():
    p1 = Paper(id="1", title="T", authors=[Author(name="Alice")])
    assert p1.display_authors == "Alice"

    p2 = Paper(id="2", title="T", authors=[
        Author(name="Alice"),
        Author(name="Bob"),
        Author(name="Carol"),
    ])
    assert p2.display_authors == "Alice, Bob, Carol"

    p3 = Paper(id="3", title="T", authors=[
        Author(name="Alice"),
        Author(name="Bob"),
        Author(name="Carol"),
        Author(name="Dave"),
    ])
    assert p3.display_authors == "Alice et al."


def test_paper_full_authors_no_truncation():
    authors = [Author(name=n) for n in ["Alice", "Bob", "Carol", "Dave", "Erin"]]
    p = Paper(id="4", title="T", authors=authors)
    assert p.full_authors == "Alice, Bob, Carol, Dave, Erin"
    assert p.display_authors == "Alice et al."  # compact variant still truncates


def test_paper_author_fields_survive_serialization():
    """computed_field must appear in model_dump — the web API serializes
    papers with model_dump(mode="json") and the frontend reads these keys
    (a bare @property would silently vanish; this regressed once)."""
    p = Paper(id="5", title="T", authors=[Author(name="Alice"), Author(name="Bob")])
    d = p.model_dump(mode="json")
    assert d["display_authors"] == "Alice, Bob"
    assert d["full_authors"] == "Alice, Bob"


def test_paper_default_id():
    paper = Paper(title="No ID Paper")
    assert paper.id  # should have a default value


def test_paper_display_authors_empty():
    paper = Paper(id="x", title="Empty")
    assert paper.display_authors == "Unknown"
