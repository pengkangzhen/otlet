"""CLI entry point for otlet.

Shares every import/dedup/auto-tag rule with the GUI through
`otlet.services.importers`, so both front-ends behave identically.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from otlet import __version__, platform
from otlet.agents.chat import ChatAgent
from otlet.agents.classify import ClassifyAgent
from otlet.agents.openalex import OpenAlexClient
from otlet.agents.search import SearchAgent
from otlet.config.settings import Settings
from otlet.llm.provider import LLMProvider
from otlet.logs import install_excepthook, setup_logging
from otlet.services.importers import (
    entry_to_paper,
    extract_auto_tags,
    find_duplicate,
    import_pdf_file,
    parse_bibtex,
    save_zotero_item,
)
from otlet.storage.database import Database, DatabaseCorruptError
from otlet.storage.pdf_metadata import PDFMetadataExtractor
from otlet.storage.pdf_store import PDFStore

console = Console()

_METHOD_LABELS = {
    "xmp_doi": "XMP metadata DOI",
    "text_doi": "PDF text DOI",
    "arxiv_id": "ArXiv ID",
    "xmp_title": "XMP title search",
    "heuristic_title": "Title heuristic search",
    "fallback_text": "Text fallback",
}


@contextmanager
def _open_db(settings: Settings):
    db = Database(settings.db_path)
    try:
        yield db
    finally:
        db.close()


def _error(msg: str) -> None:
    console.print(f"[red]✗ {msg}[/red]")


# ── gui / tui ──────────────────────────────────────────────


def cmd_gui(args, settings: Settings) -> int:
    """Launch the desktop GUI application."""
    from otlet.web.app import launch_gui

    launch_gui(settings)
    return 0


def cmd_tui(args, settings: Settings) -> int:
    """Launch the terminal UI (textual)."""
    from otlet.tui import OtletTUI

    OtletTUI(settings).run()
    return 0


# ── search (online) ────────────────────────────────────────


def cmd_search(args, settings: Settings) -> int:
    """Search for papers on Semantic Scholar and display results."""
    agent = SearchAgent(api_key=settings.s2_api_key)
    papers = agent.run(args.query, limit=args.limit)
    if not papers:
        console.print("[yellow]No results found.[/yellow]")
        return 0

    table = Table(title="Search Results", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("Authors", max_width=30)
    table.add_column("Year", justify="right", width=6)
    table.add_column("Citations", justify="right", width=10)
    table.add_column("DOI", max_width=25)

    for i, paper in enumerate(papers, 1):
        table.add_row(
            str(i),
            paper.title,
            paper.display_authors,
            str(paper.year or ""),
            str(paper.citation_count or ""),
            paper.doi or "",
        )
    console.print(table)
    console.print(
        "[dim]Import with: otlet add --doi <DOI> "
        "or otlet add --query \"...\"[/dim]"
    )
    return 0


# ── add (unified import) ───────────────────────────────────


def _llm_classify(settings: Settings, paper) -> list[str]:
    """Ask the configured LLM for tag suggestions (may fail offline)."""
    llm = LLMProvider(
        model=settings.model,
        api_key=settings.api_key,
        api_base=settings.api_base,
    )
    return ClassifyAgent(llm).run(paper)


def _add_online_paper(db: Database, paper, settings: Settings, auto_tag: bool) -> bool:
    """Import one online-fetched paper with dedup + auto tags."""
    dup = find_duplicate(db, paper)
    if dup:
        console.print(f"[yellow]⏭ Already in library:[/yellow] {dup.title}")
        return False
    paper.auto_tags = extract_auto_tags(paper)
    db.add_paper(paper)
    console.print(
            f"[green]✓ Imported:[/green] {paper.title} "
            f"[dim](id: {paper.id})[/dim]"
        )
    if auto_tag:
        try:
            suggested = _llm_classify(settings, paper)
        except Exception as e:
            _error(f"LLM auto-tag failed: {e}")
            suggested = []
        for tag in suggested:
            db.add_tag_to_paper(paper.id, tag, source="auto")
        if suggested:
            console.print(f"  [cyan]LLM tags: {', '.join(suggested)}[/cyan]")
    if paper.auto_tags:
        console.print(f"  [dim]auto tags: {', '.join(paper.auto_tags)}[/dim]")
    return True


def _print_pdf_import_result(result: dict, path: Path) -> None:
    if result.get("ok"):
        paper = result["paper"]
        method = _METHOD_LABELS.get(result.get("method", ""), result.get("method", ""))
        console.print(
            f"[green]✓[/green] {path.name} → {paper['title']} "
            f"[dim](id: {paper['id']}, via {method}, "
            f"confidence {result.get('confidence', 0) * 100:.0f}%)[/dim]"
        )
    elif result.get("duplicate"):
        note = " (PDF attached to existing entry)" if result.get("attached") else ""
        console.print(
            f"[yellow]⏭[/yellow] {path.name}: {result.get('error')}{note}"
        )
    else:
        console.print(f"[red]✗[/red] {path.name}: {result.get('error')}")


def _scan_folder_pdfs(folder: Path) -> list[Path]:
    """Recursively collect PDF files, skipping hidden/system directories."""
    skip = {".git", ".DS_Store", "__MACOSX", "node_modules"}
    return sorted(
        p for p in folder.rglob("*.pdf")
        if not any(part.startswith(".") or part in skip for part in p.parts)
    )


def cmd_add(args, settings: Settings) -> int:
    """Import papers: PDFs, DOI, online query, BibTeX, folder, or Zotero."""
    with _open_db(settings) as db:
        pdf_store = PDFStore(settings.pdf_dir)
        extractor = PDFMetadataExtractor(s2_api_key=settings.s2_api_key)
        any_imported = False
        rc = 0

        if args.bibtex:
            bib = Path(args.bibtex).expanduser().resolve()
            if not bib.exists():
                _error(f"File not found: {bib}")
                rc = 1
            else:
                entries = parse_bibtex(
                    bib.read_text(encoding="utf-8", errors="replace")
                )
                if not entries:
                    _error("No entries found in BibTeX")
                    rc = 1
                for entry in entries:
                    paper = entry_to_paper(entry)
                    if find_duplicate(db, paper):
                        console.print(
                            f"[yellow]⏭ Already in library:[/yellow] {paper.title}"
                        )
                        continue
                    db.add_paper(paper)
                    any_imported = True
                    console.print(
                        f"[green]✓ Imported:[/green] {paper.title} "
                        f"[dim](id: {paper.id})[/dim]"
                    )

        if args.folder:
            folder = Path(args.folder).expanduser().resolve()
            if not folder.is_dir():
                _error(f"Not a directory: {folder}")
                rc = 1
            else:
                pdfs = _scan_folder_pdfs(folder)
                if not pdfs:
                    console.print(f"[yellow]No PDFs under {folder}[/yellow]")
                for pdf in pdfs:
                    result = import_pdf_file(db, pdf_store, extractor, pdf)
                    any_imported |= bool(result.get("ok"))
                    _print_pdf_import_result(result, pdf)

        if args.zotero:
            from otlet.storage.zotero_import import (
                find_zotero_db,
                import_from_zotero,
            )

            zdb = find_zotero_db()
            if not zdb:
                _error("Zotero database not found")
                rc = 1
            else:
                collection_ids = None
                if args.collections:
                    try:
                        collection_ids = [
                            int(c) for c in args.collections.split(",") if c.strip()
                        ]
                    except ValueError:
                        _error("--collections expects comma-separated integers")
                        return 1
                items = import_from_zotero(
                    zdb,
                    zotero_dir=zdb.parent,
                    collection_ids=collection_ids,
                    include_pdfs=not args.no_pdf,
                )
                console.print(f"[dim]Zotero: {len(items)} items scanned[/dim]")
                for item in items:
                    result = save_zotero_item(db, pdf_store, item)
                    if result.get("ok"):
                        paper = db.get_paper(result["id"])
                        any_imported = True
                        console.print(
                            f"[green]✓ Imported:[/green] {paper.title} "
                            f"[dim](id: {paper.id})[/dim]"
                        )
                    elif result.get("duplicate"):
                        console.print("[yellow]⏭ Duplicate skipped[/yellow]")
                    else:
                        console.print(
                            f"[red]✗[/red] {result.get('error', 'invalid item')}"
                        )

        if args.doi:
            agent = SearchAgent(api_key=settings.s2_api_key)
            paper = agent.fetch_by_doi(args.doi)
            if not paper:
                _error(f"Paper not found for DOI: {args.doi}")
                rc = 1
            else:
                any_imported |= _add_online_paper(db, paper, settings, args.auto_tag)

        if args.query:
            agent = SearchAgent(api_key=settings.s2_api_key)
            results = agent.run(args.query, limit=1)
            if not results:
                _error(f"No results found for: {args.query}")
                rc = 1
            else:
                any_imported |= _add_online_paper(
                    db, results[0], settings, args.auto_tag
                )

        for raw in args.pdfs:
            pdf = Path(raw).expanduser().resolve()
            result = import_pdf_file(db, pdf_store, extractor, pdf)
            any_imported |= bool(result.get("ok"))
            _print_pdf_import_result(result, pdf)

        if not any_imported and rc == 0:
            console.print(
                "[dim]Nothing imported. Give PDF paths, or one of "
                "--doi / --query / --bibtex / --folder / --zotero.[/dim]"
            )
        return rc


# ── full-text search / index backfill ──────────────────────


def cmd_grep(args, settings: Settings) -> int:
    """Search inside indexed PDFs: which paper, which page, what context."""
    from otlet.storage.pdf_index import PDFIndex

    with _open_db(settings) as db:
        index = PDFIndex(db, PDFStore(settings.pdf_dir))
        hits = index.search(args.query, limit=args.limit)
        if not hits:
            console.print("[yellow]No full-text matches.[/yellow] "
                          "(Run `otlet index` to index stored PDFs.)")
            return 0
        titles = {
            p.id: p.title
            for p in db.list_papers()
        }

    table = Table(
        title=f"Full-text: {args.query} ({len(hits)} pages)", show_lines=True
    )
    table.add_column("Paper", style="bold", max_width=42)
    table.add_column("Page", justify="right", width=5)
    table.add_column("Context", max_width=60)
    for h in hits:
        table.add_row(
            f"{titles.get(h['paper_id'], h['paper_id'])} "
            f"[dim]({h['paper_id']})[/dim]",
            str(h["page"]),
            h["snippet"],
        )
    console.print(table)
    return 0


def cmd_index(args, settings: Settings) -> int:
    """Build the per-page full-text index for stored PDFs missing one."""
    from otlet.storage.pdf_index import PDFIndex

    with _open_db(settings) as db:
        index = PDFIndex(db, PDFStore(settings.pdf_dir))
        missing = db.papers_missing_index()
        if not missing:
            console.print("[green]✓ All stored PDFs are already indexed.[/green]")
            return 0
        rebuilt, failed = 0, 0
        for i, paper_id in enumerate(missing, 1):
            try:
                pages = index.build(paper_id)
            except Exception as e:
                failed += 1
                console.print(f"[red]✗[/red] {paper_id}: {e}")
                continue
            rebuilt += 1
            console.print(f"[green]✓[/green] {paper_id}: {pages} pages "
                          f"[dim]({i}/{len(missing)})[/dim]")
        console.print(
            f"Indexed {rebuilt} paper(s)"
            + (f", {failed} failed" if failed else "")
        )
    return 0


# ── list / show / open ─────────────────────────────────────


def cmd_list(args, settings: Settings) -> int:
    """List papers in the local library, with optional filters."""
    with _open_db(settings) as db:
        papers = db.list_papers()
        if args.query:
            keep = {p.id for p in db.search_papers(args.query)}
            papers = [p for p in papers if p.id in keep]
        if args.tags:
            tag_list = [t.strip() for t in args.tags.split(",") if t.strip()]
            keep = {
                p.id for p in db.find_papers_by_tags(tag_list, match_all=not args.any)
            }
            papers = [p for p in papers if p.id in keep]

    if args.json:
        console.print_json(
            json.dumps([p.model_dump(mode="json") for p in papers],
                       ensure_ascii=False)
        )
        return 0

    if not papers:
        console.print("[yellow]Library is empty.[/yellow]")
        return 0

    table = Table(title=f"Library ({len(papers)})", show_lines=True)
    table.add_column("ID", style="dim", width=12)
    table.add_column("Title", style="bold")
    table.add_column("Authors", max_width=25)
    table.add_column("Year", justify="right", width=6)
    table.add_column("Tags", style="cyan")

    for paper in papers:
        table.add_row(
            paper.id,
            paper.title,
            paper.display_authors,
            str(paper.year or ""),
            ", ".join(paper.tags) if paper.tags else "-",
        )
    console.print(table)
    return 0


def cmd_show(args, settings: Settings) -> int:
    """Show full details of one paper."""
    with _open_db(settings) as db:
        paper = db.get_paper(args.paper_id)
        if not paper:
            _error(f"Paper not found: {args.paper_id}")
            return 1
        notes = db.list_notes(paper.id)

    lines = [
        f"[bold]{paper.title}[/bold]",
        f"Authors:  {paper.full_authors}",
        f"Year:     {paper.year or '-'}    Venue: {paper.venue or '-'}"
        f"    Type: {paper.paper_type or '-'}",
    ]
    if paper.doi:
        lines.append(f"DOI:      {paper.doi}")
    if paper.url:
        lines.append(f"URL:      {paper.url}")
    lines.append(f"Added:    {paper.added_date}    ID: {paper.id}")
    if paper.tags:
        lines.append(f"Tags:     [cyan]{', '.join(paper.tags)}[/cyan]")
    if paper.auto_tags:
        lines.append(f"Auto:     [dim]{', '.join(paper.auto_tags)}[/dim]")
    if paper.pdf_path:
        lines.append(f"PDF:      {paper.pdf_path}")
    console.print(Panel("\n".join(lines), title="Paper"))

    if paper.abstract:
        abstract = paper.abstract if args.full else paper.abstract[:800]
        more = "" if args.full or len(paper.abstract) <= 800 else " …"
        console.print(Panel(abstract + more, title="Abstract", border_style="dim"))

    if notes:
        table = Table(title="Notes", show_lines=True)
        table.add_column("ID", style="dim", width=12)
        table.add_column("Date", width=10)
        table.add_column("Content")
        for n in notes:
            table.add_row(n["id"], str(n["created_at"])[:10], n["content"])
        console.print(table)
    return 0


def cmd_open(args, settings: Settings) -> int:
    """Open a paper's PDF with the system default viewer."""
    with _open_db(settings) as db:
        paper = db.get_paper(args.paper_id)
    if not paper or not paper.pdf_path:
        _error(f"No PDF available for: {args.paper_id}")
        return 1
    path = Path(paper.pdf_path).expanduser().resolve()
    if not path.exists():
        _error(f"PDF file not found: {path}")
        return 1

    platform.open_path(path)
    console.print(f"[green]✓ Opened[/green] {path}")
    return 0


