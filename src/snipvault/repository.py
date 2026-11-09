"""CRUD and search repository for snipvault."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snipvault.common.exceptions import DataError, NotFoundError, UsageError
from snipvault.common.paths import ToolPaths
from snipvault.config import DEFAULT_LANGUAGE, DEFAULT_LIMIT, DEFAULT_MAX_SNIPPET_BYTES
from snipvault.db import _FORCE_DISABLE_FTS5, RESTORE_HINT, check_fts5_available, open_db
from snipvault.models import (
    ListResult,
    SearchHit,
    SearchResult,
    Snippet,
    compute_content_hash,
    normalize_category,
    normalize_description,
    normalize_language,
    normalize_tags,
    normalize_title,
    utc_now_iso,
    validate_content,
)
from snipvault.search import (
    build_fts_query,
    build_matched_excerpt,
    escape_like_pattern,
)

VALID_SORTS = ("modified", "created", "title")
FTS_FALLBACK_NOTE = "FTS5 is unavailable in this SQLite build; using LIKE fallback search."


class SnippetRepository:
    """High-level interface to the SnipVault SQLite database."""

    def __init__(
        self,
        paths: ToolPaths,
        *,
        max_snippet_bytes: int = DEFAULT_MAX_SNIPPET_BYTES,
        default_language: str = DEFAULT_LANGUAGE,
        default_limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.paths = paths
        self.max_snippet_bytes = max_snippet_bytes
        self.default_language = default_language
        self.default_limit = default_limit

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fetch_tags_map(
        conn: sqlite3.Connection, snippet_ids: Sequence[int]
    ) -> dict[int, list[str]]:
        if not snippet_ids:
            return {}
        placeholders = ", ".join("?" for _ in snippet_ids)
        sql = (
            "SELECT st.snippet_id, t.name "  # noqa: S608
            "FROM snippet_tags st "
            "JOIN tags t ON t.id = st.tag_id "
            f"WHERE st.snippet_id IN ({placeholders}) "
            "ORDER BY st.rowid ASC"
        )
        rows = conn.execute(sql, tuple(snippet_ids)).fetchall()
        mapping: dict[int, list[str]] = {sid: [] for sid in snippet_ids}
        for row in rows:
            mapping.setdefault(int(row["snippet_id"]), []).append(str(row["name"]))
        return mapping

    @staticmethod
    def _row_to_snippet(row: sqlite3.Row, tags: list[str]) -> Snippet:
        return Snippet(
            id=int(row["id"]),
            title=str(row["title"]),
            description=str(row["description"] or ""),
            content=str(row["content"]),
            language=str(row["language"] or "text"),
            category=str(row["category"] or ""),
            tags=list(tags),
            favorite=bool(row["favorite"]),
            content_hash=str(row["content_hash"] or ""),
            created_at=str(row["created_at"]),
            modified_at=str(row["modified_at"]),
        )

    @staticmethod
    def _set_snippet_tags(conn: sqlite3.Connection, snippet_id: int, tags: Sequence[str]) -> None:
        conn.execute("DELETE FROM snippet_tags WHERE snippet_id = ?", (snippet_id,))
        for tag in tags:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()
            if tag_row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO snippet_tags(snippet_id, tag_id) VALUES (?, ?)",
                    (snippet_id, int(tag_row["id"])),
                )

    @staticmethod
    def _build_filter_clauses(
        *,
        language: str | None = None,
        tags: Sequence[str] | str | None = None,
        category: str | None = None,
        favorites_only: bool = False,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if language is not None and language.strip():
            clauses.append("LOWER(s.language) = LOWER(?)")
            params.append(language.strip())
        if category is not None and category.strip():
            clauses.append("LOWER(s.category) = LOWER(?)")
            params.append(category.strip())
        if favorites_only:
            clauses.append("s.favorite = 1")
        norm_tags = normalize_tags(tags)
        for tag in norm_tags:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM snippet_tags st
                    JOIN tags t ON t.id = st.tag_id
                    WHERE st.snippet_id = s.id AND t.name = ?
                )
                """
            )
            params.append(tag)
        return clauses, params

    # ------------------------------------------------------------------ CRUD
    def add_snippet(
        self,
        *,
        title: str,
        content: str,
        language: str | None = None,
        tags: Sequence[str] | str | None = None,
        category: str | None = None,
        description: str | None = None,
        favorite: bool = False,
        file_path: Path | None = None,
    ) -> Snippet:
        """Validate and insert a new snippet inside a transaction."""
        norm_title = normalize_title(title)
        norm_content = validate_content(content, max_bytes=self.max_snippet_bytes)
        norm_lang = normalize_language(language, default=self.default_language, file_path=file_path)
        norm_cat = normalize_category(category)
        norm_desc = normalize_description(description)
        norm_tags = normalize_tags(tags)
        content_hash = compute_content_hash(norm_content)
        now = utc_now_iso()

        conn = open_db(self.paths, create=True)
        if conn is None:  # pragma: no cover
            raise DataError("Database could not be opened.", hint=RESTORE_HINT)
        try:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO snippets (
                        title, description, content, language, category,
                        favorite, content_hash, created_at, modified_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        norm_title,
                        norm_desc,
                        norm_content,
                        norm_lang,
                        norm_cat,
                        1 if favorite else 0,
                        content_hash,
                        now,
                        now,
                    ),
                )
                snippet_id = int(cur.lastrowid or 0)
                self._set_snippet_tags(conn, snippet_id, norm_tags)
                row = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
                if row is None:  # pragma: no cover
                    raise DataError("Failed to read back inserted snippet.")
                return self._row_to_snippet(row, norm_tags)
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database write failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()

    def get_snippet(self, snippet_id: int) -> Snippet:
        """Fetch one snippet by ID or raise :class:`NotFoundError`."""
        conn = open_db(self.paths, create=False)
        if conn is None:
            raise NotFoundError(
                f"Snippet #{snippet_id} not found.",
                hint="Run 'snipvault list' to see available snippets.",
            )
        try:
            row = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
            if row is None:
                raise NotFoundError(
                    f"Snippet #{snippet_id} not found.",
                    hint="Run 'snipvault list' to see available snippets.",
                )
            tags_map = self._fetch_tags_map(conn, [snippet_id])
            return self._row_to_snippet(row, tags_map.get(snippet_id, []))
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database read failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()

    def list_snippets(
        self,
        *,
        language: str | None = None,
        tags: Sequence[str] | str | None = None,
        category: str | None = None,
        favorites_only: bool = False,
        sort: str = "modified",
        limit: int | None = None,
        offset: int = 0,
    ) -> ListResult:
        """Return filtered, sorted, paginated snippets without creating the DB if missing."""
        sort_key = sort.strip().lower()
        if sort_key not in VALID_SORTS:
            raise UsageError(
                f"Invalid sort field: {sort!r}",
                hint=f"Allowed sort fields: {', '.join(VALID_SORTS)}.",
            )
        effective_limit = self.default_limit if limit is None else limit
        if effective_limit < 1:
            raise UsageError("--limit must be at least 1.")
        if offset < 0:
            raise UsageError("--offset cannot be negative.")

        conn = open_db(self.paths, create=False)
        if conn is None:
            return ListResult(
                total=0,
                offset=offset,
                limit=effective_limit,
                sort=sort_key,
                snippets=[],
            )
        try:
            clauses, params = self._build_filter_clauses(
                language=language,
                tags=tags,
                category=category,
                favorites_only=favorites_only,
            )
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            order_sql = {
                "modified": "s.modified_at DESC, s.id DESC",
                "created": "s.created_at DESC, s.id DESC",
                "title": "LOWER(s.title) ASC, s.id ASC",
            }[sort_key]

            count_sql = f"SELECT COUNT(*) FROM snippets s {where_sql}"  # noqa: S608
            total_row = conn.execute(count_sql, tuple(params)).fetchone()
            total = int(total_row[0]) if total_row else 0

            list_sql = (
                f"SELECT s.* FROM snippets s {where_sql} "  # noqa: S608
                f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
            )
            rows = conn.execute(
                list_sql,
                (*params, effective_limit, offset),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            tags_map = self._fetch_tags_map(conn, ids)
            snippets = [self._row_to_snippet(r, tags_map.get(int(r["id"]), [])) for r in rows]
            return ListResult(
                total=total,
                offset=offset,
                limit=effective_limit,
                sort=sort_key,
                snippets=snippets,
            )
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database query failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()

    def search_snippets(
        self,
        query: str | None = None,
        *,
        language: str | None = None,
        tags: Sequence[str] | str | None = None,
        category: str | None = None,
        favorites_only: bool = False,
        limit: int | None = None,
    ) -> SearchResult:
        """Search snippets using SQLite FTS5 (with bm25 title boost) or LIKE fallback."""
        effective_limit = self.default_limit if limit is None else limit
        if effective_limit < 1:
            raise UsageError("--limit must be at least 1.")

        raw_query = (query or "").strip()
        conn = open_db(self.paths, create=False)
        if conn is None:
            fts_ok = not _FORCE_DISABLE_FTS5
            return SearchResult(
                query=raw_query,
                fts_enabled=fts_ok,
                note=None if fts_ok else FTS_FALLBACK_NOTE,
                total=0,
                hits=[],
            )
        try:
            fts_ok = check_fts5_available(conn)
            if fts_ok:
                tbl_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'snippets_fts'"
                ).fetchone()
                if tbl_row is None:
                    fts_ok = False

            clauses, filter_params = self._build_filter_clauses(
                language=language,
                tags=tags,
                category=category,
                favorites_only=favorites_only,
            )

            # Filter-only search (no query string provided)
            if not raw_query:
                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                filter_sql = (
                    f"SELECT s.* FROM snippets s {where_sql} "  # noqa: S608
                    "ORDER BY s.modified_at DESC, s.id DESC LIMIT ?"
                )
                rows = conn.execute(
                    filter_sql,
                    (*filter_params, effective_limit),
                ).fetchall()
                ids = [int(r["id"]) for r in rows]
                tags_map = self._fetch_tags_map(conn, ids)
                hits = [
                    SearchHit(
                        snippet=(snip := self._row_to_snippet(r, tags_map.get(int(r["id"]), []))),
                        score=0.0,
                        matched_excerpt=build_matched_excerpt(snip, None),
                    )
                    for r in rows
                ]
                return SearchResult(
                    query="",
                    fts_enabled=fts_ok,
                    note=None if fts_ok else FTS_FALLBACK_NOTE,
                    total=len(hits),
                    hits=hits,
                )

            if fts_ok:
                fts_query = build_fts_query(raw_query)
                if not fts_query:
                    return SearchResult(
                        query=raw_query, fts_enabled=True, note=None, total=0, hits=[]
                    )
                all_clauses = ["snippets_fts MATCH ?", *clauses]
                where_sql = "WHERE " + " AND ".join(all_clauses)
                fts_sql = (
                    "SELECT s.*, bm25(snippets_fts, 10.0, 2.0, 1.0, 4.0, 2.0, 2.0) AS raw_rank "  # noqa: S608
                    "FROM snippets_fts "
                    "JOIN snippets s ON s.id = snippets_fts.rowid "
                    f"{where_sql} "
                    "ORDER BY raw_rank ASC, s.modified_at DESC, s.id DESC LIMIT ?"
                )
                rows = conn.execute(
                    fts_sql,
                    (fts_query, *filter_params, effective_limit),
                ).fetchall()
                ids = [int(r["id"]) for r in rows]
                tags_map = self._fetch_tags_map(conn, ids)
                hits = []
                for r in rows:
                    snip = self._row_to_snippet(r, tags_map.get(int(r["id"]), []))
                    raw_rank = float(r["raw_rank"] or 0.0)
                    score = round(abs(raw_rank), 6)
                    hits.append(
                        SearchHit(
                            snippet=snip,
                            score=score,
                            matched_excerpt=build_matched_excerpt(snip, raw_query),
                        )
                    )
                return SearchResult(
                    query=raw_query,
                    fts_enabled=True,
                    note=None,
                    total=len(hits),
                    hits=hits,
                )

            # LIKE fallback when FTS5 is unavailable
            tokens = [t for t in raw_query.replace("\x00", " ").split() if t]
            like_clauses: list[str] = []
            like_params: list[Any] = []
            for tok in tokens:
                pat = f"%{escape_like_pattern(tok.lower())}%"
                like_clauses.append(
                    """
                    (
                        LOWER(s.title) LIKE ? ESCAPE '\\'
                        OR LOWER(s.description) LIKE ? ESCAPE '\\'
                        OR LOWER(s.content) LIKE ? ESCAPE '\\'
                        OR LOWER(s.language) LIKE ? ESCAPE '\\'
                        OR LOWER(s.category) LIKE ? ESCAPE '\\'
                        OR EXISTS (
                            SELECT 1
                            FROM snippet_tags st
                            JOIN tags t ON t.id = st.tag_id
                            WHERE st.snippet_id = s.id AND LOWER(t.name) LIKE ? ESCAPE '\\'
                        )
                    )
                    """
                )
                like_params.extend([pat] * 6)

            all_clauses = [*like_clauses, *clauses]
            where_sql = f"WHERE {' AND '.join(all_clauses)}" if all_clauses else ""
            first_pat = f"%{escape_like_pattern(tokens[0].lower())}%" if tokens else "%"
            like_sql = (
                "SELECT s.*, "  # noqa: S608
                "CASE WHEN LOWER(s.title) LIKE ? ESCAPE '\\' THEN 2.0 ELSE 1.0 END "
                "AS fallback_score "
                f"FROM snippets s {where_sql} "
                "ORDER BY fallback_score DESC, s.modified_at DESC, s.id DESC LIMIT ?"
            )
            rows = conn.execute(
                like_sql,
                (first_pat, *like_params, *filter_params, effective_limit),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            tags_map = self._fetch_tags_map(conn, ids)
            hits = [
                SearchHit(
                    snippet=(snip := self._row_to_snippet(r, tags_map.get(int(r["id"]), []))),
                    score=float(r["fallback_score"]),
                    matched_excerpt=build_matched_excerpt(snip, raw_query),
                )
                for r in rows
            ]
            return SearchResult(
                query=raw_query,
                fts_enabled=False,
                note=FTS_FALLBACK_NOTE,
                total=len(hits),
                hits=hits,
            )
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database search failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()

    def update_snippet(
        self,
        snippet_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        language: str | None = None,
        tags: Sequence[str] | str | None = None,
        add_tags: Sequence[str] | str | None = None,
        remove_tags: Sequence[str] | str | None = None,
        category: str | None = None,
        description: str | None = None,
        favorite: bool | None = None,
    ) -> tuple[Snippet, bool]:
        """Update fields on ``snippet_id``; returns ``(snippet, changed)``.

        ``modified_at`` is updated only if at least one field or tag changed.
        """
        existing = self.get_snippet(snippet_id)

        new_title = normalize_title(title) if title is not None else existing.title
        new_content = (
            validate_content(content, max_bytes=self.max_snippet_bytes)
            if content is not None
            else existing.content
        )
        new_lang = (
            normalize_language(language, default=self.default_language)
            if language is not None
            else existing.language
        )
        new_cat = normalize_category(category) if category is not None else existing.category
        new_desc = (
            normalize_description(description) if description is not None else existing.description
        )
        new_fav = bool(favorite) if favorite is not None else existing.favorite

        if tags is not None:
            computed_tags = normalize_tags(tags)
        else:
            computed_tags = list(existing.tags)

        if add_tags:
            for t in normalize_tags(add_tags):
                if t not in computed_tags:
                    computed_tags.append(t)

        if remove_tags:
            to_remove = set(normalize_tags(remove_tags))
            computed_tags = [t for t in computed_tags if t not in to_remove]

        new_hash = compute_content_hash(new_content)

        changed = (
            new_title != existing.title
            or new_content != existing.content
            or new_lang != existing.language
            or new_cat != existing.category
            or new_desc != existing.description
            or new_fav != existing.favorite
            or computed_tags != existing.tags
        )
        if not changed:
            return existing, False

        new_modified = utc_now_iso(after=existing.modified_at)
        conn = open_db(self.paths, create=True)
        if conn is None:  # pragma: no cover
            raise NotFoundError(f"Snippet #{snippet_id} not found.")
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE snippets
                    SET title = ?, description = ?, content = ?, language = ?,
                        category = ?, favorite = ?, content_hash = ?, modified_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_title,
                        new_desc,
                        new_content,
                        new_lang,
                        new_cat,
                        1 if new_fav else 0,
                        new_hash,
                        new_modified,
                        snippet_id,
                    ),
                )
                self._set_snippet_tags(conn, snippet_id, computed_tags)
                row = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
                if row is None:  # pragma: no cover
                    raise NotFoundError(f"Snippet #{snippet_id} not found.")
                return self._row_to_snippet(row, computed_tags), True
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database update failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()

    def set_favorite(self, snippet_id: int, favorite: bool) -> tuple[Snippet, bool]:
        """Set or clear the favorite flag on a snippet."""
        return self.update_snippet(snippet_id, favorite=favorite)

    def delete_snippet(self, snippet_id: int) -> Snippet:
        """Delete a snippet by ID inside a transaction and return the deleted record."""
        existing = self.get_snippet(snippet_id)
        conn = open_db(self.paths, create=True)
        if conn is None:  # pragma: no cover
            raise NotFoundError(f"Snippet #{snippet_id} not found.")
        try:
            with conn:
                conn.execute("DELETE FROM snippet_tags WHERE snippet_id = ?", (snippet_id,))
                conn.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
            return existing
        except sqlite3.DatabaseError as exc:
            raise DataError(f"Database delete failed: {exc}", hint=RESTORE_HINT) from exc
        finally:
            conn.close()
