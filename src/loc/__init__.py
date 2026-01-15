"""Count lines of code from GitHub pull requests."""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import diskcache  # type: ignore[import-untyped]
import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

app = typer.Typer(help="Count lines of code from GitHub pull requests.")
console = Console()

CACHE_DIR = Path.home() / ".cache" / "loc"
TTL_MUTABLE = 86400  # 1 day for mutable data
TTL_IMMUTABLE = None  # Never expires for immutable data
RATE_LIMIT_BUFFER = 500  # Proactively pause when remaining requests drop below this


# =============================================================================
# Data Classes
# =============================================================================


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


# =============================================================================
# GitHub Client with Caching
# =============================================================================


class GitHubClient:
    """GitHub API client with caching and pagination."""

    def __init__(self, client: httpx.Client, cache: diskcache.Cache) -> None:
        self.client = client
        self.cache = cache

    def _wait_for_rate_limit(self, response: httpx.Response) -> None:
        """Wait for rate limit to reset, showing countdown progress bar."""
        reset_timestamp = int(response.headers.get("X-RateLimit-Reset", 0))
        retry_after = int(response.headers.get("Retry-After", 0))

        # Calculate wait time
        if retry_after > 0:
            wait_seconds = retry_after
        elif reset_timestamp > 0:
            wait_seconds = max(0, reset_timestamp - int(time.time()) + 1)
        else:
            wait_seconds = 60  # Default fallback

        if wait_seconds <= 0:
            return

        console.print(f"[yellow]Rate limited. Waiting {wait_seconds}s for reset...[/yellow]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Waiting for rate limit reset", total=wait_seconds)
            for _ in range(wait_seconds):
                time.sleep(1)
                progress.advance(task)

    def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> httpx.Response:
        """Make a request with rate limit handling."""
        for _attempt in range(max_retries):
            response = self.client.get(endpoint, params=params)

            # Check for rate limiting (403 Forbidden or 429 Too Many Requests)
            rate_limit_codes = (403, 429)
            if response.status_code in rate_limit_codes:
                remaining = response.headers.get("X-RateLimit-Remaining", "1")
                if remaining == "0" or response.status_code == rate_limit_codes[1]:
                    self._wait_for_rate_limit(response)
                    continue  # Retry after waiting

            response.raise_for_status()

            # Proactively pause if approaching rate limit
            remaining = int(response.headers.get("X-RateLimit-Remaining", "9999"))
            if remaining < RATE_LIMIT_BUFFER:
                console.print(
                    f"[yellow]Approaching rate limit ({remaining} remaining). "
                    f"Pausing to preserve buffer...[/yellow]"
                )
                self._wait_for_rate_limit(response)

            return response

        # If we exhausted retries, raise the last error
        response.raise_for_status()
        return response  # Never reached, but keeps type checker happy

    def _paginate(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Paginate through API results, yielding each item."""
        params = params or {}
        page = 1
        while True:
            response = self._request(endpoint, params={**params, "per_page": 100, "page": page})
            items = response.json()
            if not items:
                break
            yield from items
            page += 1

    def _cached_fetch(
        self,
        cache_key: str,
        fetcher: Callable[[], Any],
        ttl: int | None = TTL_IMMUTABLE,
    ) -> Any:
        """Fetch with caching, gracefully handling API errors."""
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            result = fetcher()
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return None

        self.cache.set(cache_key, result, expire=ttl)
        return result

    def get_user_repos(self, username: str) -> list[str]:
        """Get all repositories for a user."""
        cache_key = f"user_repos:{username}"

        def fetch() -> list[str]:
            repos_iter = self._paginate(f"/users/{username}/repos", {"type": "owner"})
            return [repo["full_name"] for repo in repos_iter]

        return self._cached_fetch(cache_key, fetch, TTL_MUTABLE) or []

    def get_merged_prs(
        self,
        repo: str,
        username: str,
        since: datetime,
    ) -> list[dict[str, Any]]:
        """Get merged PRs for a repo by a user since a date."""
        since_str = since.strftime("%Y-%m-%d")
        cache_key = f"merged_prs:{repo}:{username}:{since_str}"

        def fetch() -> list[dict[str, Any]]:
            result = []
            params = {"state": "closed", "sort": "updated", "direction": "desc"}
            for pr in self._paginate(f"/repos/{repo}/pulls", params):
                if pr["merged_at"] is None:
                    continue
                merged_at = datetime.fromisoformat(pr["merged_at"])
                if merged_at.replace(tzinfo=None) < since:
                    continue
                if pr["user"]["login"] == username:
                    result.append(pr)
            return result

        return self._cached_fetch(cache_key, fetch, TTL_MUTABLE) or []

    def get_default_branch(self, repo: str) -> str | None:
        """Get the default branch for a repository."""
        cache_key = f"default_branch:{repo}"

        def fetch() -> str:
            response = self._request(f"/repos/{repo}")
            branch: str = response.json()["default_branch"]
            return branch

        result: str | None = self._cached_fetch(cache_key, fetch, TTL_MUTABLE)
        return result

    def get_branch_commits(
        self,
        repo: str,
        branch: str,
        username: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Get commits on a branch by a user within a date range."""
        since_str = since.strftime("%Y-%m-%d")
        until_str = until.strftime("%Y-%m-%d")
        cache_key = f"branch_commits:{repo}:{branch}:{username}:{since_str}:{until_str}"

        def fetch() -> list[dict[str, Any]]:
            params = {
                "sha": branch,
                "author": username,
                "since": since.isoformat(),
                "until": until.isoformat(),
            }
            return list(self._paginate(f"/repos/{repo}/commits", params))

        return self._cached_fetch(cache_key, fetch, TTL_MUTABLE) or []

    def get_pr_commits(self, repo: str, pr_number: int) -> list[str]:
        """Get all commit SHAs in a PR."""
        cache_key = f"pr_commits:{repo}:{pr_number}"

        def fetch() -> list[str]:
            return [c["sha"] for c in self._paginate(f"/repos/{repo}/pulls/{pr_number}/commits")]

        return self._cached_fetch(cache_key, fetch, TTL_IMMUTABLE) or []

    def get_commit_stats(self, repo: str, sha: str) -> tuple[int, int, dict[str, FileStats]]:
        """Get additions and deletions for a single commit."""
        cache_key = f"commit_stats:{repo}:{sha}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        try:
            response = self._request(f"/repos/{repo}/commits/{sha}")
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return 0, 0, {}

        return self._parse_file_stats(response.json().get("files", []), cache_key)

    def get_pr_stats_per_commit(
        self, repo: str, pr_number: int
    ) -> tuple[int, int, dict[str, FileStats]]:
        """Get total additions/deletions across all commits in a PR."""
        cache_key = f"pr_stats_per_commit:{repo}:{pr_number}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_additions = 0
        total_deletions = 0

        for sha in self.get_pr_commits(repo, pr_number):
            add, del_, ext_stats = self.get_commit_stats(repo, sha)
            total_additions += add
            total_deletions += del_
            for ext, stats in ext_stats.items():
                by_extension[ext].additions += stats.additions
                by_extension[ext].deletions += stats.deletions

        ext_data = {ext: stats.to_tuple() for ext, stats in by_extension.items()}
        self.cache.set(cache_key, (total_additions, total_deletions, ext_data))
        return total_additions, total_deletions, dict(by_extension)

    def get_pr_stats_net(self, repo: str, pr_number: int) -> tuple[int, int, dict[str, FileStats]]:
        """Get net additions/deletions for a PR (final diff only)."""
        cache_key = f"pr_stats_net:{repo}:{pr_number}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        # Fetch PR files
        files_cache_key = f"pr_files:{repo}:{pr_number}"
        files = self.cache.get(files_cache_key)
        if files is None:
            try:
                files = list(self._paginate(f"/repos/{repo}/pulls/{pr_number}/files"))
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                files = []
            self.cache.set(files_cache_key, files)

        return self._parse_file_stats(files, cache_key)

    def _parse_file_stats(
        self,
        files: list[dict[str, Any]],
        cache_key: str,
    ) -> tuple[int, int, dict[str, FileStats]]:
        """Parse file stats from API response and cache result."""
        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_additions = 0
        total_deletions = 0

        for file in files:
            ext = _get_file_extension(file["filename"])
            additions = file.get("additions", 0)
            deletions = file.get("deletions", 0)
            by_extension[ext].additions += additions
            by_extension[ext].deletions += deletions
            total_additions += additions
            total_deletions += deletions

        ext_data = {ext: stats.to_tuple() for ext, stats in by_extension.items()}
        self.cache.set(cache_key, (total_additions, total_deletions, ext_data))
        return total_additions, total_deletions, dict(by_extension)


# =============================================================================
# Helper Functions
# =============================================================================


def _get_file_extension(filename: str) -> str:
    """Extract file extension from filename."""
    path = Path(filename)
    return path.suffix.lower() if path.suffix else path.name.lower()


def _get_github_token() -> str:
    """Get GitHub token from gh CLI."""
    result = subprocess.run(
        ["gh", "auth", "token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _get_cache(no_cache: bool) -> diskcache.Cache:  # noqa: FBT001
    """Get disk cache or in-memory cache."""
    if no_cache:
        return diskcache.Cache(":memory:")
    return diskcache.Cache(str(CACHE_DIR))


# =============================================================================
# Processing Functions
# =============================================================================


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


# =============================================================================
# Display Functions
# =============================================================================


def _display_pr_table(
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


def _display_direct_commits_table(
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


def _display_extension_table(
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


def _display_summary(aggregator: StatsAggregator, since: str, *, per_commit: bool) -> None:
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


# =============================================================================
# CLI Commands
# =============================================================================


@app.command()
def count(  # noqa: PLR0913
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

    token = _get_github_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    cache = _get_cache(no_cache)
    aggregator = StatsAggregator()

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
        gh = GitHubClient(client, cache)

        fetch_task = progress.add_task("Fetching repositories...", total=None)
        repos = gh.get_user_repos(username)
        progress.remove_task(fetch_task)

        repo_task = progress.add_task("Processing repos", total=len(repos))

        for repo in repos:
            progress.update(repo_task, description=f"[cyan]{repo}[/cyan]")

            for pr in gh.get_merged_prs(repo, username, since_date):
                merged_at = datetime.fromisoformat(pr["merged_at"])
                if merged_at.replace(tzinfo=None) > until_date:
                    continue
                _process_pr(gh, repo, pr, per_commit, aggregator, include_direct_commits)

            if include_direct_commits:
                _process_direct_commits(gh, repo, username, since_date, until_date, aggregator)

            progress.advance(repo_task)

    if aggregator.prs:
        _display_pr_table(aggregator.prs, username, since, until)

    if aggregator.direct_commits:
        console.print()
        _display_direct_commits_table(aggregator.direct_commits, username, since, until)

    if show_extensions and aggregator.by_extension:
        console.print()
        _display_extension_table(
            aggregator.by_extension, aggregator.total_additions, aggregator.total_deletions
        )

    _display_summary(aggregator, since, per_commit=per_commit)


@app.command()
def clear_cache() -> None:
    """Clear the disk cache."""
    cache = diskcache.Cache(str(CACHE_DIR))
    cache.clear()
    console.print("[green]Cache cleared![/green]")


if __name__ == "__main__":
    app()
