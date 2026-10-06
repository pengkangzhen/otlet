"""Cross-platform shell helpers: open a file, reveal it in the file
manager, open a URL.

macOS uses `open` / `open -R`, Windows `cmd /c start` /
`explorer /select,`, Linux `xdg-open`. The platform lives in a module
attribute so tests can exercise every branch on any OS.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PLATFORM = sys.platform


def open_path(path: Path | str) -> None:
    """Open a file with the system default application (fire & forget)."""
    p = str(path)
    if PLATFORM == "darwin":
        cmd = ["open", p]
    elif PLATFORM == "win32":
        # start's first quoted argument is the window title — pass ""
        cmd = ["cmd", "/c", "start", "", p]
    else:
        cmd = ["xdg-open", p]
    subprocess.Popen(cmd)


def reveal_path(path: Path | str) -> None:
    """Reveal a file in the system file manager, selected if possible."""
    p = str(path)
    if PLATFORM == "darwin":
        subprocess.run(["open", "-R", p], check=True)
    elif PLATFORM == "win32":
        # explorer always exits non-zero, even on success — no check
        subprocess.run(["explorer", f"/select,{p}"], check=False)
    else:
        # No universal "select file" on Linux — open the parent folder
        subprocess.run(["xdg-open", str(Path(p).parent)], check=True)


def open_url(url: str) -> None:
    """Open a URL in the system default browser (fire & forget)."""
    if PLATFORM == "darwin":
        cmd = ["open", url]
    elif PLATFORM == "win32":
        cmd = ["cmd", "/c", "start", "", url]
    else:
        cmd = ["xdg-open", url]
    subprocess.Popen(cmd)
