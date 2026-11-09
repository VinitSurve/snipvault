"""Unit tests for snipvault models, validation, FTS query builder, and markdown fences."""

from __future__ import annotations

from pathlib import Path

import pytest

from snipvault.common.exceptions import DataError, UsageError
from snipvault.importexport import choose_markdown_fence
from snipvault.models import (
    MAX_TITLE_LENGTH,
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
from snipvault.search import (
    build_fts_query,
    build_matched_excerpt,
    escape_like_pattern,
    extract_query_terms,
)


def test_normalize_title_validation() -> None:
    assert normalize_title("  Hello World  ") == "Hello World"
    with pytest.raises(UsageError):
        normalize_title("")
    with pytest.raises(UsageError):
        normalize_title("   ")
    with pytest.raises(UsageError):
        normalize_title("x" * (MAX_TITLE_LENGTH + 1))
    with pytest.raises(UsageError):
        normalize_title("bad\x00title")
    with pytest.raises(DataError):
        normalize_title(123, error_cls=DataError)  # type: ignore[arg-type]


def test_validate_content_limits() -> None:
    assert validate_content("print('ok')", max_bytes=100) == "print('ok')"
    with pytest.raises(UsageError):
        validate_content("   \n\t  ", max_bytes=100)
    with pytest.raises(UsageError):
        validate_content("has\x00null", max_bytes=100)
    with pytest.raises(UsageError, match="exceeds maximum"):
        validate_content("a" * 51, max_bytes=50)


def test_normalize_language_and_inference() -> None:
    assert normalize_language("  TypeScript ") == "typescript"
    assert normalize_language(None, default="python") == "python"
    assert normalize_language(None, file_path=Path("script.py")) == "python"
    assert normalize_language(None, file_path=Path("Dockerfile")) == "dockerfile"
    with pytest.raises(UsageError):
        normalize_language("bad`lang")


def test_normalize_tags_dedupes_and_lowercases() -> None:
    assert normalize_tags(" Python, CLI , python,  ,Auth,cli ") == ["python", "cli", "auth"]
    assert normalize_tags(["Supabase, Auth", "AUTH", "db"]) == ["supabase", "auth", "db"]
    assert normalize_tags(None) == []
    with pytest.raises(UsageError):
        normalize_tags("tag\x00bad")


def test_normalize_category_and_description() -> None:
    assert normalize_category("  backend ") == "backend"
    assert normalize_category(None) == ""
    assert normalize_description("  useful snippet ") == "useful snippet"
    assert normalize_description(None) == ""
    with pytest.raises(UsageError):
        normalize_category("x" * 101)


def test_utc_now_iso_monotonicity_and_validation() -> None:
    t1 = utc_now_iso()
    t2 = utc_now_iso(after=t1)
    assert t2 > t1
    future = "2099-01-01T00:00:00.000000Z"
    t3 = utc_now_iso(after=future)
    assert t3 > future
    assert validate_iso_timestamp("2026-11-09T12:30:00Z").endswith("Z")
    with pytest.raises(DataError):
        validate_iso_timestamp("not-a-timestamp")
    with pytest.raises(DataError):
        validate_iso_timestamp("")


def test_build_fts_query_escapes_special_syntax() -> None:
    assert build_fts_query("") == ""
    assert build_fts_query("   ") == ""
    assert build_fts_query("supabase auth") == '"supabase" "auth"*'
    assert build_fts_query('foo"bar') == '"foo""bar"*'
    assert build_fts_query("AND") == '"AND"*'
    assert build_fts_query("*") == '"*"'
    assert build_fts_query("NEAR(") == '"NEAR("*'
    assert build_fts_query("🚀") == '"🚀"*'


def test_extract_query_terms_and_like_escape() -> None:
    terms = extract_query_terms('foo"bar auth')
    assert "auth" in terms
    assert "foo" in terms
    assert escape_like_pattern(r"100%_path\to") == r"100\%\_path\\to"


def test_build_matched_excerpt() -> None:
    snip = Snippet(
        id=1,
        title="Demo",
        description="A fallback description match",
        content="line one\nconst secretTarget = 42;\nline three",
        language="typescript",
        category="",
        tags=[],
        favorite=False,
        content_hash=compute_content_hash("x"),
        created_at="2026-01-01T00:00:00Z",
        modified_at="2026-01-01T00:00:00Z",
    )
    assert "secretTarget" in build_matched_excerpt(snip, "secretTarget")
    long_line = "prefix_" + ("a" * 100) + "_NEEDLE_" + ("b" * 100)
    snip_long = Snippet(
        id=2,
        title="Long",
        description="",
        content=long_line,
        language="text",
        category="",
        tags=[],
        favorite=False,
        content_hash=compute_content_hash(long_line),
        created_at="2026-01-01T00:00:00Z",
        modified_at="2026-01-01T00:00:00Z",
    )
    excerpt = build_matched_excerpt(snip_long, "NEEDLE", max_len=60)
    assert "NEEDLE" in excerpt
    assert len(excerpt) <= 66


def test_choose_markdown_fence_exceeds_backtick_runs() -> None:
    assert choose_markdown_fence("print('hi')") == "```"
    assert choose_markdown_fence("Use ```python\ncode\n``` inside") == "````"
    assert choose_markdown_fence("Nested ```` four backticks ```` here") == "`````"
