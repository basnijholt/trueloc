# trueloc - Lines of Code Counter

A CLI tool to count how many lines of code you've written via GitHub pull requests and direct commits.

## Goal

The primary goal is to answer: **"How many lines of code have I written since date X?"**

This tool counts ALL lines touched (not just net diff). Example: a single PR where you add 1000 lines, delete them, then add 1 line = +1001 / -1000 (even though the PR's net diff shows only +1). Additions and deletions are summed separately across all commits.

## How It Works

1. **Fetches all your repositories** from GitHub
2. **For each merged PR**: Gets all commits in the PR and sums their additions/deletions
3. **For direct commits**: Gets commits on default branch that aren't from PRs
4. **Caches aggressively**: Immutable data (commits, merged PRs) cached forever; mutable data (repo list, PR list) cached for 1 day

## Key Features

- **Per-commit counting**: Counts every line touched in every commit (default)
- **Net diff mode**: Alternative mode that only counts final diff (`--net`)
- **File extension breakdown**: Shows which languages you've worked with
- **Disk caching**: Uses diskcache to avoid hammering the GitHub API
- **Uses `gh` CLI**: Leverages existing GitHub authentication

## Usage

```bash
# Count lines from PRs since a date
trueloc count USERNAME --since 2023-01-01

# Count only net diff (not per-commit)
trueloc count USERNAME --since 2023-01-01 --net

# Clear the cache
trueloc clear-cache
```

## Architecture

- **GitHub API via httpx**: All data fetched from GitHub REST API
- **Authentication via `gh auth token`**: Uses GitHub CLI's stored credentials
- **Caching strategy**:
  - Immutable (forever): commit stats, PR commits, PR files
  - TTL 1 day: user repos, merged PR lists
- **Rich output**: Tables and progress spinners via Rich library

## Tech Stack

- Python 3.12+
- Typer (CLI framework)
- httpx (HTTP client)
- Rich (terminal output)
- diskcache (persistent caching)
- Hatch (build system)
- Ruff + mypy (linting/typing)

## TODO

- [x] Add direct commit counting (commits pushed directly to main, not via PR)
- [x] Add comprehensive test suite with respx mocking (51 tests, 62% coverage)
- [x] Add JSON output option for scripting (`--json`)
- [ ] Handle pagination edge cases for very large repos
