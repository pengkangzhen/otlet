"""Tests for the cross-platform shell helpers (otlet/platform.py)."""

from pathlib import Path

import pytest

from otlet import platform


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture every subprocess call instead of spawning processes."""
    calls: dict[str, list] = {"Popen": [], "run": []}

    def fake_popen(cmd, **kwargs):
        calls["Popen"].append(cmd)

    def fake_run(cmd, **kwargs):
        calls["run"].append((cmd, kwargs.get("check")))

    monkeypatch.setattr(platform.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(platform.subprocess, "run", fake_run)
    return calls


# ── open_path ───────────────────────────────────────────────


def test_open_path_macos(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "darwin")
    platform.open_path("/tmp/some paper.pdf")
    assert no_spawn["Popen"] == [["open", "/tmp/some paper.pdf"]]


def test_open_path_windows(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "win32")
    platform.open_path(r"C:\Papers\some paper.pdf")
    # the empty "" is start's window title — without it the quoted
    # path itself would be eaten as the title
    assert no_spawn["Popen"] == [
        ["cmd", "/c", "start", "", r"C:\Papers\some paper.pdf"]
    ]


def test_open_path_linux(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "linux")
    platform.open_path("/tmp/a.pdf")
    assert no_spawn["Popen"] == [["xdg-open", "/tmp/a.pdf"]]


# ── reveal_path ─────────────────────────────────────────────


def test_reveal_path_macos(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "darwin")
    platform.reveal_path("/tmp/a.pdf")
    assert no_spawn["run"] == [(["open", "-R", "/tmp/a.pdf"], True)]


def test_reveal_path_windows_no_check(monkeypatch, no_spawn):
    """explorer exits non-zero even on success — check must be off."""
    monkeypatch.setattr(platform, "PLATFORM", "win32")
    platform.reveal_path(r"C:\Papers\a.pdf")
    assert no_spawn["run"] == [
        (["explorer", r"/select,C:\Papers\a.pdf"], False)
    ]


def test_reveal_path_linux_opens_parent(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "linux")
    platform.reveal_path("/tmp/sub/a.pdf")
    assert no_spawn["run"] == [(["xdg-open", "/tmp/sub"], True)]


# ── open_url ────────────────────────────────────────────────


def test_open_url_windows(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "win32")
    platform.open_url("https://example.org/paper")
    assert no_spawn["Popen"] == [
        ["cmd", "/c", "start", "", "https://example.org/paper"]
    ]


def test_open_url_macos(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "darwin")
    platform.open_url("https://example.org")
    assert no_spawn["Popen"] == [["open", "https://example.org"]]


def test_pathlib_inputs_accepted(monkeypatch, no_spawn):
    monkeypatch.setattr(platform, "PLATFORM", "darwin")
    platform.open_path(Path("/tmp/a.pdf"))
    assert no_spawn["Popen"] == [["open", "/tmp/a.pdf"]]
