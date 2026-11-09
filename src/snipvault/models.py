"""Domain models and validation helpers for snipvault."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snipvault.common.exceptions import DataError, UsageError

MAX_TITLE_LENGTH = 200
MAX_CATEGORY_LENGTH = 100
MAX_LANGUAGE_LENGTH = 64
MAX_TAG_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 10_000

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
    ".lua": "lua",
    ".r": "r",
}

LANGUAGE_EXTENSION_MAP: dict[str, str] = {
    "python": ".py",
    "typescript": ".ts",
    "tsx": ".tsx",
    "javascript": ".js",
    "jsx": ".jsx",
    "bash": ".sh",
    "sh": ".sh",
    "zsh": ".zsh",
    "sql": ".sql",
    "rust": ".rs",
    "go": ".go",
    "ruby": ".rb",
    "java": ".java",
    "kotlin": ".kt",
    "swift": ".swift",
    "c": ".c",
    "cpp": ".cpp",
    "csharp": ".cs",
    "php": ".php",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "yaml": ".yaml",
    "toml": ".toml",
    "markdown": ".md",
}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Snippet:
    """A stored code snippet."""

    id: int
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


@dataclass(frozen=True)
class SearchHit:
    """One ranked search match."""

    snippet: Snippet
    score: float
    matched_excerpt: str


@dataclass(frozen=True)
class SearchResult:
    """Outcome of a snippet search."""

    query: str
    fts_enabled: bool
    note: str | None
    total: int
    hits: list[SearchHit]

    def to_payload(self) -> dict[str, Any]:
        """Serialize search results with convenient top-level keys."""
        from snipvault.common.output import to_jsonable

        snippets_out: list[dict[str, Any]] = []
        hits_out: list[dict[str, Any]] = []
        for hit in self.hits:
            snip_dict = dict(to_jsonable(hit.snippet))
            snip_with_meta = {
                **snip_dict,
                "score": hit.score,
                "matched_excerpt": hit.matched_excerpt,
            }
            snippets_out.append(snip_with_meta)
            hits_out.append(
                {
                    "id": hit.snippet.id,
                    "title": hit.snippet.title,
                    "score": hit.score,
                    "matched_excerpt": hit.matched_excerpt,
                    "snippet": snip_dict,
                }
            )
        return {
            "query": self.query,
            "fts_enabled": self.fts_enabled,
            "note": self.note,
            "total": self.total,
            "snippets": snippets_out,
            "hits": hits_out,
            "results": snippets_out,
        }


@dataclass(frozen=True)
class ListResult:
    """Outcome of listing snippets with filters and pagination."""

    total: int
    offset: int
    limit: int
    sort: str
    snippets: list[Snippet]

    def to_payload(self) -> dict[str, Any]:
        from snipvault.common.output import to_jsonable

        items = [to_jsonable(s) for s in self.snippets]
        return {
            "total": self.total,
            "count": len(items),
            "offset": self.offset,
            "limit": self.limit,
            "sort": self.sort,
            "snippets": items,
        }


@dataclass(frozen=True)
class ImportSummary:
    """Outcome of a transactional snippet import."""

    file: str
    on_duplicate: str
    imported: int
    skipped: int
    replaced: int
    renamed: int
    failed: int
    imported_ids: list[int]


@dataclass(frozen=True)
class BackupResult:
    """Outcome of a database backup operation."""

    source: str
    output: str
    size_bytes: int
    snippet_count: int
    schema_version: int


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of a database restore operation."""

    restored_from: str
    target: str
    safety_backup: str | None
    snippet_count: int
    schema_version: int


