"""Command line interface for snipvault."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from snipvault.clipboard import CLIPBOARD_FALLBACK_NOTE, copy_to_clipboard
from snipvault.common.cli import (
    ToolContext,
    add_config_commands,
    create_app,
    exit_with,
    get_ctx,
    run_app,
)
from snipvault.common.exceptions import DataError, ExitCode, UsageError
from snipvault.common.paths import require_file, resolve_user_path
from snipvault.common.safety import require_confirmation
from snipvault.common.terminal import is_interactive
from snipvault.config import SCHEMA, load_config
from snipvault.db import backup_database, restore_database, validate_database_file
from snipvault.editor import edit_in_external_editor
from snipvault.importexport import (
    VALID_DUPLICATE_POLICIES,
    VALID_EXPORT_FORMATS,
    export_snippets_markdown,
    export_snippets_payload,
    format_export_json,
    import_snippets_file,
    write_export_file,
)
from snipvault.interactive import run_interactive_browser
from snipvault.render import (
    render_backup_result,
    render_import_summary,
    render_list_result,
    render_restore_result,
    render_search_result,
    render_snippet_added,
    render_snippet_detail,
)
from snipvault.repository import SnippetRepository

TOOL = "snipvault"


def _make_repo(tctx: ToolContext) -> tuple[SnippetRepository, dict[str, object]]:
    cfg = load_config(tctx.paths)
    repo = SnippetRepository(
        tctx.paths,
        max_snippet_bytes=int(cfg["max_snippet_bytes"]),
        default_language=str(cfg["default_language"]),
        default_limit=int(cfg["default_limit"]),
    )
    return repo, cfg


def _default_handler(ctx: typer.Context) -> None:
    """Launch interactive browser when invoked on a TTY; otherwise print help."""
    tctx = get_ctx(ctx)
    if is_interactive() and not tctx.options.json:
        repo, _ = _make_repo(tctx)
        run_interactive_browser(tctx.out, repo)
    else:
        sys.stdout.write(ctx.get_help() + "\n")


app = create_app(
    TOOL,
    "A local-first, searchable library for your code snippets.",
    default=_default_handler,
)
add_config_commands(app, TOOL, SCHEMA)


def _read_snippet_file(path_arg: Path, max_bytes: int) -> tuple[Path, str]:
    resolved = require_file(resolve_user_path(path_arg), what="Snippet file")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise UsageError(
            f"Snippet file size ({size} bytes) exceeds maximum allowed ({max_bytes} bytes).",
            hint=(
                "Increase 'max_snippet_bytes' with "
                "'snipvault config set max_snippet_bytes <bytes>'."
            ),
        )
    try:
        return resolved, resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DataError(
            f"Snippet file {resolved} is not valid UTF-8 text.",
            hint="Convert the file to UTF-8 before adding it to SnipVault.",
        ) from exc
    except OSError as exc:
        raise DataError(f"Could not read snippet file {resolved}: {exc}") from exc


def _prompt_interactive_line(prompt_label: str, default: str = "") -> str:
    sys.stderr.write(prompt_label)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        return default
    val = line.rstrip("\r\n")
    return val if val else default


def _prompt_interactive_content() -> str:
    sys.stderr.write("Content (enter lines; finish with 'EOF' on its own line or Ctrl-D):\n")
    sys.stderr.flush()
    lines: list[str] = []
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        if line.rstrip("\r\n") == "EOF":
            break
        lines.append(line)
    return "".join(lines)


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    title: Annotated[
        str | None, typer.Option("--title", help="Snippet title (max 200 characters).")
    ] = None,
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Programming language.")
    ] = None,
    tags: Annotated[
        str | None, typer.Option("--tags", "-t", help="Comma-separated list of tags.")
    ] = None,
    category: Annotated[str | None, typer.Option("--category", "-c", help="Category name.")] = None,
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Optional description.")
    ] = None,
    favorite: Annotated[
        bool, typer.Option("--favorite", "--fav", help="Mark snippet as favorite.")
    ] = False,
    content: Annotated[str | None, typer.Option("--content", help="Snippet content text.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read snippet content from file.")
    ] = None,
) -> None:
    """Add a new snippet from --content, --file, piped stdin, or interactive prompt."""
    tctx = get_ctx(ctx)
    repo, cfg = _make_repo(tctx)

    if content is not None and file is not None:
        raise UsageError(
            "Cannot specify both --content and --file.",
            hint="Use either --content TEXT or --file PATH.",
        )

    resolved_file: Path | None = None
    body: str | None = content

    if file is not None:
        resolved_file, body = _read_snippet_file(file, repo.max_snippet_bytes)
        if title is None:
            title = resolved_file.name
    elif body is None:
        if is_interactive():
            if title is None:
                title = _prompt_interactive_line("Title: ")
            if language is None:
                language = _prompt_interactive_line(
                    f"Language [{cfg['default_language']}]: ",
                    default=str(cfg["default_language"]),
                )
            if tags is None:
                tags = _prompt_interactive_line("Tags (comma-separated, optional): ")
            if description is None:
                description = _prompt_interactive_line("Description (optional): ")
            body = _prompt_interactive_content()
        else:
            piped = sys.stdin.read()
            if not piped or not piped.strip():
                raise UsageError(
                    "No snippet content provided in non-interactive mode.",
                    hint=(
                        "Pass --content TEXT, --file PATH, pipe content on stdin, "
                        "or run in a terminal."
                    ),
                )
            body = piped

    if title is None:
        raise UsageError(
            "--title is required when adding a snippet non-interactively.",
            hint="Pass --title 'Snippet Title'.",
        )

    snippet = repo.add_snippet(
        title=title,
        content=body,
        language=language,
        tags=tags,
        category=category,
        description=description,
        favorite=favorite,
        file_path=resolved_file,
    )
    tctx.out.emit(snippet, lambda o: render_snippet_added(o, snippet))


@app.command("search")
def search_cmd(
    ctx: typer.Context,
    query: Annotated[
        str | None, typer.Argument(help="Full-text search query (optional when filtering).")
    ] = None,
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Filter by language.")
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Filter by tag (repeatable).")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Filter by category.")
    ] = None,
    favorites: Annotated[
        bool, typer.Option("--favorites", "--fav", help="Only match favorite snippets.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Maximum results to return.")
    ] = None,
) -> None:
    """Search snippets across title, description, content, tags, language, and category."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    result = repo.search_snippets(
        query,
        language=language,
        tags=tag,
        category=category,
        favorites_only=favorites,
        limit=limit,
    )
    tctx.out.emit(result.to_payload(), lambda o: render_search_result(o, result))


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Filter by language.")
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Filter by tag (repeatable).")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Filter by category.")
    ] = None,
    favorites: Annotated[
        bool, typer.Option("--favorites", "--fav", help="Only list favorite snippets.")
    ] = False,
    sort: Annotated[
        str,
        typer.Option("--sort", "-s", help="Sort order: modified, created, or title."),
    ] = "modified",
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Maximum snippets to return.")
    ] = None,
    offset: Annotated[
        int, typer.Option("--offset", help="Number of snippets to skip for pagination.")
    ] = 0,
) -> None:
    """List snippets with optional filters, sorting, and pagination."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    result = repo.list_snippets(
        language=language,
        tags=tag,
        category=category,
        favorites_only=favorites,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    tctx.out.emit(result.to_payload(), lambda o: render_list_result(o, result))


@app.command("show")
def show_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID.")],
    raw: Annotated[bool, typer.Option("--raw", help="Print only the raw snippet content.")] = False,
) -> None:
    """Show full details and syntax-highlighted content for a snippet."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    snippet = repo.get_snippet(snippet_id)
    if raw and not tctx.options.json:
        sys.stdout.write(snippet.content)
        if not snippet.content.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    tctx.out.emit(snippet, lambda o: render_snippet_detail(o, snippet))


