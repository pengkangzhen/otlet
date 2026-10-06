# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Otlet is a macOS desktop app for academic literature management. It features a pywebview-based GUI, SQLite storage, PDF metadata extraction, multi-source import, AI chat, a multi-perspective tag system, and an interactive knowledge graph.

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run GUI directly (development)
uv run --no-sync python -m otlet.web.app

# Run CLI commands
uv run --no-sync otlet list
uv run --no-sync otlet add paper.pdf            # PDF / --doi / --query / --bibtex / --folder / --zotero
uv run --no-sync otlet list -q "text" --tags a,b
uv run --no-sync otlet show <paper_id>
uv run --no-sync otlet chat <paper_id>          # AI 对话（-m 单次提问）

# Build macOS .app bundle
./build.sh
# Output: dist/otlet/ (onedir), dist/Otlet.app

# Dev auto-reload (also runs automatically via ZCode Stop hook)
bash reload.sh
# Logs: /tmp/otlet-reload.log; state: .reload-stamp (delete to force full rebuild)

# Lint
uv run --no-sync ruff check .

# Test
uv run --no-sync pytest
uv run --no-sync pytest tests/storage/  # single directory
uv run --no-sync pytest tests/storage/test_database.py::test_name  # single test
```

## Dev Auto-Reload (ZCode Stop Hook)

A project-level Stop hook (`.zcode/config.json`) runs `reload.sh` after every agent reply. It detects source changes newer than `.reload-stamp` and syncs `dist/Otlet.app` automatically:

- **index.html-only change** → hot-patches the bundled copy inside the .app (the app loads it from disk at `Contents/MacOS/_internal/otlet/web/static/`) and relaunches instantly.
- **Any other change** (py / spec / assets / pyproject) → `reload-build.sh` runs `build.sh` in the background (serialized via a mkdir lock; stamp touched only on success so failed builds retry) and relaunches when done.
- **No changes** → silent exit.

Implication for agents: after finishing a feature you normally do NOT need to run `build.sh` manually — the hook rebuilds and relaunches for user verification. Only build manually when you must verify packaging yourself.

## Architecture

### UI Entry Points
- **GUI (primary)**: `web/app.py` → `web/api.py` → pywebview native window loading `web/static/index.html`. The HTML/CSS/JS is the entire frontend — no framework, single file.
- **CLI (full feature parity for management tasks)**: `cli.py` with argparse subcommands (add/list/show/open/rm/trash/tag/tags/autotag/note/chat/search/export/settings/gui). `cli.py` is also the PyInstaller entry script — **running with no subcommand must keep launching the GUI** or the packaged .app breaks.
- Both front-ends share the import pipeline through `services/importers.py` (dedup, auto-tag extraction, BibTeX parsing, paper-type inference, PDF/Zotero import flows). Never reimplement these rules in one front-end only.

### Backend Layers (`src/otlet/`)
- **`services/importers.py`** — Import pipelines shared by GUI and CLI: `find_duplicate()` (DOI case-insensitive → fuzzy title), `import_pdf_file()`, `save_zotero_item()`, `parse_bibtex()`/`entry_to_paper()`, `extract_auto_tags()` (deterministic, keywords+venue+abstract), `infer_paper_type()`.
- **`web/api.py`** — The single pywebview-exposed API class. Every JS→Python call goes through here. Thin controller: import flows delegate to `services/importers.py`.
- **`web/app.py`** — Creates the pywebview window and native macOS menu bar (`Menu("__app__")`, `"File"`, `"Tags"`, `"View"`). Menu callbacks use `window.evaluate_js()` via late-binding through `api._window`.
- **`storage/database.py`** — SQLite (WAL mode). Manages papers, tags, authors, conversations, **perspectives**, **tag groups**, and **group membership**. All tag names are normalized to lowercase-hyphenated on create/link. Includes `get_knowledge_graph()` for graph data extraction.
- **`storage/pdf_metadata.py`** — 6-layer PDF identification pipeline: XMP DOI → text DOI → ArXiv ID → XMP title → heuristic title → text fallback.
- **`storage/pdf_store.py`** — Copies PDFs into `~/.otlet/pdfs/{paper_id}.pdf`, extracts text via PyMuPDF.
- **`storage/zotero_import.py`** — Reads Zotero's `zotero.sqlite` (temp copy to avoid lock). Two-phase: scan → save-item-by-item for progress.
- **`agents/`** — `SearchAgent` (Semantic Scholar API), `ChatAgent` (LLM + PDF text + metadata), `ClassifyAgent` (LLM tag suggestion). All inherit from `AgentBase`.
- **`llm/provider.py`** — Thin litellm wrapper. Config via `~/.otlet/config.yaml` or env vars `OTLET_MODEL`, `OTLET_API_KEY`, `OTLET_API_BASE`.
- **`config/settings.py`** — Pydantic Settings model with `.load()` / `.save()` for YAML persistence. Auto-creates `~/.otlet/` directory.

### Frontend (`web/static/index.html`)
Single HTML file containing all CSS, HTML, and JS. Key patterns:
- **pywebview bridge**: `pywebview.api.method_name(args)` returns a Promise resolving to a JSON string. Always `JSON.parse()` the result. Use the `api(method, ...args)` wrapper which handles errors via toast notifications.
- **State**: `papers`, `tags`, `activeTags`, `selectedPaper`, `perspectives`, `activePerspective` as global variables.
- **Multi-perspective tag system**: Users create named perspectives (e.g., "管理视角", "地理视角") each containing tag groups. The same flat tags can appear in different groups across perspectives. Sidebar shows a perspective selector (tab bar) at the top. Legacy `parent_id` hierarchy auto-migrates to a "Default" perspective on first load.
- **Knowledge graph**: Full-screen overlay using vis.js (loaded dynamically from CDN on open). Nodes: papers (blue dots), tags (purple diamonds), authors (green triangles). Edges: paper↔tag, paper→author, tag↔tag co-occurrence.
- **Toast notifications**: `toast(msg, type)` for user feedback. `api()` wrapper catches errors and shows toasts.
- **Keyboard shortcuts**: Cmd+F focuses search, Escape closes dialogs.
- **Import dedup**: All import paths check DOI then title-fuzzy-match before saving.

### Data Flow
```
index.html (JS) → pywebview.api → Api class (api.py) → Database / Agents / PDFStore
                                                  ↓
                                            SQLite (~/.otlet/library.db)
                                            PDFs   (~/.otlet/pdfs/)
                                            Config (~/.otlet/config.yaml)