# ── trash ──────────────────────────────────────────────────


def cmd_rm(args, settings: Settings) -> int:
    """Move papers to the trash (soft delete)."""
    with _open_db(settings) as db:
        for paper_id in args.paper_ids:
            paper = db.get_paper(paper_id)
            if not paper:
                _error(f"Paper not found: {paper_id}")
                continue
            db.delete_paper(paper_id)
            console.print(f"[green]✓ Moved to trash:[/green] {paper.title}")
    return 0


def cmd_trash(args, settings: Settings) -> int:
    """List / restore / purge / empty the trash."""
    with _open_db(settings) as db:
        if args.action in (None, "list"):
            papers = db.list_trash()
            if not papers:
                console.print("[yellow]Trash is empty.[/yellow]")
                return 0
            table = Table(title=f"Trash ({len(papers)})", show_lines=True)
            table.add_column("ID", style="dim", width=12)
            table.add_column("Title", style="bold")
            table.add_column("Deleted", width=10)
            for p in papers:
                table.add_row(p.id, p.title, str(p.deleted_date or ""))
            console.print(table)
            return 0

        if args.action == "restore":
            if not args.paper_id:
                _error("Usage: otlet trash restore <paper_id>")
                return 1
            # get_paper() only sees live rows — look the title up in the trash
            title = next(
                (p.title for p in db.list_trash() if p.id == args.paper_id),
                args.paper_id,
            )
            db.restore_paper(args.paper_id)
            console.print(f"[green]✓ Restored:[/green] {title}")
            return 0

        if args.action == "purge":
            if not args.paper_id:
                _error("Usage: otlet trash purge <paper_id>")
                return 1
            if not args.yes and not Confirm.ask(
                "Permanently delete this paper (and its PDF file)?"
            ):
                return 0
            db.purge_paper(args.paper_id)
            PDFStore(settings.pdf_dir).remove(args.paper_id)
            console.print("[green]✓ Purged.[/green]")
            return 0

        if not args.yes and not Confirm.ask("Empty the entire trash?"):
            return 0
        trashed = db.list_trash()
        count = db.empty_trash()
        pdf_store = PDFStore(settings.pdf_dir)
        for p in trashed:
            pdf_store.remove(p.id)
        console.print(f"[green]✓ Emptied trash:[/green] {count} papers purged.")
        return 0


