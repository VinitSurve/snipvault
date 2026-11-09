"""SQLite connection management, integrity checks, and online backup/restore."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from snipvault.common.exceptions import (
    DataError,
    NotFoundError,
    PermissionDeniedError,
    ToolkitError,
    UsageError,
)
from snipvault.common.fsutil import file_kind, unique_destination
from snipvault.common.paths import ToolPaths, ensure_not_protected, require_file
from snipvault.migrations import (
    LATEST_SCHEMA_VERSION,
    apply_migrations,
    ensure_fts5_schema,
    is_fts5_compiled,
)
from snipvault.models import BackupResult, RestoreResult

DB_FILENAME = "snippets.db"
SQLITE_HEADER = b"SQLite format 3\x00"
MIN_SQLITE_BYTES = 100
BUSY_TIMEOUT_MS = 5000
RESTORE_HINT = "Restore from a valid backup with 'snipvault restore <backup-file>'."

# Test hook to simulate environments where SQLite was compiled without FTS5.
_FORCE_DISABLE_FTS5 = False

REQUIRED_SNIPPET_COLUMNS = frozenset(
    {
        "id",
        "title",
        "description",
        "content",
        "language",
        "category",
        "favorite",
        "content_hash",
        "created_at",
        "modified_at",
    }
)


def db_file_path(paths: ToolPaths) -> Path:
    """Return the path to ``snippets.db`` inside the tool's data directory."""
    return paths.data_dir / DB_FILENAME


