"""Display and output functions for trueloc."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from trueloc.models import (
        CommitStats,
        FileStats,
        LocalCommitStats,
        PRStats,
        StatsAggregator,
    )

console = Console()


def display_pr_table(
    all_stats: list[PRStats], username: str, since: str, until: str | None
) -> None:
    """Display the PR statistics table."""
    table = Table(title=f"PRs by {username} from {since} to {until or 'now'}")
    table.add_column("Repo", style="cyan")
    table.add_column("PR #", style="magenta")
    table.add_column("Title", style="white")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Merged", style="blue")

    for stat in sorted(all_stats, key=lambda x: x.merged_at, reverse=True):
        table.add_row(
            stat.repo,
            str(stat.pr_number),
            stat.title,
            f"+{stat.additions:,}",
            f"-{stat.deletions:,}",
            stat.merged_at,
        )
    console.print(table)


def display_direct_commits_table(
    all_commits: list[CommitStats],
    username: str,
    since: str,
    until: str | None,
) -> None:
    """Display the direct commits statistics table."""
    table = Table(title=f"Direct commits by {username} from {since} to {until or 'now'}")
    table.add_column("Repo", style="cyan")
    table.add_column("SHA", style="magenta")
    table.add_column("Message", style="white")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Date", style="blue")

    for commit in sorted(all_commits, key=lambda x: x.committed_at, reverse=True):
        table.add_row(
            commit.repo,
            commit.sha[:7],
            commit.message[:50],
            f"+{commit.additions:,}",
            f"-{commit.deletions:,}",
            commit.committed_at,
        )
    console.print(table)


def display_extension_table(
    by_extension: dict[str, FileStats],
    total_additions: int,
    total_deletions: int,
) -> None:
    """Display the file extension breakdown table."""
    ext_table = Table(title="Lines by File Extension")
    ext_table.add_column("Extension", style="cyan")
    ext_table.add_column("Additions", style="green", justify="right")
    ext_table.add_column("Deletions", style="red", justify="right")
    ext_table.add_column("Total", style="white", justify="right")
    ext_table.add_column("%", style="dim", justify="right")

    total_lines = total_additions + total_deletions
    sorted_exts = sorted(
        by_extension.items(),
        key=lambda x: x[1].additions + x[1].deletions,
        reverse=True,
    )

    for ext, ext_stats in sorted_exts[:20]:
        ext_total = ext_stats.additions + ext_stats.deletions
        percentage = (ext_total / total_lines * 100) if total_lines > 0 else 0
        ext_table.add_row(
            ext,
            f"+{ext_stats.additions:,}",
            f"-{ext_stats.deletions:,}",
            f"{ext_total:,}",
            f"{percentage:.1f}%",
        )
    console.print(ext_table)


def display_summary(aggregator: StatsAggregator, since: str, *, per_commit: bool) -> None:
    """Display the summary statistics."""
    console.print()
    mode_label = "[dim](per-commit totals)[/dim]" if per_commit else "[dim](net diff)[/dim]"
    console.print(f"[bold]Total PRs:[/bold] {len(aggregator.prs)} {mode_label}")

    if aggregator.direct_commits:
        console.print(f"[bold]Direct commits:[/bold] {len(aggregator.direct_commits)}")

    console.print(f"[bold green]Total additions:[/bold green] +{aggregator.total_additions:,}")
    console.print(f"[bold red]Total deletions:[/bold red] -{aggregator.total_deletions:,}")
    total = aggregator.total_additions + aggregator.total_deletions
    console.print(f"[bold]Total lines changed:[/bold] {total:,}")

    if aggregator.cache_hits > 0:
        console.print(f"[dim]Cache hits: {aggregator.cache_hits}[/dim]")

    if aggregator.total_additions >= 1_000_000:  # noqa: PLR2004
        console.print(
            f"\n[bold yellow]Congratulations! You've written over a million lines "
            f"of code since {since}![/bold yellow]"
        )


def output_json(
    aggregator: StatsAggregator,
    username: str,
    since: str,
    until: str | None,
    *,
    per_commit: bool,
) -> None:
    """Output results as JSON to stdout."""

    # Convert FileStats to dicts
    def ext_to_dict(by_ext: dict[str, FileStats]) -> dict[str, dict[str, int]]:
        return {ext: asdict(stats) for ext, stats in by_ext.items()}

    output = {
        "username": username,
        "since": since,
        "until": until,
        "mode": "per_commit" if per_commit else "net_diff",
        "prs": [
            {
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "title": pr.title,
                "merged_at": pr.merged_at,
                "additions": pr.additions,
                "deletions": pr.deletions,
                "by_extension": ext_to_dict(pr.by_extension),
            }
            for pr in aggregator.prs
        ],
        "direct_commits": [
            {
                "repo": c.repo,
                "sha": c.sha,
                "message": c.message,
                "committed_at": c.committed_at,
                "additions": c.additions,
                "deletions": c.deletions,
                "by_extension": ext_to_dict(c.by_extension),
            }
            for c in aggregator.direct_commits
        ],
        "summary": {
            "total_prs": len(aggregator.prs),
            "total_direct_commits": len(aggregator.direct_commits),
            "total_commits": len(aggregator.prs) + len(aggregator.direct_commits),
            "total_additions": aggregator.total_additions,
            "total_deletions": aggregator.total_deletions,
            "total_lines": aggregator.total_additions + aggregator.total_deletions,
            "by_extension": ext_to_dict(dict(aggregator.by_extension)),
        },
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


def display_local_commits_table(
    commits: list[LocalCommitStats],
    repo_name: str,
    author: str,
    since: str,
    until: str | None,
) -> None:
    """Display the local commits statistics table."""
    table = Table(title=f"Commits by {author} in {repo_name} from {since} to {until or 'now'}")
    table.add_column("SHA", style="magenta")
    table.add_column("Message", style="white")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Date", style="blue")

    for commit in sorted(commits, key=lambda x: x.committed_at, reverse=True):
        table.add_row(
            commit.sha[:7],
            commit.message[:60],
            f"+{commit.additions:,}",
            f"-{commit.deletions:,}",
            commit.committed_at,
        )
    console.print(table)


def output_local_json(  # noqa: PLR0913
    commits: list[LocalCommitStats],
    by_extension: dict[str, FileStats],
    total_add: int,
    total_del: int,
    repo_name: str,
    author: str,
    since: str,
    until: str | None,
) -> None:
    """Output local repo results as JSON to stdout."""

    def ext_to_dict(by_ext: dict[str, FileStats]) -> dict[str, dict[str, int]]:
        return {ext: asdict(stats) for ext, stats in by_ext.items()}

    output = {
        "repository": repo_name,
        "username": author,
        "since": since,
        "until": until,
        "commits": [
            {
                "sha": c.sha,
                "message": c.message,
                "committed_at": c.committed_at,
                "additions": c.additions,
                "deletions": c.deletions,
                "by_extension": ext_to_dict(c.by_extension),
            }
            for c in commits
        ],
        "summary": {
            "total_commits": len(commits),
            "total_additions": total_add,
            "total_deletions": total_del,
            "total_lines": total_add + total_del,
            "by_extension": ext_to_dict(by_extension),
        },
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


def display_local_summary(
    commits: list[LocalCommitStats],
    total_additions: int,
    total_deletions: int,
) -> None:
    """Display summary for local repository analysis."""
    console.print()
    console.print(f"[bold]Total commits:[/bold] {len(commits)}")
    console.print(f"[bold green]Total additions:[/bold green] +{total_additions:,}")
    console.print(f"[bold red]Total deletions:[/bold red] -{total_deletions:,}")
    total = total_additions + total_deletions
    console.print(f"[bold]Total lines changed:[/bold] {total:,}")