# ── tags ───────────────────────────────────────────────────


def cmd_tags(args, settings: Settings) -> int:
    """Manage tags: list (default), create, rename, delete."""
    with _open_db(settings) as db:
        if args.action in (None, "list"):
            tags = db.list_tags(include_auto=args.auto)
            if not tags:
                console.print("[yellow]No tags yet.[/yellow]")
                return 0
            table = Table(title="Tags")
            table.add_column("Tag", style="cyan")
            table.add_column("Category")
            table.add_column("ID", style="dim")
            for tag in tags:
                table.add_row(
                    f"#{tag.name}", tag.category or "-", tag.id,
                )
            console.print(table)
            return 0

        if args.action == "create":
            tag = db.create_tag(args.name, color=args.color)
            console.print(f"[green]✓ Tag ready:[/green] #{tag.name}")
            return 0

        if args.action == "rename":
            if not args.new_name or not args.new_name.strip():
                _error("New tag name is empty")
                return 1
            db.rename_tag(args.name, args.new_name.strip())
            console.print(
                f"[green]✓ Renamed:[/green] #{args.name} → #{args.new_name.strip()}"
            )
            return 0

        # delete
        tag = db.get_tag_by_name(args.name)
        if not tag:
            _error(f"Tag not found: {args.name}")
            return 1
        db.delete_tag(tag.id)
        console.print(f"[green]✓ Tag deleted:[/green] #{tag.name}")
        return 0


