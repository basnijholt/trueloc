"""Display and output functions for trueloc."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from trueloc.models import (
        FileStats,
        LocalCommitStats,
        StatsAggregator,
    )

console = Console()

# Extensions to exclude from "top languages" (config, lock, docs, data files)
_EXCLUDED_EXTENSIONS: set[str] = {
    # Config files
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".config",
    ".env",
    ".properties",
    # Lock files
    ".lock",
    # Documentation
    ".md",
    ".rst",
    ".txt",
    ".adoc",
    # Data/markup
    ".xml",
    ".csv",
    ".html",
    ".htm",
    # Images/assets
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    # Other non-code
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
    ".map",
    ".min.js",
    ".min.css",
    ".d.ts",
}


def _is_code_extension(ext: str) -> bool:
    """Check if extension is a programming language (not config/lock/docs)."""
    return ext.lower() not in _EXCLUDED_EXTENSIONS


def _parse_date(date_str: str) -> datetime:
    """Parse ISO date string to datetime."""
    # Handle both "2024-01-15T10:30:00Z" and "2024-01-15" formats
    date_str = date_str.replace("Z", "+00:00")
    if "T" in date_str:
        return datetime.fromisoformat(date_str).replace(tzinfo=None)
    return datetime.fromisoformat(date_str)


def display_monthly_breakdown(aggregator: StatsAggregator) -> None:
    """Display lines of code per month."""
    monthly: dict[str, dict[str, int]] = defaultdict(lambda: {"additions": 0, "deletions": 0})

    # Aggregate PRs by month
    for pr in aggregator.prs:
        month = _parse_date(pr.merged_at).strftime("%Y-%m")
        monthly[month]["additions"] += pr.additions
        monthly[month]["deletions"] += pr.deletions

    # Aggregate direct commits by month
    for commit in aggregator.direct_commits:
        month = _parse_date(commit.committed_at).strftime("%Y-%m")
        monthly[month]["additions"] += commit.additions
        monthly[month]["deletions"] += commit.deletions

    if not monthly:
        return

    table = Table(title="Lines of Code by Month")
    table.add_column("Month", style="cyan")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Total", style="white", justify="right")
    table.add_column("", style="dim")  # Bar chart

    max_total = max((m["additions"] + m["deletions"]) for m in monthly.values())

    for month in sorted(monthly.keys()):
        stats = monthly[month]
        total = stats["additions"] + stats["deletions"]
        bar_width = int((total / max_total) * 30) if max_total > 0 else 0
        bar = "█" * bar_width

        table.add_row(
            month,
            f"+{stats['additions']:,}",
            f"-{stats['deletions']:,}",
            f"{total:,}",
            f"[green]{bar}[/green]",
        )

    console.print(table)


def _get_top_code_languages(aggregator: StatsAggregator, top_n: int) -> list[str]:
    """Get top N programming languages by total lines (excluding config/lock files)."""
    lang_totals: dict[str, int] = defaultdict(int)
    for ext, stats in aggregator.by_extension.items():
        if _is_code_extension(ext):
            lang_totals[ext] += stats.additions + stats.deletions
    top_langs = sorted(lang_totals.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [lang for lang, _ in top_langs]


def _aggregate_monthly_by_language(
    aggregator: StatsAggregator, top_lang_names: list[str]
) -> dict[str, dict[str, int]]:
    """Aggregate lines by month and language."""
    monthly_by_lang: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for pr in aggregator.prs:
        month = _parse_date(pr.merged_at).strftime("%Y-%m")
        for ext, stats in pr.by_extension.items():
            if ext in top_lang_names:
                monthly_by_lang[month][ext] += stats.additions + stats.deletions

    for commit in aggregator.direct_commits:
        month = _parse_date(commit.committed_at).strftime("%Y-%m")
        for ext, stats in commit.by_extension.items():
            if ext in top_lang_names:
                monthly_by_lang[month][ext] += stats.additions + stats.deletions

    return monthly_by_lang


def display_monthly_by_language(aggregator: StatsAggregator, top_n: int = 3) -> None:
    """Display lines of code per month for top N programming languages."""
    top_lang_names = _get_top_code_languages(aggregator, top_n)
    if not top_lang_names:
        return

    monthly_by_lang = _aggregate_monthly_by_language(aggregator, top_lang_names)
    if not monthly_by_lang:
        return

    # Build table
    table = Table(title=f"Lines by Month - Top {len(top_lang_names)} Languages")
    table.add_column("Month", style="cyan")

    # Add column for each top language
    colors = ["green", "blue", "magenta", "yellow", "red"]
    for i, lang in enumerate(top_lang_names):
        table.add_column(lang, style=colors[i % len(colors)], justify="right")
    table.add_column("Total", style="white", justify="right")

    for month in sorted(monthly_by_lang.keys()):
        row: list[str] = [month]
        month_total = 0
        for lang in top_lang_names:
            count = monthly_by_lang[month].get(lang, 0)
            month_total += count
            row.append(f"{count:,}" if count > 0 else "-")
        row.append(f"{month_total:,}")
        table.add_row(*row)

    console.print(table)


def display_repo_breakdown(aggregator: StatsAggregator) -> None:
    """Display lines of code per repository (top 15)."""
    repo_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"additions": 0, "deletions": 0, "prs": 0, "commits": 0}
    )

    for pr in aggregator.prs:
        repo_stats[pr.repo]["additions"] += pr.additions
        repo_stats[pr.repo]["deletions"] += pr.deletions
        repo_stats[pr.repo]["prs"] += 1

    for commit in aggregator.direct_commits:
        repo_stats[commit.repo]["additions"] += commit.additions
        repo_stats[commit.repo]["deletions"] += commit.deletions
        repo_stats[commit.repo]["commits"] += 1

    if not repo_stats:
        return

    table = Table(title="Lines of Code by Repository (Top 15)")
    table.add_column("Repository", style="cyan")
    table.add_column("PRs", style="magenta", justify="right")
    table.add_column("Commits", style="blue", justify="right")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Total", style="white", justify="right")

    sorted_repos = sorted(
        repo_stats.items(),
        key=lambda x: x[1]["additions"] + x[1]["deletions"],
        reverse=True,
    )

    for repo, stats in sorted_repos[:15]:
        # Shorten repo name if needed
        display_name = repo.split("/")[-1] if "/" in repo else repo
        if len(display_name) > 30:  # noqa: PLR2004
            display_name = display_name[:27] + "..."

        total = stats["additions"] + stats["deletions"]
        table.add_row(
            display_name,
            str(stats["prs"]) if stats["prs"] > 0 else "-",
            str(stats["commits"]) if stats["commits"] > 0 else "-",
            f"+{stats['additions']:,}",
            f"-{stats['deletions']:,}",
            f"{total:,}",
        )

    console.print(table)


def display_extension_table(
    by_extension: dict[str, FileStats],
    total_additions: int,
    total_deletions: int,
) -> None:
    """Display the file extension breakdown table (top 15)."""
    table = Table(title="Lines by File Extension (Top 15)")
    table.add_column("Extension", style="cyan")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Total", style="white", justify="right")
    table.add_column("%", style="dim", justify="right")

    total_lines = total_additions + total_deletions
    sorted_exts = sorted(
        by_extension.items(),
        key=lambda x: x[1].additions + x[1].deletions,
        reverse=True,
    )

    for ext, ext_stats in sorted_exts[:15]:
        ext_total = ext_stats.additions + ext_stats.deletions
        percentage = (ext_total / total_lines * 100) if total_lines > 0 else 0
        table.add_row(
            ext,
            f"+{ext_stats.additions:,}",
            f"-{ext_stats.deletions:,}",
            f"{ext_total:,}",
            f"{percentage:.1f}%",
        )
    console.print(table)


def display_top_contributions(aggregator: StatsAggregator) -> None:
    """Display top PRs and commits by size."""
    # Combine PRs and commits for ranking
    all_contributions: list[tuple[str, str, int, int, str]] = []

    for pr in aggregator.prs:
        repo_short = pr.repo.split("/")[-1] if "/" in pr.repo else pr.repo
        title = pr.title[:40] + "..." if len(pr.title) > 40 else pr.title  # noqa: PLR2004
        all_contributions.append(
            (
                f"PR #{pr.pr_number}",
                f"{repo_short}: {title}",
                pr.additions,
                pr.deletions,
                pr.merged_at[:10],
            )
        )

    for commit in aggregator.direct_commits:
        repo_short = commit.repo.split("/")[-1] if "/" in commit.repo else commit.repo
        msg = commit.message[:40] + "..." if len(commit.message) > 40 else commit.message  # noqa: PLR2004
        all_contributions.append(
            (
                commit.sha[:7],
                f"{repo_short}: {msg}",
                commit.additions,
                commit.deletions,
                commit.committed_at[:10],
            )
        )

    if not all_contributions:
        return

    # Sort by total lines changed
    sorted_contribs = sorted(
        all_contributions,
        key=lambda x: x[2] + x[3],
        reverse=True,
    )

    table = Table(title="Top 10 Contributions by Size")
    table.add_column("Ref", style="magenta")
    table.add_column("Description", style="white")
    table.add_column("Additions", style="green", justify="right")
    table.add_column("Deletions", style="red", justify="right")
    table.add_column("Date", style="dim")

    for ref, desc, additions, deletions, date in sorted_contribs[:10]:
        table.add_row(
            ref,
            desc,
            f"+{additions:,}",
            f"-{deletions:,}",
            date,
        )

    console.print(table)


def display_activity_stats(aggregator: StatsAggregator) -> None:
    """Display interesting activity statistics."""
    if not aggregator.prs and not aggregator.direct_commits:
        return

    # Calculate day of week distribution
    day_stats: dict[int, int] = defaultdict(int)
    for pr in aggregator.prs:
        day = _parse_date(pr.merged_at).weekday()
        day_stats[day] += pr.additions + pr.deletions
    for commit in aggregator.direct_commits:
        day = _parse_date(commit.committed_at).weekday()
        day_stats[day] += commit.additions + commit.deletions

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Calculate averages
    total_prs = len(aggregator.prs)
    total_commits = len(aggregator.direct_commits)
    total_items = total_prs + total_commits
    total_lines = aggregator.total_additions + aggregator.total_deletions

    avg_lines = total_lines // total_items if total_items > 0 else 0
    avg_pr_size = (
        sum(pr.additions + pr.deletions for pr in aggregator.prs) // total_prs
        if total_prs > 0
        else 0
    )

    # Find busiest day
    busiest_day = max(day_stats.items(), key=lambda x: x[1])[0] if day_stats else 0

    console.print()
    console.print("[bold]Activity Stats[/bold]")
    console.print(f"  Average lines per contribution: [cyan]{avg_lines:,}[/cyan]")
    if total_prs > 0:
        console.print(f"  Average PR size: [cyan]{avg_pr_size:,}[/cyan] lines")
    console.print(f"  Most productive day: [cyan]{day_names[busiest_day]}[/cyan]")

    # Show day of week mini-chart
    if day_stats:
        max_day = max(day_stats.values())
        console.print("\n  [dim]Lines by day of week:[/dim]")
        for i, day_name in enumerate(day_names):
            count = day_stats.get(i, 0)
            bar_width = int((count / max_day) * 20) if max_day > 0 else 0
            bar = "▓" * bar_width
            console.print(f"    {day_name}: [green]{bar}[/green] {count:,}")


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
