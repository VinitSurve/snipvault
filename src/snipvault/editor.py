"""Safe external editor invocation for interactive snippet editing."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from snipvault.common.exceptions import ExternalToolError, UsageError
from snipvault.common.safety import which
from snipvault.models import LANGUAGE_EXTENSION_MAP


def resolve_editor_argv(configured_editor: str = "") -> list[str]:
    """Resolve the external editor command into an argv list (never a shell string)."""
    candidates = [
        configured_editor.strip(),
        os.environ.get("VISUAL", "").strip(),
        os.environ.get("EDITOR", "").strip(),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise UsageError(
                f"Invalid editor command syntax: {raw!r} ({exc})",
                hint="Set a valid command in $VISUAL, $EDITOR, or 'snipvault config set editor'.",
            ) from exc
        if not parts:
            continue
        exe = parts[0]
        resolved = which(exe)
        if resolved:
            return [resolved, *parts[1:]]
        exe_path = Path(exe).expanduser()
        if exe_path.is_file() and os.access(exe_path, os.X_OK):
            return [str(exe_path), *parts[1:]]
        raise ExternalToolError(
            f"Configured editor executable not found: {exe!r}",
            hint="Set a valid editor in $VISUAL, $EDITOR, or 'snipvault config set editor'.",
        )

    for fallback in ("nano", "vi"):
        resolved = which(fallback)
        if resolved:
            return [resolved]

    raise ExternalToolError(
        "No external text editor found ($VISUAL, $EDITOR, nano, or vi).",
        hint="Set $EDITOR or configure one via 'snipvault config set editor <command>'.",
    )


def edit_in_external_editor(
    content: str, *, language: str = "text", configured_editor: str = ""
) -> str:
    """Open ``content`` in the user's editor using a private temporary file."""
    argv = resolve_editor_argv(configured_editor)
    suffix = LANGUAGE_EXTENSION_MAP.get(language.lower(), ".txt")
    fd, tmp_name = tempfile.mkstemp(prefix="snipvault-", suffix=suffix)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
        os.chmod(tmp_path, 0o600)
        try:
            proc = subprocess.run([*argv, str(tmp_path)], check=False, shell=False)
        except OSError as exc:
            raise ExternalToolError(
                f"Failed to launch editor {argv[0]!r}: {exc}",
                hint="Check your $EDITOR or 'snipvault config' settings.",
            ) from exc
        if proc.returncode != 0:
            raise ExternalToolError(
                f"Editor {argv[0]!r} exited with non-zero status {proc.returncode}.",
                hint="Snippet was not modified.",
            )
        try:
            return tmp_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ExternalToolError(
                f"Could not read edited snippet content as UTF-8: {exc}"
            ) from exc
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
