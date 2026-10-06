"""GitHub API client with caching and pagination."""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from trueloc.local import get_commits_numstat, get_existing_commits, get_pr_commits_local
from trueloc.models import FileStats
from trueloc.utils import (
    PR_REFRESH_INTERVAL,
    RATE_LIMIT_BUFFER,
    REFRESH_OVERLAP,
    TTL_IMMUTABLE,
    TTL_MUTABLE,
    get_file_extension,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import diskcache  # type: ignore[import-untyped]

    from trueloc.mirror import RepoMirrors

console = Console(stderr=True)  # Keep stdout clean for --json

CONTRIBUTED_REPOS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      pullRequestContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
      }
    }
  }
}
"""
MAX_CONTRIBUTED_REPOS = 100  # maxRepositories limit of contributionsCollection
MAX_CONTRIBUTIONS_WINDOW = timedelta(days=365)  # contributionsCollection spans at most a year
# PR contributions count when a PR is opened, so look back to catch PRs merged later
CONTRIBUTIONS_LOOKBACK = timedelta(days=365)


SEARCH_PRS_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        mergedAt
        updatedAt
        author { login }
        mergeCommit { oid }
        repository { nameWithOwner diskUsage }
        commits { totalCount }
      }
    }
  }
}
"""
MAX_SEARCH_RESULTS = 1000  # GitHub search returns at most this many results per query
# Clone a repo once this many commits need stats; below that, API requests are cheaper
LOCAL_MIN_COMMITS = 20
MAX_MIRROR_SIZE_KB = 1_000_000  # Don't clone repos larger than this (GitHub's size in KB)


def _pr_key(pr: dict[str, Any]) -> tuple[str | None, int]:
    """Identify a PR; PRs from search span repos, so include the repo."""
    return pr.get("repo"), pr["number"]


def _search_node_to_pr(node: dict[str, Any]) -> dict[str, Any]:
    """Convert a GraphQL search node to the REST-shaped PR fields used elsewhere."""
    return {
        "number": node["number"],
        "title": node["title"],
        "merged_at": node["mergedAt"],
        "updated_at": node["updatedAt"],
        "merge_commit_sha": (node["mergeCommit"] or {}).get("oid"),
        "user": {"login": (node["author"] or {}).get("login", "")},
        "repo": node["repository"]["nameWithOwner"],
        "commit_count": node["commits"]["totalCount"],
        "disk_usage": node["repository"]["diskUsage"],
    }


class GraphQLError(Exception):
    """GraphQL request returned errors (GraphQL reports these with HTTP 200)."""