def check_fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if FTS5 is available and not disabled for testing."""
    if _FORCE_DISABLE_FTS5:
        return False
    return is_fts5_compiled(conn)


def _check_sqlite_header(path: Path) -> None:
    """Verify ``path`` is a non-truncated regular file with a valid SQLite 3 header."""
    kind = file_kind(path)
    if kind == "symlink":
        raise DataError(
            f"Refusing to open symlinked database file: {path}",
            hint=RESTORE_HINT,
        )
    if kind != "file":
        raise DataError(
            f"Database path is not a regular file: {path}",
            hint=RESTORE_HINT,
        )
    if not os.access(path, os.R_OK):
        raise PermissionDeniedError(
            f"Permission denied reading database file: {path}",
            hint="Check file permissions on your snipvault data directory.",
        )
    try:
        size = path.stat().st_size
        if size < MIN_SQLITE_BYTES:
            raise DataError(
                f"Database file is corrupted or truncated ({size} bytes): {path}",
                hint=RESTORE_HINT,
            )
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise PermissionDeniedError(
            f"Cannot read database file {path}: {exc}",
            hint="Check permissions on your snipvault data directory.",
        ) from exc
    if header != SQLITE_HEADER:
        raise DataError(
            f"Database file is corrupted (invalid SQLite header): {path}",
            hint=RESTORE_HINT,
        )


def _verify_integrity_and_version(conn: sqlite3.Connection, path: Path) -> tuple[int, set[str]]:
    """Run integrity check and inspect ``user_version`` and table columns."""
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        if not integrity_rows or integrity_rows[0][0] != "ok":
            details = "; ".join(str(r[0]) for r in integrity_rows[:3])
            raise DataError(
                f"Database integrity check failed for {path}: {details}",
                hint=RESTORE_HINT,
            )
        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row else 0
    except sqlite3.DatabaseError as exc:
        raise DataError(
            f"Database file is corrupted or unreadable ({path}): {exc}",
            hint=RESTORE_HINT,
        ) from exc

    if version > LATEST_SCHEMA_VERSION:
        raise DataError(
            f"Database schema version {version} is newer than supported "
            f"version {LATEST_SCHEMA_VERSION} ({path}). Refusing to modify it.",
            hint=(
                "Upgrade snipvault to a newer release or restore an older backup "
                "with 'snipvault restore'."
            ),
        )
    if version < 1:
        raise DataError(
            f"Database file {path} has an invalid or uninitialized schema (version {version}).",
            hint=RESTORE_HINT,
        )

    try:
        cols = {
            str(row[1]).lower() for row in conn.execute("PRAGMA table_info(snippets)").fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise DataError(
            f"Database schema is malformed in {path}: {exc}",
            hint=RESTORE_HINT,
        ) from exc

    if not {"id", "title", "content"}.issubset(cols):
        raise DataError(
            f"Database file {path} is not a valid SnipVault database (missing 'snippets' columns).",
            hint=RESTORE_HINT,
        )
    return version, cols


def open_db(paths: ToolPaths, *, create: bool = False) -> sqlite3.Connection | None:
    """Open the SnipVault SQLite database.

    * When ``create=False`` and the database file does not exist, returns ``None``
      without creating the data directory or database file.
    * Validates SQLite header, ``PRAGMA integrity_check``, and ``PRAGMA user_version``.
    * Applies any pending schema migrations inside a transaction.
    """
    path = db_file_path(paths)
    existed = os.path.lexists(path)

    if not existed and not create:
        return None

    if not existed:
        # Check whether an ancestor exists and is unwritable before creating
        parent = path.parent
        probe = parent
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if probe.exists() and not os.access(probe, os.W_OK | os.X_OK):
            raise PermissionDeniedError(
                f"Permission denied creating database directory {parent}",
                hint="Grant write permissions to your snipvault data directory.",
            )
        try:
            ToolPaths.ensure(parent)
        except OSError as exc:
            raise PermissionDeniedError(
                f"Cannot create database directory {parent}: {exc}",
                hint="Check directory permissions and free disk space.",
            ) from exc
    else:
        _check_sqlite_header(path)
        if create and (not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK)):
            raise PermissionDeniedError(
                f"Permission denied writing to database {path}",
                hint="Grant write permissions to your snipvault data directory and snippets.db.",
            )

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")

        if not existed:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA journal_mode = WAL")
            fts5_ok = check_fts5_available(conn)
            apply_migrations(conn, current_version=0, fts5_enabled=fts5_ok)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            return conn

        version, cols = _verify_integrity_and_version(conn, path)
        fts5_ok = check_fts5_available(conn)

        if version < LATEST_SCHEMA_VERSION:
            if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
                raise PermissionDeniedError(
                    f"Database {path} requires schema migration from v{version} to "
                    f"v{LATEST_SCHEMA_VERSION}, but the file is read-only.",
                    hint="Grant write permissions so the schema migration can complete.",
                )
            apply_migrations(conn, current_version=version, fts5_enabled=fts5_ok)
            _, cols = _verify_integrity_and_version(conn, path)

        if not REQUIRED_SNIPPET_COLUMNS.issubset(cols):
            missing = sorted(REQUIRED_SNIPPET_COLUMNS - cols)
            raise DataError(
                f"Database schema in {path} is malformed (missing columns: {', '.join(missing)}).",
                hint=RESTORE_HINT,
            )

        # Verify tags and snippet_tags tables exist
        tables = {
            str(r[0]).lower()
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        if not {"snippets", "tags", "snippet_tags"}.issubset(tables):
            raise DataError(
                f"Database schema in {path} is malformed (missing required tables).",
                hint=RESTORE_HINT,
            )

        # Ensure FTS5 table exists if writable and FTS5 is available
        if fts5_ok and "snippets_fts" not in tables and os.access(path, os.W_OK):
            with conn:
                ensure_fts5_schema(conn, rebuild=True)

        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return conn
    except ToolkitError:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        raise
    except sqlite3.OperationalError as exc:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        msg = str(exc).lower()
        if "readonly" in msg or "permission" in msg or "attempt to write" in msg:
            raise PermissionDeniedError(
                f"Database operation failed due to read-only access on {path}: {exc}",
                hint="Check permissions on your snipvault data directory.",
            ) from exc
        raise DataError(
            f"Database error in {path}: {exc}",
            hint=RESTORE_HINT,
        ) from exc
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        raise DataError(
            f"Database file is corrupted ({path}): {exc}",
            hint=RESTORE_HINT,
        ) from exc
    except OSError as exc:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        raise PermissionDeniedError(
            f"Filesystem error accessing database {path}: {exc}",
            hint="Check permissions on your snipvault data directory.",
        ) from exc


def validate_database_file(path: Path) -> tuple[int, int]:
    """Validate that ``path`` is a healthy SnipVault SQLite DB and return (version, count)."""
    require_file(path, what="Backup file")
    _check_sqlite_header(path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        version, _ = _verify_integrity_and_version(conn, path)
        row = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()
        count = int(row[0]) if row else 0
        return version, count
    except ToolkitError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DataError(
            f"Backup file {path} is not a valid SnipVault database: {exc}",
            hint="Provide a valid SnipVault SQLite backup file.",
        ) from exc
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _sqlite_online_copy(src_path: Path, dst_path: Path) -> None:
    """Copy a SQLite database consistently using ``sqlite3.Connection.backup``."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst_path.name}.", suffix=".tmp", dir=dst_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite3.connect(str(src_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        dst_conn = sqlite3.connect(str(tmp_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        src_conn.backup(dst_conn)
        with contextlib.suppress(sqlite3.Error):
            dst_conn.execute("PRAGMA journal_mode = DELETE")
        dst_conn.commit()
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dst_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{tmp_path}{suffix}")
            if sidecar.exists():
                with contextlib.suppress(OSError):
                    sidecar.unlink()
    except BaseException:
        if dst_conn is not None:
            with contextlib.suppress(sqlite3.Error):
                dst_conn.close()
        if src_conn is not None:
            with contextlib.suppress(sqlite3.Error):
                src_conn.close()
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def backup_database(paths: ToolPaths, output_path: Path | None = None) -> BackupResult:
    """Create a consistent online backup of the SnipVault database."""
    if output_path is not None:
        ensure_not_protected(output_path, "write backup file")
        if file_kind(output_path) == "symlink":
            raise DataError(f"Refusing to write backup through symlink: {output_path}")
        if output_path.exists() and output_path.is_dir():
            raise UsageError(f"Backup output path is a directory: {output_path}")
        dest = output_path
    else:
        backups_dir = ToolPaths.ensure(paths.data_dir / "backups")
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        dest = unique_destination(backups_dir / f"snippets-{timestamp}.db")

    src = db_file_path(paths)
    # Ensure the database is initialized and healthy before backing up
    conn = open_db(paths, create=True)
    if conn is None:  # pragma: no cover
        raise NotFoundError("Database could not be initialized.")
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row else LATEST_SCHEMA_VERSION
        count_row = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()
        count = int(count_row[0]) if count_row else 0
    finally:
        conn.close()

    try:
        _sqlite_online_copy(src, dest)
    except OSError as exc:
        raise PermissionDeniedError(
            f"Cannot write backup file {dest}: {exc}",
            hint="Check write permissions on the destination folder.",
        ) from exc

    return BackupResult(
        source=str(src),
        output=str(dest),
        size_bytes=dest.stat().st_size,
        snippet_count=count,
        schema_version=version,
    )


def restore_database(paths: ToolPaths, backup_file: Path) -> RestoreResult:
    """Restore ``snippets.db`` from a validated backup file, keeping a safety backup first."""
    target = db_file_path(paths)
    if backup_file.resolve() == target.resolve():
        raise UsageError(
            "Backup file and active database are the same file.",
            hint="Specify a backup file from the backups directory or another location.",
        )

    # Validate backup_file before touching the current database
    schema_version, _ = validate_database_file(backup_file)

    ToolPaths.ensure(paths.data_dir)
    safety_backup_path: Path | None = None
    if os.path.lexists(target):
        backups_dir = ToolPaths.ensure(paths.data_dir / "backups")
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        safety_backup_path = unique_destination(backups_dir / f"pre-restore-{timestamp}.db")
        try:
            _sqlite_online_copy(target, safety_backup_path)
        except Exception:
            # If the current DB is corrupted (the reason the user is restoring!),
            # preserve its raw bytes with shutil.copy2 so nothing is lost.
            shutil.copy2(target, safety_backup_path)
            with contextlib.suppress(OSError):
                os.chmod(safety_backup_path, 0o600)

    # Remove stale WAL/SHM files from previous database before replacing
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if os.path.lexists(sidecar):
            with contextlib.suppress(OSError):
                sidecar.unlink()

    _sqlite_online_copy(backup_file, target)

    # Open restored DB to apply any pending migrations (e.g. v1 -> v2) and verify health
    conn = open_db(paths, create=True)
    if conn is None:  # pragma: no cover
        raise DataError("Failed to open restored database.")
    try:
        ver_row = conn.execute("PRAGMA user_version").fetchone()
        final_version = int(ver_row[0]) if ver_row else schema_version
        cnt_row = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()
        final_count = int(cnt_row[0]) if cnt_row else 0
    finally:
        conn.close()

    return RestoreResult(
        restored_from=str(backup_file),
        target=str(target),
        safety_backup=str(safety_backup_path) if safety_backup_path else None,
        snippet_count=final_count,
        schema_version=final_version,
    )
