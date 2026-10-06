"""Tests for OS-keychain secret storage (fake keyring; no real backend
needed) and the no-LLM guidance paths."""

import json
from pathlib import Path

import pytest

from otlet.config.settings import Settings


class FakeKeyring:
    """In-memory keyring standing in for otlet.secrets' backend calls."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.works = True

    def set_password(self, service, name, value):
        assert service == "otlet"
        if not self.works:
            raise RuntimeError("no backend")
        self.store[name] = value

    def get_password(self, service, name):
        if not self.works:
            raise RuntimeError("no backend")
        return self.store.get(name)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    kr = FakeKeyring()

    def fake_backend():  # simulate `import keyring` returning our fake
        return kr

    monkeypatch.setattr("otlet.secrets.available", lambda: kr.works)
    monkeypatch.setattr(
        "otlet.secrets.get_secret", lambda n: kr.store.get(n)
        if kr.works else None)
    monkeypatch.setattr(
        "otlet.secrets.set_secret",
        lambda n, v: (kr.set_password("otlet", n, v), True)[1]
        if kr.works else False,
    )
    return kr


def test_save_moves_secrets_to_keychain(
    tmp_path: Path, fake_keyring: FakeKeyring
):
    config = tmp_path / "config.yaml"
    s = Settings(data_dir=tmp_path, api_key="sk-secret-123", s2_api_key=None)
    s.save(config_path=config)

    text = config.read_text(encoding="utf-8")
    assert "sk-secret-123" not in text      # plaintext never lands in yaml
    assert "keychain" in text               # placeholder does
    assert fake_keyring.store["api_key"] == "sk-secret-123"

    # round trip: load resolves the placeholder back to the real secret
    loaded = Settings.load(config_path=config)
    assert loaded.api_key == "sk-secret-123"


def test_load_explicit_plaintext_still_works(
    tmp_path: Path, fake_keyring: FakeKeyring
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "data_dir: /tmp/x\napi_key: sk-legacy\n", encoding="utf-8"
    )
    loaded = Settings.load(config_path=config)
    assert loaded.api_key == "sk-legacy"  # backward compatible


def test_fallback_to_yaml_without_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("otlet.secrets.set_secret", lambda n, v: False)
    monkeypatch.setattr("otlet.secrets.get_secret", lambda n: None)

    config = tmp_path / "config.yaml"
    s = Settings(data_dir=tmp_path, api_key="sk-plain")
    s.save(config_path=config)
    assert "sk-plain" in config.read_text(encoding="utf-8")
    assert Settings.load(config_path=config).api_key == "sk-plain"


# ── no-LLM guidance ────────────────────────────────────────


def test_send_chat_message_without_key_guides(tmp_path: Path):
    from otlet.models.paper import Paper
    from otlet.web.api import Api

    api = Api(Settings(data_dir=tmp_path))  # no api_key / api_base
    api._db.add_paper(Paper(id="p1", title="T"))
    result = json.loads(api.send_chat_message("p1", "hi"))
    assert result["end_reason"] == "no_key"
    assert "api_key" in result["response"]
    api.close()


def test_cmd_chat_without_key_exits_with_hint(
    tmp_path: Path, capsys
):
    from otlet import cli

    rc = cli.main(["chat", "p1"], settings=Settings(data_dir=tmp_path))
    out = capsys.readouterr().out
    assert rc == 1
    assert "settings set api_key" in out
