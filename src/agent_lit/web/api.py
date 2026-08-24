"""pywebview API — bridges Python backend to JavaScript frontend."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import webview

from agent_lit.agents.chat import ChatAgent
from agent_lit.agents.classify import ClassifyAgent
from agent_lit.agents.search import SearchAgent
from agent_lit.config.settings import Settings
from agent_lit.llm.provider import LLMProvider
from agent_lit.models.paper import Paper
from agent_lit.storage.database import Database
from agent_lit.storage.pdf_metadata import PDFMetadataExtractor
from agent_lit.storage.pdf_store import PDFStore
from agent_lit.storage.zotero_import import (
    find_zotero_db,
    import_from_zotero,
    list_collections,
)


class Api:
    """JavaScript-callable API exposed via pywebview."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = Database(settings.db_path)
        self._pdf_store = PDFStore(settings.pdf_dir)
        self._llm = LLMProvider()
        self._search_agent = SearchAgent(api_key=settings.s2_api_key)
        self._chat_agent = ChatAgent(self._llm, self._pdf_store)
        self._chat_agent._db = self._db
        self._pdf_extractor = PDFMetadataExtractor(
            s2_api_key=settings.s2_api_key
        )
        self._window: webview.Window | None = None

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    # ── Papers ────────────────────────────────────────────────

    def list_papers(self) -> str:
        """Return all papers as JSON."""
        papers = self._db.list_papers()
        return json.dumps([p.model_dump(mode="json") for p in papers])

    def search_papers(self, query: str) -> str:
        """Search papers locally."""
        papers = self._db.search_papers(query)
        return json.dumps([p.model_dump(mode="json") for p in papers])

    def search_online(self, query: str, limit: int = 10) -> str:
        """Search papers on Semantic Scholar."""
        papers = self._search_agent.run(query, limit=limit)
        return json.dumps([p.model_dump(mode="json") for p in papers])

    def import_paper(self, paper_json: str) -> str:
        """Import a paper from search results."""
        data = json.loads(paper_json)
        paper = Paper(**data)
        self._db.add_paper(paper)
        return json.dumps({"ok": True, "id": paper.id})

    def delete_paper(self, paper_id: str) -> str:
        if not paper_id:
            return json.dumps({"ok": False, "error": "Missing paper_id"})
        self._db.delete_paper(paper_id)
        return json.dumps({"ok": True})

    # ── Trash ─────────────────────────────────────────────────

    def get_trash(self) -> str:
        """List papers in the trash."""
        papers = self._db.list_trash()
        return json.dumps([p.model_dump(mode="json") for p in papers])

    def restore_paper(self, paper_id: str) -> str:
        if not paper_id:
            return json.dumps({"ok": False, "error": "Missing paper_id"})
        self._db.restore_paper(paper_id)
        return json.dumps({"ok": True})

    def purge_paper(self, paper_id: str) -> str:
        """Permanently delete one trashed paper (row + PDF file)."""
        if not paper_id:
            return json.dumps({"ok": False, "error": "Missing paper_id"})
        self._db.purge_paper(paper_id)
        self._pdf_store.remove(paper_id)
        return json.dumps({"ok": True})

    def empty_trash(self) -> str:
        """Permanently delete all trashed papers (rows + PDF files)."""
        trashed = self._db.list_trash()
        count = self._db.empty_trash()
        for p in trashed:
            self._pdf_store.remove(p.id)
        return json.dumps({"ok": True, "purged": count})

    def import_pdf(self, file_path: str) -> str:
        """Import a PDF file: extract metadata and auto-tag."""
        # Strip shell escape characters and surrounding quotes
        cleaned = file_path.strip().strip("'\"")
        cleaned = cleaned.replace("\\ ", " ")
        cleaned = cleaned.replace("\\(", "(").replace("\\)", ")")
        cleaned = cleaned.replace("\\[", "[").replace("\\]", "]")
        path = Path(cleaned).expanduser().resolve()
        if not path.exists():
            return json.dumps({
                "ok": False,
                "error": f"File not found: {path}",
            })
        if path.suffix.lower() != ".pdf":
            return json.dumps({"ok": False, "error": "Not a PDF file"})

        result = self._pdf_extractor.extract(path)
        if result.paper is None:
            return json.dumps({"ok": False, "error": "Could not identify paper"})

        paper = result.paper

        # Dedup: check existing paper by DOI or title
        dup = None
        if paper.doi:
            dup = self._db.get_paper_by_doi(paper.doi)
        if not dup:
            dup = self._db.get_paper_by_title(paper.title)
        if dup:
            # The dropped PDF is still valuable: attach it to the existing
            # paper when it has none, instead of discarding the file
            attached = False
            if not dup.pdf_path:
                try:
                    stored = self._pdf_store.import_file(path, paper_id=dup.id)
                    self._db.update_paper_pdf(dup.id, str(stored))
                    attached = True
                except Exception:
                    pass
            return json.dumps({
                "ok": False,
                "duplicate": True,
                "existing_id": dup.id,
                "attached": attached,
                "error": f"Already in library: {dup.title}",
            })

        # Infer paper type from venue if not already set
        if not paper.paper_type:
            paper.paper_type = _infer_paper_type(paper.venue)

        # Auto-tag: extract from paper keywords + venue (kept out of manual tags)
        paper.auto_tags = _extract_auto_tags(paper)

        try:
            stored = self._pdf_store.import_file(path, paper_id=paper.id)
            paper.pdf_path = str(stored)
            self._db.add_paper(paper)
        except Exception as e:
            return json.dumps({
                "ok": False,
                "error": f"Save error: {e} (db_path: {self._settings.db_path})",
            })

        return json.dumps({
            "ok": True,
            "paper": paper.model_dump(mode="json"),
            "method": result.method,
            "confidence": result.confidence,
        })

    def import_pdf_base64(self, filename: str, b64data: str) -> str:
        """Import a PDF from base64-encoded content (used by drag & drop)."""
        import base64
        import binascii
        import tempfile

        if not filename or not filename.lower().endswith(".pdf"):
            return json.dumps({"ok": False, "error": "Not a PDF file"})
        if not b64data:
            return json.dumps({"ok": False, "error": "Empty file content"})
        try:
            raw = base64.b64decode(b64data)
        except (binascii.Error, ValueError):
            return json.dumps({"ok": False, "error": "Invalid file data"})
        if not raw:
            return json.dumps({"ok": False, "error": "Empty file content"})

        tmp_dir = Path(tempfile.mkdtemp())
        # Path(filename).name blocks path traversal from the dropped filename
        tmp = tmp_dir / Path(filename).name
        try:
            tmp.write_bytes(raw)
            return self.import_pdf(str(tmp))
        finally:
            # rmtree(ignore_errors=True) — Path.rmdir() has no missing_ok kwarg
            # and must never raise here: an exception in finally would swallow
            # the import result and break every drag-and-drop import
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def get_paper_pdf_text(self, paper_id: str) -> str:
        """Extract text from a paper's PDF."""
        text = self._pdf_store.extract_text(paper_id)
        return json.dumps({"text": text})

    # ── Tags ──────────────────────────────────────────────────

    def list_tags(self, include_auto: bool = False) -> str:
        tags = self._db.list_tags(include_auto=include_auto)
        return json.dumps([t.model_dump(mode="json") for t in tags])

    def create_tag(self, name: str) -> str:
        if not name or not name.strip():
            return json.dumps({"ok": False, "error": "Tag name is empty"})
        tag = self._db.create_tag(name.strip())
        return json.dumps(tag.model_dump(mode="json"))

    def add_tag_to_paper(self, paper_id: str, tag: str) -> str:
        if not paper_id or not tag:
            return json.dumps({"ok": False, "error": "Missing paper_id or tag"})
        self._db.add_tag_to_paper(paper_id, tag, source="manual")
        return json.dumps({"ok": True})

    def add_auto_tag_to_paper(self, paper_id: str, tag: str) -> str:
        """Attach an LLM/heuristic-suggested tag (hidden from the manual sidebar)."""
        if not paper_id or not tag:
            return json.dumps({"ok": False, "error": "Missing paper_id or tag"})
        self._db.add_tag_to_paper(paper_id, tag, source="auto")
        return json.dumps({"ok": True})

    def reclassify_auto_tags(self) -> str:
        """One-time cleanup for libraries created before the source column.

        Re-runs the deterministic import-time auto-tag extraction on every
        paper and marks matching historical links as 'auto' (they used to
        be merged into manual tags). Nothing is deleted — flip the
        "Show auto tags" toggle to see them again.
        """
        changed = 0
        for p in self._db.list_papers():
            auto = set(_extract_auto_tags(p))
            changed += self._db.mark_tag_source(p.id, auto, "auto")
        return json.dumps({"ok": True, "reclassified": changed})

    def remove_tag_from_paper(self, paper_id: str, tag: str) -> str:
        if not paper_id or not tag:
            return json.dumps({"ok": False, "error": "Missing paper_id or tag"})
        self._db.remove_tag_from_paper(paper_id, tag)
        return json.dumps({"ok": True})

    def find_papers_by_tags(self, tags_json: str) -> str:
        """Filter papers by tags (AND logic)."""
        tags = json.loads(tags_json)
        papers = self._db.find_papers_by_tags(tags)
        return json.dumps([p.model_dump(mode="json") for p in papers])

    def set_tag_category(self, tag_id: str, category: str) -> str:
        """Classify a tag as problem / model / algorithm (empty clears it)."""
        if not tag_id:
            return json.dumps({"ok": False, "error": "Missing tag_id"})
        try:
            self._db.set_tag_category(tag_id, category)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True})

    def backfill_affiliations(self) -> str:
        """Fill empty author affiliations from Semantic Scholar (all DOI papers)."""
        pairs = self._db.get_papers_with_dois()
        if not pairs:
            return json.dumps(
                {"ok": True, "queried": 0, "papers_updated": 0, "authors_updated": 0}
            )
        aff_by_doi = self._search_agent.fetch_affiliations(
            [doi for _pid, doi in pairs]
        )
        papers_updated = 0
        authors_updated = 0
        for pid, doi in pairs:
            mapping = aff_by_doi.get(doi.lower())
            if not mapping:
                continue
            n = self._db.set_paper_author_affiliations(pid, mapping)
            if n:
                papers_updated += 1
                authors_updated += n
        return json.dumps(
            {
                "ok": True,
                "queried": len(pairs),
                "papers_updated": papers_updated,
                "authors_updated": authors_updated,
            }
        )

    # ── Chat ──────────────────────────────────────────────────

    def get_chat_history(self, paper_id: str) -> str:
        conv_id = self._db.get_conversation(paper_id)
        if not conv_id:
            return json.dumps([])
        messages = self._db.get_messages(conv_id)
        return json.dumps(messages)

    def send_chat_message(self, paper_id: str, message: str) -> str:
        """Send a chat message and return the AI response."""
        # Get or create conversation
        conv_id = self._db.get_conversation(paper_id)
        if not conv_id:
            conv_id = self._db.create_conversation(paper_id)

        # Save user message
        self._db.add_message(conv_id, "user", message)

        # Build history
        history = self._db.get_messages(conv_id)
        messages = [{"role": m["role"], "content": m["content"]} for m in history]

        # Get AI response
        response = self._chat_agent.run(paper_id, messages)

        # Save response
        self._db.add_message(conv_id, "assistant", response)

        return json.dumps({"response": response})

    # ── Bulk Import ───────────────────────────────────────────

    def scan_folder_pdfs(self, folder_path: str) -> str:
        """Recursively scan a folder for PDF files."""

        folder = Path(folder_path.strip().strip("'\"")).expanduser().resolve()
        if not folder.is_dir():
            return json.dumps({"ok": False, "error": f"Not a directory: {folder}"})

        # Skip hidden dirs and common non-paper dirs
        skip = {".git", ".DS_Store", "__MACOSX", "node_modules"}
        pdfs = sorted(
            p for p in folder.rglob("*.pdf")
            if not any(part.startswith(".") or part in skip for part in p.parts)
        )
        return json.dumps({
            "ok": True,
            "folder": str(folder),
            "pdfs": [str(p) for p in pdfs],
        })

    def import_bibtex(self, file_path: str) -> str:
        """Import papers from a BibTeX file (Zotero export)."""
        path = Path(file_path.strip().strip("'\"")).expanduser().resolve()
        if not path.exists():
            return json.dumps({"ok": False, "error": f"File not found: {path}"})

        text = path.read_text(encoding="utf-8", errors="replace")
        entries = _parse_bibtex(text)

        if not entries:
            return json.dumps({"ok": False, "error": "No entries found in BibTeX"})

        imported = []
        for entry in entries:
            paper = Paper(
                title=entry.get("title", "Untitled"),
                authors=_bibtex_authors(entry.get("author", "")),
                year=_bibtex_int(entry.get("year")),
                venue=entry.get("journal") or entry.get("booktitle"),
                volume=entry.get("volume"),
                issue=entry.get("number"),
                pages=entry.get("pages"),
                publisher=entry.get("publisher"),
                language=entry.get("language"),
                doi=entry.get("doi"),
                url=entry.get("url"),
                abstract=entry.get("abstract"),
                keywords=_bibtex_keywords(entry.get("keywords", "")),
                bibtex_key=entry.get("_key"),
                paper_type=_bibtex_type(entry.get("_type", "")),
            )
            auto_tags = _extract_auto_tags(paper)
            paper.auto_tags = auto_tags
            self._db.add_paper(paper)
            imported.append(paper.model_dump(mode="json"))

        return json.dumps({
            "ok": True,
            "imported": len(imported),
            "papers": imported,
        })

    # ── Zotero Import ─────────────────────────────────────────

    def detect_zotero(self) -> str:
        """Detect Zotero data directory and return its path."""
        db_path = find_zotero_db()
        if db_path:
            return json.dumps({
                "ok": True,
                "db_path": str(db_path),
                "zotero_dir": str(db_path.parent),
            })
        return json.dumps({"ok": False, "error": "Zotero database not found"})

    def list_zotero_collections(self) -> str:
        """List collections from the Zotero database."""
        db_path = find_zotero_db()
        if not db_path:
            return json.dumps({"ok": False, "error": "Zotero not found"})
        try:
            colls = list_collections(db_path)
            return json.dumps({"ok": True, "collections": colls})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def scan_zotero_items(self, options_json: str) -> str:
        """Scan Zotero database and return item data (no DB writes).

        options_json: {"collection_ids": [1,2,...], "include_pdfs": true}
        Returns list of {paper_data, pdf_path} for frontend to import one-by-one.
        """
        db_path = find_zotero_db()
        if not db_path:
            return json.dumps({"ok": False, "error": "Zotero not found"})

        opts = json.loads(options_json)
        collection_ids = opts.get("collection_ids") or None
        include_pdfs = opts.get("include_pdfs", True)

        try:
            results = import_from_zotero(
                db_path,
                zotero_dir=db_path.parent,
                collection_ids=collection_ids,
                include_pdfs=include_pdfs,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

        return json.dumps({"ok": True, "total": len(results), "items": results})

    def save_zotero_item(self, item_json: str) -> str:
        """Save a single Zotero-scanned item to the Agent-Lit database."""
        item = json.loads(item_json)
        if not item.get("ok") or not item.get("paper"):
            return json.dumps({"ok": False, "error": "Invalid item"})

        paper = Paper(**item["paper"])
        # Dedup check
        dup = None
        if paper.doi:
            dup = self._db.get_paper_by_doi(paper.doi)
        if not dup:
            dup = self._db.get_paper_by_title(paper.title)
        if dup:
            return json.dumps(
                {"ok": False, "duplicate": True, "existing_id": dup.id,
                 "error": "Duplicate"}
            )
        # Auto-tag
        paper.auto_tags = _extract_auto_tags(paper)
        # Import PDF if available
        pdf_path = item.get("pdf_path")
        if pdf_path:
            try:
                stored = self._pdf_store.import_file(
                    Path(pdf_path), paper_id=paper.id
                )
                paper.pdf_path = str(stored)
            except Exception:
                pass  # PDF copy failure is non-fatal
        self._db.add_paper(paper)
        return json.dumps({"ok": True, "id": paper.id})

    # ── File dialogs ──────────────────────────────────────────

    def open_file_dialog(self) -> str:
        """Open a native file dialog for PDF selection (multiple files allowed)."""
        if not self._window:
            return json.dumps({"paths": [], "error": "No window"})
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("PDF files (*.pdf)",),
            )
        except Exception as e:
            return json.dumps({"paths": [], "error": str(e)})

        if not result or len(result) == 0:
            return json.dumps({"paths": []})

        # result can be a tuple of strings or a single string
        raw = result if isinstance(result, (list, tuple)) else [result]
        # Ensure absolute paths
        paths = [str(Path(p).resolve()) for p in raw]
        return json.dumps({"paths": paths})

    def open_folder_dialog(self) -> str:
        """Open a native folder selection dialog."""
        if not self._window:
            return json.dumps({"path": "", "error": "No window"})
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            return json.dumps({"path": "", "error": str(e)})

        if not result or len(result) == 0:
            return json.dumps({"path": ""})
        return json.dumps({"path": result[0]})

    def open_bibtex_dialog(self) -> str:
        """Open a native file dialog for BibTeX selection."""
        if not self._window:
            return json.dumps({"path": "", "error": "No window"})
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("BibTeX files (*.bib)", "All files (*.*)"),
            )
        except Exception as e:
            return json.dumps({"path": "", "error": str(e)})

        if not result or len(result) == 0:
            return json.dumps({"path": ""})
        raw = result[0] if isinstance(result, (list, tuple)) else result
        return json.dumps({"path": str(Path(raw).resolve())})

    # ── Auto-tag ──────────────────────────────────────────────

    def open_pdf(self, paper_id: str) -> str:
        """Open a paper's PDF with the system default viewer."""
        paper = self._db.get_paper(paper_id)
        if not paper or not paper.pdf_path:
            return json.dumps({"ok": False, "error": "No PDF available"})
        path = Path(paper.pdf_path).expanduser().resolve()
        if not path.exists():
            return json.dumps({"ok": False, "error": "PDF file not found"})
        import subprocess

        subprocess.Popen(["open", str(path)])
        return json.dumps({"ok": True})

    def reveal_pdf(self, paper_id: str) -> str:
        """Reveal the paper's PDF in Finder (opens Finder with the file selected)."""
        paper = self._db.get_paper(paper_id)
        if not paper or not paper.pdf_path:
            return json.dumps({"ok": False, "error": "No PDF available"})
        path = Path(paper.pdf_path).expanduser().resolve()
        if not path.exists():
            return json.dumps({"ok": False, "error": "PDF file not found"})
        import subprocess

        subprocess.run(["open", "-R", str(path)], check=True)
        return json.dumps({"ok": True})

    def open_url(self, url: str) -> str:
        """Open an external URL with the system default browser."""
        import subprocess

        if not url.startswith(("http://", "https://")):
            return json.dumps({"ok": False, "error": "Unsupported URL"})
        subprocess.Popen(["open", url])
        return json.dumps({"ok": True})

    def update_paper(self, paper_id: str, fields_json: str) -> str:
        """Update paper metadata fields."""
        fields = json.loads(fields_json)
        self._db.update_paper(paper_id, **fields)
        return json.dumps({"ok": True})

    def update_paper_authors(self, paper_id: str, authors_json: str) -> str:
        """Replace a paper's author list."""
        names = json.loads(authors_json)
        from agent_lit.models.author import Author

        self._db.set_paper_authors(paper_id, [Author(name=n) for n in names])
        return json.dumps({"ok": True})

    # ── Notes ──────────────────────────────────────────────────

    def get_notes(self, paper_id: str) -> str:
        """All notes of a paper."""
        return json.dumps(self._db.list_notes(paper_id))

    def add_note(self, paper_id: str, content: str) -> str:
        if not paper_id or not content or not content.strip():
            return json.dumps({"ok": False, "error": "Empty note"})
        note = self._db.add_note(paper_id, content.strip())
        return json.dumps({"ok": True, "note": note})

    def update_note(self, note_id: str, content: str) -> str:
        if not note_id or not content or not content.strip():
            return json.dumps({"ok": False, "error": "Empty note"})
        self._db.update_note(note_id, content.strip())
        return json.dumps({"ok": True})

    def delete_note(self, note_id: str) -> str:
        if not note_id:
            return json.dumps({"ok": False, "error": "Missing note_id"})
        self._db.delete_note(note_id)
        return json.dumps({"ok": True})

    def rename_tag(self, old_name: str, new_name: str) -> str:
        """Rename a tag."""
        if not old_name or not new_name or not new_name.strip():
            return json.dumps({"ok": False, "error": "Missing tag name"})
        self._db.rename_tag(old_name, new_name.strip())
        return json.dumps({"ok": True})

    def set_tag_parent(self, tag_name: str, parent_name: str) -> str:
        """Set a tag's parent theme. Empty parent_name = move to top level."""
        parent = parent_name.strip() if parent_name else None
        self._db.set_tag_parent(tag_name, parent)
        return json.dumps({"ok": True})

    def list_tag_tree(self) -> str:
        """Return tags as flat list with parent info for tree rendering."""
        rows = self._db.get_tag_tree()
        return json.dumps({"ok": True, "tags": rows})

    def create_theme(self, name: str) -> str:
        """Create a top-level theme tag."""
        if not name or not name.strip():
            return json.dumps({"ok": False, "error": "Theme name is empty"})
        tag = self._db.create_tag(name.strip())
        return json.dumps({"ok": True, "name": tag.name})

    def auto_group_tags(self) -> str:
        """Use co-occurrence to suggest tag groupings."""
        papers = self._db.list_papers()
        # Build co-occurrence
        cooc: dict[tuple[str, str], int] = {}
        for p in papers:
            ptags = p.tags or []
            for i in range(len(ptags)):
                for j in range(i + 1, len(ptags)):
                    key = tuple(sorted([ptags[i], ptags[j]]))
                    cooc[key] = cooc.get(key, 0) + 1

        # Find tag groups via simple clustering
        tag_counts: dict[str, int] = {}
        for p in papers:
            for t in (p.tags or []):
                tag_counts[t] = tag_counts.get(t, 0) + 1

        # Tags with ≥2 papers are potential themes
        themes = {t for t, c in tag_counts.items() if c >= 2}
        # For each rare tag, find the theme it co-occurs most with
        suggestions: list[dict] = []
        for tag, count in tag_counts.items():
            if tag in themes or count >= 2:
                continue
            best_theme = None
            best_score = 0
            for (a, b), c in cooc.items():
                if a == tag and b in themes and c > best_score:
                    best_theme = b
                    best_score = c
                elif b == tag and a in themes and c > best_score:
                    best_theme = a
                    best_score = c
            if best_theme:
                suggestions.append(
                    {"tag": tag, "theme": best_theme, "cooc": best_score}
                )

        return json.dumps({
            "ok": True,
            "themes": sorted(themes),
            "suggestions": suggestions,
        })

    # ── Perspectives ───────────────────────────────────────

    def list_perspectives(self) -> str:
        return json.dumps({"ok": True, "perspectives": self._db.list_perspectives()})

    def create_perspective(self, name: str) -> str:
        if not name or not name.strip():
            return json.dumps({"ok": False, "error": "Name is empty"})
        p = self._db.create_perspective(name.strip())
        return json.dumps({"ok": True, **p})

    def delete_perspective(self, perspective_id: str) -> str:
        if not perspective_id:
            return json.dumps({"ok": False, "error": "Missing perspective_id"})
        self._db.delete_perspective(perspective_id)
        return json.dumps({"ok": True})

    def rename_perspective(self, perspective_id: str, name: str) -> str:
        if not perspective_id or not name or not name.strip():
            return json.dumps({"ok": False, "error": "Missing parameters"})
        self._db.rename_perspective(perspective_id, name.strip())
        return json.dumps({"ok": True})

    # ── Tag Groups ─────────────────────────────────────────

    def list_groups(self, perspective_id: str) -> str:
        groups = self._db.list_groups(perspective_id)
        return json.dumps({"ok": True, "groups": groups})

    def create_group(self, perspective_id: str, name: str) -> str:
        if not perspective_id or not name or not name.strip():
            return json.dumps({"ok": False, "error": "Missing parameters"})
        g = self._db.create_group(perspective_id, name.strip())
        return json.dumps({"ok": True, **g})

    def delete_group(self, group_id: str) -> str:
        if not group_id:
            return json.dumps({"ok": False, "error": "Missing group_id"})
        self._db.delete_group(group_id)
        return json.dumps({"ok": True})

    def rename_group(self, group_id: str, name: str) -> str:
        if not group_id or not name or not name.strip():
            return json.dumps({"ok": False, "error": "Missing parameters"})
        self._db.rename_group(group_id, name.strip())
        return json.dumps({"ok": True})

    # ── Group Membership ───────────────────────────────────

    def add_tag_to_group(self, group_id: str, tag_name: str) -> str:
        if not group_id or not tag_name:
            return json.dumps({"ok": False, "error": "Missing parameters"})
        tag = self._db.get_tag_by_name(tag_name)
        if not tag:
            tag = self._db.create_tag(tag_name)
        self._db.add_tag_to_group(group_id, tag.id)
        return json.dumps({"ok": True})

    def remove_tag_from_group(self, group_id: str, tag_name: str) -> str:
        if not group_id or not tag_name:
            return json.dumps({"ok": False, "error": "Missing parameters"})
        tag = self._db.get_tag_by_name(tag_name)
        if not tag:
            return json.dumps({"ok": False, "error": "Tag not found"})
        self._db.remove_tag_from_group(group_id, tag.id)
        return json.dumps({"ok": True})

    def add_paper_to_group(self, paper_id: str, group_id: str) -> str:
        """File a paper directly into a project (used after imports)."""
        if not paper_id or not group_id:
            return json.dumps({"ok": False, "error": "Missing parameters"})
        self._db.add_paper_to_group(group_id, paper_id)
        return json.dumps({"ok": True})

    def remove_paper_from_group(self, paper_id: str, group_id: str) -> str:
        if not paper_id or not group_id:
            return json.dumps({"ok": False, "error": "Missing parameters"})
        self._db.remove_paper_from_group(group_id, paper_id)
        return json.dumps({"ok": True})

    def get_group_papers(self, group_id: str) -> str:
        """Directly-filed papers of a project: [{paper_id, title}]."""
        return json.dumps(self._db.get_group_papers(group_id))

    def get_perspective_view(
        self, perspective_id: str, include_auto: bool = False
    ) -> str:
        """Return groups with tags for a perspective."""
        groups = self._db.get_perspective_view(perspective_id, include_auto)
        ungrouped = self._db.get_ungrouped_tags(perspective_id, include_auto)
        return json.dumps({
            "ok": True,
            "groups": groups,
            "ungrouped": ungrouped,
        })

    def migrate_to_perspectives(self) -> str:
        """One-time migration from parent_id to perspectives."""
        msg = self._db.migrate_to_perspectives()
        return json.dumps({"ok": True, "message": msg})

    # ── Knowledge Graph ────────────────────────────────────

    def get_knowledge_graph(self, options_json: str = "{}") -> str:
        """Return graph data for visualization.

        options_json: {"tag_filter": ["tag1","tag2"], "min_papers": 1}
        """
        opts = json.loads(options_json) if options_json else {}
        graph = self._db.get_knowledge_graph(
            tag_filter=opts.get("tag_filter"),
            min_papers=opts.get("min_papers", 1),
        )
        return json.dumps({"ok": True, **graph})

    def auto_tag(self, paper_json: str) -> str:
        """Use LLM to suggest tags for a paper."""
        data = json.loads(paper_json)
        paper = Paper(**data)
        classifier = ClassifyAgent(self._llm)
        tags = classifier.run(paper)
        return json.dumps(tags)

    def auto_tag_paper(self, paper_id: str, method: str = "llm") -> str:
        """Suggest tags for one stored paper and link them as auto tags.

        method="llm" asks the configured LLM (cloud or local Ollama);
        method="nlp" runs the offline RAKE keyword extractor — free, no
        model or API key required.
        """
        paper = self._db.get_paper(paper_id)
        if paper is None:
            return json.dumps({"ok": False, "error": "Paper not found"})
        if method == "nlp":
            from agent_lit.storage.keyword_extract import extract_keywords

            suggested = extract_keywords(paper.title, paper.abstract)
        else:
            try:
                suggested = ClassifyAgent(self._llm).run(paper)
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
        for t in suggested:
            self._db.add_tag_to_paper(paper.id, t, source="auto")
        return json.dumps({"ok": True, "count": len(suggested), "tags": suggested})

    # ── Settings ──────────────────────────────────────────────

    def get_settings(self) -> str:
        """Return current LLM settings."""

        def mask(k: str | None) -> str:
            return (k or "")[:8] + "..." if k else ""

        return json.dumps({
            "ok": True,
            "lit_model": self._settings.lit_model,
            "lit_api_key": mask(self._settings.lit_api_key),
            "lit_api_base": self._settings.lit_api_base or "",
            "s2_api_key": mask(self._settings.s2_api_key),
            "theme": self._settings.theme,
            # A configured base URL alone counts: local OpenAI-compatible
            # servers (Ollama, LM Studio, vLLM) don't need an API key
            "has_llm": bool(self._settings.lit_api_key or self._settings.lit_api_base),
        })

    def list_ollama_models(self) -> str:
        """List models from a local Ollama daemon (for one-click setup)."""
        import urllib.request

        try:
            with urllib.request.urlopen(
                "http://localhost:11434/api/tags", timeout=3
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return json.dumps({"ok": True, "models": models})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def save_settings(self, settings_json: str) -> str:
        """Save LLM settings to config file."""
        data = json.loads(settings_json)
        if "lit_model" in data:
            self._settings.lit_model = data["lit_model"]
        if "lit_api_key" in data and data["lit_api_key"]:
            self._settings.lit_api_key = data["lit_api_key"]
        if "lit_api_base" in data:
            self._settings.lit_api_base = data["lit_api_base"] or None
        if "s2_api_key" in data and data["s2_api_key"]:
            self._settings.s2_api_key = data["s2_api_key"]
        self._settings.save()
        # Re-init LLM with new settings
        self._llm = LLMProvider(
            model=self._settings.lit_model,
            api_key=self._settings.lit_api_key,
            api_base=self._settings.lit_api_base,
        )
        self._chat_agent = ChatAgent(self._llm, self._pdf_store)
        return json.dumps({"ok": True})

    def set_theme(self, theme: str) -> str:
        """Persist the UI theme choice to the config file."""
        if theme not in ("light", "dark", "classic-light", "classic-dark"):
            return json.dumps({"ok": False, "error": "Invalid theme"})
        self._settings.theme = theme
        self._settings.save()
        return json.dumps({"ok": True})

    # ── Export ────────────────────────────────────────────────

    def export_bibtex(
        self,
        target_path: str | None = None,
        paper_ids: str | None = None,
        save_filename: str | None = None,
    ) -> str:
        """Export papers to a .bib file.

        Exports the whole library by default; pass a JSON array of paper ids
        to export a single paper or one project's papers. Shows a native
        save dialog when target_path is empty.
        """
        from agent_lit.storage.bibtex_export import generate_bibtex

        all_papers = self._db.list_papers()
        if paper_ids:
            try:
                wanted = set(json.loads(paper_ids))
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"ok": False, "error": "Invalid paper_ids"})
            papers = [p for p in all_papers if p.id in wanted]
        else:
            papers = all_papers
        if not papers:
            return json.dumps({"ok": False, "error": "No papers to export"})

        if target_path:
            path = Path(target_path).expanduser()
        elif self._window:
            try:
                result = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=save_filename or "library.bib",
                    file_types=("BibTeX files (*.bib)",),
                )
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
            if not result:
                return json.dumps({"ok": False, "error": "Cancelled"})
            raw = result[0] if isinstance(result, (list, tuple)) else result
            path = Path(str(raw))
        else:
            return json.dumps({"ok": False, "error": "No window"})

        if path.suffix == "":
            path = path.with_suffix(".bib")

        try:
            path.write_text(generate_bibtex(papers), encoding="utf-8")
        except OSError as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True, "path": str(path), "count": len(papers)})

    # ── Lifecycle ─────────────────────────────────────────────

    def close(self) -> None:
        self._db.close()


