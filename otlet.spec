# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Otlet — works on macOS and Windows build
hosts (run build.sh on macOS / build.ps1 on Windows)."""

import sys
from pathlib import Path

block_cipher = None

src_dir = Path(SPECPATH) / "src"

# litellm runtime data ONLY (≈5.5 MB): every .json/.yaml in the
# package (model prices, provider endpoint maps, tokenizer configs —
# they are scattered across subpackages, e.g. containers/endpoints
# .json). The package's CODE still comes in through Analysis — the
# proxy's web UI assets (js/svg/png, ~40 MB) and any .py are excluded
# so nothing ships twice.
litellm_pkg = Path(__import__("litellm").__file__).parent
litellm_datas = [
    (
        str(p),
        # datas targets are DIRECTORIES: put each file into its
        # package-relative parent dir (litellm/, litellm/containers/, …)
        str(p.relative_to(litellm_pkg.parent).parent),
    )
    for p in litellm_pkg.rglob("*")
    if p.is_file()
    and p.suffix in (".json", ".yaml")
    and "__pycache__" not in p.parts
]

# pywebview ships per-platform GUI backends as lazily imported modules
if sys.platform == "darwin":
    webview_hiddenimports = ["pywebview.platforms.cocoa"]
else:
    webview_hiddenimports = [
        "pywebview.platforms.winforms",
        "pywebview.platforms.edgechromium",
        "clr",  # pythonnet bridge used by the winforms backend
    ]

a = Analysis(
    [str(src_dir / "otlet" / "cli.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[
        # Web UI static files
        (str(src_dir / "otlet" / "web" / "static"), "otlet/web/static"),
        *litellm_datas,
    ],
    hiddenimports=[
        "pywebview",
        "pywebview.platforms",
        *webview_hiddenimports,
        "litellm",
        "pymupdf",
        "fitz",
        "pydantic",
        "httpx",
        "yaml",
        "rich",
        "bottle",
        # tiktoken loads encodings via the tiktoken_ext namespace
        # package (entry-point plugins) — PyInstaller misses it
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        # NOTE: litellm's proxy subpackage (~40 MB) cannot be excluded —
        # litellm's own import chain references it ("No module named
        # 'litellm.proxy'" at completion time). It rides along in the
        # compressed PYZ instead.
        # GUI backends for platforms this build is not targeting (CI
        # skips installing them; keep the belt-and-braces excludes)
        "PyQt6",
        "qtpy",
        "PySide6",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "IPython",
        "notebook",
        "jupyter",
        "pytest",
        "ruff",
        "pylint",
        "mypy",
        "black",
        "isort",
        "setuptools._vendor.jaraco",
        "setuptools._vendor.more_itertools",
        "setuptools._vendor.tomli",
        "setuptools._vendor.wheel",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onedir mode: faster startup (no extraction on each launch)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="otlet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="otlet",
)
