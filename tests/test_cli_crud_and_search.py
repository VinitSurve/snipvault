"""Functional CLI tests for snipvault add, list, show, edit, delete, favorite, and search."""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from snipvault import db as snipvault_db
from snipvault.common.exceptions import ExitCode

RunFn = Callable[..., Any]


def test_root_no_args_prints_help_when_non_interactive(run: RunFn) -> None:
    res = run("snipvault")
    assert res.exit_code == 0
    assert "snipvault" in res.stdout
    assert "search" in res.stdout


def test_read_commands_do_not_create_db_or_data_dir(run: RunFn, tmp_path: Path) -> None:
    data_dir = tmp_path / "dtk" / "data" / "snipvault"
    assert not data_dir.exists()

    res_list = run("snipvault", "list", "--json")
    assert res_list.exit_code == 0
    assert res_list.json()["total"] == 0
    assert not data_dir.exists()

    res_search = run("snipvault", "search", "anything", "--json")
    assert res_search.exit_code == 0
    assert res_search.json()["total"] == 0
    assert not data_dir.exists()

    res_show = run("snipvault", "show", "1")
    assert res_show.exit_code == int(ExitCode.ERROR)
    assert not data_dir.exists()


def test_add_via_content_file_stdin_and_interactive(run: RunFn, tmp_path: Path) -> None:
    # 1. Add via --content with tag normalization
    r1 = run(
        "snipvault",
        "add",
        "--title",
        "Supabase Auth",
        "--language",
        "TypeScript",
        "--tags",
        " Supabase, AUTH , supabase , jwt ",
        "--category",
        "backend",
        "--description",
        "Verify JWT session",
        "--favorite",
        "--content",
        "const user = await supabase.auth.getUser(token);",
        "--json",
    )
    assert r1.exit_code == 0
    s1 = r1.json()
    assert s1["id"] == 1
    assert s1["title"] == "Supabase Auth"
    assert s1["language"] == "typescript"
    assert s1["tags"] == ["supabase", "auth", "jwt"]
    assert s1["favorite"] is True

    # Verify DB file has 0600 permissions
    db_file = tmp_path / "dtk" / "data" / "snipvault" / "snippets.db"
    assert db_file.exists()
    assert (db_file.stat().st_mode & 0o077) == 0

    # 2. Add via --file (infers language and defaults title to filename)
    py_file = tmp_path / "helper_util.py"
    py_file.write_text("def greet(name: str) -> str:\n    return f'Hello {name}'\n")
    r2 = run("snipvault", "add", "--file", str(py_file), "--tags", "python,util", "--json")
    assert r2.exit_code == 0
    s2 = r2.json()
    assert s2["id"] == 2
    assert s2["title"] == "helper_util.py"
    assert s2["language"] == "python"

    # 3. Add via piped stdin
    r3 = run(
        "snipvault",
        "add",
        "--title",
        "Piped SQL Query",
        "--language",
        "sql",
        "--json",
        input="SELECT id, email FROM users WHERE active = 1;\n",
    )
    assert r3.exit_code == 0
    assert r3.json()["id"] == 3
    assert "SELECT id, email" in r3.json()["content"]

    # 4. Add via interactive TTY prompt until EOF marker
    interactive_input = (
        "Interactive Bash\nbash\nshell,ops\nEcho greeting\n"
        "echo 'hello world'\nEOF\nignored after eof\n"
    )
    r4 = run(
        "snipvault",
        "add",
        "--json",
        interactive=True,
        input=interactive_input,
    )
    assert r4.exit_code == 0
    s4 = r4.json()
    assert s4["id"] == 4
    assert s4["title"] == "Interactive Bash"
    assert s4["language"] == "bash"
    assert s4["tags"] == ["shell", "ops"]
    assert s4["content"].strip() == "echo 'hello world'"


