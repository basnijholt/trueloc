# trueloc - Development Notes

See [README.md](README.md) for user-facing documentation.

## Project Structure

```
src/trueloc/
├── cli.py       # Typer commands (count, count-local, clear-cache)
├── display.py   # Rich table formatting and JSON output
├── github.py    # GitHubClient - API calls with caching
├── local.py     # Local git repo analysis (git log/show)
├── models.py    # Pydantic-style dataclasses for stats
└── utils.py     # Shared utilities (cache, token, date parsing)
```

## Key Implementation Details

### Caching Strategy

Cache lives at `~/.cache/trueloc/` using diskcache with SQLite backend.

**Cache key patterns:**
- `pr_stats_per_commit:{repo}:{pr_number}` - Per-commit PR stats (immutable)
- `pr_stats_net:{repo}:{pr_number}` - Net diff PR stats (immutable)
- `commit_stats:{repo}:{sha}` - Individual commit stats (immutable)
- `merged_prs_v2:{repo}:{author}` - Merged PRs with `cached_since` watermark
- `branch_commits_v2:{repo}:{branch}:{author}` - Branch commits with range-aware caching

The `v2` keys use range-aware caching: they store a `cached_since` timestamp and only fetch newer data on subsequent calls.

### GitHub API Flow

1. `get_user_repos()` → all repos user has access to
2. For each repo: `get_merged_prs()` → PRs merged by user since date
3. For each PR: `get_pr_stats_per_commit()` or `get_pr_stats_net()`
4. For direct commits: `get_branch_commits()` → `get_commit_stats()` for each

### Testing

Tests use `respx` to mock HTTP calls. The `conftest.py` has two autouse fixtures:
- `_isolate_cache` - Patches `CACHE_DIR` to temp directory (prevents touching real cache)
- `_respx_mock` - Sets up respx with `assert_all_mocked=True`

Run tests: `pytest` or `pytest -x` to stop on first failure.

## TODO

- [ ] Handle pagination edge cases for very large repos
