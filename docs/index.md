---
icon: lucide/chart-column-increasing
---

# trueloc

**A CLI tool to count your true lines of code from GitHub**

<div style="text-align: center; margin: 2rem 0;">
  <img src="assets/logo.svg" alt="trueloc Logo" width="200" />
</div>

Analyze your coding activity via GitHub pull requests and direct commits:

- **Total lines** written since any date
- **Per-PR and per-commit breakdown** showing when each contribution was made
- **File type analysis** revealing which languages you've worked with most

## Why trueloc?

GitHub's contribution stats can be misleading. When you squash-merge a PR, GitHub only shows the final diff—not all the work you actually did.

**Example:** A PR where you add 1000 lines, delete them, then add 1 line shows just `+1` on GitHub. But trueloc counts what you *actually wrote*: `+1001 / -1000`.

trueloc gives you the **true** picture of your coding activity.

<div style="text-align: center; margin: 2rem 0;">
  <a href="https://pypi.org/project/trueloc/" class="md-button md-button--primary">
    Install from PyPI
  </a>
  <a href="https://github.com/basnijholt/trueloc" class="md-button">
    View on GitHub
  </a>
</div>

## Quick Start

```bash
# Install (recommended)
uv tool install trueloc

# Or run directly without installing
uvx trueloc count USERNAME --since 1m

# Authenticate with GitHub (if not already)
gh auth login

# Count your lines since a date
trueloc count USERNAME --since 2024-01-01
```

[Get Started →](getting-started.md){ .md-button .md-button--primary }
[View Usage →](usage.md){ .md-button }

## Features

- **Per-commit counting** (default): Counts every line touched in every commit
- **Net diff mode**: Alternative mode that only counts final diff (`--net`)
- **Direct commits**: Includes commits pushed directly to main (not via PR)
- **File extension breakdown**: Shows which languages you've worked with
- **JSON output**: Machine-readable output for scripting (`--json`)
- **Disk caching**: Uses diskcache to avoid hammering the GitHub API
- **Rate limit handling**: Automatically waits when rate limited with progress bar
- **Flexible dates**: Supports relative (`5d`, `2w`, `3m`, `1y`) and natural language (`last month`)

## License

MIT License - see [LICENSE](https://github.com/basnijholt/trueloc/blob/main/LICENSE) for details.
