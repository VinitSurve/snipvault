"""Clipboard integration with macOS pbcopy, Linux fallbacks, and safe print fallback."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from snipvault.common.safety import run_command, which

CLIPBOARD_FALLBACK_NOTE = "clipboard unavailable; content printed instead"


@dataclass(frozen=True)
class ClipboardOutcome:
    """Result of attempting to copy snippet content to the system clipboard."""

    copied: bool
    backend: str | None
    note: str | None = None


def candidate_clipboard_commands() -> list[list[str]]:
    """Return ordered clipboard command argv lists for the current platform."""
    macos_cmds = [["pbcopy"]]
    linux_cmds = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    if sys.platform == "darwin":
        return macos_cmds + linux_cmds
    return linux_cmds + macos_cmds


def copy_to_clipboard(content: str) -> ClipboardOutcome:
    """Copy ``content`` to the system clipboard using argv lists (never a shell).

    If no clipboard program is available or all fail, returns ``copied=False`` with
    the documented warning note so the caller can print content and exit with
    ``ExitCode.WARNING``.
    """
    for argv in candidate_clipboard_commands():
        exe = which(argv[0])
        if not exe:
            continue
        result = run_command([exe, *argv[1:]], input_text=content, timeout=5.0)
        if result.ok:
            return ClipboardOutcome(copied=True, backend=argv[0], note=None)
    return ClipboardOutcome(copied=False, backend=None, note=CLIPBOARD_FALLBACK_NOTE)
