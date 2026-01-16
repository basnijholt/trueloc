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

try:
    from trueloc._version import __version__, __version_tuple__
except ImportError:
    __version__ = "0.0.0"
    __version_tuple__ = (0, 0, 0)

__all__ = [
    "CACHE_DIR",
    "CommitStats",
    "FileStats",
    "GitHubClient",
    "LocalCommitStats",
    "PRStats",
    "StatsAggregator",
    "__version__",
    "__version_tuple__",
    "app",
    "parse_date",
]

if __name__ == "__main__":
    app()
