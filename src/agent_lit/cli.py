"""CLI entry point for agent-lit."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent_lit import __version__
from agent_lit.agents.classify import ClassifyAgent
from agent_lit.agents.search import SearchAgent
from agent_lit.config.settings import Settings
from agent_lit.llm.provider import LLMProvider
from agent_lit.storage.database import Database
from agent_lit.storage.pdf_metadata import PDFMetadataExtractor
from agent_lit.storage.pdf_store import PDFStore

console = Console()


def cmd_search(args, settings: Settings) -> None:
    """Search for papers on Semantic Scholar and display results."""
    agent = SearchAgent(api_key=settings.s2_api_key)
    papers = agent.run(args.query, limit=args.limit)
    if not papers:
        console.print("[yellow]No results found.[/yellow]")
        return

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


def cmd_import(args, settings: Settings) -> None:
    """Import a paper from DOI or search, then save to local library."""
    db = Database(settings.db_path)
    agent = SearchAgent(api_key=settings.s2_api_key)

    if args.doi:
        paper = agent.fetch_by_doi(args.doi)
        if not paper:
            console.print(f"[red]Paper not found for DOI: {args.doi}[/red]")
            return
    elif args.query:
        results = agent.run(args.query, limit=1)
        if not results:
            console.print("[red]No results found.[/red]")
            return
        paper = results[0]
    else:
        console.print("[red]Provide --doi or a search query.[/red]")
        return

    # Auto-classify if requested
    if args.auto_tag:
        llm = LLMProvider(
            model=settings.lit_model,
            api_key=settings.lit_api_key,
            api_base=settings.lit_api_base,
        )
        classifier = ClassifyAgent(llm)
        suggested = classifier.run(paper)
        if suggested:
            paper.tags.extend(suggested)
            console.print(
                f"[cyan]Suggested tags: {', '.join(suggested)}[/cyan]"
            )

    db.add_paper(paper)
    console.print(f"[green]✓ Imported:[/green] {paper.title}")
    if paper.tags:
        console.print(f"  Tags: {', '.join(paper.tags)}")
    db.close()


def cmd_import_pdf(args, settings: Settings) -> None:
    """Import a paper by dragging/dropping a PDF file.

    Automatically extracts metadata (DOI → S2 lookup → title search).
    """
    pdf_path = Path(args.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        console.print(f"[red]File not found: {pdf_path}[/red]")
        return
    if not pdf_path.suffix.lower() == ".pdf":
        console.print(f"[red]Not a PDF file: {pdf_path}[/red]")
        return

    console.print(f"[dim]Scanning {pdf_path.name}...[/dim]")

    db = Database(settings.db_path)
    pdf_store = PDFStore(settings.pdf_dir)
    extractor = PDFMetadataExtractor(s2_api_key=settings.s2_api_key)

    result = extractor.extract(pdf_path)

    if result.paper is None:
        console.print("[red]Could not identify the paper.[/red]")
        db.close()
        return

    paper = result.paper

    # Dedup: same check as the GUI import — never double-add or overwrite
    dup = None
    if paper.doi:
        dup = db.get_paper_by_doi(paper.doi)
    if not dup:
        dup = db.get_paper_by_title(paper.title)
    if dup:
        console.print(f"[yellow]⏭ Already in library:[/yellow] {dup.title}")
        db.close()
        return

    # Store the PDF file
    stored_path = pdf_store.import_file(pdf_path, paper_id=paper.id)
    paper.pdf_path = str(stored_path)

    # Display identification result
    method_labels = {
        "xmp_doi": "XMP metadata DOI",
        "text_doi": "PDF text DOI",
        "arxiv_id": "ArXiv ID",
        "xmp_title": "XMP title search",
        "heuristic_title": "Title heuristic search",
        "fallback_text": "Text fallback",
    }
    method_name = method_labels.get(result.method, result.method)
    confidence_pct = f"{result.confidence * 100:.0f}%"

    console.print(
        f"[green]✓ Identified[/green] via {method_name} "
        f"(confidence: {confidence_pct})"
    )
    console.print(f"  [bold]{paper.title}[/bold]")
    if paper.authors:
        console.print(f"  Authors: {paper.display_authors}")
    if paper.year:
        console.print(f"  Year: {paper.year}")
    if paper.doi:
        console.print(f"  DOI: {paper.doi}")

    # Auto-classify if requested
    if args.auto_tag:
        llm = LLMProvider(
            model=settings.lit_model,
            api_key=settings.lit_api_key,
            api_base=settings.lit_api_base,
        )
        classifier = ClassifyAgent(llm)
        suggested = classifier.run(paper)
        if suggested:
            paper.tags.extend(suggested)
            console.print(
                f"  [cyan]Auto tags: {', '.join(suggested)}[/cyan]"
            )

    db.add_paper(paper)
    console.print(f"[green]✓ Added to library[/green] (id: {paper.id})")
    db.close()


def cmd_list(args, settings: Settings) -> None:
    """List all papers in the local library."""
    db = Database(settings.db_path)
    papers = db.list_papers()
    db.close()

    if not papers:
        console.print("[yellow]Library is empty.[/yellow]")
        return

    table = Table(title="Library", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("Authors", max_width=25)
    table.add_column("Year", justify="right", width=6)
    table.add_column("Tags", style="cyan")

    for i, paper in enumerate(papers, 1):
        table.add_row(
            str(i),
            paper.title,
            paper.display_authors,
            str(paper.year or ""),
            ", ".join(paper.tags) if paper.tags else "-",
        )
    console.print(table)


def cmd_tags(args, settings: Settings) -> None:
    """List or manage tags."""
    db = Database(settings.db_path)

    if args.action == "list":
        tags = db.list_tags()
        if not tags:
            console.print("[yellow]No tags yet.[/yellow]")
        for tag in tags:
            console.print(f"  #{tag.name}")
    elif args.action == "add":
        tag = db.create_tag(args.name, color=args.color)
        console.print(f"[green]Created tag:[/green] #{tag.name}")
    elif args.action == "delete":
        db.delete_tag(args.tag_id)
        console.print("[green]Tag deleted.[/green]")

    db.close()


def cmd_tag(args, settings: Settings) -> None:
    """Add/remove a tag on a paper."""
    db = Database(settings.db_path)

    if args.action == "add":
        db.add_tag_to_paper(args.paper_id, args.tag)
        console.print(f"[green]Added #{args.tag} to paper {args.paper_id}[/green]")
    elif args.action == "remove":
        db.remove_tag_from_paper(args.paper_id, args.tag)
        console.print(f"[green]Removed #{args.tag} from paper {args.paper_id}[/green]")

    db.close()


def cmd_export(args, settings: Settings) -> None:
    """Export the library to a BibTeX (.bib) file."""
    from agent_lit.storage.bibtex_export import generate_bibtex

    db = Database(settings.db_path)
    papers = db.list_papers()
    db.close()

    if not papers:
        console.print("[yellow]Library is empty — nothing to export.[/yellow]")
        return

    out = Path(args.output).expanduser()
    out.write_text(generate_bibtex(papers), encoding="utf-8")
    console.print(
        f"[green]✓ Exported {len(papers)} papers[/green] → {out}"
    )


def cmd_tui(args, settings: Settings) -> None:
    """Launch the interactive TUI application."""
    from agent_lit.tui.app import AgentLitApp

    app = AgentLitApp(settings=settings)
    app.run()


def cmd_gui(args, settings: Settings) -> None:
    """Launch the desktop GUI application."""
    from agent_lit.web.app import launch_gui

    launch_gui(settings)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-lit",
        description="Agent-based literature management",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.set_defaults(func=None)

    sub = parser.add_subparsers(title="commands")

    # search
    p_search = sub.add_parser("search", help="Search papers online")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    # import
    p_import = sub.add_parser("import", help="Import a paper to library")
    p_import.add_argument("--doi", help="Import by DOI")
    p_import.add_argument("query", nargs="?", help="Search query to import")
    p_import.add_argument(
        "--auto-tag", action="store_true", help="Auto-classify with LLM"
    )
    p_import.set_defaults(func=cmd_import)

    # import-pdf
    p_import_pdf = sub.add_parser(
        "import-pdf", help="Import a PDF (auto-identify metadata)"
    )
    p_import_pdf.add_argument(
        "pdf_path", help="Path to PDF file (drag & drop supported)"
    )
    p_import_pdf.add_argument(
        "--auto-tag", action="store_true", help="Auto-classify with LLM"
    )
    p_import_pdf.set_defaults(func=cmd_import_pdf)

    # list
    p_list = sub.add_parser("list", help="List papers in library")
    p_list.set_defaults(func=cmd_list)

    # export
    p_export = sub.add_parser(
        "export", help="Export library to a BibTeX (.bib) file"
    )
    p_export.add_argument(
        "-o", "--output", default="library.bib", help="Output .bib file path"
    )
    p_export.set_defaults(func=cmd_export)

    # tags
    p_tags = sub.add_parser("tags", help="Manage tags")
    p_tags.add_argument("action", choices=["list", "add", "delete"])
    p_tags.add_argument("--name", help="Tag name (for add)")
    p_tags.add_argument("--tag-id", help="Tag ID (for delete)")
    p_tags.add_argument("--color", help="Tag color")
    p_tags.set_defaults(func=cmd_tags)

    # tag (per-paper)
    p_tag = sub.add_parser("tag", help="Tag/untag a paper")
    p_tag.add_argument("action", choices=["add", "remove"])
    p_tag.add_argument("--paper-id", required=True)
    p_tag.add_argument("--tag", required=True)
    p_tag.set_defaults(func=cmd_tag)

    # tui
    p_tui = sub.add_parser("tui", help="Launch interactive TUI")
    p_tui.set_defaults(func=cmd_tui)

    # gui (default)
    p_gui = sub.add_parser("gui", help="Launch desktop GUI")
    p_gui.set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)
    if args.func is None:
        # No subcommand → launch desktop GUI directly
        settings = Settings.load()
        cmd_gui(args, settings)
        return

    settings = Settings.load()
    args.func(args, settings)


if __name__ == "__main__":
    main()