def cmd_tag(args, settings: Settings) -> int:
    """Add/remove tags on a paper."""
    with _open_db(settings) as db:
        for tag in args.tags:
            if args.action == "add":
                db.add_tag_to_paper(args.paper_id, tag, source="manual")
                console.print(f"[green]✓ Added #{tag} to {args.paper_id}[/green]")
            else:
                db.remove_tag_from_paper(args.paper_id, tag)
                console.print(f"[green]✓ Removed #{tag} from {args.paper_id}[/green]")
    return 0


def cmd_autotag(args, settings: Settings) -> int:
    """Suggest tags for a stored paper (offline RAKE or LLM)."""
    with _open_db(settings) as db:
        paper = db.get_paper(args.paper_id)
        if not paper:
            _error(f"Paper not found: {args.paper_id}")
            return 1

        if args.method == "nlp":
            from otlet.storage.keyword_extract import extract_keywords

            suggested = extract_keywords(paper.title, paper.abstract)
        else:
            try:
                suggested = _llm_classify(settings, paper)
            except Exception as e:
                _error(f"LLM auto-tag failed: {e} "
                       "(configure via `otlet settings set`, "
                       "or use --method nlp)")
                return 1

        for tag in suggested:
            db.add_tag_to_paper(paper.id, tag, source="auto")
    if suggested:
        console.print(f"[cyan]Suggested tags: {', '.join(suggested)}[/cyan]")
    else:
        console.print("[yellow]No tags suggested.[/yellow]")
    return 0


# ── notes ──────────────────────────────────────────────────


