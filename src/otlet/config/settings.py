from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from otlet import secrets as _secrets


class Settings(BaseModel):
    """Application settings loaded from a YAML config file.

    All data (library.db + pdfs/) lives under a single data_dir; the
    config file itself is always ~/.otlet/config.yaml. Secret fields
    (api_key, s2_api_key) are stored in the OS keychain when one is
    available — the yaml keeps a "keychain" placeholder.
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
        instance: "Settings" = cls()
        if config_path.exists():
            try:
                raw = yaml.safe_load(
                    config_path.read_text(encoding="utf-8")
                )
                if isinstance(raw, dict):
                    instance = cls(**raw)
            except (yaml.YAMLError, TypeError, ValueError):
                pass
        # keychain placeholders resolve to the real secret at load time
        for field in _secrets.SECRET_FIELDS:
            value = getattr(instance, field, None)
            if value is None or _secrets.is_placeholder(value):
                secret = _secrets.get_secret(field)
                if secret:
                    setattr(instance, field, secret)
        return instance

    def save(self, config_path: Path | None = None) -> None:
        """Save current settings to a YAML config file.

        Secrets move to the OS keychain when available; the yaml keeps
        a placeholder. Without a keyring backend the value stays in the
        yaml (documented fallback).
        """
        if config_path is None:
            config_path = Settings.config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        dump = self.model_dump(mode="json")
        for field in _secrets.SECRET_FIELDS:
            value = dump.get(field)
            if value:
                if _secrets.set_secret(field, str(value)):
                    dump[field] = _secrets.placeholder()
        # mode="json" keeps Path values as plain strings — yaml.safe_load
        # in load() cannot parse the python-specific tags yaml.dump
        # emits for Path objects
        config_path.write_text(
            yaml.dump(
                dump,
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