@app.command("edit")
def edit_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID to edit.")],
    title: Annotated[str | None, typer.Option("--title", help="New snippet title.")] = None,
    content: Annotated[str | None, typer.Option("--content", help="New snippet content.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read new content from file.")
    ] = None,
    language: Annotated[str | None, typer.Option("--language", "-l", help="New language.")] = None,
    tags: Annotated[
        str | None, typer.Option("--tags", help="Replace all tags (comma-separated).")
    ] = None,
    add_tag: Annotated[
        list[str] | None, typer.Option("--add-tag", help="Add tag(s) to existing tags.")
    ] = None,
    remove_tag: Annotated[
        list[str] | None, typer.Option("--remove-tag", help="Remove tag(s) from snippet.")
    ] = None,
    category: Annotated[str | None, typer.Option("--category", "-c", help="New category.")] = None,
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="New description.")
    ] = None,
    favorite: Annotated[
        bool | None,
        typer.Option("--favorite/--no-favorite", help="Set or clear favorite status."),
    ] = None,
) -> None:
    """Edit a snippet via flags or interactively in $VISUAL / $EDITOR."""
    tctx = get_ctx(ctx)
    repo, cfg = _make_repo(tctx)

    if content is not None and file is not None:
        raise UsageError(
            "Cannot specify both --content and --file.",
            hint="Use either --content TEXT or --file PATH.",
        )

    existing = repo.get_snippet(snippet_id)

    has_flags = (
        any(
            x is not None
            for x in (title, content, file, language, tags, category, description, favorite)
        )
        or bool(add_tag)
        or bool(remove_tag)
    )

    new_content = content
    if file is not None:
        _, new_content = _read_snippet_file(file, repo.max_snippet_bytes)
    elif not has_flags:
        if not is_interactive():
            raise UsageError(
                "No edit options provided in non-interactive mode.",
                hint=(
                    "Pass flags such as --title or --content, or run in a terminal to open $EDITOR."
                ),
            )
        new_content = edit_in_external_editor(
            existing.content,
            language=existing.language,
            configured_editor=str(cfg.get("editor", "")),
        )

    updated, changed = repo.update_snippet(
        snippet_id,
        title=title,
        content=new_content,
        language=language,
        tags=tags,
        add_tags=add_tag,
        remove_tags=remove_tag,
        category=category,
        description=description,
        favorite=favorite,
    )

    def _render(_out: object) -> None:
        if changed:
            tctx.out.success(f"Updated snippet #{updated.id}: {updated.title}")
        else:
            tctx.out.info(f"No changes made to snippet #{updated.id}: {updated.title}")
        render_snippet_detail(tctx.out, updated)

    from snipvault.common.output import to_jsonable

    payload = {**to_jsonable(updated), "changed": changed}
    tctx.out.emit(payload, _render)


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID to delete.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without interactive confirmation.")
    ] = False,
) -> None:
    """Delete a snippet by ID (requires confirmation or --yes)."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    existing = repo.get_snippet(snippet_id)
    require_confirmation(
        tctx.out,
        f"Delete snippet #{existing.id} ({existing.title!r})?",
        assume_yes=yes,
    )
    deleted = repo.delete_snippet(snippet_id)
    tctx.out.emit(
        {"deleted": True, "id": deleted.id, "title": deleted.title},
        lambda o: o.success(f"Deleted snippet #{deleted.id}: {deleted.title}"),
    )


@app.command("favorite")
def favorite_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID to mark as favorite.")],
) -> None:
    """Mark a snippet as a favorite."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    updated, changed = repo.set_favorite(snippet_id, True)
    from snipvault.common.output import to_jsonable

    tctx.out.emit(
        {**to_jsonable(updated), "changed": changed},
        lambda o: o.success(f"Marked snippet #{updated.id} ({updated.title!r}) as favorite."),
    )