def cmd_note(args, settings: Settings) -> int:
    """Manage reading notes: list, add, rm."""
    with _open_db(settings) as db:
        if args.note_cmd == "add":
            note = db.add_note(args.paper_id, args.text.strip())
            console.print(
                f"[green]✓ Note added[/green] (id: {note['id']}) "
                f"to paper {args.paper_id}"
            )
            return 0

        if args.note_cmd == "rm":
            db.delete_note(args.note_id)
            console.print(f"[green]✓ Note deleted:[/green] {args.note_id}")
            return 0

        # list
        notes = db.list_notes(args.paper_id)
        if not notes:
            console.print("[yellow]No notes.[/yellow]")
            return 0
        table = Table(title="Notes", show_lines=True)
        table.add_column("ID", style="dim", width=12)
        table.add_column("Date", width=10)
        table.add_column("Content")
        for n in notes:
            table.add_row(n["id"], str(n["created_at"])[:10], n["content"])
        console.print(table)
        return 0


# ── chat ───────────────────────────────────────────────────


def cmd_chat(args, settings: Settings) -> int:
    """Chat with a paper: one-shot (-m) or an interactive session.

    The agent runs a tool loop: it reads PDF pages (read_pdf_pages) and
    searches the library (search_library) as needed; intermediate tool
    traffic is persisted with the conversation and replayed as context.
    """
    with _open_db(settings) as db:
        paper = db.get_paper(args.paper_id)
        if not paper:
            _error(f"Paper not found: {args.paper_id}")
            return 1

        llm = LLMProvider(
            model=settings.model,
            api_key=settings.api_key,
            api_base=settings.api_base,
        )
        agent = ChatAgent(
            llm,
            PDFStore(settings.pdf_dir),
            db=db,
            openalex=OpenAlexClient(mailto=settings.openalex_email),
            search_agent=SearchAgent(api_key=settings.s2_api_key),
        )

        def ask(message: str) -> None:
            console.print("[bold cyan]otlet›[/bold cyan] ", end="")
            streamed = False
            try:
                for event in agent.ask(args.paper_id, message):
                    if event["type"] == "delta":
                        if event["text"]:
                            streamed = True
                            console.out(event["text"], end="")
                    elif event["type"] == "tool":
                        # tool activity line — new line if mid-stream text
                        if streamed:
                            console.print()
                            streamed = False
                        console.print(
                            f"  [dim]→ {event['name']}({event['detail']})"
                            "...[/dim]"
                        )
                    elif event["type"] == "done":
                        if event["end_reason"] != "done" or not event[
                            "answer"
                        ]:
                            streamed = True  # non-streamed outcome below
                            console.print(event["answer"])
            except Exception as e:
                console.print()
                _error(f"LLM call failed: {e} "
                       "(configure via `otlet settings set`)")
                return
            console.print("\n")

        if args.message:
            ask(args.message)
            return 0

        console.print(
            f"[bold]Chatting about:[/bold] {paper.title}\n"
            "[dim]Type your question; /quit or Ctrl+D to leave. "
            "History is saved per paper.[/dim]"
        )
        while True:
            try:
                question = Prompt.ask("[bold cyan]you›[/bold cyan]")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if question.strip() in ("/quit", "/exit"):
                break
            if not question.strip():
                continue
            ask(question)
        return 0


# ── backup / restore / log ─────────────────────────────────


def cmd_backup(args, settings: Settings) -> int:
    """Create a verified snapshot (library + PDFs; config excluded)."""
    from otlet.storage.backup import create_backup

    out_dir = Path(args.out) if args.out else settings.data_dir / "backups"
    try:
        archive = create_backup(
            settings.db_path, settings.pdf_dir, out_dir=out_dir
        )
    except Exception as e:
        _error(f"Backup failed: {e}")
        return 1
    console.print(f"[green]✓ Backup written[/green] {archive}")
    console.print("[dim]Run `otlet backup` regularly — see docs for the "
                  "7-copy rotation policy.[/dim]")
    return 0


def cmd_restore(args, settings: Settings) -> int:
    """Restore a backup archive (current library is renamed aside)."""
    from otlet.storage.backup import restore_archive, verify_archive

    archive = Path(args.archive).expanduser().resolve()
    if not archive.exists():
        _error(f"Archive not found: {archive}")
        return 1
    try:
        manifest = verify_archive(archive)
    except Exception as e:
        _error(f"Archive verification failed: {e}")
        return 1
    console.print(
        f"Archive OK: {manifest.get('papers')} papers, "
        f"{len(manifest.get('pdfs', []))} PDFs, "
        f"created {manifest.get('created_at')}"
    )
    if not args.yes and not Confirm.ask(
        f"Restore over {settings.data_dir}? (current library.db is "
        "renamed aside, never deleted)"
    ):
        return 0
    try:
        result = restore_archive(
            archive, settings.db_path, settings.pdf_dir
        )
    except Exception as e:
        _error(f"Restore failed (rolled back): {e}")
        return 1
    console.print(
        f"[green]✓ Restored[/green] {result['papers']} papers, "
        f"{result['pdfs']} PDFs. Aside copies: "
        f"{', '.join(result['moved_aside']) or '(none)'}"
    )
    return 0