# ── Auto-tag helpers (module-level) ──────────────────────

def _extract_auto_tags(paper: Paper) -> list[str]:
    """Extract tags from paper metadata: keywords, venue, abstract."""
    tags: list[str] = []

    # 1. Paper keywords → direct tags
    for kw in paper.keywords:
        tag = _normalize_tag(kw)
        if tag and tag not in tags:
            tags.append(tag)

    # 2. Venue → high-level domain tag
    if paper.venue:
        venue_tag = _venue_to_tag(paper.venue)
        if venue_tag and venue_tag not in tags:
            tags.append(venue_tag)

    # 3. Abstract → extract key phrases via frequency heuristic
    if paper.abstract:
        abstract_tags = _extract_from_abstract(paper.abstract)
        for t in abstract_tags:
            if t not in tags:
                tags.append(t)

    return tags[:12]  # cap at 12 tags


def _normalize_tag(kw: str) -> str:
    """Normalize a keyword into a clean tag."""
    tag = kw.strip().lower()
    tag = tag.replace(" ", "-")
    # Remove very short or very long tags
    if len(tag) < 2 or len(tag) > 40:
        return ""
    return tag


# Well-known venue → domain mapping
_VENUE_MAP = {
    "neurips": "neural-networks",
    "nips": "neural-networks",
    "icml": "machine-learning",
    "iclr": "deep-learning",
    "aaai": "artificial-intelligence",
    "ijcai": "artificial-intelligence",
    "cvpr": "computer-vision",
    "iccv": "computer-vision",
    "eccv": "computer-vision",
    "acl": "natural-language-processing",
    "emnlp": "natural-language-processing",
    "naacl": "natural-language-processing",
    "sigkdd": "data-mining",
    "kdd": "data-mining",
    "www": "web-mining",
    "sigir": "information-retrieval",
    "icse": "software-engineering",
    "ase": "software-engineering",
    "ismir": "music-information-retrieval",
}