@app.command("unfavorite")
def unfavorite_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID to remove from favorites.")],
) -> None:
    """Remove a snippet from favorites."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    updated, changed = repo.set_favorite(snippet_id, False)
    from snipvault.common.output import to_jsonable

    tctx.out.emit(
        {**to_jsonable(updated), "changed": changed},
        lambda o: o.success(f"Removed snippet #{updated.id} ({updated.title!r}) from favorites."),
    )


@app.command("copy")
def copy_cmd(
    ctx: typer.Context,
    snippet_id: Annotated[int, typer.Argument(help="Snippet ID to copy.")],
    print_only: Annotated[
        bool, typer.Option("--print", "-p", help="Print content to stdout instead of copying.")
    ] = False,
) -> None:
    """Copy a snippet's content to the clipboard (or print if unavailable)."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    snippet = repo.get_snippet(snippet_id)

    if print_only:
        if tctx.options.json:
            tctx.out.emit_json(
                {
                    "id": snippet.id,
                    "title": snippet.title,
                    "copied": False,
                    "printed": True,
                    "backend": None,
                    "content": snippet.content,
                }
            )
        else:
            sys.stdout.write(snippet.content)
            if not snippet.content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        return

    outcome = copy_to_clipboard(snippet.content)
    if outcome.copied:
        msg = (
            f"Copied snippet #{snippet.id} ({snippet.title!r}) to clipboard via {outcome.backend}."
        )
        tctx.out.emit(
            {
                "id": snippet.id,
                "title": snippet.title,
                "copied": True,
                "printed": False,
                "backend": outcome.backend,
            },
            lambda o: o.success(msg),
        )
        return

    # Documented fallback when clipboard is unavailable
    note = outcome.note or CLIPBOARD_FALLBACK_NOTE
    tctx.out.status("warn", note, to_stderr=True)
    if tctx.options.json:
        tctx.out.emit_json(
            {
                "id": snippet.id,
                "title": snippet.title,
                "copied": False,
                "printed": True,
                "backend": None,
                "content": snippet.content,
                "warning": note,
            }
        )
    else:
        sys.stdout.write(snippet.content)
        if not snippet.content.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    exit_with(ExitCode.WARNING)


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="Export format: json or markdown.")
    ] = "json",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write export to FILE instead of stdout.")
    ] = None,
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Filter by language.")
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Filter by tag (repeatable).")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Filter by category.")
    ] = None,
    favorites: Annotated[
        bool, typer.Option("--favorites", "--fav", help="Only export favorite snippets.")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing output file.")] = False,
) -> None:
    """Export snippets to JSON or Markdown."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)

    norm_fmt = fmt.strip().lower()
    if norm_fmt == "md":
        norm_fmt = "markdown"
    if norm_fmt not in VALID_EXPORT_FORMATS:
        raise UsageError(
            f"Invalid export format: {fmt!r}",
            hint=f"Allowed formats: {', '.join(VALID_EXPORT_FORMATS)}.",
        )

    # Fetch all matching snippets
    list_res = repo.list_snippets(
        language=language,
        tags=tag,
        category=category,
        favorites_only=favorites,
        sort="modified",
        limit=100_000,
        offset=0,
    )
    snippets = list_res.snippets

    if norm_fmt == "json":
        rendered = format_export_json(snippets)
    else:
        rendered = export_snippets_markdown(snippets)

    if output is not None:
        out_path = resolve_user_path(output)
        write_export_file(out_path, rendered, force=force)
        tctx.out.emit(
            {
                "output": str(out_path),
                "format": norm_fmt,
                "count": len(snippets),
            },
            lambda o: o.success(f"Exported {len(snippets)} snippet(s) ({norm_fmt}) to {out_path}"),
        )
        return

    if norm_fmt == "json":
        sys.stdout.write(rendered)
        sys.stdout.flush()
    elif tctx.options.json:
        payload = export_snippets_payload(snippets)
        payload["format"] = "markdown"
        payload["markdown"] = rendered
        tctx.out.emit_json(payload)
    else:
        sys.stdout.write(rendered)
        sys.stdout.flush()


@app.command("import")
def import_cmd(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="JSON snippet export file to import.")],
    on_duplicate: Annotated[
        str,
        typer.Option("--on-duplicate", help="Duplicate handling: skip, rename, or replace."),
    ] = "skip",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm overwriting duplicates when using replace."),
    ] = False,
) -> None:
    """Import snippets from a JSON file (all-or-nothing validation and transaction)."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)

    policy = on_duplicate.strip().lower()
    if policy not in VALID_DUPLICATE_POLICIES:
        raise UsageError(
            f"Invalid --on-duplicate policy: {on_duplicate!r}",
            hint=f"Allowed values: {', '.join(VALID_DUPLICATE_POLICIES)}.",
        )

    resolved_path = resolve_user_path(file)
    if policy == "replace":
        require_confirmation(
            tctx.out,
            (
                f"Import from {resolved_path} with --on-duplicate=replace "
                "will overwrite matching snippets. Continue?"
            ),
            assume_yes=yes,
        )

    summary = import_snippets_file(
        tctx.paths,
        resolved_path,
        on_duplicate=policy,
        max_snippet_bytes=repo.max_snippet_bytes,
        default_language=repo.default_language,
    )
    tctx.out.emit(summary, lambda o: render_import_summary(o, summary))


