"""Count lines of code from GitHub pull requests."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import dateparser  # type: ignore[import-untyped]
import diskcache  # type: ignore[import-untyped]
import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

app = typer.Typer(
    help="Count lines of code from GitHub pull requests.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
console = Console()

CACHE_DIR = Path.home() / ".cache" / "trueloc"
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

    def _calc_rate_limit_wait(self, response: httpx.Response) -> int:
        """Calculate seconds to wait for rate limit reset."""
        reset_timestamp = int(response.headers.get("X-RateLimit-Reset", 0))
        retry_after = int(response.headers.get("Retry-After", 0))

        if retry_after > 0:
            return retry_after
        if reset_timestamp > 0:
            return max(0, reset_timestamp - int(time.time()) + 1)
        return 60  # Default fallback

    def _show_wait_progress(self, wait_seconds: int, message: str) -> None:
        """Show a countdown progress bar."""
        console.print(f"[yellow]{message}[/yellow]")
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

    def _wait_for_rate_limit(self, response: httpx.Response) -> None:
        """Wait for rate limit to reset, showing countdown progress bar."""
        wait_seconds = self._calc_rate_limit_wait(response)
        if wait_seconds > 0:
            msg = f"Rate limited. Waiting {wait_seconds}s for reset..."
            self._show_wait_progress(wait_seconds, msg)

    def _is_rate_limited(self, response: httpx.Response) -> bool:
        """Check if response indicates rate limiting."""
        rate_limit_codes = (403, 429)  # Forbidden, Too Many Requests
        if response.status_code not in rate_limit_codes:
            return False
        remaining = response.headers.get("X-RateLimit-Remaining", "1")
        return remaining == "0" or response.status_code == rate_limit_codes[1]

    def _check_rate_limit_buffer(self, response: httpx.Response) -> None:
        """Proactively pause if approaching rate limit buffer."""
        remaining = int(response.headers.get("X-RateLimit-Remaining", "9999"))
        if remaining < RATE_LIMIT_BUFFER:
            msg = f"Approaching rate limit ({remaining} remaining). Pausing to preserve buffer..."
            self._show_wait_progress(self._calc_rate_limit_wait(response), msg)

    def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> httpx.Response:
        """Make a request with rate limit handling."""
        response: httpx.Response | None = None
        for _attempt in range(max_retries):
            response = self.client.get(endpoint, params=params)

            if self._is_rate_limited(response):
                self._wait_for_rate_limit(response)
                continue

            response.raise_for_status()
            self._check_rate_limit_buffer(response)
            return response

        # Exhausted retries - raise the last response's error or a generic one
        if response is not None:
            response.raise_for_status()
        msg = f"Request to {endpoint} failed after {max_retries} retries"
        raise httpx.HTTPStatusError(msg, request=None, response=None)  # type: ignore[arg-type]

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

    def _fetch_prs_in_range(
        self,
        repo: str,
        username: str,
        since: datetime,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch merged PRs in a date range from the API."""
        result = []
        params = {"state": "closed", "sort": "updated", "direction": "desc"}
        for pr in self._paginate(f"/repos/{repo}/pulls", params):
            if pr["merged_at"] is None:
                continue
            merged_at = datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None)
            if merged_at < since:
                continue
            if until and merged_at >= until:
                continue
            if pr["user"]["login"] == username:
                result.append(pr)
        return result

    def _filter_prs_since(self, prs: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
        """Filter PRs to only those merged on or after the given date."""
        return [
            pr
            for pr in prs
            if datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None) >= since
        ]

    def _save_pr_cache(self, cache_key: str, since: datetime, prs: list[dict[str, Any]]) -> None:
        """Save PRs to cache with the given watermark date."""
        self.cache.set(
            cache_key,
            {"cached_since": since.isoformat(), "prs": prs},
            expire=TTL_MUTABLE,
        )

    def get_merged_prs(
        self,
        repo: str,
        username: str,
        since: datetime,
    ) -> list[dict[str, Any]]:
        """Get merged PRs for a repo by a user since a date.

        Uses smart range-aware caching:
        - If cached range covers requested range, filter locally (instant)
        - If requesting older data, fetch only the gap and merge
        """
        cache_key = f"merged_prs_v2:{repo}:{username}"
        cached = self.cache.get(cache_key)

        if cached is None:
            prs = self._fetch_prs_in_range(repo, username, since)
            self._save_pr_cache(cache_key, since, prs)
            return prs

        cached_since = datetime.fromisoformat(cached["cached_since"])
        prs = cached["prs"]

        # Requested range is within cached range - filter locally!
        if since >= cached_since:
            return self._filter_prs_since(prs, since)

        # Requesting older data - fetch the gap and merge
        gap_prs = self._fetch_prs_in_range(repo, username, since, cached_since)
        all_prs = gap_prs + prs
        self._save_pr_cache(cache_key, since, all_prs)
        return self._filter_prs_since(all_prs, since)

    def get_default_branch(self, repo: str) -> str | None:
        """Get the default branch for a repository."""
        cache_key = f"default_branch:{repo}"

        def fetch() -> str:
            response = self._request(f"/repos/{repo}")
            branch: str = response.json()["default_branch"]
            return branch

        result: str | None = self._cached_fetch(cache_key, fetch, TTL_MUTABLE)
        return result

    def _fetch_commits_in_range(
        self,
        repo: str,
        branch: str,
        username: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch commits in a date range from the API."""
        params = {
            "sha": branch,
            "author": username,
            "since": since.isoformat(),
            "until": until.isoformat(),
        }
        return list(self._paginate(f"/repos/{repo}/commits", params))

    def _filter_commits_in_range(
        self,
        commits: list[dict[str, Any]],
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Filter commits to only those within the given date range."""
        result = []
        for commit in commits:
            date_str = commit["commit"]["author"]["date"]
            commit_date = datetime.fromisoformat(date_str).replace(tzinfo=None)
            if since <= commit_date <= until:
                result.append(commit)
        return result

    def _save_commits_cache(
        self,
        cache_key: str,
        since: datetime,
        until: datetime,
        commits: list[dict[str, Any]],
    ) -> None:
        """Save commits to cache with watermark dates."""
        self.cache.set(
            cache_key,
            {
                "cached_since": since.isoformat(),
                "cached_until": until.isoformat(),
                "commits": commits,
            },
            expire=TTL_MUTABLE,
        )

    def get_branch_commits(
        self,
        repo: str,
        branch: str,
        username: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Get commits on a branch by a user within a date range.

        Uses smart range-aware caching:
        - If cached range covers requested range, filter locally (instant)
        - If requesting older/newer data, fetch only the gap and merge
        """
        cache_key = f"branch_commits_v2:{repo}:{branch}:{username}"
        cached = self.cache.get(cache_key)

        if cached is None:
            commits = self._fetch_commits_in_range(repo, branch, username, since, until)
            self._save_commits_cache(cache_key, since, until, commits)
            return commits

        cached_since = datetime.fromisoformat(cached["cached_since"])
        cached_until = datetime.fromisoformat(cached["cached_until"])
        commits = cached["commits"]

        # Requested range is within cached range - filter locally!
        if since >= cached_since and until <= cached_until:
            return self._filter_commits_in_range(commits, since, until)

        # Need to expand the cached range
        new_since = min(since, cached_since)
        new_until = max(until, cached_until)

        # Fetch older commits if needed
        if since < cached_since:
            older_commits = self._fetch_commits_in_range(
                repo, branch, username, since, cached_since
            )
            commits = older_commits + commits

        # Fetch newer commits if needed
        if until > cached_until:
            newer_commits = self._fetch_commits_in_range(
                repo, branch, username, cached_until, until
            )
            commits = commits + newer_commits

        self._save_commits_cache(cache_key, new_since, new_until, commits)
        return self._filter_commits_in_range(commits, since, until)

    def get_pr_commits_raw(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get all commits in a PR (raw API response, cached forever)."""
        cache_key = f"pr_commits_raw:{repo}:{pr_number}"

        def fetch() -> list[dict[str, Any]]:
            return list(self._paginate(f"/repos/{repo}/pulls/{pr_number}/commits"))

        return self._cached_fetch(cache_key, fetch, TTL_IMMUTABLE) or []

    def get_pr_commits(self, repo: str, pr_number: int) -> list[str]:
        """Get all commit SHAs in a PR."""
        return [c["sha"] for c in self.get_pr_commits_raw(repo, pr_number)]

    def get_commit_raw(self, repo: str, sha: str) -> dict[str, Any] | None:
        """Get full commit data (raw API response, cached forever)."""
        cache_key = f"commit_raw:{repo}:{sha}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            result: dict[str, Any] = cached
            return result

        try:
            response = self._request(f"/repos/{repo}/commits/{sha}")
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return None

        data: dict[str, Any] = response.json()
        self.cache.set(cache_key, data, expire=TTL_IMMUTABLE)
        return data

    def get_commit_stats(self, repo: str, sha: str) -> tuple[int, int, dict[str, FileStats]]:
        """Get additions and deletions for a single commit.

        Uses cached raw commit data, also caches processed stats for speed.
        """
        # Check processed cache first (fast path)
        stats_cache_key = f"commit_stats:{repo}:{sha}"
        cached = self.cache.get(stats_cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        # Get raw data (cached separately for flexibility)
        raw = self.get_commit_raw(repo, sha)
        if raw is None:
            return 0, 0, {}

        # Extract and cache processed stats
        result = self._extract_file_stats(raw.get("files", []))
        ext_data = {ext: stats.to_tuple() for ext, stats in result[2].items()}
        self.cache.set(stats_cache_key, (result[0], result[1], ext_data), expire=TTL_IMMUTABLE)
        return result

    def get_pr_files_raw(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get all files changed in a PR (raw API response, cached forever)."""
        cache_key = f"pr_files_raw:{repo}:{pr_number}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            result: list[dict[str, Any]] = cached
            return result

        try:
            files: list[dict[str, Any]] = list(
                self._paginate(f"/repos/{repo}/pulls/{pr_number}/files")
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            files = []

        self.cache.set(cache_key, files, expire=TTL_IMMUTABLE)
        return files

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
        """Get net additions/deletions for a PR (final diff only).

        Uses cached raw PR files, also caches processed stats for speed.
        """
        # Check processed cache first (fast path)
        stats_cache_key = f"pr_stats_net:{repo}:{pr_number}"
        cached = self.cache.get(stats_cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        # Get raw files (cached separately for flexibility)
        files = self.get_pr_files_raw(repo, pr_number)

        # Extract and cache processed stats
        result = self._extract_file_stats(files)
        ext_data = {ext: stats.to_tuple() for ext, stats in result[2].items()}
        self.cache.set(stats_cache_key, (result[0], result[1], ext_data), expire=TTL_IMMUTABLE)
        return result

    def _extract_file_stats(
        self,
        files: list[dict[str, Any]],
    ) -> tuple[int, int, dict[str, FileStats]]:
        """Extract file stats from API response (no caching, pure extraction)."""
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


def _parse_date(date_str: str) -> datetime:
    """Parse a date string using dateparser.

    Supports:
    - Relative: "5d", "2w", "3m", "1y", "5 days ago", "last week", "last month"
    - Absolute: "2024-01-01", "Jan 1 2024"
    """
    # Handle shorthand like "5d", "2w", "3m", "1y"
    shorthand = re.match(r"^(\d+)([dwmy])$", date_str.strip().lower())
    if shorthand:
        num, unit = shorthand.groups()
        unit_map = {"d": "days", "w": "weeks", "m": "months", "y": "years"}
        date_str = f"{num} {unit_map[unit]} ago"

    parsed: datetime | None = dateparser.parse(
        date_str,
        settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    if parsed is None:
        msg = f"Could not parse date: {date_str!r}"
        raise typer.BadParameter(msg)
    return parsed


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


def _output_json(
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


# =============================================================================
# CLI Commands
# =============================================================================


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
    output_json: bool = typer.Option(
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
    since_date = _parse_date(since)
    until_date = _parse_date(until) if until else datetime.now()  # noqa: DTZ005

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
            TextColumn("[cyan]{task.fields[status]}[/cyan]"),
            console=console,
            disable=output_json,  # Suppress progress when outputting JSON
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

    if output_json:
        _output_json(aggregator, username, since, until, per_commit=per_commit)
    else:
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


# =============================================================================
# Local Git Repository Counting
# =============================================================================


@dataclass
class LocalCommitStats:
    """Statistics for a local git commit."""

    sha: str
    message: str
    additions: int
    deletions: int
    committed_at: str
    by_extension: dict[str, FileStats] = field(default_factory=dict)


def _run_git(repo_path: Path, *args: str) -> str:
    """Run a git command in the specified repository."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _get_local_commits(
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
    output = _run_git(repo_path, *args)

    commits = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:  # noqa: PLR2004
            commits.append({
                "sha": parts[0],
                "date": parts[1][:10],  # Just the date part
                "message": parts[2],
            })
    return commits


def _get_commit_numstat(repo_path: Path, sha: str) -> tuple[int, int, dict[str, FileStats]]:
    """Get additions/deletions for a commit using git show --numstat.

    Returns (total_additions, total_deletions, by_extension).
    """
    try:
        output = _run_git(repo_path, "show", "--numstat", "--format=", sha)
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
        ext = _get_file_extension(filename)

        total_add += additions
        total_del += deletions
        by_extension[ext].additions += additions
        by_extension[ext].deletions += deletions

    return total_add, total_del, dict(by_extension)


def _display_local_commits_table(
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


def _output_local_json(
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


@app.command("count-local")
def count_local(
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
    output_json: bool = typer.Option(
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

    since_date = _parse_date(since)
    until_date = _parse_date(until) if until else datetime.now()  # noqa: DTZ005

    repo_name = path.name

    # Get commits
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.fields[status]}[/cyan]"),
        console=console,
        disable=output_json,
    ) as progress:
        fetch_task = progress.add_task("Finding commits...", total=None, status="")
        raw_commits = _get_local_commits(path, author, since_date, until_date, no_merges=not include_merges)
        progress.remove_task(fetch_task)

        if not raw_commits:
            if not output_json:
                console.print(f"[yellow]No commits found for {author} in {repo_name}[/yellow]")
            return

        # Process each commit
        commits: list[LocalCommitStats] = []
        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_additions = 0
        total_deletions = 0

        commit_task = progress.add_task(
            f"Processing commits (0/{len(raw_commits)})",
            total=len(raw_commits),
            status="",
        )

        for idx, raw in enumerate(raw_commits, 1):
            progress.update(
                commit_task,
                description=f"Processing commits ({idx}/{len(raw_commits)})",
                status=raw["sha"][:7],
            )

            add, del_, ext_stats = _get_commit_numstat(path, raw["sha"])

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

    if output_json:
        _output_local_json(
            commits, dict(by_extension), total_additions, total_deletions,
            repo_name, author, since, until
        )
    else:
        _display_local_commits_table(commits, repo_name, author, since, until)

        if show_extensions and by_extension:
            console.print()
            _display_extension_table(dict(by_extension), total_additions, total_deletions)

        console.print()
        console.print(f"[bold]Total commits:[/bold] {len(commits)}")
        console.print(f"[bold green]Total additions:[/bold green] +{total_additions:,}")
        console.print(f"[bold red]Total deletions:[/bold red] -{total_deletions:,}")
        total = total_additions + total_deletions
        console.print(f"[bold]Total lines changed:[/bold] {total:,}")


if __name__ == "__main__":
    app()
