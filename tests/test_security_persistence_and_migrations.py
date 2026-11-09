"""Security, corruption, schema migration, and cross-process persistence tests for snipvault."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from snipvault.common.exceptions import ExitCode

RunFn = Callable[..., Any]


def test_cross_process_persistence_and_concurrent_writers(
    run_proc: RunFn,
) -> None:
    # Process 1: Add snippet
    r1 = run_proc(
        "snipvault",
        "add",
        "--title",
        "Subprocess Snippet",
        "--language",
        "python",
        "--tags",
        "proc,test",
        "--content",
        "print('from process 1')",
        "--json",
    )
    assert r1.exit_code == 0
    snip_id = r1.json()["id"]

    # Process 2: Edit snippet
    r2 = run_proc(
        "snipvault",
        "edit",
        str(snip_id),
        "--title",
        "Subprocess Edited",
        "--add-tag",
        "v2",
        "--json",
    )
    assert r2.exit_code == 0
    assert r2.json()["title"] == "Subprocess Edited"

    # Process 3: Concurrent writers adding snippets simultaneously
    def _add_one(idx: int) -> Any:
        return run_proc(
            "snipvault",
            "add",
            "--title",
            f"Concurrent #{idx}",
            "--language",
            "python",
            "--content",
            f"val = {idx}",
            "--json",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_add_one, range(1, 7)))
    assert all(o.exit_code == 0 for o in outcomes)

    # Process 4: Verify all 7 snippets persisted cleanly
    listed = run_proc("snipvault", "list", "--json")
    assert listed.exit_code == 0
    assert listed.json()["total"] == 7


@pytest.mark.parametrize(
    "adversarial_query",
    [
        'foo"bar',
        "AND",
        "OR",
        "NOT",
        "*",
        "NEAR(",
        "NEAR(a, b)",
        "title:foo",
        "^start",
        '"unclosed quote',
        '"""',
        "(a OR b) AND NOT c",
        "{title content} : foo",
        "SELECT * FROM snippets; DROP TABLE snippets; --",
        "100%_literal\\match",
        "c++",
        "@decorator",
        "🚀✨",
        "日本語 テスト",
        "---",
        "((()))",
    ],
)
def test_fts_special_characters_and_injection_queries_never_error(
    run: RunFn, adversarial_query: str
) -> None:
    run(
        "snipvault",
        "add",
        "--title",
        'Special foo"bar 🚀✨ 日本語',
        "--language",
        "python",
        "--tags",
        "security,fts",
        "--content",
        'x = "foo\\"bar" # AND NEAR( * 100%_literal\\match @decorator',
    )

    res = run("snipvault", "--json", "search", "--", adversarial_query)
    assert res.exit_code == 0
    data = res.json()
    assert isinstance(data["total"], int)

    # Verify table was not dropped or damaged
    assert run("snipvault", "list", "--json").json()["total"] == 1


def test_corrupted_or_truncated_database_raises_data_error_with_restore_hint(
    run: RunFn, tmp_path: Path
) -> None:
    run("snipvault", "add", "--title", "Healthy", "--content", "ok = True")
    backup_res = run("snipvault", "backup", "--json").json()
    backup_file = Path(backup_res["output"])

    db_path = tmp_path / "dtk" / "data" / "snipvault" / "snippets.db"
    assert db_path.exists()

    # 1. Truncate DB to 0 bytes
    db_path.write_bytes(b"")
    res_zero = run("snipvault", "list")
    assert res_zero.exit_code == int(ExitCode.ERROR)
    assert "restore" in res_zero.stderr.lower()
    assert "Traceback" not in res_zero.stderr

    # 2. Truncate DB to 32 bytes
    db_path.write_bytes(b"SQLite format 3\x00" + b"x" * 16)
    res_short = run("snipvault", "search", "Healthy")
    assert res_short.exit_code == int(ExitCode.ERROR)
    assert "restore" in res_short.stderr.lower()

    # 3. Write non-SQLite garbage bytes (>= 100 bytes)
    db_path.write_bytes(b"NOT A SQLITE DATABASE FILE AT ALL " * 10)
    res_garbage = run("snipvault", "add", "--title", "New", "--content", "x = 1")
    assert res_garbage.exit_code == int(ExitCode.ERROR)
    assert "restore" in res_garbage.stderr.lower()

    # 4. Recover corrupted DB using `snipvault restore`!
    rec = run("snipvault", "restore", str(backup_file), "--yes", "--json")
    assert rec.exit_code == 0
    assert run("snipvault", "list", "--json").json()["total"] == 1


