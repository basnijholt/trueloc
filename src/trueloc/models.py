"""Data classes for trueloc statistics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class FileStats:
    """Statistics per file extension."""

    additions: int = 0
    deletions: int = 0

    def to_tuple(self) -> tuple[int, int]:
        """Convert to tuple for caching."""
        return (self.additions, self.deletions)

    @classmethod
    def from_tuple(cls, data: tuple[int, int]) -> FileStats:
        """Create from cached tuple."""
        return cls(additions=data[0], deletions=data[1])


@dataclass
class PRStats:
    """Statistics for a pull request."""

    repo: str
    pr_number: int
    title: str
    additions: int
    deletions: int
    merged_at: str
    by_extension: dict[str, FileStats] = field(default_factory=dict)


@dataclass
class CommitStats:
    """Statistics for a direct commit (not from a PR)."""

    repo: str
    sha: str
    message: str
    additions: int
    deletions: int
    committed_at: str
    by_extension: dict[str, FileStats] = field(default_factory=dict)


@dataclass
class LocalCommitStats:
    """Statistics for a local git commit."""

    sha: str
    message: str
    additions: int
    deletions: int
    committed_at: str
    by_extension: dict[str, FileStats] = field(default_factory=dict)


@dataclass
class StatsAggregator:
    """Aggregates statistics across PRs and commits."""

    prs: list[PRStats] = field(default_factory=list)
    direct_commits: list[CommitStats] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    by_extension: dict[str, FileStats] = field(default_factory=lambda: defaultdict(FileStats))
    cache_hits: int = 0
    pr_commit_shas: set[str] = field(default_factory=set)

    def add_extension_stats(self, by_ext: dict[str, FileStats]) -> None:
        """Merge extension stats into totals."""
        for ext, stats in by_ext.items():
            self.by_extension[ext].additions += stats.additions
            self.by_extension[ext].deletions += stats.deletions

    def add_pr(self, pr: PRStats) -> None:
        """Add a PR and update totals."""
        self.prs.append(pr)
        self.total_additions += pr.additions
        self.total_deletions += pr.deletions
        self.add_extension_stats(pr.by_extension)

    def add_commit(self, commit: CommitStats) -> None:
        """Add a direct commit and update totals."""
        self.direct_commits.append(commit)
        self.total_additions += commit.additions
        self.total_deletions += commit.deletions
        self.add_extension_stats(commit.by_extension)