@app.command("backup")
def backup_cmd(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Destination backup file (defaults to data_dir/backups/)."
        ),
    ] = None,
) -> None:
    """Create a consistent online backup of the SnipVault SQLite database."""
    tctx = get_ctx(ctx)
    out_path = resolve_user_path(output) if output is not None else None
    result = backup_database(tctx.paths, out_path)
    tctx.out.emit(result, lambda o: render_backup_result(o, result))


@app.command("restore")
def restore_cmd(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Backup SQLite database file to restore from.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Restore without interactive confirmation."),
    ] = False,
) -> None:
    """Restore the SnipVault database from a backup file (takes a safety backup first)."""
    tctx = get_ctx(ctx)
    src_path = resolve_user_path(file)
    # Validate the backup file first before prompting for confirmation
    validate_database_file(src_path)
    require_confirmation(
        tctx.out,
        f"Restore SnipVault database from {src_path}?",
        assume_yes=yes,
    )
    result = restore_database(tctx.paths, src_path)
    tctx.out.emit(result, lambda o: render_restore_result(o, result))


@app.command("interactive")
def interactive_cmd(ctx: typer.Context) -> None:
    """Launch the interactive numbered snippet browser."""
    tctx = get_ctx(ctx)
    repo, _ = _make_repo(tctx)
    run_interactive_browser(tctx.out, repo)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    return run_app(app, tool=TOOL, argv=argv)
