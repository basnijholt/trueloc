# trueloc - Lines of Code Counter

[![PyPI](https://img.shields.io/pypi/v/trueloc)](https://pypi.org/project/trueloc/)
[![Python](https://img.shields.io/pypi/pyversions/trueloc)](https://pypi.org/project/trueloc/)
[![License](https://img.shields.io/github/license/basnijholt/trueloc)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/trueloc)](https://pypi.org/project/trueloc/)
[![CI](https://img.shields.io/github/actions/workflow/status/basnijholt/trueloc/pytest.yml?label=tests)](https://github.com/basnijholt/trueloc/actions/workflows/pytest.yml)
[![Docs](https://img.shields.io/badge/docs-trueloc.nijho.lt-blue)](https://trueloc.nijho.lt)

<img src="https://raw.githubusercontent.com/basnijholt/trueloc/main/logo.svg" alt="trueloc Logo" align="right" style="width: 150px;" />

A CLI tool to answer: **"How many lines of code have I written since date X?"** — counting ALL lines touched via GitHub pull requests and direct commits (not just net diff). For example, a single PR where you add 1000 lines, delete them, then add 1 line = +1001 / -1000. Additions and deletions are summed separately across all commits.

## Installation

```bash
# Clone and install
git clone https://github.com/basnijholt/trueloc.git
cd trueloc
pip install -e .

# Or with uv
uv pip install -e .
```

Requires the GitHub CLI (`gh`) to be installed and authenticated:
```bash
gh auth login
```

## Usage

```bash
# Count lines from PRs and direct commits since a date
trueloc count USERNAME --since 2023-01-01

# Use relative dates
trueloc count USERNAME --since 5d      # 5 days ago
trueloc count USERNAME --since 2w      # 2 weeks ago
trueloc count USERNAME --since 3m      # 3 months ago
trueloc count USERNAME --since 1y      # 1 year ago
trueloc count USERNAME --since "last month"

# Specify a date range
trueloc count USERNAME --since 2024-01-01 --until 2024-06-30

# Count only net diff (not per-commit)
trueloc count USERNAME --since 2023-01-01 --net

# Exclude direct commits (PRs only)
trueloc count USERNAME --since 2023-01-01 --no-direct-commits

# Hide file extension breakdown
trueloc count USERNAME --since 2023-01-01 --no-extensions

# Disable caching (fresh API calls)
trueloc count USERNAME --since 2023-01-01 --no-cache

# Output as JSON for scripting/postprocessing
trueloc count USERNAME --since 2023-01-01 --json

# Clear the cache
trueloc clear-cache
```

## Features

- **Per-commit counting** (default): Counts every line touched in every commit
- **Net diff mode**: Alternative mode that only counts final diff (`--net`)
- **Direct commits**: Includes commits pushed directly to main (not via PR)
- **File extension breakdown**: Shows which languages you've worked with
- **JSON output**: Machine-readable output for scripting (`--json`)
- **Disk caching**: Uses diskcache to avoid hammering the GitHub API
- **Rate limit handling**: Automatically waits when rate limited with progress bar
- **Flexible dates**: Supports relative (`5d`, `2w`, `3m`, `1y`) and natural language (`last month`)

## Caching Strategy

- **Immutable data** (cached forever): commit stats, PR commits, PR files
- **Mutable data** (1 day TTL): user repos, merged PR lists

Cache is stored in `~/.cache/trueloc/`.

## Tech Stack

- Python 3.12+
- [Typer](https://typer.tiangolo.com/) - CLI framework
- [httpx](https://www.python-httpx.org/) - HTTP client
- [Rich](https://rich.readthedocs.io/) - Terminal output
- [diskcache](https://grantjenks.com/docs/diskcache/) - Persistent caching
- [dateparser](https://dateparser.readthedocs.io/) - Flexible date parsing
- [Hatch](https://hatch.pypa.io/) - Build system
- [Ruff](https://docs.astral.sh/ruff/) + [mypy](https://mypy-lang.org/) - Linting/typing

## Development

```bash
# Install dev dependencies
uv sync --group dev

# Run tests
pytest

# Run linting
ruff check .
ruff format .
mypy .
```

## License

MIT