def _venue_to_tag(venue: str) -> str | None:
    """Map a venue name to a domain tag."""
    v = venue.lower().strip()
    for key, tag in _VENUE_MAP.items():
        if key in v:
            return tag
    return None


# Common academic phrases to ignore in abstract tag extraction
_IGNORE_WORDS = frozenset({
    "paper", "propose", "propose", "method", "approach", "result",
    "show", "use", "using", "used", "based", "propose", "novel",
    "propose", "also", "however", "study", "propose", "present",
    "proposed", "propose", "work", "propose", "provide", "introduce",
    "presented", "propose", "well", "two", "one", "new", "first",
    "second", "propose", "different", "several", "various", "given",
    "experimental", "experiments", "evaluation", "performance",
    "compare", "comparison", "state-of-the-art", "achieve", "obtained",
    "obtain", "demonstrate", "significant", "significantly",
    "effective", "efficient", "improve", "improvement",
})


def _extract_from_abstract(abstract: str) -> list[str]:
    """Extract potential tags from abstract via noun-phrase heuristic."""
    import re

    # Extract 2-3 word phrases that look like domain terms
    # Pattern: capitalized phrase or technical term
    phrases = re.findall(
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', abstract
    )

    # Also extract common hyphenated terms
    hyphenated = re.findall(
        r'\b([a-z]+-[a-z]+(?:-[a-z]+)*)\b', abstract.lower()
    )

    tags = []
    for phrase in phrases:
        tag = phrase.lower().replace(" ", "-")
        words = tag.split("-")
        # Skip if any word is in ignore list or too common
        if any(w in _IGNORE_WORDS for w in words):
            continue
        if 3 <= len(tag) <= 35 and tag not in tags:
            tags.append(tag)

    for tag in hyphenated:
        if 3 <= len(tag) <= 35 and tag not in tags:
            tags.append(tag)

    return tags[:8]


