"""Interactive numbered snippet browser that works cleanly on a TTY and with piped stdin."""

from __future__ import annotations

import sys

from snipvault.clipboard import copy_to_clipboard
from snipvault.common.output import Output
from snipvault.models import Snippet
from snipvault.render import render_snippet_detail
from snipvault.repository import SnippetRepository

PAGE_SIZE = 10


def _read_line(prompt: str, out: Output) -> str | None:
    """Print ``prompt`` and read one line from ``sys.stdin``; return ``None`` on EOF."""
    out.text(prompt)
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    if line == "":
        return None
    return line.rstrip("\r\n")


def _render_browser_page(
    out: Output,
    snippets: list[Snippet],
    *,
    total: int,
    page: int,
    page_size: int,
    query: str,
    language_filter: str | None,
) -> None:
    total_pages = max(1, (total + page_size - 1) // page_size)
    filters_desc: list[str] = []
    if query:
        filters_desc.append(f"search={query!r}")
    if language_filter:
        filters_desc.append(f"language={language_filter!r}")
    filter_suffix = f" ({', '.join(filters_desc)})" if filters_desc else ""
    out.heading(f"SnipVault Interactive Browser — Page {page + 1}/{total_pages}{filter_suffix}")

    if not snippets:
        out.info("No snippets to display.")
    else:
        rows = [
            [
                str(idx),
                f"#{snip.id}",
                "★" if snip.favorite else "",
                snip.title,
                snip.language,
                ", ".join(snip.tags) or "-",
            ]
            for idx, snip in enumerate(snippets, start=1)
        ]
        out.table(
            ["#", "ID", "Fav", "Title", "Language", "Tags"],
            rows,
            caption=f"Total matching snippets: {total}",
            right_align=("#", "ID"),
        )
    out.text(
        "Commands: [1-N] select  [/text] search  [f] filter language  "
        "[c] clear filters  [n/p] next/prev page  [q] quit"
    )


def _handle_snippet_menu(out: Output, repo: SnippetRepository, snippet: Snippet) -> bool:
    """Show snippet detail and handle item actions; returns False if user chose to quit."""
    current = snippet
    render_snippet_detail(out, current)
    out.text(
        "Snippet action: [Enter/b] back  [c] copy  [f] toggle favorite  "
        "[e] edit title  [d] delete  [q] quit"
    )
    action_raw = _read_line("Action> ", out)
    if action_raw is None:
        return False
    action = action_raw.strip().lower()
    if action in ("", "b", "back", "v", "view"):
        return True
    if action in ("q", "quit", "exit"):
        out.info("Exiting interactive browser.")
        return False
    if action in ("c", "copy"):
        outcome = copy_to_clipboard(current.content)
        if outcome.copied:
            out.success(
                f"Copied snippet #{current.id} ({current.title!r}) "
                f"to clipboard via {outcome.backend}."
            )
        else:
            out.warn(outcome.note or "clipboard unavailable; content printed instead")
            out.text(current.content)
        return True
    if action in ("f", "fav", "favorite"):
        updated, _ = repo.set_favorite(current.id, not current.favorite)
        state = "favorite" if updated.favorite else "unfavorited"
        out.success(f"Snippet #{updated.id} ({updated.title!r}) is now {state}.")
        return True
    if action in ("e", "edit"):
        new_title = _read_line("New title (blank to cancel)> ", out)
        if new_title is None:
            return False
        if new_title.strip():
            updated, _ = repo.update_snippet(current.id, title=new_title.strip())
            out.success(f"Updated snippet #{updated.id} title to {updated.title!r}.")
        else:
            out.info("Edit cancelled.")
        return True
    if action in ("d", "delete"):
        confirm_ans = _read_line(f"Delete snippet #{current.id} ({current.title!r})? [y/N]> ", out)
        if confirm_ans is None:
            return False
        if confirm_ans.strip().lower() in ("y", "yes"):
            repo.delete_snippet(current.id)
            out.success(f"Deleted snippet #{current.id}: {current.title}")
        else:
            out.info("Delete cancelled.")
        return True

    out.warn(f"Unknown snippet action: {action_raw!r}")
    return True


def run_interactive_browser(out: Output, repo: SnippetRepository) -> int:
    """Run the interactive numbered browser loop until quit or EOF."""
    page = 0
    query = ""
    language_filter: str | None = None

    while True:
        if query:
            search_res = repo.search_snippets(
                query, language=language_filter, limit=PAGE_SIZE * (page + 1)
            )
            total = search_res.total
            all_hits = [h.snippet for h in search_res.hits]
            start = page * PAGE_SIZE
            page_snippets = all_hits[start : start + PAGE_SIZE]
        else:
            list_res = repo.list_snippets(
                language=language_filter,
                limit=PAGE_SIZE,
                offset=page * PAGE_SIZE,
            )
            total = list_res.total
            page_snippets = list_res.snippets

        _render_browser_page(
            out,
            page_snippets,
            total=total,
            page=page,
            page_size=PAGE_SIZE,
            query=query,
            language_filter=language_filter,
        )

        raw_cmd = _read_line("snipvault> ", out)
        if raw_cmd is None:
            break
        cmd = raw_cmd.strip()
        if not cmd:
            continue
        low = cmd.lower()
        if low in ("q", "quit", "exit"):
            out.info("Exiting interactive browser.")
            break
        if low in ("n", "next"):
            if (page + 1) * PAGE_SIZE < total:
                page += 1
            else:
                out.info("Already on the last page.")
            continue
        if low in ("p", "prev", "previous"):
            if page > 0:
                page -= 1
            else:
                out.info("Already on the first page.")
            continue
        if cmd.startswith("/"):
            query = cmd[1:].strip()
            page = 0
            out.info(f"Search filter set to {query!r}" if query else "Search filter cleared.")
            continue
        if low == "f" or low.startswith("f "):
            if low.startswith("f "):
                lang_val = cmd[2:].strip()
            else:
                ans = _read_line("Language filter (blank to clear)> ", out)
                if ans is None:
                    break
                lang_val = ans.strip()
            language_filter = lang_val or None
            page = 0
            out.info(
                f"Language filter set to {language_filter!r}"
                if language_filter
                else "Language filter cleared."
            )
            continue
        if low in ("c", "clear", "reset"):
            query = ""
            language_filter = None
            page = 0
            out.info("Cleared all filters.")
            continue
        if cmd.isdigit():
            num = int(cmd)
            selected: Snippet | None = None
            if 1 <= num <= len(page_snippets):
                selected = page_snippets[num - 1]
            else:
                for s in page_snippets:
                    if s.id == num:
                        selected = s
                        break
            if selected is None:
                out.warn(f"Invalid selection: {num}. Choose between 1 and {len(page_snippets)}.")
                continue
            keep_going = _handle_snippet_menu(out, repo, selected)
            if not keep_going:
                break
            continue

        out.warn(f"Unrecognized command: {cmd!r}. Enter 1-N, /query, f, c, n, p, or q.")

    return 0
