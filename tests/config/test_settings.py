"""Regression tests for Settings YAML round-trip."""

from pathlib import Path

from agent_lit.config.settings import Settings


def test_save_then_load_roundtrips(tmp_path: Path):
    """save() must emit yaml.safe_load-compatible YAML (plain strings for
    Paths — regression: model_dump() emitted !!python/object/apply tags
    that load() silently swallowed and fell back to defaults)."""
    cfg = tmp_path / "config.yaml"
    settings = Settings(data_dir=tmp_path / "lib", lit_model="glm-4.7", theme="dark")
    settings.save(cfg)

    loaded = Settings.load(cfg)
    assert loaded.data_dir == tmp_path / "lib"
    assert loaded.lit_model == "glm-4.7"
    assert loaded.theme == "dark"


def test_load_missing_file_returns_defaults(tmp_path: Path):
    settings = Settings.load(tmp_path / "missing.yaml")
    assert settings.lit_model == Settings().lit_model