# ── BibTeX Parsing ─────────────────────────────────────

def _parse_bibtex(text: str) -> list[dict]:
    """Parse a BibTeX string into a list of entry dicts."""
    import re

    entries = []
    # Match @type{key, ... }
    for m in re.finditer(
        r"@(\w+)\s*\{\s*([^,\s]+)\s*,\s*(.*?)\n\s*\}",
        text,
        re.DOTALL,
    ):
        entry_type = m.group(1).lower()
        if entry_type in ("comment", "string", "preamble"):
            continue
        key = m.group(2)
        body = m.group(3)

        fields = {"_key": key, "_type": entry_type}
        # Parse field = {value} or field = "value" or field = number
        for fm in re.finditer(
            r"(\w+)\s*=\s*(?:\{(.*?)\}|\"(.*?)\"|(\S+))",
            body,
            re.DOTALL,
        ):
            fname = fm.group(1).lower()
            # Value is in one of the three capture groups
            fval = fm.group(2) or fm.group(3) or fm.group(4) or ""
            # Remove outer braces (nested brace handling)
            fval = fval.strip()
            # Un-escape BibTeX
            fval = fval.replace("\\&", "&")
            fields[fname] = fval

        if "title" in fields:
            entries.append(fields)
    return entries


def _bibtex_authors(author_str: str) -> list:
    """Parse 'Last, First and Last, First' into Author objects."""
    from agent_lit.models.author import Author

    if not author_str:
        return []
    authors = []
    for part in re.split(r"\s+and\s+", author_str):
        part = part.strip()
        if not part:
            continue
        # "Last, First" → explicit first/last names for correct re-export
        if "," in part:
            segments = part.split(",", 1)
            last = segments[0].strip()
            first = segments[1].strip()
            authors.append(
                Author(name=f"{first} {last}".strip(), first_name=first, last_name=last)
            )
        else:
            authors.append(Author(name=part))
    return authors


