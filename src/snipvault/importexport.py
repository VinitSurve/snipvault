"""Snippet export (JSON and Markdown) and transactional all-or-nothing import."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snipvault.common.exceptions import DataError, SafetyError, UsageError
from snipvault.common.fsutil import atomic_write_text, file_kind
from snipvault.common.output import dumps_json, to_jsonable
from snipvault.common.paths import ToolPaths, ensure_not_protected, require_file
from snipvault.config import DEFAULT_LANGUAGE, DEFAULT_MAX_SNIPPET_BYTES
from snipvault.db import RESTORE_HINT, open_db
from snipvault.migrations import LATEST_SCHEMA_VERSION
from snipvault.models import (
    MAX_TITLE_LENGTH,
    ImportSummary,
    Snippet,
    compute_content_hash,
    normalize_category,
    normalize_description,
    normalize_language,
    normalize_tags,
    normalize_title,
    utc_now_iso,
    validate_content,
    validate_iso_timestamp,
)
from snipvault.repository import SnippetRepository

VALID_EXPORT_FORMATS = ("json", "markdown")
VALID_DUPLICATE_POLICIES = ("skip", "rename", "replace")
_BACKTICK_RUN_RE = re.compile(r"`+")


@dataclass(frozen=True)
class _ValidatedImportRecord:
    exported_id: int | None
    title: str
    description: str
    content: str
    language: str
    category: str
    tags: list[str]
    favorite: bool
    content_hash: str
    created_at: str
    modified_at: str


def choose_markdown_fence(content: str) -> str:
    """Choose a backtick fence strictly longer than any contiguous backtick run in ``content``."""
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(content)), default=0)
    return "`" * max(3, longest + 1)


def export_snippets_payload(snippets: Sequence[Snippet]) -> dict[str, Any]:
    """Build the structured JSON export payload."""
    return {
        "schema_version": LATEST_SCHEMA_VERSION,
        "exported_at": utc_now_iso(),
        "count": len(snippets),
        "snippets": [to_jsonable(s) for s in snippets],
    }


def export_snippets_markdown(snippets: Sequence[Snippet]) -> str:
    """Render snippets as Markdown with safe dynamic code fences."""
    lines: list[str] = ["# SnipVault Export", ""]
    if not snippets:
        lines.append("_No snippets matched the export filter._")
        lines.append("")
        return "\n".join(lines)

    for snip in snippets:
        lines.append(f"## {snip.title}")
        lines.append("")
        lines.append(f"- **ID**: {snip.id}")
        lines.append(f"- **Language**: `{snip.language}`")
        if snip.category:
            lines.append(f"- **Category**: `{snip.category}`")
        if snip.tags:
            tags_str = ", ".join(f"`{t}`" for t in snip.tags)
            lines.append(f"- **Tags**: {tags_str}")
        lines.append(f"- **Favorite**: {'yes' if snip.favorite else 'no'}")
        lines.append(f"- **Modified**: `{snip.modified_at}`")
        if snip.description:
            lines.append("")
            lines.append(snip.description)
        lines.append("")
        fence = choose_markdown_fence(snip.content)
        lang_tag = snip.language if snip.language != "text" else ""
        lines.append(f"{fence}{lang_tag}")
        lines.append(snip.content.rstrip("\n"))
        lines.append(fence)
        lines.append("")
    return "\n".join(lines)


def write_export_file(path: Path, content: str, *, force: bool = False) -> None:
    """Safely write exported content to ``path``, refusing to overwrite without ``--force``."""
    ensure_not_protected(path, "write export file")
    kind = file_kind(path)
    if kind == "symlink":
        raise SafetyError(
            f"Refusing to write export through symlink: {path}",
            hint="Choose a regular file path instead.",
        )
    if kind == "dir":
        raise UsageError(f"Export output path is a directory: {path}")
    if path.exists() and not force:
        raise SafetyError(
            f"Output file already exists: {path}",
            hint="Re-run with --force to overwrite the existing file.",
        )
    atomic_write_text(path, content, mode=0o600)


def format_export_json(snippets: Sequence[Snippet]) -> str:
    """Return formatted JSON string for export."""
    return dumps_json(export_snippets_payload(snippets)) + "\n"


def _validate_import_item(
    raw: Any,
    idx: int,
    *,
    max_snippet_bytes: int,
    default_language: str,
) -> _ValidatedImportRecord:
    if not isinstance(raw, dict):
        raise DataError(
            f"Invalid snippet at index {idx}: expected a JSON object, got {type(raw).__name__}.",
            hint="Each entry in 'snippets' must be a JSON object.",
        )

    exported_id: int | None = None
    if "id" in raw and raw["id"] is not None:
        raw_id = raw["id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
            raise DataError(
                f"Invalid snippet at index {idx}: 'id' must be a positive integer.",
                hint="Fix or remove the invalid 'id' field in the import file.",
            )
        exported_id = raw_id

    if "title" not in raw:
        raise DataError(
            f"Invalid snippet at index {idx}: missing required field 'title'.",
            hint="Every snippet must include 'title' and 'content'.",
        )
    if "content" not in raw:
        raise DataError(
            f"Invalid snippet at index {idx}: missing required field 'content'.",
            hint="Every snippet must include 'title' and 'content'.",
        )

    try:
        title = normalize_title(raw["title"], error_cls=DataError)
        content = validate_content(raw["content"], max_bytes=max_snippet_bytes, error_cls=DataError)
        language = normalize_language(
            raw.get("language"), default=default_language, error_cls=DataError
        )
        category = normalize_category(raw.get("category"), error_cls=DataError)
        description = normalize_description(raw.get("description"), error_cls=DataError)
    except DataError as exc:
        raise DataError(
            f"Invalid snippet at index {idx} ({raw.get('title', 'untitled')!r}): {exc.message}",
            hint=exc.hint or "Fix the invalid field in the import file and retry.",
        ) from exc

    raw_tags = raw.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise DataError(
            f"Invalid snippet at index {idx} ({title!r}): 'tags' must be a list of strings.",
            hint='Use a JSON array of strings such as ["python", "cli"].',
        )
    tags = normalize_tags(raw_tags, error_cls=DataError)

    raw_fav = raw.get("favorite", False)
    if not isinstance(raw_fav, bool):
        raise DataError(
            f"Invalid snippet at index {idx} ({title!r}): 'favorite' must be a boolean.",
            hint="Use true or false for the 'favorite' field.",
        )

    now = utc_now_iso()
    created_at = (
        validate_iso_timestamp(raw["created_at"], field_name="created_at")
        if "created_at" in raw and raw["created_at"] is not None
        else now
    )
    modified_at = (
        validate_iso_timestamp(raw["modified_at"], field_name="modified_at")
        if "modified_at" in raw and raw["modified_at"] is not None
        else created_at
    )

    computed_hash = compute_content_hash(content)
    if (
        "content_hash" in raw
        and raw["content_hash"] is not None
        and not isinstance(raw["content_hash"], str)
    ):
        raise DataError(
            f"Invalid snippet at index {idx} ({title!r}): 'content_hash' must be a string."
        )

    return _ValidatedImportRecord(
        exported_id=exported_id,
        title=title,
        description=description,
        content=content,
        language=language,
        category=category,
        tags=tags,
        favorite=raw_fav,
        content_hash=computed_hash,
        created_at=created_at,
        modified_at=modified_at,
    )


def _unique_renamed_title(conn: sqlite3.Connection, base_title: str) -> str:
    """Generate a non-colliding ``Title (1)``, ``Title (2)`` within ``MAX_TITLE_LENGTH``."""
    counter = 1
    while True:
        suffix = f" ({counter})"
        trimmed_base = base_title[: MAX_TITLE_LENGTH - len(suffix)].rstrip()
        candidate = f"{trimmed_base}{suffix}"
        row = conn.execute(
            "SELECT 1 FROM snippets WHERE LOWER(TRIM(title)) = LOWER(?) LIMIT 1",
            (candidate,),
        ).fetchone()
        if row is None:
            return candidate
        counter += 1


def _find_duplicate_id(conn: sqlite3.Connection, rec: _ValidatedImportRecord) -> int | None:
    """Check whether ``rec`` matches an existing snippet by (title+hash) or (exported_id+hash)."""
    row = conn.execute(
        """
        SELECT id FROM snippets
        WHERE LOWER(TRIM(title)) = LOWER(?) AND content_hash = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (rec.title, rec.content_hash),
    ).fetchone()
    if row is not None:
        return int(row["id"])

    if rec.exported_id is not None:
        row2 = conn.execute(
            """
            SELECT id FROM snippets
            WHERE id = ? AND content_hash = ?
            LIMIT 1
            """,
            (rec.exported_id, rec.content_hash),
        ).fetchone()
        if row2 is not None:
            return int(row2["id"])

    return None


