"""GitHub API client with caching and pagination."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from trueloc.models import FileStats
from trueloc.utils import RATE_LIMIT_BUFFER, TTL_IMMUTABLE, TTL_MUTABLE, get_file_extension

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import diskcache  # type: ignore[import-untyped]

console = Console()


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
            ext = get_file_extension(file["filename"])
            additions = file.get("additions", 0)
            deletions = file.get("deletions", 0)
            by_extension[ext].additions += additions
            by_extension[ext].deletions += deletions
            total_additions += additions
            total_deletions += deletions

        return total_additions, total_deletions, dict(by_extension)