def test_newer_schema_version_is_refused_and_never_modified(run: RunFn, tmp_path: Path) -> None:
    run("snipvault", "add", "--title", "V2 Snippet", "--content", "v = 2")
    db_path = tmp_path / "dtk" / "data" / "snipvault" / "snippets.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    before_bytes = db_path.read_bytes()

    # Both read and write commands must refuse to touch a v99 DB
    res_list = run("snipvault", "list")
    assert res_list.exit_code == int(ExitCode.ERROR)
    assert "newer than supported" in res_list.stderr
    assert db_path.read_bytes() == before_bytes

    res_add = run("snipvault", "add", "--title", "Fail", "--content", "x = 1")
    assert res_add.exit_code == int(ExitCode.ERROR)
    assert "newer than supported" in res_add.stderr
    assert db_path.read_bytes() == before_bytes


def test_migration_from_hand_built_v1_database_preserves_data_and_indexes_fts(
    run: RunFn, tmp_path: Path
) -> None:
    data_dir = tmp_path / "dtk" / "data" / "snipvault"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "snippets.db"

    # Build a minimal v1 database by hand (without FTS table and without content_hash column)
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE snippets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'python',
                created_at TEXT NOT NULL,
                modified_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)"
        )
        conn.execute(
            """
            CREATE TABLE snippet_tags (
                snippet_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (snippet_id, tag_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO snippets (
                id, title, description, content, language, created_at, modified_at
            )
            VALUES (
                1, 'Legacy V1 Auth Helper', 'From v1 schema',
                'def verify_v1_token(): return True', 'python',
                '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z'
            )
            """
        )
        conn.execute("INSERT INTO tags (id, name) VALUES (1, 'legacy')")
        conn.execute("INSERT INTO snippet_tags (snippet_id, tag_id) VALUES (1, 1)")
        conn.execute("PRAGMA user_version = 1")
    conn.close()

    # Running search upgrades v1 -> v2, backfills content_hash, and indexes existing rows in FTS5
    search_res = run("snipvault", "search", "verify_v1_token", "--json")
    assert search_res.exit_code == 0
    hits = search_res.json()["snippets"]
    assert len(hits) == 1
    assert hits[0]["title"] == "Legacy V1 Auth Helper"
    assert hits[0]["tags"] == ["legacy"]
    assert len(hits[0]["content_hash"]) == 64

    # Verify PRAGMA user_version is now 2
    conn2 = sqlite3.connect(str(db_path))
    ver = conn2.execute("PRAGMA user_version").fetchone()[0]
    conn2.close()
    assert ver == 2


def test_read_only_data_dir_and_path_safety(run: RunFn, tmp_path: Path) -> None:
    # Path-traversal-like title is stored purely as SQLite text, never touching filesystem
    res_title = run(
        "snipvault",
        "add",
        "--title",
        "../../etc/passwd",
        "--content",
        "safe = True",
        "--json",
    )
    assert res_title.exit_code == 0
    assert res_title.json()["title"] == "../../etc/passwd"

    # Symlinked export destination is refused
    real_target = tmp_path / "target.json"
    real_target.write_text("{}")
    symlink_out = tmp_path / "symlink_export.json"
    symlink_out.symlink_to(real_target)
    res_sym = run("snipvault", "export", "--output", str(symlink_out), "--force")
    assert res_sym.exit_code == int(ExitCode.REFUSED)

    # Read-only data directory on write -> PermissionDeniedError (exit code 2)
    data_dir = tmp_path / "dtk" / "data" / "snipvault"
    db_file = data_dir / "snippets.db"
    os.chmod(db_file, 0o400)
    os.chmod(data_dir, 0o500)
    try:
        res_ro = run("snipvault", "add", "--title", "Fail RO", "--content", "x = 1")
        assert res_ro.exit_code == int(ExitCode.ERROR)
        assert "Traceback" not in res_ro.stderr
    finally:
        os.chmod(data_dir, 0o700)
        os.chmod(db_file, 0o600)
