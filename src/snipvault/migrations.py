"""Ordered SQLite schema migrations for snipvault."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from snipvault.models import compute_content_hash, utc_now_iso

LATEST_SCHEMA_VERSION = 2


def is_fts5_compiled(conn: sqlite3.Connection) -> bool:
    """Return True if SQLite was compiled with the FTS5 extension."""
    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
        for (opt,) in rows:
            if isinstance(opt, str) and "ENABLE_FTS5" in opt.upper():
                return True
        # Fallback probe in case compile_options is omitted in custom builds
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.Error:
        return False


def _migrate_v1(conn: sqlite3.Connection, _fts5: bool) -> None:
    """Migration 0 -> 1: Base relational schema for snippets and tags."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'text',
            category TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            modified_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snippet_tags (
            snippet_id INTEGER NOT NULL REFERENCES snippets(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (snippet_id, tag_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_language ON snippets(language)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_category ON snippets(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_favorite ON snippets(favorite)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_modified ON snippets(modified_at DESC)")


def _migrate_v2(conn: sqlite3.Connection, fts5: bool) -> None:
    """Migration 1 -> 2: Upgrade v1 tables, backfill hashes, and build FTS5 index + triggers."""
    existing_cols = {
        str(row[1]).lower() for row in conn.execute("PRAGMA table_info(snippets)").fetchall()
    }
    now = utc_now_iso()
    additions: list[tuple[str, str]] = [
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("language", "TEXT NOT NULL DEFAULT 'text'"),
        ("category", "TEXT NOT NULL DEFAULT ''"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
        ("content_hash", "TEXT NOT NULL DEFAULT ''"),
        ("created_at", f"TEXT NOT NULL DEFAULT '{now}'"),
        ("modified_at", f"TEXT NOT NULL DEFAULT '{now}'"),
    ]
    for col_name, col_def in additions:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE snippets ADD COLUMN {col_name} {col_def}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snippet_tags (
            snippet_id INTEGER NOT NULL REFERENCES snippets(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (snippet_id, tag_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_language ON snippets(language)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_category ON snippets(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_favorite ON snippets(favorite)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_modified ON snippets(modified_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_hash ON snippets(content_hash)")

    # Backfill missing content_hash or timestamps on upgraded v1 records
    rows = conn.execute(
        "SELECT id, content, content_hash, created_at, modified_at FROM snippets"
    ).fetchall()
    for row_id, content, content_hash, created_at, modified_at in rows:
        new_hash = content_hash or compute_content_hash(content or "")
        new_created = created_at or now
        new_modified = modified_at or new_created
        if new_hash != content_hash or new_created != created_at or new_modified != modified_at:
            conn.execute(
                """
                UPDATE snippets
                SET content_hash = ?, created_at = ?, modified_at = ?
                WHERE id = ?
                """,
                (new_hash, new_created, new_modified, row_id),
            )

    if fts5:
        ensure_fts5_schema(conn, rebuild=True)


def ensure_fts5_schema(conn: sqlite3.Connection, *, rebuild: bool = False) -> None:
    """Create the FTS5 virtual table and synchronization triggers if not present."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
            title,
            description,
            content,
            tags,
            language,
            category,
            tokenize = "unicode61 categories 'L* N* Co So'"
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_snippets_fts_ai AFTER INSERT ON snippets BEGIN
            DELETE FROM snippets_fts WHERE rowid = NEW.id;
            INSERT INTO snippets_fts(rowid, title, description, content, tags, language, category)
            VALUES (
                NEW.id,
                NEW.title,
                NEW.description,
                NEW.content,
                COALESCE(
                    (SELECT GROUP_CONCAT(t.name, ' ')
                     FROM snippet_tags st
                     JOIN tags t ON t.id = st.tag_id
                     WHERE st.snippet_id = NEW.id),
                    ''
                ),
                NEW.language,
                NEW.category
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_snippets_fts_au AFTER UPDATE ON snippets BEGIN
            DELETE FROM snippets_fts WHERE rowid = OLD.id;
            INSERT INTO snippets_fts(rowid, title, description, content, tags, language, category)
            VALUES (
                NEW.id,
                NEW.title,
                NEW.description,
                NEW.content,
                COALESCE(
                    (SELECT GROUP_CONCAT(t.name, ' ')
                     FROM snippet_tags st
                     JOIN tags t ON t.id = st.tag_id
                     WHERE st.snippet_id = NEW.id),
                    ''
                ),
                NEW.language,
                NEW.category
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_snippets_fts_ad AFTER DELETE ON snippets BEGIN
            DELETE FROM snippets_fts WHERE rowid = OLD.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_snippet_tags_fts_ai AFTER INSERT ON snippet_tags BEGIN
            UPDATE snippets_fts
            SET tags = COALESCE(
                (SELECT GROUP_CONCAT(t.name, ' ')
                 FROM snippet_tags st
                 JOIN tags t ON t.id = st.tag_id
                 WHERE st.snippet_id = NEW.snippet_id),
                ''
            )
            WHERE rowid = NEW.snippet_id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_snippet_tags_fts_ad AFTER DELETE ON snippet_tags BEGIN
            UPDATE snippets_fts
            SET tags = COALESCE(
                (SELECT GROUP_CONCAT(t.name, ' ')
                 FROM snippet_tags st
                 JOIN tags t ON t.id = st.tag_id
                 WHERE st.snippet_id = OLD.snippet_id),
                ''
            )
            WHERE rowid = OLD.snippet_id;
        END
        """
    )
    if rebuild:
        rebuild_fts_index(conn)


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Repopulate ``snippets_fts`` from ``snippets`` and ``snippet_tags``."""
    conn.execute("DELETE FROM snippets_fts")
    conn.execute(
        """
        INSERT INTO snippets_fts(rowid, title, description, content, tags, language, category)
        SELECT
            s.id,
            s.title,
            s.description,
            s.content,
            COALESCE(
                (SELECT GROUP_CONCAT(t.name, ' ')
                 FROM snippet_tags st
                 JOIN tags t ON t.id = st.tag_id
                 WHERE st.snippet_id = s.id),
                ''
            ),
            s.language,
            s.category
        FROM snippets s
        """
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection, bool], None]] = {
    1: _migrate_v1,
    2: _migrate_v2,
}


def apply_migrations(conn: sqlite3.Connection, *, current_version: int, fts5_enabled: bool) -> int:
    """Apply pending migrations in order inside a transaction."""
    version = current_version
    with conn:
        for target_version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
            migration = MIGRATIONS[target_version]
            migration(conn, fts5_enabled)
            conn.execute(f"PRAGMA user_version = {target_version}")
            version = target_version
    return version
