"""Regression tests for Settings YAML round-trip and single data_dir config."""

from pathlib import Path

import yaml

from otlet.config.settings import Settings


def test_save_then_load_roundtrips(tmp_path: Path):
    """save() must emit yaml.safe_load-compatible YAML (plain strings for
    Paths — regression: model_dump() emitted !!python/object/apply tags
    that load() silently swallowed and fell back to defaults)."""
    cfg = tmp_path / "config.yaml"
    settings = Settings(
        data_dir=tmp_path / "lib",
        model="glm-4.7",
        theme="dark",
    )
    settings.save(cfg)

    loaded = Settings.load(cfg)
    assert loaded.data_dir == tmp_path / "lib"
    assert loaded.db_path == tmp_path / "lib" / "library.db"
    assert loaded.pdf_dir == tmp_path / "lib" / "pdfs"
    assert loaded.model == "glm-4.7"
    assert loaded.theme == "dark"


def test_load_missing_file_returns_defaults(tmp_path: Path):
    settings = Settings.load(tmp_path / "missing.yaml")
    assert settings.model == Settings().model
    assert settings.data_dir == Path.home() / ".otlet"


def test_load_ignores_legacy_multi_library_keys(tmp_path: Path):
    """A config written by the multi-library build loads fine; the stale
    libraries/active_library keys are dropped on the next save."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "libraries": {
                    "default": str(tmp_path / "old"),
                    "work": str(tmp_path / "w"),
                },
                "active_library": "work",
                "model": "glm-4.7",
            }
        ),
        encoding="utf-8",
    )

    loaded = Settings.load(cfg)
    assert loaded.model == "glm-4.7"
    assert loaded.data_dir == Path.home() / ".otlet"

    loaded.save(cfg)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "libraries" not in raw
    assert "active_library" not in raw
