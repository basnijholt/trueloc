"""Count lines of code from GitHub pull requests."""

from __future__ import annotations

from trueloc.cli import app
from trueloc.github import GitHubClient
from trueloc.models import (
    CommitStats,
    FileStats,
    LocalCommitStats,
    PRStats,
    StatsAggregator,
)
from trueloc.utils import CACHE_DIR, parse_date

__all__ = [
    "CACHE_DIR",
    "CommitStats",
    "FileStats",
    "GitHubClient",
    "LocalCommitStats",
    "PRStats",
    "StatsAggregator",
    "app",
    "parse_date",
]

if __name__ == "__main__":
    app()
