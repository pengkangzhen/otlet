from agent_lit.agents.classify import ClassifyAgent
from agent_lit.agents.search import SearchAgent
from agent_lit.llm.provider import LLMProvider


def test_search_agent_instantiation():
    agent = SearchAgent()
    assert agent.name == "search"


def test_classify_agent_instantiation():
    llm = LLMProvider(model="gpt-4o-mini")
    agent = ClassifyAgent(llm)
    assert agent.name == "classify"


def test_classify_parse_tags():
    """Test the tag parsing logic with various LLM response formats."""
    # JSON array
    assert ClassifyAgent._parse_tags('["rl", "robotics"]') == ["rl", "robotics"]

    # JSON with extra text
    assert ClassifyAgent._parse_tags(
        'Here are the tags: ["nlp", "transformer"]'
    ) == ["nlp", "transformer"]

    # Plain comma-separated
    assert ClassifyAgent._parse_tags("a, b, c") == ["a", "b", "c"]


def test_search_agent_to_paper():
    """Test the paper conversion from S2 API response."""
    raw = {
        "paperId": "abc123",
        "title": "Test Paper",
        "year": 2024,
        "venue": "NeurIPS",
        "externalIds": {"DOI": "10.1234/test"},
        "url": "https://example.com",
        "abstract": "A test abstract.",
        "citationCount": 42,
        "authors": [{"name": "Alice"}, {"name": "Bob"}],
    }
    paper = SearchAgent._to_paper(raw)
    assert paper.title == "Test Paper"
    assert paper.year == 2024
    assert len(paper.authors) == 2
    assert paper.citation_count == 42


def test_search_agent_to_paper_affiliations():
    """S2 author affiliations map onto the first affiliation string."""
    raw = {
        "paperId": "abc123",
        "title": "Affil Paper",
        "authors": [
            {"name": "Alice", "affiliations": ["MIT", "Stanford"]},
            {"name": "Bob", "affiliations": []},
        ],
    }
    paper = SearchAgent._to_paper(raw)
    assert paper.authors[0].affiliation == "MIT"
    assert paper.authors[1].affiliation is None
