import uuid
from datetime import date

from pydantic import BaseModel, Field, computed_field

from otlet.models.author import Author


class Paper(BaseModel):
    """Represents an academic paper / literature entry."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    authors: list[Author] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None
    language: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    citation_count: int | None = None
    added_date: str = Field(default_factory=lambda: date.today().isoformat())
    tags: list[str] = Field(default_factory=list)
    # LLM/heuristic tags, kept out of the manual sidebar
    auto_tags: list[str] = Field(default_factory=list)
    pdf_path: str | None = None
    # SHA-256 of the stored PDF's bytes — exact-file dedup key
    pdf_fingerprint: str | None = None
    bibtex_key: str | None = None
    paper_type: str | None = None  # conference/journal/book/thesis/preprint
    deleted_date: str | None = None  # set while the paper is in the trash

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_authors(self) -> str:
        """Compact author string for display (truncates past 3 names)."""
        if not self.authors:
            return "Unknown"
        names = [a.name for a in self.authors]
        if len(names) <= 3:
            return ", ".join(names)
        return f"{names[0]} et al."

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_authors(self) -> str:
        """Complete author list without 'et al.' truncation (detail panel)."""
        if not self.authors:
            return "Unknown"
        return ", ".join(a.name for a in self.authors)
