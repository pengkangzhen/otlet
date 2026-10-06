"""Desktop app entry point — pywebview native window."""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from otlet.config.settings import Settings
from otlet.web.api import Api


def _ensure_stdio() -> None:
    """PyInstaller console=False builds leave sys.stdout/stderr as None.

    Any library print/traceback would then raise (or worse) with no console
    attached — route them to devnull so the windowed app never dies on IO.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def _js_cb(api: Api, code: str):
    """Create a menu callback that evaluates JS in the webview.

    Uses api._window for late binding — the window is set after
    create_window() returns but before any menu item is clicked.
    """
    def _fn():
        try:
            api._window.evaluate_js(code)
        except Exception:
            pass
    return _fn


def _build_menu(api: Api) -> list[Menu]:
    """Build the native macOS menu bar."""

    def js(code: str):
        return _js_cb(api, code)

    return [
        # __app__ menu: items appear under the macOS "Otlet" app menu
        Menu("__app__", [
            MenuAction("Preferences…", js("openSettingsDialog()")),
            MenuAction("Toggle Theme", js("toggleThemeMenu()")),
        ]),
        # Custom menus (default Edit/View menus are auto-added by pywebview)
        Menu("File", [
            MenuAction("Import PDF…", js("openImportDialog()")),
            MenuAction("Import Folder…", js("importFolder()")),
            MenuSeparator(),
            MenuAction("Import from BibTeX…", js("importBibtex()")),
            MenuAction("Import from Zotero…", js("openZoteroDialog()")),
            MenuSeparator(),
            MenuAction("Search Online…", js("searchOnline()")),
            MenuSeparator(),
            MenuAction("Export BibTeX…", js("exportBibtex()")),
        ]),
        Menu("Tags", [
            MenuAction("New Theme…", js("createTheme()")),
        ]),
        Menu("View", [
            MenuAction("Knowledge Graph", js("openGraph()")),
        ]),
    ]



def launch_gui(settings: Settings | None = None) -> None:
    """Launch the Otlet desktop application."""
    _ensure_stdio()
    settings = settings or Settings.load()
    api = Api(settings)
    # Checkpoint WAL even if the process exits unexpectedly (crash, kill)
    atexit.register(api.close)

    static_dir = Path(__file__).parent / "static"

    window = webview.create_window(
        title="Otlet",
        url=str(static_dir / "index.html"),
        js_api=api,
        width=1280,
        height=800,
        min_size=(900, 600),
        text_select=True,
        menu=_build_menu(api),
    )
    api.set_window(window)

    webview.start(debug=False)
    api.close()


if __name__ == "__main__":
    launch_gui()
