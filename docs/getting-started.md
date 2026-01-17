---
icon: lucide/rocket
---

# Getting Started

## Prerequisites

trueloc requires the GitHub CLI (`gh`) to be installed and authenticated:

```bash
# Install GitHub CLI (if not already installed)
# macOS
brew install gh

# Ubuntu/Debian
sudo apt install gh

# Windows
winget install GitHub.cli
```

Then authenticate:

```bash
gh auth login
```

## Installation

=== "uv tool (Recommended)"

    ```bash
    uv tool install trueloc
    ```

=== "uvx (Run without installing)"

    ```bash
    uvx trueloc count USERNAME --since 1m
    ```

=== "pip"

    ```bash
    pip install trueloc
    ```

=== "pipx"

    ```bash
    pipx install trueloc
    ```

=== "From source"

    ```bash
    git clone https://github.com/basnijholt/trueloc.git
    cd trueloc
    pip install -e .
    ```

## Verify Installation

```bash
trueloc --help
```

You should see the available commands and options.

## Your First Count

Count your lines of code from the last month:

```bash
trueloc count YOUR_USERNAME --since 1m
```

This will:

1. Fetch all repositories you have access to
2. Find merged PRs and direct commits since the specified date
3. Count lines added and deleted across all commits
4. Display a summary with per-PR breakdown and file type analysis

## Next Steps

- Learn about all [usage options](usage.md)
- Understand the [caching strategy](usage.md#caching)