def test_add_validation_and_size_limits(run: RunFn) -> None:
    # Non-interactive without content source -> usage error (never hangs)
    res_no_content = run("snipvault", "add", "--title", "Missing Content")
    assert res_no_content.exit_code == int(ExitCode.USAGE)

    # Non-interactive stdin without --title -> usage error
    res_no_title = run("snipvault", "add", input="print(1)\n")
    assert res_no_title.exit_code == int(ExitCode.USAGE)

    # Both --content and --file -> usage error
    res_both = run("snipvault", "add", "--title", "Both", "--content", "a", "--file", "b.py")
    assert res_both.exit_code == int(ExitCode.USAGE)

    # Title > 200 chars -> usage error
    res_long_title = run("snipvault", "add", "--title", "T" * 201, "--content", "print(1)")
    assert res_long_title.exit_code == int(ExitCode.USAGE)

    # Configurable max_snippet_bytes limit
    assert run("snipvault", "config", "set", "max_snippet_bytes", "50").exit_code == 0
    res_too_big = run("snipvault", "add", "--title", "Big", "--content", "x" * 60)
    assert res_too_big.exit_code == int(ExitCode.USAGE)
    assert "exceeds maximum" in res_too_big.stderr


def test_list_filters_sorting_and_pagination(run: RunFn) -> None:
    run(
        "snipvault",
        "add",
        "--title",
        "Bravo",
        "--language",
        "python",
        "--tags",
        "alpha,shared",
        "--category",
        "cat1",
        "--content",
        "b = 2",
    )
    run(
        "snipvault",
        "add",
        "--title",
        "Alpha",
        "--language",
        "rust",
        "--tags",
        "beta,shared",
        "--category",
        "cat2",
        "--favorite",
        "--content",
        "let a = 1;",
    )
    run(
        "snipvault",
        "add",
        "--title",
        "Charlie",
        "--language",
        "python",
        "--tags",
        "gamma,shared",
        "--category",
        "cat1",
        "--content",
        "c = 3",
    )

    # Sort by title
    by_title = run("snipvault", "list", "--sort", "title", "--json").json()
    assert [s["title"] for s in by_title["snippets"]] == ["Alpha", "Bravo", "Charlie"]

    # Pagination (--limit and --offset)
    page = run(
        "snipvault", "list", "--sort", "title", "--limit", "1", "--offset", "1", "--json"
    ).json()
    assert page["total"] == 3
    assert len(page["snippets"]) == 1
    assert page["snippets"][0]["title"] == "Bravo"

    # Filter by language + category
    py_list = run(
        "snipvault", "list", "--language", "python", "--category", "cat1", "--json"
    ).json()
    assert py_list["total"] == 2

    # Filter by repeatable tags
    tagged = run("snipvault", "list", "--tag", "shared", "--tag", "beta", "--json").json()
    assert tagged["total"] == 1
    assert tagged["snippets"][0]["title"] == "Alpha"

    # Filter by favorites
    favs = run("snipvault", "list", "--favorites", "--json").json()
    assert favs["total"] == 1
    assert favs["snippets"][0]["title"] == "Alpha"

    # Invalid sort
    assert run("snipvault", "list", "--sort", "bogus").exit_code == int(ExitCode.USAGE)


