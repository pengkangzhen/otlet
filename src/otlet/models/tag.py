from datetime import datetime

from pydantic import BaseModel, Field


class Tag(BaseModel):
    """Represents a tag for organizing papers.

    Supports hierarchical tags via parent_id, enabling tree-like
    tag structures (e.g., 'method/RL', 'topic/NLP').
    """

    id: str
    name: str
    parent_id: str | None = None
    color: str | None = None
    # Research-facet classification used by the project overview panel:
    # 'problem' | 'model' | 'algorithm' | None (uncategorized)
    category: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def display_name(self) -> str:
        """Return a human-readable tag name (leaf portion)."""
        return self.name.rsplit("/", maxsplit=1)[-1]
