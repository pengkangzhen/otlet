"""Rotating file logging + uncaught-exception capture.

Windowed GUI builds have no console (sys.stderr is None — anything
printed would crash the app); CLI users reporting bugs need something
to attach to an issue. Both get ~/.otlet/logs/otlet.log.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

_LOGGER_NAME = "otlet"


def log_path(data_dir: Path) -> Path:
    return data_dir / "logs" / "otlet.log"


def setup_logging(data_dir: Path) -> Path:
    """Configure the otlet logger to a rotating file. Reconfigures when
    the requested path differs from the bound one (tests use one data
    dir per case); returns the log file path."""
    path = log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    bound = getattr(logger, "_otlet_log_path", None)
    if logger.handlers and bound == str(path):
        return path
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger._otlet_log_path = str(path)  # type: ignore[attr-defined]
    return path


def install_excepthook() -> None:
    """Log uncaught exceptions instead of losing them to devnull."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        logging.getLogger(_LOGGER_NAME).error(
            "uncaught exception", exc_info=(exc_type, exc, tb)
        )
        previous(exc_type, exc, tb)

    sys.excepthook = hook


class StderrToLog(TextIO):
    """A sys.stderr replacement for windowed builds: writes go to the
    log instead of a nonexistent console."""

    def write(self, s: str) -> int:  # pragma: no cover - thin adapter
        if s and s.strip():
            logging.getLogger(_LOGGER_NAME).error(s.rstrip())
        return len(s)

    def flush(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass
