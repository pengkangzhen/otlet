from datetime import datetime

from pydantic import BaseModel, Field


class Tag(BaseModel):
    """A tag. is_top marks research themes (top-level sidebar entries);
    parent_id stores manual nesting (a tag dragged onto another tag).
    Free tags (neither top nor nested) surface via co-occurrence.
    """

    id: str
    name: str
    color: str | None = None
    is_top: bool = False
    parent_id: str | None = None
    # Research-facet classification used by the project overview panel:
    # 'problem' | 'model' | 'algorithm' | None (uncategorized)
    category: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def display_name(self) -> str:
        """Return a human-readable tag name (leaf portion)."""
        return self.name.rsplit("/", maxsplit=1)[-1]