def import_snippets_file(
    paths: ToolPaths,
    file_path: Path,
    *,
    on_duplicate: str = "skip",
    max_snippet_bytes: int = DEFAULT_MAX_SNIPPET_BYTES,
    default_language: str = DEFAULT_LANGUAGE,
) -> ImportSummary:
    """Validate and import snippets from a JSON file in a single all-or-nothing transaction."""
    policy = on_duplicate.strip().lower()
    if policy not in VALID_DUPLICATE_POLICIES:
        raise UsageError(
            f"Invalid --on-duplicate policy: {on_duplicate!r}",
            hint=f"Allowed values: {', '.join(VALID_DUPLICATE_POLICIES)}.",
        )

    require_file(file_path, what="Import file")
    if file_kind(file_path) == "symlink":
        raise SafetyError(
            f"Refusing to import from symlink: {file_path}",
            hint="Pass the path to a regular JSON file.",
        )

    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DataError(
            f"Cannot read import file {file_path} as UTF-8 text: {exc}",
            hint="Ensure the file exists, is readable, and uses UTF-8 encoding.",
        ) from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DataError(
            f"Import file {file_path} is not valid JSON ({exc.msg} at line {exc.lineno}).",
            hint="Fix the JSON syntax or generate a fresh file with 'snipvault export'.",
        ) from exc

    raw_snippets: list[Any]
    if isinstance(payload, dict):
        if "schema_version" in payload:
            sv = payload["schema_version"]
            if isinstance(sv, bool) or not isinstance(sv, int) or sv < 1:
                raise DataError(
                    f"Invalid 'schema_version' in {file_path}: {sv!r}",
                    hint="Expected a positive integer schema_version.",
                )
            if sv > LATEST_SCHEMA_VERSION:
                raise DataError(
                    f"Import file schema_version {sv} is newer than supported "
                    f"version {LATEST_SCHEMA_VERSION}.",
                    hint="Upgrade snipvault before importing this file.",
                )
        if "snippets" not in payload or not isinstance(payload["snippets"], list):
            raise DataError(
                f"Import file {file_path} must contain a 'snippets' array.",
                hint='Expected JSON structure: {"snippets": [...]}.',
            )
        raw_snippets = payload["snippets"]
    elif isinstance(payload, list):
        raw_snippets = payload
    else:
        raise DataError(
            f"Import file {file_path} must contain a JSON object or array.",
            hint='Expected JSON structure: {"snippets": [...]}.',
        )

    # All-or-nothing pre-validation BEFORE touching the database
    validated: list[_ValidatedImportRecord] = [
        _validate_import_item(
            item,
            idx,
            max_snippet_bytes=max_snippet_bytes,
            default_language=default_language,
        )
        for idx, item in enumerate(raw_snippets)
    ]

    if not validated:
        return ImportSummary(
            file=str(file_path),
            on_duplicate=policy,
            imported=0,
            skipped=0,
            replaced=0,
            renamed=0,
            failed=0,
            imported_ids=[],
        )

    conn = open_db(paths, create=True)
    if conn is None:  # pragma: no cover
        raise DataError("Database could not be opened.", hint=RESTORE_HINT)

    imported_count = 0
    skipped_count = 0
    replaced_count = 0
    renamed_count = 0
    imported_ids: list[int] = []

    try:
        with conn:
            for rec in validated:
                dup_id = _find_duplicate_id(conn, rec)
                if dup_id is not None:
                    if policy == "skip":
                        skipped_count += 1
                        continue
                    if policy == "replace":
                        new_mod = utc_now_iso(after=rec.modified_at)
                        conn.execute(
                            """
                            UPDATE snippets
                            SET title = ?, description = ?, content = ?, language = ?,
                                category = ?, favorite = ?, content_hash = ?, modified_at = ?
                            WHERE id = ?
                            """,
                            (
                                rec.title,
                                rec.description,
                                rec.content,
                                rec.language,
                                rec.category,
                                1 if rec.favorite else 0,
                                rec.content_hash,
                                new_mod,
                                dup_id,
                            ),
                        )
                        SnippetRepository._set_snippet_tags(conn, dup_id, rec.tags)
                        imported_count += 1
                        replaced_count += 1
                        imported_ids.append(dup_id)
                        continue
                    # policy == "rename"
                    target_title = _unique_renamed_title(conn, rec.title)
                    renamed_count += 1
                else:
                    target_title = rec.title

                cur = conn.execute(
                    """
                    INSERT INTO snippets (
                        title, description, content, language, category,
                        favorite, content_hash, created_at, modified_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_title,
                        rec.description,
                        rec.content,
                        rec.language,
                        rec.category,
                        1 if rec.favorite else 0,
                        rec.content_hash,
                        rec.created_at,
                        rec.modified_at,
                    ),
                )
                new_id = int(cur.lastrowid or 0)
                SnippetRepository._set_snippet_tags(conn, new_id, rec.tags)
                imported_count += 1
                imported_ids.append(new_id)
    except sqlite3.DatabaseError as exc:
        raise DataError(f"Database import transaction failed: {exc}", hint=RESTORE_HINT) from exc
    finally:
        conn.close()

    return ImportSummary(
        file=str(file_path),
        on_duplicate=policy,
        imported=imported_count,
        skipped=skipped_count,
        replaced=replaced_count,
        renamed=renamed_count,
        failed=0,
        imported_ids=imported_ids,
    )
