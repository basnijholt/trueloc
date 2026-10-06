# trueloc - Development Notes

See [README.md](README.md) for user-facing documentation.

## Project Structure

```
src/trueloc/
├── cli.py       # Typer commands (count, count-local, clear-cache)
├── display.py   # Rich table formatting and JSON output
├── github.py    # GitHubClient - API calls with caching
├── local.py     # Local git repo analysis (git log/show, numstat parsing)
├── mirror.py    # RepoMirrors - bare clones of GitHub repos for local stats
├── models.py    # Pydantic-style dataclasses for stats
└── utils.py     # Shared utilities (cache, token, date parsing)
```

## Key Implementation Details

### Caching Strategy

Cache lives at `~/.cache/trueloc/` using diskcache with SQLite backend.

**Cache key patterns:**
- `pr_stats_per_commit_v2:{repo}:{pr_number}` - Per-commit PR stats, excluding merge commits (immutable)
- `pr_stats_net:{repo}:{pr_number}` - Net diff PR stats (immutable)
- `commit_stats:{repo}:{sha}` - Individual commit stats (immutable)
- `user_repos_v2:{user}` - Owned non-fork repos (7 days)
- `merged_prs_v3:{repo}:{author}` - Merged PRs with `cached_since`/`cached_until` watermarks
- `merged_prs_search:{author}:{repo}` - Merged PRs from GraphQL search (same watermarks, UTC)
- `repo_info:{repo}` - Default branch and size (7 days)
- `contributed_repos:{user}:{year}` - Repos with contributions in a completed calendar year (7 days; repos get renamed or transferred)
- `fork_commits:{repo}:{pushed_at}` - A fork's commits ahead of its parent, until the fork is pushed to again (7 days)
- `branch_commits_v3:{repo}:{branch}:{author}` - Branch commits with range-aware caching

Dates are naive UTC everywhere (like the dates GitHub returns); `count` converts `--since`/`--until` once with `to_utc()`, and requests send explicit `Z` timestamps (GitHub reads timestamps without a timezone as US Pacific time). Direct commits are filtered by author date.

These keys use range-aware caching: they store `cached_since`/`cached_until` timestamps and only fetch the missing ranges on subsequent calls. Merged PRs are re-checked for newly merged ones once `cached_until` is older than `PR_REFRESH_INTERVAL` (1 hour). Branch commits are only re-fetched for repos pushed to since `cached_until` (`get_pushed_at()`, one GraphQL request per 100 repos), starting `COMMIT_REFRESH_OVERLAP` (7 days) earlier for commits pushed after they were made.

### GitHub API Flow

1. `get_user_repos()` → user's own non-fork repos, plus `get_contributed_repos()` → repos with PR/commit contributions (GraphQL `contributionsCollection`, includes other owners' and private repos)
   - Owned forks pushed since `--since` (`get_active_owned_forks()`): direct commits come from `get_fork_commits()`, i.e. commits ahead of the parent, so synced upstream commits are skipped
2. `search_merged_prs()` → all PRs merged by user in the range via GraphQL search (falls back to `get_merged_prs()` per repo)
3. For each PR: `get_pr_stats_per_commit()` or `get_pr_stats_net()`
4. For direct commits: `get_branch_commits()` → `get_commit_stats()` for each

### Local git stats

Before steps 3 and 4, `prefetch_pr_commits()` / `prefetch_commit_stats()` fill the `pr_commits_raw` and `commit_stats` cache entries from a bare clone (`RepoMirrors` in `mirror.py`, under `~/.cache/trueloc/repos/`) when a repo needs at least `LOCAL_MIN_COMMITS` commits and is at most `MAX_MIRROR_SIZE_KB`. PR commits are `refs/pull/N/head --not <merge commit>^1`; stats come from one `git log --numstat` call (`local.py`). Everything else falls back to the API.

### Testing

Tests use `respx` to mock HTTP calls. The `conftest.py` has two autouse fixtures:
- `_isolate_cache` - Patches `CACHE_DIR` to temp directory (prevents touching real cache)
- `_respx_mock` - Sets up respx with `assert_all_mocked=True`

Run tests: `pytest` or `pytest -x` to stop on first failure.

## TODO

- [ ] Handle pagination edge cases for very large repos
