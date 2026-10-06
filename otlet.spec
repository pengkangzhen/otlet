# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Otlet — macOS desktop app."""

from pathlib import Path

block_cipher = None

src_dir = Path(SPECPATH) / "src"

# Collect litellm package
litellm_pkg = Path(__import__("litellm").__file__).parent

a = Analysis(
    [str(src_dir / "otlet" / "cli.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[
        # litellm — entire package including JSON configs & tokenizers
        (str(litellm_pkg), "litellm"),
        # Web UI static files
        (str(src_dir / "otlet" / "web" / "static"), "otlet/web/static"),
    ],
    hiddenimports=[
        "pywebview",
        "pywebview.platforms",
        "pywebview.platforms.cocoa",
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
