"""Tests for snipvault copy, export, import, backup, restore, interactive, and config."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from snipvault.common.exceptions import ExitCode

RunFn = Callable[..., Any]


def test_copy_command_and_missing_clipboard_fallback(run: RunFn, tmp_path: Path) -> None:
    run("snipvault", "add", "--title", "Clip Test", "--content", "echo 'copied text'")

    # 1. --print always prints to stdout and exits 0
    res_print = run("snipvault", "copy", "1", "--print")
    assert res_print.exit_code == 0
    assert res_print.stdout.strip() == "echo 'copied text'"

    # 2. Fake pbcopy on PATH receives content on stdin and exits 0
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    captured_file = tmp_path / "clipboard_out.txt"
    fake_pbcopy = bin_dir / "pbcopy"
    fake_pbcopy.write_text(f'#!/bin/sh\n/bin/cat > "{captured_file}"\n')
    fake_pbcopy.chmod(fake_pbcopy.stat().st_mode | stat.S_IXUSR)

    res_copy = run("snipvault", "copy", "1", env={"PATH": str(bin_dir)})
    assert res_copy.exit_code == 0
    assert captured_file.read_text(encoding="utf-8") == "echo 'copied text'"

    # 3. Missing clipboard tool (empty PATH) -> prints content, warns on stderr, exits WARNING (1)
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    res_fallback = run("snipvault", "copy", "1", env={"PATH": str(empty_bin)})
    assert res_fallback.exit_code == int(ExitCode.WARNING)
    assert "echo 'copied text'" in res_fallback.stdout
    assert "clipboard unavailable; content printed instead" in res_fallback.stderr

    # 4. JSON mode with missing clipboard
    res_fallback_json = run("snipvault", "copy", "1", "--json", env={"PATH": str(empty_bin)})
    assert res_fallback_json.exit_code == int(ExitCode.WARNING)
    payload = res_fallback_json.json()
    assert payload["copied"] is False
    assert payload["printed"] is True
    assert payload["content"] == "echo 'copied text'"


def test_export_json_and_markdown_with_dynamic_fences(run: RunFn, tmp_path: Path) -> None:
    content_with_ticks = "Here is embedded markdown:\n```python\nprint(1)\n```\nDone."
    run(
        "snipvault",
        "add",
        "--title",
        "Nested Backticks",
        "--language",
        "markdown",
        "--tags",
        "docs,md",
        "--content",
        content_with_ticks,
    )

    # Export JSON to stdout
    exp_json = run("snipvault", "export", "--format", "json")
    assert exp_json.exit_code == 0
    parsed = json.loads(exp_json.stdout)
    assert parsed["schema_version"] == 2
    assert parsed["count"] == 1
    assert parsed["snippets"][0]["title"] == "Nested Backticks"

    # Export Markdown to stdout -> fence must be 4 backticks (````markdown)
    exp_md = run("snipvault", "export", "--format", "markdown")
    assert exp_md.exit_code == 0
    assert "## Nested Backticks" in exp_md.stdout
    assert "````markdown" in exp_md.stdout

    # Export to file and refuse overwrite without --force
    out_file = tmp_path / "exported.json"
    assert run("snipvault", "export", "--output", str(out_file)).exit_code == 0
    assert out_file.exists()

    refused = run("snipvault", "export", "--output", str(out_file))
    assert refused.exit_code == int(ExitCode.REFUSED)

    forced = run("snipvault", "export", "--output", str(out_file), "--force")
    assert forced.exit_code == 0


def test_import_sample_collection_duplicates_and_transactional_rollback(
    run: RunFn, tmp_path: Path
) -> None:
    sample_file = (
        Path(__file__).resolve().parents[1] / "examples" / "snipvault_sample_snippets.json"
    )
    assert sample_file.exists()

    # 1. Import the sample collection (>= 12 snippets)
    imp1 = run("snipvault", "import", str(sample_file), "--json")
    assert imp1.exit_code == 0
    s1 = imp1.json()
    assert s1["imported"] >= 12
    assert s1["skipped"] == 0
    total_count = s1["imported"]

    # 2. Re-import with default --on-duplicate=skip -> all skipped
    imp_skip = run("snipvault", "import", str(sample_file), "--json").json()
    assert imp_skip["imported"] == 0
    assert imp_skip["skipped"] == total_count

    # 3. Re-import with --on-duplicate=replace without --yes -> refused (ExitCode.REFUSED)
    imp_refused = run("snipvault", "import", str(sample_file), "--on-duplicate", "replace")
    assert imp_refused.exit_code == int(ExitCode.REFUSED)

    # 4. Re-import with --on-duplicate=replace --yes -> replaces in-place without changing count
    imp_replace = run(
        "snipvault", "import", str(sample_file), "--on-duplicate", "replace", "--yes", "--json"
    ).json()
    assert imp_replace["replaced"] == total_count
    assert run("snipvault", "list", "--json").json()["total"] == total_count

    # 5. Re-import a single duplicate with --on-duplicate=rename -> creates "Title (1)"
    one_dup = tmp_path / "one_dup.json"
    sample_data = json.loads(sample_file.read_text(encoding="utf-8"))
    one_dup.write_text(json.dumps({"snippets": [sample_data["snippets"][0]]}))
    imp_rename = run(
        "snipvault", "import", str(one_dup), "--on-duplicate", "rename", "--json"
    ).json()
    assert imp_rename["renamed"] == 1
    assert run("snipvault", "list", "--json").json()["total"] == total_count + 1

    # 6. All-or-nothing transactional validation: if 2nd item is invalid, 1st item is NOT imported
    count_before = run("snipvault", "list", "--json").json()["total"]
    bad_batch = tmp_path / "bad_batch.json"
    bad_batch.write_text(
        json.dumps(
            {
                "snippets": [
                    {
                        "title": "Should Not Be Committed",
                        "content": "print('valid first item')",
                        "language": "python",
                    },
                    {
                        "title": "Invalid Second Item",
                        "content": "print('bad favorite type')",
                        "favorite": "not-a-boolean",
                    },
                ]
            }
        )
    )
    res_bad = run("snipvault", "import", str(bad_batch))
    assert res_bad.exit_code == int(ExitCode.ERROR)
    assert run("snipvault", "list", "--json").json()["total"] == count_before

    # 7. Malformed JSON -> friendly DataError
    broken_json = tmp_path / "broken.json"
    broken_json.write_text("{not valid json")
    res_broken = run("snipvault", "import", str(broken_json))
    assert res_broken.exit_code == int(ExitCode.ERROR)
    assert "not valid JSON" in res_broken.stderr


def test_backup_and_restore_with_safety_backup(run: RunFn, tmp_path: Path) -> None:
    run("snipvault", "add", "--title", "Before Backup", "--content", "x = 1")

    # Default backup in data_dir/backups/
    b1 = run("snipvault", "backup", "--json")
    assert b1.exit_code == 0
    backup_path = Path(b1.json()["output"])
    assert backup_path.exists()
    assert b1.json()["snippet_count"] == 1

    # Add another snippet after backup
    run("snipvault", "add", "--title", "Added Later", "--content", "y = 2")
    assert run("snipvault", "list", "--json").json()["total"] == 2

    # Restore without --yes in non-interactive mode -> refused
    assert run("snipvault", "restore", str(backup_path)).exit_code == int(ExitCode.REFUSED)

    # Restore with --yes -> restores to 1 snippet and creates pre-restore safety backup
    restored = run("snipvault", "restore", str(backup_path), "--yes", "--json")
    assert restored.exit_code == 0
    r_data = restored.json()
    assert r_data["snippet_count"] == 1
    assert r_data["safety_backup"] is not None
    assert Path(r_data["safety_backup"]).exists()
    assert run("snipvault", "list", "--json").json()["total"] == 1

    # Custom backup output path
    custom_backup = tmp_path / "custom_backup.db"
    b2 = run("snipvault", "backup", "--output", str(custom_backup), "--json")
    assert b2.exit_code == 0
    assert custom_backup.exists()

    # Refuses restoring a non-SnipVault file without modifying current DB
    bad_backup = tmp_path / "not_a_db.db"
    bad_backup.write_text("plain text, not sqlite")
    assert run("snipvault", "restore", str(bad_backup), "--yes").exit_code == int(ExitCode.ERROR)
    assert run("snipvault", "list", "--json").json()["total"] == 1


def test_default_root_launches_interactive_on_tty_and_paging_works(
    run: RunFn,
) -> None:
    for i in range(1, 13):
        run(
            "snipvault",
            "add",
            "--title",
            f"Paged Snippet {i:02d}",
            "--content",
            f"x = {i}",
        )

    # Invoking `snipvault` with no subcommand when interactive=True launches interactive browser
    res = run("snipvault", interactive=True, input="n\np\nq\n")
    assert res.exit_code == 0
    assert "Page 1/2" in res.stdout
    assert "Page 2/2" in res.stdout


def test_interactive_browser_via_piped_stdin(run: RunFn, tmp_path: Path) -> None:
    run(
        "snipvault",
        "add",
        "--title",
        "Python Retry",
        "--language",
        "python",
        "--tags",
        "retry",
        "--content",
        "def retry(): pass",
    )
    run(
        "snipvault",
        "add",
        "--title",
        "Rust Error",
        "--language",
        "rust",
        "--tags",
        "errors",
        "--content",
        "enum AppError {}",
    )

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()

    # Exercise search (/Rust), select #1, toggle favorite (f), clear filters (c),
    # filter language (f python), select #1, edit title (e), copy (c), delete #2, quit (q)
    script = "\n".join(
        [
            "/Rust",
            "1",
            "f",
            "c",
            "f python",
            "1",
            "e",
            "Python Retry Updated",
            "1",
            "c",
            "c",
            "2",
            "d",
            "y",
            "q",
            "",
        ]
    )
    res = run("snipvault", "interactive", input=script, env={"PATH": str(empty_bin)})
    assert res.exit_code == 0
    assert "SnipVault Interactive Browser" in res.stdout
    assert "Python Retry Updated" in res.stdout

    remaining = run("snipvault", "list", "--json").json()
    assert remaining["total"] == 1
    assert remaining["snippets"][0]["title"] == "Python Retry Updated"


def test_config_subcommands(run: RunFn) -> None:
    show = run("snipvault", "config", "show", "--json").json()
    assert show["values"]["default_language"] == "text"
    assert run("snipvault", "config", "set", "default_language", "python").exit_code == 0
    assert run("snipvault", "config", "get", "default_language").stdout.strip() == "python"
    assert run("snipvault", "config", "reset", "--yes").exit_code == 0
