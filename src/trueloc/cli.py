"""CLI commands for trueloc."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import diskcache  # type: ignore[import-untyped]
import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from trueloc.display import (
    display_direct_commits_table,
    display_extension_table,
    display_local_commits_table,
    display_local_summary,
    display_pr_table,
    display_summary,
    output_json,
    output_local_json,
)
from trueloc.github import GitHubClient
from trueloc.local import get_commit_numstat, get_local_commits
from trueloc.models import CommitStats, FileStats, LocalCommitStats, PRStats, StatsAggregator
from trueloc.utils import CACHE_DIR, get_cache, get_github_token, parse_date

app = typer.Typer(
    help="Count lines of code from GitHub pull requests.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
console = Console()


def _process_pr(  # noqa: PLR0913
    gh: GitHubClient,
    repo: str,
    pr: dict[str, Any],
    per_commit: bool,  # noqa: FBT001
    aggregator: StatsAggregator,
    include_direct_commits: bool,  # noqa: FBT001
) -> None:
    """Process a single PR and update aggregator."""
    get_stats = gh.get_pr_stats_per_commit if per_commit else gh.get_pr_stats_net
    cache_prefix = "pr_stats_per_commit" if per_commit else "pr_stats_net"

    cache_key = f"{cache_prefix}:{repo}:{pr['number']}"
    was_cached = cache_key in gh.cache

    additions, deletions, by_ext = get_stats(repo, pr["number"])

    if was_cached:
        aggregator.cache_hits += 1

    if include_direct_commits:
        pr_commits = gh.get_pr_commits(repo, pr["number"])
        aggregator.pr_commit_shas.update(pr_commits)

    aggregator.add_pr(
        PRStats(
            repo=repo,
            pr_number=pr["number"],
            title=pr["title"][:50],
            additions=additions,
            deletions=deletions,
            merged_at=pr["merged_at"][:10],
            by_extension=by_ext,
        )
    )


def _process_direct_commits(  # noqa: PLR0913
    gh: GitHubClient,
    repo: str,
    username: str,
    since: datetime,
    until: datetime,
    aggregator: StatsAggregator,
) -> None:
    """Process direct commits for a repo and update aggregator."""
    default_branch = gh.get_default_branch(repo)
    if not default_branch:
        return

    branch_commits = gh.get_branch_commits(repo, default_branch, username, since, until)

    for commit in branch_commits:
        sha = commit["sha"]
        if sha in aggregator.pr_commit_shas:
            continue

        cache_key = f"commit_stats:{repo}:{sha}"
        was_cached = cache_key in gh.cache

        additions, deletions, by_ext = gh.get_commit_stats(repo, sha)

        if was_cached:
            aggregator.cache_hits += 1

        commit_date = commit["commit"]["author"]["date"][:10]
        message = commit["commit"]["message"].split("\n")[0]

        aggregator.add_commit(
            CommitStats(
                repo=repo,
                sha=sha,
                message=message[:50],
                additions=additions,
                deletions=deletions,
                committed_at=commit_date,
                by_extension=by_ext,
            )
        )


@app.command()
def count(  # noqa: PLR0913
    username: str = typer.Argument(..., help="GitHub username"),
    since: str = typer.Option(
        ..., "--since", "-s", help="Start date (e.g., 5d, 2w, 3m, 1y, 'last month', 2024-01-01)"
    ),
    until: str | None = typer.Option(
        None, "--until", "-u", help="End date (e.g., 1d, 'yesterday', 2024-12-31)"
    ),
    *,
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable disk cache"),  # noqa: FBT003
    show_extensions: bool = typer.Option(
        True,  # noqa: FBT003
        "--extensions/--no-extensions",
        help="Show breakdown by file extension",
    ),
    per_commit: bool = typer.Option(
        True,  # noqa: FBT003
        "--per-commit/--net",
        help="Count all lines touched per commit (default) vs net diff only",
    ),
    include_direct_commits: bool = typer.Option(
        True,  # noqa: FBT003
        "--direct-commits/--no-direct-commits",
        help="Include direct commits to main branch (not from PRs)",
    ),
    output_json_flag: bool = typer.Option(
        False,  # noqa: FBT003
        "--json",
        help="Output results as JSON for scripting",
    ),
) -> None:
    """Count lines of code from merged PRs and direct commits.

    By default, counts all lines touched across all commits in each PR,
    plus direct commits to the main branch (not from PRs).

    Example: a single PR where you add 1000 lines, delete them, then add
    1 line = +1001 / -1000 (even though the PR's net diff shows only +1).
    Additions and deletions are summed separately across all commits.

    Use --net to count only the final diff (net additions/deletions).
    Use --no-direct-commits to exclude direct commits to main branch.
    """
    since_date = parse_date(since)
    until_date = parse_date(until) if until else datetime.now()  # noqa: DTZ005

    token = get_github_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    cache = get_cache(no_cache)
    aggregator = StatsAggregator()

    with (
        httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30.0) as client,
        Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[cyan]{task.fields[status]}[/cyan]"),
            console=console,
            disable=output_json_flag,  # Suppress progress when outputting JSON
        ) as progress,
    ):
        gh = GitHubClient(client, cache)

        # Fetch repos
        fetch_task = progress.add_task("Fetching repositories...", total=None, status="")
        repos = gh.get_user_repos(username)
        progress.remove_task(fetch_task)

        # Main repo progress
        repo_task = progress.add_task(
            f"[bold]Repos[/bold] (0/{len(repos)})", total=len(repos), status=""
        )

        for repo_idx, repo in enumerate(repos, 1):
            short_repo = repo.split("/")[-1][:20]
            progress.update(
                repo_task,
                description=f"[bold]Repos[/bold] ({repo_idx}/{len(repos)})",
                status=short_repo,
            )

            # Fetch PRs for this repo
            prs = [
                pr
                for pr in gh.get_merged_prs(repo, username, since_date)
                if datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None) <= until_date
            ]

            if prs:
                pr_task = progress.add_task("  PRs", total=len(prs), status=f"0/{len(prs)}")
                for pr in prs:
                    progress.update(pr_task, status=f"#{pr['number']}")
                    _process_pr(gh, repo, pr, per_commit, aggregator, include_direct_commits)
                    progress.advance(pr_task)
                progress.remove_task(pr_task)

            # Process direct commits
            if include_direct_commits:
                commit_task = progress.add_task("  Direct commits", total=None, status="")
                _process_direct_commits(gh, repo, username, since_date, until_date, aggregator)
                progress.remove_task(commit_task)

            progress.advance(repo_task)

    if output_json_flag:
        output_json(aggregator, username, since, until, per_commit=per_commit)
    else:
        if aggregator.prs:
            display_pr_table(aggregator.prs, username, since, until)

        if aggregator.direct_commits:
            console.print()
            display_direct_commits_table(aggregator.direct_commits, username, since, until)

        if show_extensions and aggregator.by_extension:
            console.print()
            display_extension_table(
                aggregator.by_extension, aggregator.total_additions, aggregator.total_deletions
            )

        display_summary(aggregator, since, per_commit=per_commit)


@app.command()
def clear_cache() -> None:
    """Clear the disk cache."""
    cache = diskcache.Cache(str(CACHE_DIR))
    cache.clear()
    console.print("[green]Cache cleared![/green]")


@app.command("count-local")
def count_local(  # noqa: PLR0913
    repo_path: str = typer.Argument(..., help="Path to local git repository"),
    author: str = typer.Option(..., "--author", "-a", help="Git author name or email"),
    since: str = typer.Option(
        ..., "--since", "-s", help="Start date (e.g., 5d, 2w, 3m, 1y, 'last month', 2024-01-01)"
    ),
    until: str | None = typer.Option(
        None, "--until", "-u", help="End date (e.g., 1d, 'yesterday', 2024-12-31)"
    ),
    *,
    show_extensions: bool = typer.Option(
        True,  # noqa: FBT003
        "--extensions/--no-extensions",
        help="Show breakdown by file extension",
    ),
    output_json_flag: bool = typer.Option(
        False,  # noqa: FBT003
        "--json",
        help="Output results as JSON for scripting",
    ),
    include_merges: bool = typer.Option(
        False,  # noqa: FBT003
        "--include-merges",
        help="Include merge commits (usually inflates counts by double-counting)",
    ),
) -> None:
    """Count lines of code from a local git repository.

    Uses git log and git show --numstat to count lines per commit.
    This works for any git repository, including clones from Gitea,
    and provides per-file extension breakdown.

    Example:
        trueloc count-local ../my-repo --author "John Doe" --since 1y
        trueloc count-local . --author john@example.com --since 2024-01-01
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        msg = f"Not a git repository: {path}"
        raise typer.BadParameter(msg)

    since_date = parse_date(since)
    until_date = parse_date(until) if until else datetime.now()  # noqa: DTZ005

    repo_name = path.name

    # Get commits
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.fields[status]}[/cyan]"),
        console=console,
        disable=output_json_flag,
    ) as progress:
        fetch_task = progress.add_task("Finding commits...", total=None, status="")
        raw_commits = get_local_commits(
            path, author, since_date, until_date, no_merges=not include_merges
        )
        progress.remove_task(fetch_task)

        if not raw_commits:
            if not output_json_flag:
                console.print(f"[yellow]No commits found for {author} in {repo_name}[/yellow]")
            return

        # Process each commit with progress
        commit_task = progress.add_task(
            f"Processing commits (0/{len(raw_commits)})",
            total=len(raw_commits),
            status="",
        )

        commits: list[LocalCommitStats] = []
        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_additions = 0
        total_deletions = 0

        for idx, raw in enumerate(raw_commits, 1):
            progress.update(
                commit_task,
                description=f"Processing commits ({idx}/{len(raw_commits)})",
                status=raw["sha"][:7],
            )

            add, del_, ext_stats = get_commit_numstat(path, raw["sha"])

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

            progress.advance(commit_task)

    if output_json_flag:
        output_local_json(
            commits,
            dict(by_extension),
            total_additions,
            total_deletions,
            repo_name,
            author,
            since,
            until,
        )
    else:
        display_local_commits_table(commits, repo_name, author, since, until)

        if show_extensions and by_extension:
            console.print()
            display_extension_table(dict(by_extension), total_additions, total_deletions)

        display_local_summary(commits, total_additions, total_deletions)


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
