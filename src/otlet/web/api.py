"""pywebview API — bridges Python backend to JavaScript frontend."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import webview

from otlet import platform
from otlet.agents.chat import ChatAgent
from otlet.agents.classify import ClassifyAgent
from otlet.agents.openalex import OpenAlexClient
from otlet.agents.search import SearchAgent
from otlet.config.settings import Settings
from otlet.llm.provider import LLMProvider
from otlet.models.paper import Paper
from otlet.services.importers import (
    entry_to_paper,
    extract_auto_tags,
    import_pdf_file,
    parse_bibtex,
    save_zotero_item,
)
from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_metadata import PDFMetadataExtractor
from otlet.storage.pdf_store import PDFStore
from otlet.storage.zotero_import import (
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
        self._openalex = OpenAlexClient(mailto=settings.openalex_email)
        self._chat_agent = ChatAgent(
            self._llm,
            self._pdf_store,
            db=self._db,
            openalex=self._openalex,
            search_agent=self._search_agent,
        )
        self._pdf_index = PDFIndex(self._db, self._pdf_store)
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

    def search_fulltext(self, query: str, limit: int = 50) -> str:
        """Search inside indexed PDFs: [{paper_id, title, page, snippet}].

        >=3-character queries hit the FTS trigram index; shorter ones
        fall back to scanning the compressed page texts.
        """
        hits = self._pdf_index.search(query, limit=limit)
        titles = {p.id: p.title for p in self._db.list_papers()}
        return json.dumps(
            [
                {
                    "paper_id": h["paper_id"],
                    "title": titles.get(h["paper_id"], h["paper_id"]),
                    "page": h["page"],
                    "snippet": h["snippet"],
                }
                for h in hits
            ],
            ensure_ascii=False,
        )

    def build_pdf_index(self, paper_id: str | None = None) -> str:
        """(Re)build the per-page full-text index.

        With paper_id: one paper. Without: every stored PDF that has no
        index yet (backfill for libraries predating the index).
        """
        if paper_id:
            pages = self._pdf_index.build(paper_id)
            return json.dumps(
                {"ok": pages is not None, "pages": pages}
            )
        missing = self._db.papers_missing_index()
        done = sum(1 for pid in missing if self._pdf_index.build(pid))
        return json.dumps({"ok": True, "indexed": done, "total": len(missing)})

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

        result = import_pdf_file(
            self._db, self._pdf_store, self._pdf_extractor, path
        )
        return json.dumps(result)

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
            auto = set(extract_auto_tags(p))
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

    def list_tags_with_counts(self, include_auto: bool = False) -> str:
        """Sidebar tag list: [{id, name, is_top, category, color, paper_count}]."""
        return json.dumps(self._db.list_tags_with_counts(include_auto=include_auto))

    def get_cooccurring_tags(self, tag_id: str, include_auto: bool = False) -> str:
        """Tags co-occurring with a tag — the derived level under a theme."""
        if not tag_id:
            return json.dumps({"ok": False, "error": "Missing tag_id"})
        return json.dumps(
            self._db.get_cooccurring_tags(tag_id, include_auto=include_auto)
        )

    def set_tag_top(self, tag_id: str, is_top: bool) -> str:
        """Mark or unmark a tag as a top-level theme."""
        if not tag_id:
            return json.dumps({"ok": False, "error": "Missing tag_id"})
        self._db.set_tag_top(tag_id, is_top)
        return json.dumps({"ok": True})

    def set_tag_parent(self, tag_id: str, parent_id: str | None = None) -> str:
        """Nest a tag under another (drag-and-drop); None detaches it."""
        if not tag_id:
            return json.dumps({"ok": False, "error": "Missing tag_id"})
        try:
            self._db.set_tag_parent(tag_id, parent_id or None)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True})

    def delete_tag(self, tag_id: str) -> str:
        """Delete a tag and all its paper links."""
        if not tag_id:
            return json.dumps({"ok": False, "error": "Missing tag_id"})
        self._db.delete_tag(tag_id)
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
        """Chat bubbles for the frontend: user turns and assistant
        answers only — tool traffic and empty tool-request turns stay
        internal."""
        conv_id = self._db.get_conversation(paper_id)
        if not conv_id:
            return json.dumps([])
        messages = [
            {
                "role": m["role"],
                "content": m["content"],
                "created_at": m["created_at"],
            }
            for m in self._db.get_messages(conv_id)
            if m["role"] == "user"
            or (m["role"] == "assistant" and (m["content"] or "").strip())
        ]
        return json.dumps(messages, ensure_ascii=False)

    def send_chat_message(self, paper_id: str, message: str) -> str:
        """Send a chat message; the agent may read PDF pages, search the
        library, and verify claims through tools before answering (tool
        traffic is persisted with the conversation).

        Events stream live into the frontend via
        ``window.onChatEvent({type, ...})`` when the page defines the
        handler: {type: "delta", text}, {type: "tool", name, detail},
        {type: "done", answer, end_reason}. The final result is also
        returned as JSON {"response", "end_reason"} for frontends
        without the handler.
        """

        def push(event: dict) -> None:
            if self._window is None:
                return
            try:
                payload = json.dumps(event, ensure_ascii=False)
                self._window.evaluate_js(
                    f"window.onChatEvent && onChatEvent({payload})"
                )
            except Exception:
                pass  # page not ready / older frontend: return value covers it

        answer, end_reason = "", "failed"
        for event in self._chat_agent.ask(paper_id, message):
            if event["type"] == "done":
                answer = event["answer"]
                end_reason = event["end_reason"]
            push(event)
        return json.dumps(
            {"response": answer, "end_reason": end_reason},
            ensure_ascii=False,
        )

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
        entries = parse_bibtex(text)

        if not entries:
            return json.dumps({"ok": False, "error": "No entries found in BibTeX"})

        imported = []
        for entry in entries:
            paper = entry_to_paper(entry)
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
        """Save a single Zotero-scanned item to the Otlet database."""
        item = json.loads(item_json)
        result = save_zotero_item(self._db, self._pdf_store, item)
        return json.dumps(result)

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

        platform.open_path(path)
        return json.dumps({"ok": True})

    def reveal_pdf(self, paper_id: str) -> str:
        """Reveal the paper's PDF in the system file manager (Finder on
        macOS, Explorer with the file selected on Windows)."""
        paper = self._db.get_paper(paper_id)
        if not paper or not paper.pdf_path:
            return json.dumps({"ok": False, "error": "No PDF available"})
        path = Path(paper.pdf_path).expanduser().resolve()
        if not path.exists():
            return json.dumps({"ok": False, "error": "PDF file not found"})

        platform.reveal_path(path)
        return json.dumps({"ok": True})

    def open_url(self, url: str) -> str:
        """Open an external URL with the system default browser."""
        if not url.startswith(("http://", "https://")):
            return json.dumps({"ok": False, "error": "Unsupported URL"})
        platform.open_url(url)
        return json.dumps({"ok": True})

    def update_paper(self, paper_id: str, fields_json: str) -> str:
        """Update paper metadata fields."""
        fields = json.loads(fields_json)
        self._db.update_paper(paper_id, **fields)
        return json.dumps({"ok": True})

    def update_paper_authors(self, paper_id: str, authors_json: str) -> str:
        """Replace a paper's author list."""
        names = json.loads(authors_json)
        from otlet.models.author import Author

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

    def create_theme(self, name: str) -> str:
        """Create a top-level theme tag (is_top=1)."""
        if not name or not name.strip():
            return json.dumps({"ok": False, "error": "Theme name is empty"})
        tag = self._db.create_tag(name.strip())
        self._db.set_tag_top(tag.id, True)
        return json.dumps({"ok": True, "id": tag.id, "name": tag.name})

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
            from otlet.storage.keyword_extract import extract_keywords

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
            "model": self._settings.model,
            "api_key": mask(self._settings.api_key),
            "api_base": self._settings.api_base or "",
            "s2_api_key": mask(self._settings.s2_api_key),
            "theme": self._settings.theme,
            # A configured base URL alone counts: local OpenAI-compatible
            # servers (Ollama, LM Studio, vLLM) don't need an API key
            "has_llm": bool(self._settings.api_key or self._settings.api_base),
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
        if "model" in data:
            self._settings.model = data["model"]
        if "api_key" in data and data["api_key"]:
            self._settings.api_key = data["api_key"]
        if "api_base" in data:
            self._settings.api_base = data["api_base"] or None
        if "s2_api_key" in data and data["s2_api_key"]:
            self._settings.s2_api_key = data["s2_api_key"]
        self._settings.save()
        # Re-init LLM with new settings
        self._llm = LLMProvider(
            model=self._settings.model,
            api_key=self._settings.api_key,
            api_base=self._settings.api_base,
        )
        self._chat_agent = ChatAgent(
            self._llm,
            self._pdf_store,
            db=self._db,
            openalex=self._openalex,
            search_agent=self._search_agent,
        )
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
        to export a single paper or the current tag-filtered selection.
        Shows a native save dialog when target_path is empty.
        """
        from otlet.storage.bibtex_export import generate_bibtex

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
