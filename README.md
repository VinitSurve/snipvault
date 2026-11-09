# SnipVault (`snipvault`)

> Local-First SQLite FTS5 Developer Snippet Library with macOS Clipboard Integration

[![CI](https://github.com/VinitSurve/snipvault/actions/workflows/ci.yml/badge.svg)](https://github.com/VinitSurve/snipvault/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://www.python.org/)

`snipvault` is a local-first, zero-telemetry CLI snippet manager for developers. Backed by SQLite FTS5 (Full-Text Search 5) with BM25 ranking, it delivers instant searches across code, titles, descriptions, and tags. It features macOS `pbcopy`/`pbpaste` pasteboard integration, parameterized snippet templates, transactional export/import, online backups, and a terminal TUI browser.

---

## Features

- **Instant Full-Text Search (SQLite FTS5):** BM25 ranking with title boosting, prefix matching on query tokens, and query escaping to neutralize syntax errors. Falls back gracefully to parameterized `LIKE` queries if FTS5 is compiled out.
- **macOS Clipboard Integration:** Seamless `pbcopy` copy support and `pbpaste` pasting from terminal. Linux `wl-copy`/`xclip` fallback supported.
- **Flexible Snippet Ingestion:** Add code via `--content`, `--file` (auto-detecting language from file extension), piped `stdin`, or interactive multi-line terminal prompt.
- **Parameterized Templates:** Create snippets with placeholder variables (e.g. `{{PORT}}`, `{{DATABASE_URL}}`); `snipvault` prompts or substitutes them during copy.
- **Transactional Import & Export:** Export collections to Markdown or JSON. Import JSON collections with duplicate handling (`skip`, `rename`, `replace`) in an atomic transaction.
- **Database Safety & Migrations:** Automated schema migrations (v1 to v2), online hot backups (`sqlite3.Connection.backup`), and pre-restore safety copies.
- **Interactive TUI Browser:** Launch `snipvault interactive` for a numbered terminal browser to filter, view, edit, favorite, and copy snippets.
- **Zero Telemetry & 100% Offline:** All snippets are stored locally on your machine in `~/Library/Application Support/devtoolkit/snipvault/snippets.db`.

---

## Installation

### Using pipx (Recommended for standalone CLI use)

```bash
pipx install git+https://github.com/VinitSurve/snipvault.git
```

Or install from a local checkout:

```bash
git clone https://github.com/VinitSurve/snipvault.git
cd snipvault
pipx install .
```

### Using pip (in an active virtual environment)

```bash
pip install .
```

---

## Quick Start

```bash
# Add a snippet with tags and language
snipvault add --title "Supabase Auth" --language typescript --tags supabase,auth \
  --content "const { data } = await supabase.auth.getSession();"

# Full-text search across snippets
snipvault search "supabase"

# List snippets sorted by title
snipvault list --sort title

# View snippet details and syntax highlighting
snipvault show 1

# Copy snippet directly to macOS system clipboard
snipvault copy 1

# Add snippet content from current clipboard
snipvault paste --title "Useful Command" --tags shell

# Launch the interactive terminal browser
snipvault interactive
```

---

## Command Reference

| Command | Description |
|---|---|
| `snipvault add` | Add a new snippet (`--title`, `--content`, `--file`, `--language`, `--tags`, `--favorite`). |
| `snipvault search [QUERY]` | Full-text search with BM25 ranking (`--language`, `--tag`, `--favorites`, `--limit`). |
| `snipvault list` | List snippets with sorting (`modified`, `created`, `title`) and pagination. |
| `snipvault show ID` | Display snippet metadata and syntax-highlighted code (`--raw` prints only code). |
| `snipvault edit ID` | Modify snippet metadata/tags, or open in `$EDITOR`. |
| `snipvault copy ID` | Copy snippet content to macOS clipboard (`pbcopy`). |
| `snipvault paste` | Create a snippet from the current clipboard contents (`pbpaste`). |
| `snipvault favorite ID` | Mark snippet as a favorite. |
| `snipvault unfavorite ID` | Remove favorite status. |
| `snipvault delete ID` | Delete snippet (requires confirmation or `--yes`). |
| `snipvault export` | Export snippet collection (`--format json\|markdown`, `--output FILE`). |
| `snipvault import FILE` | Import snippets from JSON (`--on-duplicate skip\|rename\|replace`). |
| `snipvault backup [DEST]` | Create a verified hot backup of the SQLite database. |
| `snipvault restore FILE` | Restore database from backup file with automatic safety snapshot. |
| `snipvault interactive` | Launch numbered interactive terminal browser. |
| `snipvault config` | View and edit `snipvault` configuration. |

### Global Flags

- `--json`: Output machine-readable JSON on `stdout`.
- `--quiet`: Suppress banners, headings, and decoration.
- `--verbose`: Print diagnostic debugging details to `stderr`.
- `--no-color`: Disable ANSI color escapes (respects `NO_COLOR=1`).
- `--help`, `-h`: Show usage assistance.
- `--version`: Display `snipvault` version.

---

## Configuration & Storage

`snipvault` stores its database and configuration in macOS standard platform directories:
- **Database:** `~/Library/Application Support/devtoolkit/snipvault/snippets.db`
- **Config:** `~/Library/Application Support/devtoolkit/snipvault/config.json`
- **Backups:** `~/Library/Application Support/devtoolkit/snipvault/backups/`
- **Logs:** `~/Library/Logs/devtoolkit/snipvault/snipvault.log`

Override with `DEVTOOLKIT_HOME=/custom/path`.

Example configuration commands:
```bash
# View configuration
snipvault config show

# Set default programming language
snipvault config set default_language python

# Change default search limit
snipvault config set default_limit 20
```

---

## Safety & Data Integrity

- **Write-Ahead Logging (WAL):** SQLite is operated in WAL mode with full foreign keys enabled.
- **FTS Query Sanitization:** Arbitrary user queries are escaped against unbalanced quotes, unmatched parentheses, and operator injection.
- **Atomic Pre-Restore Backups:** `snipvault restore` always makes a timestamped backup of the current database before restoring from an archive.
- **Confirmation Guards:** Destructive actions (`snipvault delete`, `config reset`) require interactive approval or `--yes`.

---

## Development Setup

```bash
git clone https://github.com/VinitSurve/snipvault.git
cd snipvault
make dev
make test
make check
```

---

## Running Tests

```bash
# Run pytest suite
pytest -v

# Run fast unit tests
pytest -m "not slow" -q
```

---

## Known Limitations

- **Headless Environments:** When running without a display or clipboard server (e.g. headless SSH), `snipvault copy` prints the snippet content to `stdout` with an informative warning on `stderr`.

---

## License

This project is licensed under the [MIT License](LICENSE).