def test_show_edit_favorite_and_delete(run: RunFn, tmp_path: Path) -> None:
    run(
        "snipvault",
        "add",
        "--title",
        "Original Title",
        "--language",
        "python",
        "--tags",
        "one,two",
        "--content",
        "print('v1')",
    )

    # show human, --raw, and --json
    res_human = run("snipvault", "show", "1")
    assert res_human.exit_code == 0
    assert "Original Title" in res_human.stdout
    assert "print('v1')" in res_human.stdout

    res_raw = run("snipvault", "show", "1", "--raw")
    assert res_raw.exit_code == 0
    assert res_raw.stdout.strip() == "print('v1')"

    orig = run("snipvault", "show", "1", "--json").json()
    orig_modified = orig["modified_at"]

    # edit with identical title -> modified_at must NOT change
    unchanged = run("snipvault", "edit", "1", "--title", "Original Title", "--json").json()
    assert unchanged["changed"] is False
    assert unchanged["modified_at"] == orig_modified

    # edit fields and tags -> modified_at MUST update
    updated = run(
        "snipvault",
        "edit",
        "1",
        "--title",
        "Renamed Title",
        "--add-tag",
        "three",
        "--remove-tag",
        "one",
        "--category",
        "core",
        "--description",
        "Updated desc",
        "--favorite",
        "--json",
    ).json()
    assert updated["changed"] is True
    assert updated["title"] == "Renamed Title"
    assert updated["tags"] == ["two", "three"]
    assert updated["favorite"] is True
    assert updated["modified_at"] > orig_modified

    # non-interactive edit with no flags -> usage error
    assert run("snipvault", "edit", "1").exit_code == int(ExitCode.USAGE)

    # edit via external $EDITOR in interactive mode (using shlex-split script)
    editor_script = tmp_path / "fake_editor.py"
    editor_script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "path = sys.argv[-1]\n"
        "# Verify private 0600 permissions on temp file\n"
        "assert (os.stat(path).st_mode & 0o077) == 0\n"
        "with open(path, 'w', encoding='utf-8') as f:\n"
        "    f.write(\"print('edited by external editor')\\n\")\n"
    )
    editor_script.chmod(editor_script.stat().st_mode | stat.S_IXUSR)

    ed_res = run(
        "snipvault",
        "edit",
        "1",
        "--json",
        interactive=True,
        env={"EDITOR": f'"{editor_script}" --flag'},
    )
    assert ed_res.exit_code == 0
    assert "edited by external editor" in ed_res.json()["content"]

    # favorite / unfavorite commands
    assert (
        run(
            "snipvault",
            "unfavorite",
            "1",
            "--json",
            env={"DEVTOOLKIT_FORCE_INTERACTIVE": "0"},
        ).json()["favorite"]
        is False
    )
    assert run("snipvault", "favorite", "1", "--json").json()["favorite"] is True

    # delete without --yes in non-interactive mode -> refused (ExitCode.REFUSED = 3)
    refused = run("snipvault", "delete", "1", env={"DEVTOOLKIT_FORCE_INTERACTIVE": "0"})
    assert refused.exit_code == int(ExitCode.REFUSED)

    # delete with --yes succeeds
    deleted = run("snipvault", "delete", "1", "--yes")
    assert deleted.exit_code == 0
    assert "Renamed Title" in deleted.stdout
    assert run("snipvault", "show", "1").exit_code == int(ExitCode.ERROR)


def test_search_fts5_bm25_title_boost_prefix_and_fallback(
    run: RunFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Snippet 1 has "kubernetes" only in content
    run(
        "snipvault",
        "add",
        "--title",
        "Cluster Notes",
        "--language",
        "yaml",
        "--tags",
        "infra",
        "--content",
        "Deploy pods to kubernetes cluster in production.",
    )
    # Snippet 2 has "kubernetes" in the title (should rank #1 due to title boost)
    run(
        "snipvault",
        "add",
        "--title",
        "Kubernetes Pod Rollout",
        "--language",
        "bash",
        "--tags",
        "k8s,devops",
        "--category",
        "deployment",
        "--description",
        "Rollout restart command",
        "--content",
        "kubectl rollout restart deployment/api",
    )

    # Prefix search ("kubernet" matches "kubernetes") + BM25 title boost
    res = run("snipvault", "search", "kubernet", "--json")
    assert res.exit_code == 0
    data = res.json()
    assert data["fts_enabled"] is True
    assert data["total"] == 2
    assert data["snippets"][0]["title"] == "Kubernetes Pod Rollout"
    assert data["snippets"][1]["title"] == "Cluster Notes"

    # Search by tag, description, or category
    assert run("snipvault", "search", "devops", "--json").json()["total"] == 1
    assert run("snipvault", "search", "Rollout restart", "--json").json()["total"] == 1

    # Filter-only search with no query (`snipvault search --language bash`)
    no_q = run("snipvault", "search", "--language", "bash", "--json").json()
    assert no_q["total"] == 1
    assert no_q["snippets"][0]["title"] == "Kubernetes Pod Rollout"

    # Simulate SQLite compiled without FTS5 -> LIKE fallback with clear note
    monkeypatch.setattr(snipvault_db, "_FORCE_DISABLE_FTS5", True)
    fallback_res = run("snipvault", "search", "kubernetes")
    assert fallback_res.exit_code == 0
    assert "LIKE fallback" in fallback_res.stderr
    assert "Kubernetes Pod Rollout" in fallback_res.stdout

    fallback_json = run("snipvault", "search", "kubernetes", "--json").json()
    assert fallback_json["fts_enabled"] is False
    assert "LIKE fallback" in fallback_json["note"]
    assert fallback_json["total"] == 2
    assert fallback_json["snippets"][0]["title"] == "Kubernetes Pod Rollout"
