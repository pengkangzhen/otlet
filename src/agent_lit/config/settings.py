from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Application settings loaded from a YAML config file."""

    data_dir: Path = Field(default=Path.home() / ".agent-lit")
    library_file: str = "library.yaml"
    default_limit: int = 10
    lit_model: str = "gpt-4o-mini"
    lit_api_key: str | None = None
    lit_api_base: str | None = None
    s2_api_key: str | None = None
    theme: Literal["light", "dark", "classic-light", "classic-dark"] = "light"

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        """Load settings from a YAML file, falling back to defaults."""
        if config_path is None:
            config_path = Path.home() / ".agent-lit" / "config.yaml"
        # Ensure data directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            try:
                raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return cls(**raw)
            except (yaml.YAMLError, TypeError, ValueError):
                pass
        return cls()

    def save(self, config_path: Path | None = None) -> None:
        """Save current settings to a YAML file."""
        if config_path is None:
            config_path = Path.home() / ".agent-lit" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.dump(self.model_dump(), default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

    @property
    def library_path(self) -> Path:
        return self.data_dir / self.library_file

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"