class GitHubClient:
    """GitHub API client with caching and pagination."""

    def __init__(
        self,
        client: httpx.Client,
        cache: diskcache.Cache,
        mirrors: RepoMirrors | None = None,
    ) -> None:
        self.client = client
        self.cache = cache
        # Local clones to compute commit stats without API requests (None: API only)
        self.mirrors = mirrors

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
        *,
        method: str = "GET",
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make a request with rate limit handling."""
        response: httpx.Response | None = None
        for _attempt in range(max_retries):
            response = self.client.request(method, endpoint, params=params, json=json)

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

    def _warn_skipped(self, repo: str, what: str, error: Exception | None = None) -> None:
        """Report a skipped API call, so missing data is not silent."""
        if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
            reason = f" (HTTP {error.response.status_code})"
        elif isinstance(error, GraphQLError):
            reason = f" (GraphQL: {error})"
        elif error is not None:
            reason = f" ({type(error).__name__})"
        else:
            reason = ""
        console.print(f"[yellow]Skipping {what} for {repo}{reason}[/yellow]")

    def get_user_repos(self, username: str) -> list[str]:
        """Get all repositories for a user."""
        cache_key = f"user_repos_v2:{username}"

        def fetch() -> list[str]:
            repos_iter = self._paginate(f"/users/{username}/repos", {"type": "owner"})
            # Forks contain synced upstream commits; PRs merged in forks are found via
            # get_contributed_repos
            return [repo["full_name"] for repo in repos_iter if not repo["fork"]]

        return self._cached_fetch(cache_key, fetch, TTL_MUTABLE) or []

    def get_active_owned_forks(self, username: str, since: datetime) -> list[str]:
        """Get the user's forks pushed to since `since` (not cached, pushed_at changes)."""
        try:
            repos = list(self._paginate(f"/users/{username}/repos", {"type": "owner"}))
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            self._warn_skipped(username, "listing forks", e)
            return []
        return [
            repo["full_name"]
            for repo in repos
            if repo["fork"]
            and repo["pushed_at"]
            and datetime.fromisoformat(repo["pushed_at"]).replace(tzinfo=None) >= since
        ]

    def get_fork_parent(self, repo: str) -> dict[str, str] | None:
        """Get a fork's default branch and its parent, or None if it is not a fork.

        Not cached, as a parent can rename its default branch.
        """
        try:
            data = self._request(f"/repos/{repo}").json()
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            self._warn_skipped(repo, "fork details", e)
            return None
        parent = data.get("parent")
        if parent is None:
            return None
        return {
            "branch": data["default_branch"],
            "parent": parent["full_name"],
            "parent_branch": parent["default_branch"],
        }

    def get_fork_commits(
        self,
        repo: str,
        username: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Get commits by the user on a fork's default branch that its parent doesn't have.

        Comparing against the parent excludes upstream commits synced into the fork.
        """

        info = self.get_fork_parent(repo)
        if info is None:
            return []
        parent_owner, parent_name = info["parent"].split("/")
        base = f"{parent_owner}:{parent_name}:{info['parent_branch']}"
        endpoint = f"/repos/{repo}/compare/{base}...{info['branch']}"

        commits: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                response = self._request(endpoint, params={"per_page": 100, "page": page})
                page_commits = response.json()["commits"]
                commits.extend(page_commits)
                if len(page_commits) < 100:  # noqa: PLR2004
                    break
                page += 1
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            self._warn_skipped(repo, "fork commits", e)
            return []

        mine = [
            c for c in commits if c["author"] and c["author"]["login"].lower() == username.lower()
        ]
        return self._filter_commits_in_range(mine, since, until)

    def _fetch_contributed_repos(self, username: str, since: datetime, until: datetime) -> set[str]:
        """Fetch repos with PR or commit contributions in a range of at most a year."""
        variables = {
            "login": username,
            # Naive datetimes are local time
            "from": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        response = self._request(
            "/graphql",
            method="POST",
            json={"query": CONTRIBUTED_REPOS_QUERY, "variables": variables},
        )
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            # Unknown login (e.g. an organization): nothing to discover
            if all(error.get("type") == "NOT_FOUND" for error in errors):
                return set()
            raise GraphQLError("; ".join(error.get("message", "") for error in errors))
        collection = payload["data"]["user"]["contributionsCollection"]
        groups = [
            collection["pullRequestContributionsByRepository"],
            collection["commitContributionsByRepository"],
        ]
        repos = {c["repository"]["nameWithOwner"] for group in groups for c in group}

        # Results were truncated: split the range in half
        truncated = any(len(group) >= MAX_CONTRIBUTED_REPOS for group in groups)
        if truncated and until - since > timedelta(days=1):
            middle = since + (until - since) / 2
            repos |= self._fetch_contributed_repos(username, since, middle)
            repos |= self._fetch_contributed_repos(username, middle, until)
        return repos

    def get_contributed_repos(self, username: str, since: datetime, until: datetime) -> list[str]:
        """Get all repos (including other owners' and private) the user contributed to."""
        repos: set[str] = set()
        start = since - CONTRIBUTIONS_LOOKBACK
        try:
            while start < until:
                end = min(start + MAX_CONTRIBUTIONS_WINDOW, until)
                repos |= self._fetch_contributed_repos(username, start, end)
                start = end
        except (httpx.HTTPStatusError, httpx.TimeoutException, GraphQLError) as e:
            self._warn_skipped(username, "discovering contributed repos", e)
        return sorted(repos)

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
            # Sorted by updated_at desc, and merged_at <= updated_at, so no later PR can match
            updated_at = pr.get("updated_at")
            if updated_at and datetime.fromisoformat(updated_at).replace(tzinfo=None) < since:
                break
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

    def _save_pr_cache(
        self,
        cache_key: str,
        since: datetime,
        until: datetime,
        prs: list[dict[str, Any]],
    ) -> None:
        """Save PRs to cache with the given watermark dates.

        Uses TTL_IMMUTABLE since range-aware caching fetches PRs merged after `until`
        (once older than PR_REFRESH_INTERVAL) and older gaps before `since`.
        """
        self.cache.set(
            cache_key,
            {"cached_since": since.isoformat(), "cached_until": until.isoformat(), "prs": prs},
            expire=TTL_IMMUTABLE,
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
        - If the cache is older than PR_REFRESH_INTERVAL, fetch PRs merged since then
        - If requesting older data, fetch only the gap and merge
        """
        cache_key = f"merged_prs_v2:{repo}:{username}"

        def fetch(start: datetime, end: datetime | None) -> list[dict[str, Any]]:
            return self._fetch_prs_in_range(repo, username, start, end)

        prs = self._get_prs_range_cached(cache_key, since, fetch, repo)
        return [] if prs is None else prs

    def _get_prs_range_cached(
        self,
        cache_key: str,
        since: datetime,
        fetch: Callable[[datetime, datetime | None], list[dict[str, Any]]],
        label: str,
    ) -> list[dict[str, Any]] | None:
        """Get merged PRs since a date with range-aware caching.

        `fetch(start, end)` fetches PRs merged in [start, end), or since `start` if `end`
        is None. Returns None if nothing was cached and fetching failed.
        """
        cached = self.cache.get(cache_key)
        now = datetime.now(UTC).replace(tzinfo=None)

        if cached is None:
            try:
                prs = fetch(since, None)
            except (httpx.HTTPStatusError, httpx.TimeoutException, GraphQLError) as e:
                self._warn_skipped(label, "PRs", e)
                return None
            self._save_pr_cache(cache_key, since, now, prs)
            return prs

        cached_since = datetime.fromisoformat(cached["cached_since"])
        # Entries written before cached_until existed: unknown fetch time, refresh all
        cached_until = datetime.fromisoformat(cached.get("cached_until", cached["cached_since"]))
        prs = cached["prs"]

        # Requested range is within cached range and cache is fresh - filter locally!
        if since >= cached_since and now - cached_until < PR_REFRESH_INTERVAL:
            return self._filter_prs_since(prs, since)

        try:
            # Fetch PRs merged after the cache was written
            if now - cached_until >= PR_REFRESH_INTERVAL:
                # Overlap the previous fetch to tolerate clock skew; dedup below
                newer_prs = fetch(cached_until - REFRESH_OVERLAP, None)
                newer_keys = {_pr_key(pr) for pr in newer_prs}
                prs = newer_prs + [pr for pr in prs if _pr_key(pr) not in newer_keys]
                cached_until = now

            # Requesting older data - fetch the gap and merge
            if since < cached_since:
                prs = prs + fetch(since, cached_since)
                cached_since = since
        except (httpx.HTTPStatusError, httpx.TimeoutException, GraphQLError) as e:
            # Don't save, so the missing range is retried next run
            self._warn_skipped(label, "new or older PRs", e)
            return self._filter_prs_since(prs, since)

        self._save_pr_cache(cache_key, cached_since, cached_until, prs)
        return self._filter_prs_since(prs, since)

    def search_merged_prs(
        self,
        username: str,
        since: datetime,
        until: datetime,
        repo: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Get PRs by a user merged in [since, until] across all repos, via GraphQL search.

        A few requests per thousand PRs, instead of listing every repo's PRs. PRs have
        the REST fields used elsewhere, plus `repo`, `commit_count`, and `disk_usage`
        (KB). Returns None if the search failed, so callers can fall back to REST.
        """
        cache_key = f"merged_prs_search:{username}:{repo or ''}"
        # Search dates are in UTC, while `since`/`until` are naive local times
        since_utc = since.astimezone(UTC).replace(tzinfo=None)
        until_utc = until.astimezone(UTC).replace(tzinfo=None)

        def fetch(start: datetime, end: datetime | None) -> list[dict[str, Any]]:
            end = end or datetime.now(UTC).replace(tzinfo=None)
            return self._search_prs_in_window(username, start, end, repo)

        prs = self._get_prs_range_cached(cache_key, since_utc, fetch, username)
        if prs is None:
            return None
        return [
            pr
            for pr in prs
            if datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None) <= until_utc
        ]

    def _search_prs_in_window(
        self,
        username: str,
        start: datetime,
        end: datetime,
        repo: str | None,
    ) -> list[dict[str, Any]]:
        """Search PRs merged in [start, end] (naive UTC), splitting busy windows."""
        query = f"is:pr is:merged author:{username}"
        if repo:
            query += f" repo:{repo}"
        query += f" merged:{start:%Y-%m-%dT%H:%M:%SZ}..{end:%Y-%m-%dT%H:%M:%SZ}"
        prs: list[dict[str, Any]] = []
        cursor = None
        while True:
            search = self._graphql_search(query, cursor)
            # Search returns at most 1000 results per query: split the window in half
            too_many = cursor is None and search["issueCount"] > MAX_SEARCH_RESULTS
            if too_many and end - start > timedelta(minutes=1):
                middle = start + (end - start) / 2
                return self._search_prs_in_window(
                    username, start, middle, repo
                ) + self._search_prs_in_window(username, middle, end, repo)
            prs.extend(_search_node_to_pr(node) for node in search["nodes"] if node)
            if not search["pageInfo"]["hasNextPage"]:
                return prs
            cursor = search["pageInfo"]["endCursor"]

    def _graphql_search(self, query: str, cursor: str | None) -> dict[str, Any]:
        response = self._request(
            "/graphql",
            method="POST",
            json={"query": SEARCH_PRS_QUERY, "variables": {"q": query, "cursor": cursor}},
        )
        payload = response.json()
        search = (payload.get("data") or {}).get("search")
        if search is None:
            errors = payload.get("errors") or []
            raise GraphQLError("; ".join(error.get("message", "") for error in errors))
        # Inaccessible PRs (e.g. organizations enforcing SAML) are null nodes with errors
        skipped = sum(1 for node in search["nodes"] if not node)
        if skipped:
            console.print(f"[yellow]Skipping {skipped} PR(s) the token can't access[/yellow]")
        result: dict[str, Any] = search
        return result

    def _get_repo_info(self, repo: str) -> dict[str, Any] | None:
        """Get a repository's default branch and size (KB)."""
        cache_key = f"repo_info:{repo}"

        def fetch() -> dict[str, Any]:
            data = self._request(f"/repos/{repo}").json()
            return {"default_branch": data["default_branch"], "size": data.get("size", 0)}

        result: dict[str, Any] | None = self._cached_fetch(cache_key, fetch, TTL_MUTABLE)
        return result

    def get_default_branch(self, repo: str) -> str | None:
        """Get the default branch for a repository."""
        info = self._get_repo_info(repo)
        return None if info is None else info["default_branch"]

    def prefetch_pr_commits(self, repo: str, prs: list[dict[str, Any]]) -> None:
        """Get PR commits and their stats from a local clone, if enough are needed.

        Fills the same cache entries the API path uses, so later lookups make no requests.
        PRs need `merge_commit_sha`, `commit_count`, and `disk_usage` (from search).
        """
        todo = [
            pr
            for pr in prs
            if pr.get("merge_commit_sha")
            and not (
                f"pr_stats_per_commit_v2:{repo}:{pr['number']}" in self.cache
                and f"pr_commits_raw:{repo}:{pr['number']}" in self.cache
            )
        ]
        n_commits = sum(pr.get("commit_count", 0) for pr in todo)
        disk_usage = max((pr.get("disk_usage", 0) for pr in todo), default=0)
        if not self._worth_cloning(repo, n_commits, disk_usage):
            return
        path = self.mirrors.sync(repo, [pr["number"] for pr in todo])  # type: ignore[union-attr]
        if path is None:
            return

        shas: list[str] = []
        for pr in todo:
            cache_key = f"pr_commits_raw:{repo}:{pr['number']}"
            commits = self.cache.get(cache_key)
            if commits is None:
                commits = get_pr_commits_local(path, pr["number"], pr["merge_commit_sha"])
                # If git disagrees with GitHub (e.g. a commit already in the base branch),
                # leave the PR to the API
                if commits is None or len(commits) != pr.get("commit_count"):
                    continue
                self.cache.set(cache_key, commits, expire=TTL_IMMUTABLE)
            shas.extend(c["sha"] for c in commits if len(c.get("parents", [])) <= 1)
        self._store_local_commit_stats(repo, path, shas)

    def prefetch_commit_stats(self, repo: str, shas: list[str]) -> None:
        """Compute stats of commits from a local clone, if enough are missing."""
        missing = [sha for sha in shas if f"commit_stats:{repo}:{sha}" not in self.cache]
        if not missing or self.mirrors is None:
            return
        # Updating an existing clone is cheap, so use it even for a few commits
        if not self.mirrors.path(repo).exists():
            info = self._get_repo_info(repo)
            if info is None or not self._worth_cloning(repo, len(missing), info["size"]):
                return
        path = self.mirrors.sync(repo)
        if path is not None:
            self._store_local_commit_stats(repo, path, missing)

    def _worth_cloning(self, repo: str, n_commits: int, size_kb: int) -> bool:
        if self.mirrors is None or n_commits == 0:
            return False
        if self.mirrors.path(repo).exists():
            return True
        return n_commits >= LOCAL_MIN_COMMITS and size_kb <= MAX_MIRROR_SIZE_KB

    def _store_local_commit_stats(self, repo: str, path: Path, shas: list[str]) -> None:
        """Cache stats computed with `git log --numstat` for the commits found locally."""
        try:
            existing = get_existing_commits(path, shas)
            stats = get_commits_numstat(path, sorted(existing))
        except subprocess.CalledProcessError as e:
            console.print(
                f"[yellow]git failed for {repo}, using the API instead: {e.stderr}[/yellow]"
            )
            return
        for sha, (additions, deletions, by_ext) in stats.items():
            ext_data = {ext: ext_stats.to_tuple() for ext, ext_stats in by_ext.items()}
            self.cache.set(
                f"commit_stats:{repo}:{sha}",
                (additions, deletions, ext_data),
                expire=TTL_IMMUTABLE,
            )

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
        """Save commits to cache with watermark dates.

        Uses TTL_IMMUTABLE since range-aware caching handles new commits by fetching gaps.
        """
        self.cache.set(
            cache_key,
            {
                "cached_since": since.isoformat(),
                "cached_until": until.isoformat(),
                "commits": commits,
            },
            expire=TTL_IMMUTABLE,
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
        try:
            return self._get_branch_commits(cache_key, repo, branch, username, since, until)
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            # E.g. 409 for an empty repository; skip the repo instead of aborting the run
            self._warn_skipped(repo, "direct commits", e)
            cached = self.cache.get(cache_key)
            return self._filter_commits_in_range(cached["commits"], since, until) if cached else []

    def _get_branch_commits(  # noqa: PLR0913
        self,
        cache_key: str,
        repo: str,
        branch: str,
        username: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
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

    def _get_pr_commits_raw(self, repo: str, pr_number: int) -> list[dict[str, Any]] | None:
        """Get all commits in a PR, or None if the API call failed."""
        cache_key = f"pr_commits_raw:{repo}:{pr_number}"

        def fetch() -> list[dict[str, Any]]:
            return list(self._paginate(f"/repos/{repo}/pulls/{pr_number}/commits"))

        result: list[dict[str, Any]] | None = self._cached_fetch(cache_key, fetch, TTL_IMMUTABLE)
        return result

    def get_pr_commits_raw(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get all commits in a PR (raw API response, cached forever)."""
        return self._get_pr_commits_raw(repo, pr_number) or []

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
        """Get additions and deletions for a single commit (zeros if the API call failed)."""
        return self._get_commit_stats(repo, sha) or (0, 0, {})

    def _get_commit_stats(
        self, repo: str, sha: str
    ) -> tuple[int, int, dict[str, FileStats]] | None:
        """Get additions and deletions for a single commit, or None if the API call failed.

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
            return None

        # Extract and cache processed stats
        result = self._extract_file_stats(raw.get("files", []))
        ext_data = {ext: stats.to_tuple() for ext, stats in result[2].items()}
        self.cache.set(stats_cache_key, (result[0], result[1], ext_data), expire=TTL_IMMUTABLE)
        return result

    def get_pr_files_raw(self, repo: str, pr_number: int) -> list[dict[str, Any]] | None:
        """Get all files changed in a PR (raw API response, cached forever).

        Returns None if the API call failed, so the failure is not cached.
        """
        cache_key = f"pr_files_raw:{repo}:{pr_number}"

        def fetch() -> list[dict[str, Any]]:
            return list(self._paginate(f"/repos/{repo}/pulls/{pr_number}/files"))

        result: list[dict[str, Any]] | None = self._cached_fetch(cache_key, fetch, TTL_IMMUTABLE)
        return result

    def get_pr_stats_per_commit(
        self, repo: str, pr_number: int
    ) -> tuple[int, int, dict[str, FileStats]]:
        """Get total additions/deletions across all commits in a PR."""
        # v2: merge commits are skipped (v1 cached totals that included them)
        cache_key = f"pr_stats_per_commit_v2:{repo}:{pr_number}"

        cached = self.cache.get(cache_key)
        if cached is not None:
            total_add, total_del, ext_data = cached
            by_ext = {ext: FileStats.from_tuple(t) for ext, t in ext_data.items()}
            return total_add, total_del, by_ext

        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_additions = 0
        total_deletions = 0

        commits = self._get_pr_commits_raw(repo, pr_number)
        complete = commits is not None
        for commit in commits or []:
            # Merging the base branch into the PR branch brings in others' changes
            if len(commit.get("parents", [])) > 1:
                continue
            commit_stats = self._get_commit_stats(repo, commit["sha"])
            if commit_stats is None:
                complete = False
                continue
            add, del_, ext_stats = commit_stats
            total_additions += add
            total_deletions += del_
            for ext, stats in ext_stats.items():
                by_extension[ext].additions += stats.additions
                by_extension[ext].deletions += stats.deletions

        # Only cache complete results, so failed API calls are retried next run
        if not complete:
            self._warn_skipped(repo, f"some commits of PR #{pr_number}")
        else:
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
        if files is None:
            self._warn_skipped(repo, f"files of PR #{pr_number}")
            return 0, 0, {}

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
