from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class Settings(BaseModel):
    """Application settings loaded from a YAML config file.

    All data (library.db + pdfs/) lives under a single data_dir; the
    config file itself is always ~/.otlet/config.yaml.
    """

    data_dir: Path = Path.home() / ".otlet"
    default_limit: int = 10
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    api_base: str | None = None
    s2_api_key: str | None = None
    # OpenAlex polite-pool contact email (optional, improves rate limits)
    openalex_email: str | None = None
    theme: Literal["light", "dark", "classic-light", "classic-dark"] = "light"

    @classmethod
    def config_path(cls) -> Path:
        """Global config location."""
        return Path.home() / ".otlet" / "config.yaml"

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        """Load settings from a YAML config file, falling back to defaults."""
        if config_path is None:
            config_path = cls.config_path()
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
        """Save current settings to a YAML config file."""
        if config_path is None:
            config_path = Settings.config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # mode="json" keeps Path values as plain strings — yaml.safe_load
        # in load() cannot parse the python-specific tags yaml.dump
        # emits for Path objects
        config_path.write_text(
            yaml.dump(
                self.model_dump(mode="json"),
                default_flow_style=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"