def utc_now_iso(*, after: str | None = None) -> str:
    """Return an ISO-8601 UTC timestamp, guaranteed strictly greater than ``after`` if given."""
    now = dt.datetime.now(dt.UTC)
    if after:
        try:
            prev = dt.datetime.fromisoformat(after.replace("Z", "+00:00"))
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=dt.UTC)
            if now <= prev:
                now = prev + dt.timedelta(microseconds=1)
        except ValueError:
            pass
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_content_hash(content: str) -> str:
    """Return the SHA-256 hex digest of UTF-8 encoded snippet content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_title(
    title: str | None, *, error_cls: type[UsageError | DataError] = UsageError
) -> str:
    """Validate and normalize a snippet title."""
    if not isinstance(title, str):
        raise error_cls("Snippet title must be a string.")
    if "\x00" in title or _CONTROL_CHAR_RE.search(title):
        raise error_cls("Snippet title cannot contain control characters or null bytes.")
    cleaned = title.strip()
    if not cleaned:
        raise error_cls(
            "Snippet title cannot be empty.",
            hint="Provide a non-empty --title (up to 200 characters).",
        )
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise error_cls(
            f"Snippet title exceeds {MAX_TITLE_LENGTH} characters (got {len(cleaned)}).",
            hint=f"Shorten the title to {MAX_TITLE_LENGTH} characters or fewer.",
        )
    return cleaned


def validate_content(
    content: str | None,
    *,
    max_bytes: int,
    error_cls: type[UsageError | DataError] = UsageError,
) -> str:
    """Validate snippet content is non-empty, contains no null bytes, and fits within size limit."""
    if not isinstance(content, str):
        raise error_cls("Snippet content must be a string.")
    if "\x00" in content:
        raise error_cls("Snippet content cannot contain null bytes.")
    if not content.strip():
        raise error_cls(
            "Snippet content cannot be empty.",
            hint="Provide snippet content via --content, --file, stdin, or interactive prompt.",
        )
    byte_len = len(content.encode("utf-8"))
    if byte_len > max_bytes:
        raise error_cls(
            f"Snippet content size ({byte_len} bytes) exceeds maximum allowed ({max_bytes} bytes).",
            hint="Increase the limit with 'snipvault config set max_snippet_bytes <bytes>'.",
        )
    return content


def normalize_language(
    language: str | None,
    *,
    default: str = "text",
    file_path: Path | None = None,
    error_cls: type[UsageError | DataError] = UsageError,
) -> str:
    """Normalize language name; infer from file extension if omitted."""
    if language is not None and not isinstance(language, str):
        raise error_cls("Snippet language must be a string.")
    raw = (language or "").strip().lower()
    if not raw and file_path is not None:
        ext = file_path.suffix.lower()
        if ext in EXTENSION_LANGUAGE_MAP:
            raw = EXTENSION_LANGUAGE_MAP[ext]
        elif file_path.name.lower() == "dockerfile":
            raw = "dockerfile"
    if not raw:
        raw = (default or "text").strip().lower() or "text"
    if "\x00" in raw or "\n" in raw or "\r" in raw or "`" in raw:
        raise error_cls("Snippet language contains invalid characters.")
    if len(raw) > MAX_LANGUAGE_LENGTH:
        raise error_cls(f"Snippet language exceeds {MAX_LANGUAGE_LENGTH} characters.")
    return raw


def normalize_category(
    category: str | None, *, error_cls: type[UsageError | DataError] = UsageError
) -> str:
    """Validate and normalize snippet category."""
    if category is None:
        return ""
    if not isinstance(category, str):
        raise error_cls("Snippet category must be a string.")
    if "\x00" in category or _CONTROL_CHAR_RE.search(category):
        raise error_cls("Snippet category cannot contain control characters.")
    cleaned = category.strip()
    if len(cleaned) > MAX_CATEGORY_LENGTH:
        raise error_cls(f"Snippet category exceeds {MAX_CATEGORY_LENGTH} characters.")
    return cleaned


def normalize_description(
    description: str | None, *, error_cls: type[UsageError | DataError] = UsageError
) -> str:
    """Validate and normalize snippet description."""
    if description is None:
        return ""
    if not isinstance(description, str):
        raise error_cls("Snippet description must be a string.")
    if "\x00" in description:
        raise error_cls("Snippet description cannot contain null bytes.")
    cleaned = description.strip()
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise error_cls(f"Snippet description exceeds {MAX_DESCRIPTION_LENGTH} characters.")
    return cleaned


def normalize_tags(
    raw_tags: Sequence[str] | str | None,
    *,
    error_cls: type[UsageError | DataError] = UsageError,
) -> list[str]:
    """Normalize tags: split on commas, trim whitespace, lowercase, and deduplicate."""
    if raw_tags is None:
        return []
    items: list[str] = []
    if isinstance(raw_tags, str):
        items = raw_tags.split(",")
    elif isinstance(raw_tags, Sequence):
        for elem in raw_tags:
            if not isinstance(elem, str):
                raise error_cls("Every tag must be a string.")
            items.extend(elem.split(","))
    else:
        raise error_cls("Tags must be a comma-separated string or a list of strings.")

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if "\x00" in item or _CONTROL_CHAR_RE.search(item):
            raise error_cls("Tags cannot contain control characters or null bytes.")
        tag = item.strip().lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise error_cls(f"Tag {tag!r} exceeds maximum length of {MAX_TAG_LENGTH} characters.")
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def validate_iso_timestamp(value: Any, *, field_name: str = "timestamp") -> str:
    """Validate an ISO-8601 timestamp string and normalize to UTC string."""
    if not isinstance(value, str) or not value.strip():
        raise DataError(f"Field {field_name!r} must be a non-empty ISO-8601 timestamp string.")
    raw = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataError(f"Field {field_name!r} is not a valid ISO-8601 timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    else:
        parsed = parsed.astimezone(dt.UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