def cmd_log(args, settings: Settings) -> int:
    """Show the tail of the log file (for bug reports)."""
    from otlet.logs import log_path

    path = log_path(settings.data_dir)
    if not path.exists():
        console.print(f"[yellow]No log yet:[/yellow] {path}")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        console.print(line, highlight=False)
    console.print(f"[dim]{path}[/dim]")
    return 0


# ── verify (claim checking, no LLM needed) ─────────────────


def cmd_verify(args, settings: Settings) -> int:
    """Check whether literature supports claims — verdicts come from
    fixed code rules (supports/partial/none), local + OpenAlex + S2."""
    from otlet.agents.find_literature import run_find_literature
    from otlet.storage.pdf_index import PDFIndex

    with _open_db(settings) as db:
        text = args.claims.strip()
        # multi-sentence input (sentence punctuation present) splits
        # into claims; a single phrase stays one claim
        has_sentences = any(c in text for c in ".。!！?？;")
        result = run_find_literature(
            db,
            claims=None if has_sentences else [text],
            claim_text=text if has_sentences else None,
            claims_en=[args.en] if args.en else None,
            year_from=args.year_from,
            year_to=args.year_to,
            openalex=OpenAlexClient(mailto=settings.openalex_email),
            search_agent=SearchAgent(api_key=settings.s2_api_key),
            pdf_index=PDFIndex(db, PDFStore(settings.pdf_dir)),
        )

    for note in result["notes"]:
        console.print(f"[yellow]note:[/yellow] {note}")
    for item in result["claims"]:
        verdict = item["verdict"]
        style = {
            "supports": "green", "partial": "yellow", "none": "red",
        }.get(verdict, "white")
        console.print(
            f"\n[bold]声明[/bold] {item['claim']}\n"
            f"判定: [{style}]{verdict}[/{style}]"
        )
        if item["evidence"]:
            table = Table(show_lines=False)
            table.add_column("Verdict", width=9)
            table.add_column("Paper", max_width=44)
            table.add_column("Evidence", max_width=60, overflow="fold")
            for ev in item["evidence"]:
                lib = " [cyan](在库内)[/cyan]" if ev["in_library"] else ""
                table.add_row(
                    ev["verdict"],
                    (ev["title"] or "?") + lib,
                    (ev["evidence_text"] or "")[:200],
                )
            console.print(table)
        if item["recalled_but_not_supporting"]:
            titles = "; ".join(
                c["title"] or "?" for c in item[
                    "recalled_but_not_supporting"
                ]
            )
            console.print(f"[dim]召回但不支撑: {titles}[/dim]")
    s = result["summary"]
    console.print(
        f"\n[bold]小结[/bold]: supported={s['supported']} "
        f"partial={s['partial']} not_found={s['not_found']} — "
        "not_found 意为未判定，不等于证伪"
    )
    return 0


# ── enrich (metadata backfill) ─────────────────────────────


def cmd_enrich(args, settings: Settings) -> int:
    """Backfill missing metadata via OpenAlex → Semantic Scholar."""
    from otlet.services.enrich import enrich_paper

    with _open_db(settings) as db:
        if args.ids:
            wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
            papers = [p for p in (db.get_paper(i) for i in wanted) if p]
        else:
            ids = db.papers_needing_enrich()
            if args.limit:
                ids = ids[: args.limit]
            papers = [db.get_paper(i) for i in ids]
            papers = [p for p in papers if p]

        if not papers:
            console.print("[yellow]No papers need enrichment.[/yellow]")
            return 0

        openalex = OpenAlexClient(mailto=settings.openalex_email)
        s2 = SearchAgent(api_key=settings.s2_api_key)
        filled_count = matched = 0
        for paper in papers:
            result = enrich_paper(
                db, paper, openalex=openalex, search_agent=s2,
                dry_run=args.dry_run,
            )
            if result["match"] is None:
                console.print(f"[yellow]⏭[/yellow] {paper.title} — no match")
                continue
            matched += 1
            patch = result["filled"]
            if not patch:
                console.print(
                    f"[dim]✓[/dim] {paper.title} — already complete "
                    f"({result['match']})"
                )
                continue
            filled_count += 1
            fields = ", ".join(
                f"{k}={str(v)[:40]}" for k, v in patch.items()
            )
            tag = "[cyan]preview[/cyan] " if args.dry_run else ""
            console.print(
                f"[green]✓[/green] {paper.title} — {tag}{fields}"
            )
        console.print(
            f"{matched}/{len(papers)} matched, "
            f"{filled_count} papers {'to fill' if args.dry_run else 'filled'}"
        )
    return 0


# ── export / settings ──────────────────────────────────────


def cmd_export(args, settings: Settings) -> int:
    """Export the library (or selected papers) to a BibTeX file."""
    from otlet.storage.bibtex_export import generate_bibtex

    with _open_db(settings) as db:
        papers = db.list_papers()
        if args.ids:
            wanted = {i.strip() for i in args.ids.split(",")}
            papers = [p for p in papers if p.id in wanted]

    if not papers:
        console.print("[yellow]No papers to export.[/yellow]")
        return 1

    out = Path(args.output).expanduser()
    out.write_text(generate_bibtex(papers), encoding="utf-8")
    console.print(f"[green]✓ Exported {len(papers)} papers[/green] → {out}")
    return 0


