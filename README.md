# loc - Lines of Code Counter

A CLI tool to count how many lines of code you've written via GitHub pull requests and direct commits.

## Goal

Answer the question: **"How many lines of code have I written since date X?"**

This tool counts ALL lines touched (not just net diff). For example, a single PR where you add 1000 lines, delete them, then add 1 line = +1001 / -1000 (even though the PR's net diff shows only +1). Additions and deletions are summed separately across all commits.

## Installation

```bash
# Clone and install
git clone https://github.com/basnijholt/loc.git
cd loc
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
loc count USERNAME --since 2023-01-01

# Use relative dates
loc count USERNAME --since 5d      # 5 days ago
loc count USERNAME --since 2w      # 2 weeks ago
loc count USERNAME --since 3m      # 3 months ago
loc count USERNAME --since 1y      # 1 year ago
loc count USERNAME --since "last month"

# Specify a date range
loc count USERNAME --since 2024-01-01 --until 2024-06-30

# Count only net diff (not per-commit)
loc count USERNAME --since 2023-01-01 --net

# Exclude direct commits (PRs only)
loc count USERNAME --since 2023-01-01 --no-direct-commits

# Hide file extension breakdown
loc count USERNAME --since 2023-01-01 --no-extensions

# Disable caching (fresh API calls)
loc count USERNAME --since 2023-01-01 --no-cache

# Clear the cache
loc clear-cache
```

## Features

- **Per-commit counting** (default): Counts every line touched in every commit
- **Net diff mode**: Alternative mode that only counts final diff (`--net`)
- **Direct commits**: Includes commits pushed directly to main (not via PR)
- **File extension breakdown**: Shows which languages you've worked with
- **Disk caching**: Uses diskcache to avoid hammering the GitHub API
- **Rate limit handling**: Automatically waits when rate limited with progress bar
- **Flexible dates**: Supports relative (`5d`, `2w`, `3m`, `1y`) and natural language (`last month`)

## Caching Strategy

- **Immutable data** (cached forever): commit stats, PR commits, PR files
- **Mutable data** (1 day TTL): user repos, merged PR lists

Cache is stored in `~/.cache/loc/`.

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
