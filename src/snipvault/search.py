"""Safe FTS5 query construction, LIKE escaping, and snippet excerpt highlighting."""

from __future__ import annotations

import re
import unicodedata

from snipvault.models import Snippet

_WORD_RE = re.compile(r"\w+", flags=re.UNICODE)


def _is_fts_token_char(ch: str) -> bool:
    """True if ``ch`` is indexed as a token character by our unicode61 configuration."""
    if ch.isalnum() or ch == "_":
        return True
    return unicodedata.category(ch) in ("So", "Co")


def build_fts_query(raw_query: str | None) -> str:
    """Escape arbitrary user text into a safe SQLite FTS5 query string.

    * Every whitespace-separated token is enclosed in double quotes with internal
      double quotes doubled (``"`` -> ``""``), neutralizing FTS5 operators such as
      ``AND``, ``OR``, ``NOT``, ``NEAR(``, ``*``, ``^``, column filters ``col:``, etc.
    * The last token receives a trailing ``*`` for prefix matching when it contains
      at least one indexable token character.
    """
    if not raw_query:
        return ""
    cleaned = raw_query.replace("\x00", " ").strip()
    if not cleaned:
        return ""
    tokens = cleaned.split()
    parts: list[str] = []
    for idx, token in enumerate(tokens):
        escaped = '"' + token.replace('"', '""') + '"'
        if idx == len(tokens) - 1 and any(_is_fts_token_char(c) for c in token):
            escaped += "*"
        parts.append(escaped)
    return " ".join(parts)


def extract_query_terms(raw_query: str | None) -> list[str]:
    """Extract search terms from a user query for highlighting and LIKE matching."""
    if not raw_query:
        return []
    cleaned = raw_query.replace("\x00", " ").strip()
    if not cleaned:
        return []
    seen: set[str] = set()
    terms: list[str] = []
    for tok in cleaned.split():
        low = tok.lower()
        if low and low not in seen:
            seen.add(low)
            terms.append(tok)
    for word in _WORD_RE.findall(cleaned):
        low = word.lower()
        if low and low not in seen:
            seen.add(low)
            terms.append(word)
    return terms


def escape_like_pattern(term: str) -> str:
    """Escape ``\\``, ``%``, and ``_`` for ``LIKE ? ESCAPE '\\'``."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_matched_excerpt(snippet: Snippet, query: str | None, *, max_len: int = 120) -> str:
    """Return a concise single-line preview centered around the first query match."""
    terms = extract_query_terms(query)
    lines = [line.strip() for line in snippet.content.splitlines() if line.strip()]
    if not lines:
        return snippet.description[:max_len]

    chosen_line = lines[0]
    if terms:
        for line in lines:
            low_line = line.lower()
            if any(t.lower() in low_line for t in terms):
                chosen_line = line
                break
        else:
            if snippet.description and any(t.lower() in snippet.description.lower() for t in terms):
                chosen_line = snippet.description.strip().splitlines()[0]

    if len(chosen_line) <= max_len:
        return chosen_line

    if terms:
        low_chosen = chosen_line.lower()
        for t in terms:
            pos = low_chosen.find(t.lower())
            if pos != -1:
                start = max(0, pos - max_len // 3)
                end = min(len(chosen_line), start + max_len)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(chosen_line) else ""
                return f"{prefix}{chosen_line[start:end]}{suffix}"

    return chosen_line[: max_len - 3] + "..."