def _mask(secret: str | None) -> str:
    return (secret[:8] + "…") if secret else ""


def cmd_settings(args, settings: Settings) -> int:
    """Show or update settings stored in ~/.otlet/config.yaml."""
    if args.action in (None, "show"):
        config_path = Settings.config_path()
        table = Table(title=f"Settings ({config_path})")
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_row("model", settings.model)
        table.add_row(
            "api_key", _mask(settings.api_key) or "[dim]<unset>[/dim]"
        )
        table.add_row(
            "api_base", settings.api_base or "[dim]<unset>[/dim]"
        )
        table.add_row("s2_api_key", _mask(settings.s2_api_key) or "[dim]<unset>[/dim]")
        table.add_row("theme", settings.theme)
        table.add_row("data_dir", str(settings.data_dir))
        console.print(table)
        console.print(
            "[dim]Change with: otlet settings set <key> <value>[/dim]"
        )
        return 0

    key = args.key
    value = args.value
    if key not in Settings.model_fields:
        _error(
            f"Unknown setting: {key} "
            f"(valid: {', '.join(Settings.model_fields)})"
        )
        return 1
    if key == "default_limit":
        try:
            value = int(value)
        except ValueError:
            _error("default_limit must be an integer")
            return 1
    if key == "theme":
        valid_themes = ("light", "dark", "classic-light", "classic-dark")
        if value not in valid_themes:
            _error(f"theme must be one of: {', '.join(valid_themes)}")
            return 1
    setattr(settings, key, value)
    settings.save()
    console.print(f"[green]✓ {key} saved.[/green]")
    return 0


