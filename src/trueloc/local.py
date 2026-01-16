"""Local git repository analysis functions."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from typing import TYPE_CHECKING

from trueloc.models import FileStats, LocalCommitStats
from trueloc.utils import get_file_extension

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


def run_git(repo_path: Path, *args: str) -> str:
    """Run a git command in the specified repository."""
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_path), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def get_local_commits(
    repo_path: Path,
    author: str,
    since: datetime,
    until: datetime,
    *,
    no_merges: bool = False,
) -> list[dict[str, str]]:
    """Get commits from local git repository by author in date range.

    Returns list of dicts with 'sha', 'date', 'message'.
    """
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")

    # Format: sha|date|message (first line only)
    log_format = "%H|%aI|%s"
    args = [
        "log",
        f"--author={author}",
        f"--since={since_str}",
        f"--until={until_str}",
        f"--format={log_format}",
    ]
    if no_merges:
        args.append("--no-merges")
    output = run_git(repo_path, *args)

    commits = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:  # noqa: PLR2004
            commits.append(
                {
                    "sha": parts[0],
                    "date": parts[1][:10],  # Just the date part
                    "message": parts[2],
                }
            )
    return commits


def get_commit_numstat(repo_path: Path, sha: str) -> tuple[int, int, dict[str, FileStats]]:
    """Get additions/deletions for a commit using git show --numstat.

    Returns (total_additions, total_deletions, by_extension).
    """
    try:
        output = run_git(repo_path, "show", "--numstat", "--format=", sha)
    except subprocess.CalledProcessError:
        return 0, 0, {}

    by_extension: dict[str, FileStats] = defaultdict(FileStats)
    total_add = 0
    total_del = 0

    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:  # noqa: PLR2004
            continue

        add_str, del_str, filename = parts
        # Binary files show "-" for additions/deletions
        if add_str == "-" or del_str == "-":
            continue

        additions = int(add_str)
        deletions = int(del_str)
        ext = get_file_extension(filename)

        total_add += additions
        total_del += deletions
        by_extension[ext].additions += additions
        by_extension[ext].deletions += deletions

    return total_add, total_del, dict(by_extension)


def process_local_commits(
    repo_path: Path,
    raw_commits: list[dict[str, str]],
) -> tuple[list[LocalCommitStats], dict[str, FileStats], int, int]:
    """Process raw commits and return stats.

    Returns (commits, by_extension, total_additions, total_deletions).
    """
    commits: list[LocalCommitStats] = []
    by_extension: dict[str, FileStats] = defaultdict(FileStats)
    total_additions = 0
    total_deletions = 0

    for raw in raw_commits:
        add, del_, ext_stats = get_commit_numstat(repo_path, raw["sha"])

        commits.append(
            LocalCommitStats(
                sha=raw["sha"],
                message=raw["message"][:60],
                additions=add,
                deletions=del_,
                committed_at=raw["date"],
                by_extension=ext_stats,
            )
        )

        total_additions += add
        total_deletions += del_
        for ext, stats in ext_stats.items():
            by_extension[ext].additions += stats.additions
            by_extension[ext].deletions += stats.deletions

    return commits, dict(by_extension), total_additions, total_deletions
