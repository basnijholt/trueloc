"""Count lines of code from GitHub pull requests."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import diskcache  # type: ignore[import-untyped]
import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

app = typer.Typer(help="Count lines of code from GitHub pull requests.")
console = Console()

CACHE_DIR = Path.home() / ".cache" / "loc"

# Cache TTL in seconds (1 day for mutable data, None for immutable)
TTL_MUTABLE = 86400  # 1 day
TTL_IMMUTABLE = None  # Never expires


def get_cache() -> diskcache.Cache:
    """Get or create the disk cache."""
    return diskcache.Cache(str(CACHE_DIR))


def get_github_token() -> str:
    """Get GitHub token from gh CLI."""
    result = subprocess.run(
        ["gh", "auth", "token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@dataclass
class FileStats:
    """Statistics per file extension."""

    additions: int = 0
    deletions: int = 0


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


def get_user_repos(
    client: httpx.Client,
    cache: diskcache.Cache,
    username: str,
) -> list[str]:
    """Get all repositories for a user (cached with TTL)."""
    cache_key = f"user_repos:{username}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    repos: list[str] = []
    page = 1
    try:
        while True:
            response = client.get(
                f"/users/{username}/repos",
                params={"per_page": 100, "page": page, "type": "owner"},
            )
            response.raise_for_status()
            page_repos = response.json()
            if not page_repos:
                break
            repos.extend(repo["full_name"] for repo in page_repos)
            page += 1
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        # Rate limited or timeout - return what we have
        pass

    cache.set(cache_key, repos, expire=TTL_MUTABLE)
    return repos


def get_merged_prs(  # noqa: C901
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    username: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """Get all merged PRs for a repo by a user since a date (cached with TTL)."""
    # Cache key includes the since date (rounded to day for better cache hits)
    since_str = since.strftime("%Y-%m-%d")
    cache_key = f"merged_prs:{repo}:{username}:{since_str}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    result: list[dict[str, Any]] = []
    page = 1
    try:
        while True:
            response = client.get(
                f"/repos/{repo}/pulls",
                params={
                    "state": "closed",
                    "per_page": 100,
                    "page": page,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
            response.raise_for_status()
            prs: list[dict[str, Any]] = response.json()
            if not prs:
                break

            for pr in prs:
                if pr["merged_at"] is None:
                    continue
                merged_at = datetime.fromisoformat(pr["merged_at"])
                if merged_at.replace(tzinfo=None) < since:
                    continue
                if pr["user"]["login"] == username:
                    result.append(pr)

            if prs:
                oldest = prs[-1]
                if oldest["updated_at"]:
                    oldest_date = datetime.fromisoformat(oldest["updated_at"])
                    if oldest_date.replace(tzinfo=None) < since:
                        break
            page += 1
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        # Rate limited or timeout - return what we have
        pass

    cache.set(cache_key, result, expire=TTL_MUTABLE)
    return result


def get_file_extension(filename: str) -> str:
    """Extract file extension from filename."""
    path = Path(filename)
    return path.suffix.lower() if path.suffix else path.name.lower()


def get_default_branch(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
) -> str:
    """Get the default branch for a repository."""
    cache_key = f"default_branch:{repo}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    response = client.get(f"/repos/{repo}")
    response.raise_for_status()
    branch = response.json()["default_branch"]

    cache.set(cache_key, branch, expire=TTL_MUTABLE)
    return branch  # type: ignore[no-any-return]


def get_branch_commits(  # noqa: PLR0913
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    branch: str,
    username: str,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """Get all commits on a branch by a user within a date range (cached with TTL)."""
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")
    cache_key = f"branch_commits:{repo}:{branch}:{username}:{since_str}:{until_str}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    result: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.get(
            f"/repos/{repo}/commits",
            params={
                "sha": branch,
                "author": username,
                "since": since.isoformat(),
                "until": until.isoformat(),
                "per_page": 100,
                "page": page,
            },
        )
        response.raise_for_status()
        commits: list[dict[str, Any]] = response.json()
        if not commits:
            break
        result.extend(commits)
        page += 1

    cache.set(cache_key, result, expire=TTL_MUTABLE)
    return result


def is_commit_from_pr(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    sha: str,
) -> bool:
    """Check if a commit is associated with a PR."""
    cache_key = f"commit_pr_assoc:{repo}:{sha}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    response = client.get(f"/repos/{repo}/commits/{sha}/pulls")
    response.raise_for_status()
    prs = response.json()

    # If commit is associated with any merged PR, it came from a PR
    is_from_pr = any(pr.get("merged_at") is not None for pr in prs)

    cache.set(cache_key, is_from_pr)  # Immutable - commit association doesn't change
    return is_from_pr


def get_pr_commits(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    pr_number: int,
) -> list[str]:
    """Get all commit SHAs in a PR."""
    cache_key = f"pr_commits:{repo}:{pr_number}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    commits: list[str] = []
    page = 1
    try:
        while True:
            response = client.get(
                f"/repos/{repo}/pulls/{pr_number}/commits",
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            page_commits: list[dict[str, Any]] = response.json()
            if not page_commits:
                break
            commits.extend(c["sha"] for c in page_commits)
            page += 1
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        # Rate limited or timeout - return what we have
        pass

    cache.set(cache_key, commits)
    return commits


def get_commit_stats(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    sha: str,
) -> tuple[int, int, dict[str, FileStats]]:
    """Get additions and deletions for a single commit."""
    cache_key = f"commit_stats:{repo}:{sha}"

    cached = cache.get(cache_key)
    if cached is not None:
        total_add, total_del, ext_data = cached
        cached_by_ext = {ext: FileStats(add, del_) for ext, (add, del_) in ext_data.items()}
        return total_add, total_del, cached_by_ext

    try:
        response = client.get(f"/repos/{repo}/commits/{sha}")
        response.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        # Rate limited or timeout - return empty stats
        return 0, 0, {}
    commit_data = response.json()

    by_extension: dict[str, FileStats] = defaultdict(FileStats)
    total_additions = 0
    total_deletions = 0

    for file in commit_data.get("files", []):
        ext = get_file_extension(file["filename"])
        additions = file.get("additions", 0)
        deletions = file.get("deletions", 0)
        by_extension[ext].additions += additions
        by_extension[ext].deletions += deletions
        total_additions += additions
        total_deletions += deletions

    ext_data = {ext: (stats.additions, stats.deletions) for ext, stats in by_extension.items()}
    cache.set(cache_key, (total_additions, total_deletions, ext_data))

    return total_additions, total_deletions, dict(by_extension)


def get_pr_stats_per_commit(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    pr_number: int,
) -> tuple[int, int, dict[str, FileStats]]:
    """Get total additions/deletions across all commits in a PR (counts all changes)."""
    cache_key = f"pr_stats_per_commit:{repo}:{pr_number}"

    cached = cache.get(cache_key)
    if cached is not None:
        total_add, total_del, ext_data = cached
        cached_by_ext = {ext: FileStats(add, del_) for ext, (add, del_) in ext_data.items()}
        return total_add, total_del, cached_by_ext

    commits = get_pr_commits(client, cache, repo, pr_number)

    by_extension: dict[str, FileStats] = defaultdict(FileStats)
    total_additions = 0
    total_deletions = 0

    for sha in commits:
        add, del_, ext_stats = get_commit_stats(client, cache, repo, sha)
        total_additions += add
        total_deletions += del_
        for ext, stats in ext_stats.items():
            by_extension[ext].additions += stats.additions
            by_extension[ext].deletions += stats.deletions

    ext_data = {ext: (stats.additions, stats.deletions) for ext, stats in by_extension.items()}
    cache.set(cache_key, (total_additions, total_deletions, ext_data))

    return total_additions, total_deletions, dict(by_extension)


def get_pr_stats_net(
    client: httpx.Client,
    cache: diskcache.Cache,
    repo: str,
    pr_number: int,
) -> tuple[int, int, dict[str, FileStats]]:
    """Get net additions/deletions for a PR (final diff only)."""
    cache_key = f"pr_stats_net:{repo}:{pr_number}"

    cached = cache.get(cache_key)
    if cached is not None:
        total_add, total_del, ext_data = cached
        cached_by_ext = {ext: FileStats(add, del_) for ext, (add, del_) in ext_data.items()}
        return total_add, total_del, cached_by_ext

    # Fetch PR files (final diff)
    files_cache_key = f"pr_files:{repo}:{pr_number}"
    files = cache.get(files_cache_key)
    if files is None:
        files = []
        page = 1
        try:
            while True:
                response = client.get(
                    f"/repos/{repo}/pulls/{pr_number}/files",
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
                page_files: list[dict[str, Any]] = response.json()
                if not page_files:
                    break
                files.extend(page_files)
                page += 1
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            # Rate limited or timeout - return what we have
            pass
        cache.set(files_cache_key, files)

    by_extension: dict[str, FileStats] = defaultdict(FileStats)
    total_additions = 0
    total_deletions = 0

    for file in files:
        ext = get_file_extension(file["filename"])
        additions = file.get("additions", 0)
        deletions = file.get("deletions", 0)
        by_extension[ext].additions += additions
        by_extension[ext].deletions += deletions
        total_additions += additions
        total_deletions += deletions

    ext_data = {ext: (stats.additions, stats.deletions) for ext, stats in by_extension.items()}
    cache.set(cache_key, (total_additions, total_deletions, ext_data))

    return total_additions, total_deletions, dict(by_extension)


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
    all_commits: list[CommitStats], username: str, since: str, until: str | None
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
    total_by_extension: dict[str, FileStats],
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
        total_by_extension.items(),
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


def display_summary(  # noqa: PLR0913
    all_stats: list[PRStats],
    all_commits: list[CommitStats],
    total_additions: int,
    total_deletions: int,
    cache_hits: int,
    since: str,
    *,
    per_commit: bool,
) -> None:
    """Display the summary statistics."""
    console.print()
    mode_label = "[dim](per-commit totals)[/dim]" if per_commit else "[dim](net diff)[/dim]"
    console.print(f"[bold]Total PRs:[/bold] {len(all_stats)} {mode_label}")
    if all_commits:
        console.print(f"[bold]Direct commits:[/bold] {len(all_commits)}")
    console.print(f"[bold green]Total additions:[/bold green] +{total_additions:,}")
    console.print(f"[bold red]Total deletions:[/bold red] -{total_deletions:,}")
    console.print(f"[bold]Total lines changed:[/bold] {total_additions + total_deletions:,}")

    if cache_hits > 0:
        console.print(f"[dim]Cache hits: {cache_hits}[/dim]")

    if total_additions >= 1_000_000:  # noqa: PLR2004
        console.print(
            f"\n[bold yellow]Congratulations! You've written over a million lines "
            f"of code since {since}![/bold yellow]"
        )


@app.command()
def count(  # noqa: PLR0913, PLR0915, PLR0912, C901
    username: str = typer.Argument(..., help="GitHub username"),
    since: str = typer.Option(..., "--since", "-s", help="Start date (YYYY-MM-DD)"),
    until: str | None = typer.Option(None, "--until", "-u", help="End date (YYYY-MM-DD)"),
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
    since_date = datetime.strptime(since, "%Y-%m-%d")  # noqa: DTZ007
    until_date = datetime.strptime(until, "%Y-%m-%d") if until else datetime.now()  # noqa: DTZ005, DTZ007

    token = get_github_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}

    all_stats: list[PRStats] = []
    all_direct_commits: list[CommitStats] = []
    total_additions = 0
    total_deletions = 0
    total_by_extension: dict[str, FileStats] = defaultdict(FileStats)
    cache_hits = 0

    cache = get_cache() if not no_cache else diskcache.Cache(":memory:")
    get_stats = get_pr_stats_per_commit if per_commit else get_pr_stats_net
    cache_prefix = "pr_stats_per_commit" if per_commit else "pr_stats_net"

    with (
        httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30.0) as client,
        Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress,
    ):
        fetch_task = progress.add_task("Fetching repositories...", total=None)
        repos = get_user_repos(client, cache, username)
        progress.remove_task(fetch_task)

        repo_task = progress.add_task("Processing repos", total=len(repos))

        for repo in repos:
            progress.update(repo_task, description=f"[cyan]{repo}[/cyan]")

            # Process PRs
            for pr in get_merged_prs(client, cache, repo, username, since_date):
                merged_at = datetime.fromisoformat(pr["merged_at"])
                if merged_at.replace(tzinfo=None) > until_date:
                    continue

                cache_key = f"{cache_prefix}:{repo}:{pr['number']}"
                was_cached = cache_key in cache
                additions, deletions, by_ext = get_stats(client, cache, repo, pr["number"])
                if was_cached:
                    cache_hits += 1

                for ext, ext_stats in by_ext.items():
                    total_by_extension[ext].additions += ext_stats.additions
                    total_by_extension[ext].deletions += ext_stats.deletions

                all_stats.append(
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
                total_additions += additions
                total_deletions += deletions

            # Process direct commits (not from PRs)
            if include_direct_commits:
                try:
                    default_branch = get_default_branch(client, cache, repo)
                    branch_commits = get_branch_commits(
                        client, cache, repo, default_branch, username, since_date, until_date
                    )

                    for commit in branch_commits:
                        sha = commit["sha"]
                        # Check if this commit came from a PR
                        if is_commit_from_pr(client, cache, repo, sha):
                            continue

                        # This is a direct commit
                        cache_key = f"commit_stats:{repo}:{sha}"
                        was_cached = cache_key in cache
                        additions, deletions, by_ext = get_commit_stats(client, cache, repo, sha)
                        if was_cached:
                            cache_hits += 1

                        for ext, ext_stats in by_ext.items():
                            total_by_extension[ext].additions += ext_stats.additions
                            total_by_extension[ext].deletions += ext_stats.deletions

                        commit_date = commit["commit"]["author"]["date"][:10]
                        message = commit["commit"]["message"].split("\n")[0]

                        all_direct_commits.append(
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
                        total_additions += additions
                        total_deletions += deletions
                except (httpx.HTTPStatusError, httpx.TimeoutException):
                    # Some repos might not have commits or access issues, or timeout
                    pass

            progress.advance(repo_task)

    if all_stats:
        display_pr_table(all_stats, username, since, until)

    if all_direct_commits:
        console.print()
        display_direct_commits_table(all_direct_commits, username, since, until)

    if show_extensions and total_by_extension:
        console.print()
        display_extension_table(total_by_extension, total_additions, total_deletions)

    display_summary(
        all_stats,
        all_direct_commits,
        total_additions,
        total_deletions,
        cache_hits,
        since,
        per_commit=per_commit,
    )


@app.command()
def clear_cache() -> None:
    """Clear the disk cache."""
    cache = get_cache()
    cache.clear()
    console.print("[green]Cache cleared![/green]")


if __name__ == "__main__":
    app()