def _bibtex_int(val: str | None) -> int | None:
    if not val:
        return None
    import re
    m = re.search(r"\d{4}", val)
    return int(m.group()) if m else None


def _bibtex_keywords(kw_str: str) -> list[str]:
    """Split 'kw1; kw2, kw3' into a clean list."""
    if not kw_str:
        return []
    # Semicolons first, then commas
    kws = re.split(r"[;,]", kw_str)
    return [k.strip() for k in kws if k.strip()]


# ── Paper Type Inference ──────────────────────────────

_BIBTEX_TYPE_MAP = {
    "article": "journal",
    "inproceedings": "conference",
    "conference": "conference",
    "proceedings": "conference",
    "book": "book",
    "incollection": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "misc": "preprint",
    "unpublished": "preprint",
}

_VENUE_TYPE_PATTERNS = [
    (r"\b(?:conf(?:erence)?|proc(?:eedings)?|symposium|workshop)\b", "conference"),
    (r"\b(?:journal|trans(?:action)?|letters?|review)\b", "journal"),
    (r"\b(?:book|chapter|monograph)\b", "book"),
    (r"\b(?:thesis|dissertation)\b", "thesis"),
    (r"\b(?:arxiv|preprint)\b", "preprint"),
]


def _bibtex_type(entry_type: str) -> str | None:
    """Map BibTeX entry type to paper_type."""
    return _BIBTEX_TYPE_MAP.get(entry_type.lower())


def _infer_paper_type(venue: str | None) -> str | None:
    """Infer paper type from venue name using pattern matching."""
    if not venue:
        return None
    v = venue.lower()
    for pattern, ptype in _VENUE_TYPE_PATTERNS:
        if re.search(pattern, v):
            return ptype
    return None
