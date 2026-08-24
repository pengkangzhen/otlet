from pydantic import BaseModel


class Author(BaseModel):
    """Represents an author of an academic paper.

    `name` is the canonical display string ("First Last"); first/last are
    stored separately when known for correct BibTeX output.
    """

    name: str
    first_name: str = ""
    last_name: str = ""
    affiliation: str | None = None
    orcid: str | None = None
    email: str | None = None
