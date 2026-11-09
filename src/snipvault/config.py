"""Configuration schema for snipvault (stored in this tool's own config directory)."""

from __future__ import annotations

from typing import Any

from snipvault.common.config import ConfigSchema, ConfigStore, Field
from snipvault.common.paths import ToolPaths

DEFAULT_MAX_SNIPPET_BYTES = 1_000_000
DEFAULT_LANGUAGE = "text"
DEFAULT_LIMIT = 50

SCHEMA = ConfigSchema(
    {
        "max_snippet_bytes": Field(
            "int",
            DEFAULT_MAX_SNIPPET_BYTES,
            "Maximum allowed snippet content size in bytes.",
            minimum=1,
            maximum=50_000_000,
        ),
        "default_language": Field(
            "str",
            DEFAULT_LANGUAGE,
            "Default language assigned when adding a snippet without --language.",
        ),
        "default_limit": Field(
            "int",
            DEFAULT_LIMIT,
            "Default maximum number of snippets returned by list and search.",
            minimum=1,
            maximum=1000,
        ),
        "editor": Field(
            "str",
            "",
            "Preferred external editor command (falls back to $VISUAL, $EDITOR, nano, vi).",
        ),
    }
)


def load_config(paths: ToolPaths) -> dict[str, Any]:
    """Load validated snipvault configuration (using defaults if file does not exist)."""
    return ConfigStore(paths, SCHEMA).load()
