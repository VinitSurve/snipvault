"""Human-friendly Rich rendering for snipvault commands."""

from __future__ import annotations

from rich.syntax import Syntax
from rich.text import Text

from snipvault.common.fsutil import human_size
from snipvault.common.output import Output
from snipvault.models import (
    BackupResult,
    ImportSummary,
    ListResult,
    RestoreResult,
    SearchResult,
    Snippet,
)
from snipvault.search import extract_query_terms


def highlight_text(content: str, query: str | None) -> Text:
    """Return a Rich :class:`Text` with search query tokens highlighted."""
    txt = Text(content)
    terms = extract_query_terms(query)
    if terms:
        txt.highlight_words(terms, style="bold yellow", case_sensitive=False)
    return txt


def render_snippet_added(out: Output, snippet: Snippet) -> None:
    """Render confirmation after ``snipvault add``."""
    out.success(f"Added snippet #{snippet.id}: {snippet.title}")
    if out.quiet:
        out.text(str(snippet.id))
        return
    out.kv(
        [
            ("ID", str(snippet.id)),
            ("Title", snippet.title),
            ("Language", snippet.language),
            ("Category", snippet.category or "(none)"),
            ("Tags", ", ".join(snippet.tags) or "(none)"),
            ("Favorite", "yes" if snippet.favorite else "no"),
        ]
    )
    out.next_steps([f"snipvault show {snippet.id}", f"snipvault copy {snippet.id}"])


def render_snippet_detail(out: Output, snippet: Snippet) -> None:
    """Render full metadata and syntax-highlighted content for ``snipvault show``."""
    if out.quiet:
        out.text(snippet.content)
        return
    out.heading(f"Snippet #{snippet.id}: {snippet.title}")
    rows: list[tuple[str, str]] = [
        ("ID", str(snippet.id)),
        ("Title", snippet.title),
        ("Language", snippet.language),
        ("Category", snippet.category or "(none)"),
        ("Tags", ", ".join(snippet.tags) or "(none)"),
        ("Favorite", "★ yes" if snippet.favorite else "no"),
    ]
    if snippet.description:
        rows.append(("Description", snippet.description))
    rows.extend(
        [
            ("Created", snippet.created_at),
            ("Modified", snippet.modified_at),
            ("SHA-256", snippet.content_hash[:16]),
        ]
    )
    out.kv(rows)
    use_syntax = (
        not out.console.no_color
        and out.console.is_terminal
        and bool(snippet.language)
        and snippet.language != "text"
    )
    if use_syntax:
        syntax = Syntax(snippet.content, snippet.language, word_wrap=True)
        out.panel(syntax, title=f"{snippet.title} ({snippet.language})")
    else:
        out.panel(snippet.content, title=f"{snippet.title} ({snippet.language})")


def render_list_result(out: Output, result: ListResult) -> None:
    """Render a paginated table of snippets for ``snipvault list``."""
    if not result.snippets:
        out.info("No snippets found.")
        out.next_steps(["snipvault add --title 'Example' --content 'echo hello'"])
        return

    rows = [
        [
            str(s.id),
            "★" if s.favorite else "",
            s.title,
            s.language,
            s.category or "-",
            ", ".join(s.tags) or "-",
            s.modified_at[:19].replace("T", " "),
        ]
        for s in result.snippets
    ]
    caption = (
        f"Showing {result.offset + 1}-{result.offset + len(result.snippets)} "
        f"of {result.total} snippet(s) (sorted by {result.sort})"
    )
    out.table(
        ["ID", "Fav", "Title", "Language", "Category", "Tags", "Modified"],
        rows,
        title="Snippets",
        caption=caption,
        right_align=("ID",),
    )


def render_search_result(out: Output, result: SearchResult) -> None:
    """Render ranked search results with highlighted matches."""
    if result.note:
        out.warn(result.note)
    if not result.hits:
        msg = (
            f"No snippets matched query {result.query!r}."
            if result.query
            else "No snippets matched the specified filters."
        )
        out.info(msg)
        return

    rows = []
    for hit in result.hits:
        s = hit.snippet
        rows.append(
            [
                str(s.id),
                "★" if s.favorite else "",
                highlight_text(s.title, result.query),
                s.language,
                highlight_text(", ".join(s.tags) or "-", result.query),
                highlight_text(hit.matched_excerpt, result.query),
            ]
        )
    title = f"Search results for {result.query!r}" if result.query else "Filtered snippets"
    out.table(
        ["ID", "Fav", "Title", "Language", "Tags", "Preview"],
        rows,
        title=title,
        caption=f"{result.total} match(es)",
        right_align=("ID",),
    )


def render_import_summary(out: Output, summary: ImportSummary) -> None:
    """Render import counts."""
    out.success(
        f"Import complete ({summary.imported} imported, "
        f"{summary.skipped} skipped, {summary.failed} failed)."
    )
    out.summary(
        "Import Summary",
        [
            ("File", summary.file),
            ("Duplicate policy", summary.on_duplicate),
            ("Imported", str(summary.imported)),
            ("Skipped", str(summary.skipped)),
            ("Replaced", str(summary.replaced)),
            ("Renamed", str(summary.renamed)),
            ("Failed", str(summary.failed)),
        ],
    )


def render_backup_result(out: Output, result: BackupResult) -> None:
    """Render database backup summary."""
    out.success(f"Backup written to {result.output}")
    out.kv(
        [
            ("Output", result.output),
            ("Snippets", str(result.snippet_count)),
            ("Size", human_size(result.size_bytes)),
            ("Schema", f"v{result.schema_version}"),
        ]
    )


def render_restore_result(out: Output, result: RestoreResult) -> None:
    """Render database restore summary."""
    out.success(f"Restored {result.snippet_count} snippet(s) from {result.restored_from}")
    rows = [
        ("Restored from", result.restored_from),
        ("Database", result.target),
        ("Snippets", str(result.snippet_count)),
        ("Schema", f"v{result.schema_version}"),
    ]
    if result.safety_backup:
        rows.append(("Safety backup", result.safety_backup))
    out.kv(rows)
