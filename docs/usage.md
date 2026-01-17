---
icon: lucide/terminal
---

# Usage

## Basic Commands

### Count Lines of Code

```bash
trueloc count USERNAME --since DATE
```

This counts all lines from merged PRs and direct commits since the specified date.

### Clear Cache

```bash
trueloc clear-cache
```

Removes all cached data from `~/.cache/trueloc/`.

## Date Formats

trueloc supports multiple date formats:

### Relative Dates

```bash
trueloc count USERNAME --since 5d      # 5 days ago
trueloc count USERNAME --since 2w      # 2 weeks ago
trueloc count USERNAME --since 3m      # 3 months ago
trueloc count USERNAME --since 1y      # 1 year ago
```

### Natural Language

```bash
trueloc count USERNAME --since "last month"
trueloc count USERNAME --since "last week"
trueloc count USERNAME --since "yesterday"
```

### ISO Dates

```bash
trueloc count USERNAME --since 2024-01-01
trueloc count USERNAME --since 2024-06-15
```

### Date Ranges

```bash
trueloc count USERNAME --since 2024-01-01 --until 2024-06-30
```

## Counting Modes

### Per-Commit Mode (Default)

Counts every line touched in every commit across all PRs:

```bash
trueloc count USERNAME --since 2024-01-01
```

This gives you the "true" count of all lines you wrote, even if some were later deleted or modified.

### Net Diff Mode

Only counts the final diff of each PR:

```bash
trueloc count USERNAME --since 2024-01-01 --net
```

This matches what GitHub shows in the PR diff view.

## Filtering Options

### Exclude Direct Commits

Only count lines from PRs, ignoring commits pushed directly to branches:

```bash
trueloc count USERNAME --since 2024-01-01 --no-direct-commits
```

### Hide File Extensions

Skip the file extension breakdown in output:

```bash
trueloc count USERNAME --since 2024-01-01 --no-extensions
```

### Single Repository

Count lines from a specific repository only:

```bash
trueloc count USERNAME --since 2024-01-01 --repo owner/repo-name
```

## Output Options

### JSON Output

Get machine-readable JSON output for scripting:

```bash
trueloc count USERNAME --since 2024-01-01 --json
```

### Disable Caching

Force fresh API calls without using cached data:

```bash
trueloc count USERNAME --since 2024-01-01 --no-cache
```

## Caching

trueloc caches API responses to avoid rate limiting and speed up repeated queries.

### Cache Location

Cache is stored in `~/.cache/trueloc/`.

### Cache Strategy

| Data Type | TTL | Reason |
|-----------|-----|--------|
| Commit stats | Forever | Immutable once created |
| PR commits | Forever | Immutable once merged |
| PR files | Forever | Immutable once merged |
| User repos | 1 day | Can change over time |
| Merged PR lists | Incremental | Only fetches new PRs |

### Cache Management

```bash
# Clear all cached data
trueloc clear-cache
```

## Rate Limiting

When GitHub rate limits are hit, trueloc automatically:

1. Detects the rate limit response
2. Shows a progress bar with wait time
3. Resumes automatically when the limit resets

## Examples

### Count last year's contributions

```bash
trueloc count myusername --since 1y
```

### Get JSON for a specific quarter

```bash
trueloc count myusername --since 2024-01-01 --until 2024-03-31 --json
```

### Quick check of recent activity

```bash
trueloc count myusername --since 1w --no-extensions
```

### Fresh count without cache

```bash
trueloc count myusername --since 1m --no-cache
```
