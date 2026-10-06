"""Desktop app entry point — pywebview native window."""

from __future__ import annotations

import atexit
import html
import os
import sys
from pathlib import Path

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from otlet.config.settings import Settings
from otlet.logs import StderrToLog, install_excepthook, setup_logging
from otlet.storage.database import DatabaseCorruptError
from otlet.web.api import Api


def _ensure_stdio() -> None:
    """PyInstaller console=False builds leave sys.stdout/stderr as None.

    stdout goes to devnull; stderr is routed into the rotating log so
    library tracebacks are never lost in windowed builds.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = StderrToLog()  # type: ignore[assignment]


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
            MenuAction("About Otlet", js("showAbout()")),
            MenuSeparator(),
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



def _corrupt_db_window(error: DatabaseCorruptError) -> None:
    """Fail-closed: show what happened and how to recover instead of
    silently dying in a windowed build."""
    msg = html.escape(str(error)).replace("\n", "<br>")
    webview.create_window(
        "Otlet — 数据库无法打开",
        html=f"""<body style="font-family:system-ui;background:#1e1e1e;
color:#eee;padding:40px;line-height:1.6">
<h2 style="color:#e5484d">文献库数据库损坏，Otlet 已停止</h2>
<p>{msg}</p>
<p>恢复方法：在终端运行
<code style="background:#333;padding:2px 6px;border-radius:4px">
otlet restore &lt;备份文件&gt;</code></p>
<p style="opacity:.7">日志位置见 ~/.otlet/logs/otlet.log</p>
</body>""",
        width=640, height=320,
    )
    webview.start(debug=False)


def launch_gui(settings: Settings | None = None) -> None:
    """Launch the Otlet desktop application."""
    settings = settings or Settings.load()
    setup_logging(settings.data_dir)
    install_excepthook()
    _ensure_stdio()

    try:
        api = Api(settings)
    except DatabaseCorruptError as e:
        _corrupt_db_window(e)
        return

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
        # Native menu bar is a pywebview cocoa (macOS) feature — on
        # Windows/Linux the same actions live in the page UI itself
        menu=_build_menu(api) if sys.platform == "darwin" else None,
    )
    api.set_window(window)

    webview.start(debug=False)
    api.close()


if __name__ == "__main__":
    launch_gui()