```

### PyInstaller Packaging
- `otlet.spec`: builds in **onedir** mode with `console=False`. No AppleScript wrapper.
- `build.sh`: runs PyInstaller, creates standard `.app` bundle structure (`Contents/MacOS/` for executable + `_internal/`, `Contents/Frameworks/` as symlink to `MacOS/_internal/`, `Contents/Resources/AppIcon.icns`). Reads version from `pyproject.toml`.
- litellm is bundled as a full data directory (needed for runtime config loading).

## Key Design Decisions

- **Tag normalization**: All tags are lowercased, spaces→hyphens on creation. Dedup happens at the name level, not ID level.
- **Multi-perspective tags**: Flat tags + perspectives + groups (M:N). A tag can belong to multiple groups across perspectives. `perspectives` → `tag_groups` → `tag_group_members` tables. Old `parent_id` hierarchy auto-migrates to perspectives.
- **Import dedup**: `get_paper_by_doi()` then `get_paper_by_title()` (fuzzy, 120-char normalized match) before any import.
- **Zotero import**: Two-phase (scan all → save one-by-one) to allow frontend progress updates. DB is copied to `/tmp` to avoid lock conflicts with running Zotero.
- **Chat system prompt**: Includes paper title, authors, year, venue, DOI + first 12K chars of PDF text.
- **Settings**: LLM model/key/base changed from GUI, persist to YAML immediately (no restart; LLMProvider re-instantiated on save).
- **Native menu bar**: pywebview `Menu` API with `__app__` convention for macOS app-menu items (Preferences, Toggle Theme). File/Edit/Tags/View menus.
- **Knowledge graph**: vis.js loaded dynamically (not at page load) to avoid crash when offline.

## Common Pitfalls

- **JS syntax errors**: The entire frontend is one `<script>` block in `index.html`. A single `let` redeclaration or missing closing brace kills ALL interactivity silently. Always run `node --check` on extracted JS after edits.
- **pywebview API returns JSON strings**: Not objects. Every call needs `JSON.parse(response)`. Use the `api()` wrapper function which handles this.
- **`file.path` is empty in pywebview**: Drag-and-drop files don't expose filesystem paths in pywebview's WebKit. Use `FileReader.readAsDataURL()` → base64 → backend `import_pdf_base64()` instead.
- **Zotero SQLite is locked** when Zotero is running. The importer copies it to `/tmp` first.
- **External CDN scripts crash packaged app**: Do NOT add `<script src="https://...">` in `<head>`. Load external libraries dynamically only when needed (see vis.js pattern in `openGraph()`).
- **pywebview menu on macOS**: Use `Menu("__app__", items)` to place items under the macOS app menu. Other menus appear after the default Edit/View menus.
- **onedir `.app` bundle**: PyInstaller inside `Contents/MacOS/` looks for libs at `../Frameworks/`. The build script creates a symlink `Contents/Frameworks → MacOS/_internal`.