# ── argument parser ────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otlet",
        description="Otlet — agent-based literature management",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.set_defaults(func=None)
    sub = parser.add_subparsers(title="commands")

    # gui
    p_gui = sub.add_parser("gui", help="Launch desktop GUI (default)")
    p_gui.set_defaults(func=cmd_gui)

    # search
    p_search = sub.add_parser("search", help="Search papers online (Semantic Scholar)")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    # grep / index — per-page PDF full-text search
    p_grep = sub.add_parser(
        "grep", help="Search inside stored PDFs (paper + page + context)"
    )
    p_grep.add_argument("query", help="Text to find (>=3 chars uses the index)")
    p_grep.add_argument("--limit", type=int, default=50, help="Max page hits")
    p_grep.set_defaults(func=cmd_grep)

    p_index = sub.add_parser(
        "index", help="Build the full-text index for stored PDFs missing one"
    )
    p_index.set_defaults(func=cmd_index)

    # enrich — metadata backfill
    p_enrich = sub.add_parser(
        "enrich", help="Backfill missing metadata (OpenAlex → S2)"
    )
    p_enrich.add_argument(
        "--ids", help="Comma-separated paper ids (default: papers missing fields)"
    )
    p_enrich.add_argument(
        "--limit", type=int, help="Max papers to process"
    )
    p_enrich.add_argument(
        "--dry-run", action="store_true", help="Preview fills without writing"
    )
    p_enrich.set_defaults(func=cmd_enrich)

    # verify — claim checking without an LLM
    p_verify = sub.add_parser(
        "verify", help="Check whether literature supports a claim "
                       "(supports/partial/none)"
    )
    p_verify.add_argument("claims", help="Claim text (splits on sentence "
                                         "punctuation)")
    p_verify.add_argument("--en", help="English variant of the claim "
                                       "(recommended for Chinese claims)")
    p_verify.add_argument("--year-from", type=int)
    p_verify.add_argument("--year-to", type=int)
    p_verify.set_defaults(func=cmd_verify)

    # tui — terminal user interface
    p_tui = sub.add_parser("tui", help="Terminal UI (textual)")
    p_tui.set_defaults(func=cmd_tui)

    # backup / restore / log
    p_backup = sub.add_parser(
        "backup", help="Create a verified snapshot (library + PDFs)"
    )
    p_backup.add_argument(
        "--out", help="Backup directory (default: <data_dir>/backups)"
    )
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser(
        "restore", help="Restore a backup archive (current library "
                        "renamed aside)"
    )
    p_restore.add_argument("archive", help="Path to the backup .zip")
    p_restore.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    p_restore.set_defaults(func=cmd_restore)

    p_log = sub.add_parser(
        "log", help="Show the tail of the log file (for bug reports)"
    )
    p_log.add_argument(
        "--lines", type=int, default=40, help="Lines to show (default 40)"
    )
    p_log.set_defaults(func=cmd_log)

    # add — unified import
    p_add = sub.add_parser(
        "add", help="Import papers (PDF/DOI/query/BibTeX/folder/Zotero)"
    )
    p_add.add_argument("pdfs", nargs="*", help="PDF file paths")
    p_add.add_argument("--doi", help="Import by DOI")
    p_add.add_argument("--query", help="Import the top online search result")
    p_add.add_argument("--bibtex", help="Import from a .bib file")
    p_add.add_argument("--folder", help="Import every PDF under a folder")
    p_add.add_argument("--zotero", action="store_true", help="Import from Zotero")
    p_add.add_argument("--collections", help="Zotero collection ids, comma-separated")
    p_add.add_argument("--no-pdf", action="store_true", help="Zotero: skip PDF copies")
    p_add.add_argument("--auto-tag", action="store_true", help="LLM tag suggestions")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", help="List papers in the library")
    p_list.add_argument("-q", "--query", help="Local full-text filter")
    p_list.add_argument("--tags", help="Filter by tags (comma-separated, AND)")
    p_list.add_argument("--any", action="store_true", help="Tag filter uses OR logic")
    p_list.add_argument("--json", action="store_true", help="Output JSON")
    p_list.set_defaults(func=cmd_list)

    # show / open / rm
    p_show = sub.add_parser("show", help="Show paper details")
    p_show.add_argument("paper_id")
    p_show.add_argument("--full", action="store_true", help="Show full abstract")
    p_show.set_defaults(func=cmd_show)

    p_open = sub.add_parser("open", help="Open a paper's PDF")
    p_open.add_argument("paper_id")
    p_open.set_defaults(func=cmd_open)

    p_rm = sub.add_parser("rm", help="Move papers to trash")
    p_rm.add_argument("paper_ids", nargs="+")
    p_rm.set_defaults(func=cmd_rm)

    # trash
    p_trash = sub.add_parser("trash", help="Trash: list/restore/purge/empty")
    p_trash.add_argument(
        "action", nargs="?", choices=["list", "restore", "purge", "empty"]
    )
    p_trash.add_argument("paper_id", nargs="?", help="Paper id (restore/purge)")
    p_trash.add_argument("--yes", action="store_true", help="Skip confirmation")
    p_trash.set_defaults(func=cmd_trash)

    # tags / tag
    p_tags = sub.add_parser("tags", help="Tags: list (default)/create/rename/delete")
    p_tags.add_argument(
        "action", nargs="?", choices=["list", "create", "rename", "delete"]
    )
    p_tags.add_argument("name", nargs="?", help="Tag name")
    p_tags.add_argument("new_name", nargs="?", help="New name (for rename)")
    p_tags.add_argument("--color", help="Tag color (for create)")
    p_tags.add_argument("--auto", action="store_true", help="Include auto-only tags")
    p_tags.set_defaults(func=cmd_tags)

    p_tag = sub.add_parser("tag", help="Tag/untag a paper")
    p_tag.add_argument("action", choices=["add", "remove"])
    p_tag.add_argument("paper_id")
    p_tag.add_argument("tags", nargs="+", help="One or more tag names")
    p_tag.set_defaults(func=cmd_tag)

    p_autotag = sub.add_parser("autotag", help="Suggest tags for a paper")
    p_autotag.add_argument("paper_id")
    p_autotag.add_argument(
        "--method", choices=["nlp", "llm"], default="nlp",
        help="nlp = offline keyword extraction (default), llm = configured model",
    )
    p_autotag.set_defaults(func=cmd_autotag)

    # notes — verbs as sub-subparsers so `rm` takes a note id, not a paper id
    p_note = sub.add_parser("note", help="Reading notes")
    note_sub = p_note.add_subparsers(dest="note_cmd", required=True)
    p_nl = note_sub.add_parser("list", help="List a paper's notes")
    p_nl.add_argument("paper_id")
    p_nl.set_defaults(func=cmd_note)
    p_na = note_sub.add_parser("add", help="Add a note")
    p_na.add_argument("paper_id")
    p_na.add_argument("text", help="Note text")
    p_na.set_defaults(func=cmd_note)
    p_nr = note_sub.add_parser("rm", help="Delete a note by id")
    p_nr.add_argument("note_id")
    p_nr.set_defaults(func=cmd_note)

    # chat
    p_chat = sub.add_parser("chat", help="Chat with a paper (AI)")
    p_chat.add_argument("paper_id")
    p_chat.add_argument("-m", "--message", help="One-shot question; omit for REPL")
    p_chat.set_defaults(func=cmd_chat)

    # export / settings
    p_export = sub.add_parser("export", help="Export library to BibTeX")
    p_export.add_argument("-o", "--output", default="library.bib")
    p_export.add_argument("--ids", help="Comma-separated paper ids (default: all)")
    p_export.set_defaults(func=cmd_export)

    p_settings = sub.add_parser("settings", help="Show/change settings")
    p_settings.add_argument("action", nargs="?", choices=["show", "set"])
    p_settings.add_argument("key", nargs="?", help="Setting key (for set)")
    p_settings.add_argument("value", nargs="?", help="New value (for set)")
    p_settings.set_defaults(func=cmd_settings)

    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings or Settings.load()
    setup_logging(settings.data_dir)
    install_excepthook()
    if args.func is None:
        # No subcommand → launch desktop GUI directly
        # (the packaged .app runs this module as its entry script)
        cmd_gui(args, settings)
        return 0

    try:
        return args.func(args, settings)
    except DatabaseCorruptError as e:
        _error(str(e))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
