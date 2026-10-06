"""Tests for the trueloc CLI tool."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import diskcache  # type: ignore[import-untyped]
import httpx
import pytest
import respx

from trueloc import (
    CommitStats,
    FileStats,
    GitHubClient,
    PRStats,
    StatsAggregator,
)
from trueloc.utils import get_file_extension, parse_date

if TYPE_CHECKING:
    pass


# =============================================================================
# Fixtures
# =============================================================================


def _utc(dt: datetime) -> str:
    """Format a naive local datetime as the UTC string sent to GraphQL."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def memory_cache(tmp_path: Path) -> Generator[diskcache.Cache, None, None]:
    """Create an isolated cache for testing using a temp directory."""
    cache = diskcache.Cache(tmp_path / "test_cache")
    yield cache
    cache.clear()
    cache.close()


@pytest.fixture
def mock_client() -> httpx.Client:
    """Create a mock httpx client."""
    return httpx.Client(base_url="https://api.github.com")


@pytest.fixture
def gh_client(mock_client: httpx.Client, memory_cache: diskcache.Cache) -> GitHubClient:
    """Create a GitHubClient with mocked dependencies."""
    return GitHubClient(mock_client, memory_cache)


# =============================================================================
# FileStats Tests
# =============================================================================


class TestFileStats:
    """Tests for FileStats dataclass."""

    def test_default_values(self) -> None:
        """Test default values are zero."""
        stats = FileStats()
        assert stats.additions == 0
        assert stats.deletions == 0

    def test_to_tuple(self) -> None:
        """Test conversion to tuple."""
        stats = FileStats(additions=100, deletions=50)
        assert stats.to_tuple() == (100, 50)

    def test_from_tuple(self) -> None:
        """Test creation from tuple."""
        stats = FileStats.from_tuple((200, 75))
        assert stats.additions == 200
        assert stats.deletions == 75

    def test_round_trip(self) -> None:
        """Test tuple serialization round-trip."""
        original = FileStats(additions=42, deletions=17)
        restored = FileStats.from_tuple(original.to_tuple())
        assert restored.additions == original.additions
        assert restored.deletions == original.deletions


# =============================================================================
# PRStats Tests
# =============================================================================


class TestPRStats:
    """Tests for PRStats dataclass."""

    def test_creation(self) -> None:
        """Test PRStats creation."""
        stats = PRStats(
            repo="user/repo",
            pr_number=123,
            title="Test PR",
            additions=100,
            deletions=50,
            merged_at="2024-01-15",
            by_extension={".py": FileStats(100, 50)},
        )
        assert stats.repo == "user/repo"
        assert stats.pr_number == 123
        assert stats.additions == 100


# =============================================================================
# CommitStats Tests
# =============================================================================


class TestCommitStats:
    """Tests for CommitStats dataclass."""

    def test_creation(self) -> None:
        """Test CommitStats creation."""
        stats = CommitStats(
            repo="user/repo",
            sha="abc123",
            message="Test commit",
            additions=50,
            deletions=25,
            committed_at="2024-01-15",
            by_extension={".py": FileStats(50, 25)},
        )
        assert stats.sha == "abc123"
        assert stats.additions == 50


# =============================================================================
# StatsAggregator Tests
# =============================================================================


class TestStatsAggregator:
    """Tests for StatsAggregator dataclass."""

    def test_initial_state(self) -> None:
        """Test initial state is empty."""
        agg = StatsAggregator()
        assert agg.prs == []
        assert agg.direct_commits == []
        assert agg.total_additions == 0
        assert agg.total_deletions == 0
        assert agg.cache_hits == 0

    def test_add_extension_stats(self) -> None:
        """Test adding extension stats."""
        agg = StatsAggregator()
        agg.add_extension_stats({".py": FileStats(100, 50)})
        assert agg.by_extension[".py"].additions == 100
        assert agg.by_extension[".py"].deletions == 50

        # Add more to same extension
        agg.add_extension_stats({".py": FileStats(50, 25)})
        assert agg.by_extension[".py"].additions == 150
        assert agg.by_extension[".py"].deletions == 75

    def test_add_pr(self) -> None:
        """Test adding a PR updates totals."""
        agg = StatsAggregator()
        pr = PRStats(
            repo="user/repo",
            pr_number=1,
            title="Test",
            additions=100,
            deletions=50,
            merged_at="2024-01-15",
            by_extension={".py": FileStats(100, 50)},
        )
        agg.add_pr(pr)

        assert len(agg.prs) == 1
        assert agg.total_additions == 100
        assert agg.total_deletions == 50
        assert agg.by_extension[".py"].additions == 100

    def test_add_commit(self) -> None:
        """Test adding a commit updates totals."""
        agg = StatsAggregator()
        commit = CommitStats(
            repo="user/repo",
            sha="abc",
            message="Test",
            additions=50,
            deletions=25,
            committed_at="2024-01-15",
            by_extension={".js": FileStats(50, 25)},
        )
        agg.add_commit(commit)

        assert len(agg.direct_commits) == 1
        assert agg.total_additions == 50
        assert agg.total_deletions == 25
        assert agg.by_extension[".js"].additions == 50


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestGetFileExtension:
    """Tests for get_file_extension helper."""

    def test_python_file(self) -> None:
        """Test Python file extension."""
        assert get_file_extension("src/main.py") == ".py"

    def test_javascript_file(self) -> None:
        """Test JavaScript file extension."""
        assert get_file_extension("app/index.js") == ".js"

    def test_no_extension(self) -> None:
        """Test file without extension returns filename."""
        assert get_file_extension("Makefile") == "makefile"
        assert get_file_extension("Dockerfile") == "dockerfile"

    def test_hidden_file(self) -> None:
        """Test hidden file."""
        assert get_file_extension(".gitignore") == ".gitignore"

    def test_multiple_dots(self) -> None:
        """Test file with multiple dots."""
        assert get_file_extension("app.config.js") == ".js"

    def test_uppercase_extension(self) -> None:
        """Test uppercase extension is lowercased."""
        assert get_file_extension("README.MD") == ".md"


class TestParseDate:
    """Tests for parse_date helper."""

    def test_absolute_date(self) -> None:
        """Test parsing absolute date."""
        result = parse_date("2024-01-15")
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_shorthand_days(self) -> None:
        """Test parsing shorthand days."""
        result = parse_date("5d")
        # Should be 5 days ago from now
        assert isinstance(result, datetime)

    def test_shorthand_weeks(self) -> None:
        """Test parsing shorthand weeks."""
        result = parse_date("2w")
        assert isinstance(result, datetime)

    def test_shorthand_months(self) -> None:
        """Test parsing shorthand months."""
        result = parse_date("1m")
        assert isinstance(result, datetime)

    def test_shorthand_years(self) -> None:
        """Test parsing shorthand years."""
        result = parse_date("1y")
        assert isinstance(result, datetime)

    def test_natural_language(self) -> None:
        """Test parsing natural language."""
        result = parse_date("last week")
        assert isinstance(result, datetime)

    def test_invalid_date_raises(self) -> None:
        """Test invalid date raises BadParameter."""
        import typer

        with pytest.raises(typer.BadParameter):
            parse_date("not-a-date-xyz")


# =============================================================================
# GitHubClient Tests
# =============================================================================


class TestGitHubClientRateLimiting:
    """Tests for GitHubClient rate limiting."""

    def test_calc_rate_limit_wait_retry_after(self, gh_client: GitHubClient) -> None:
        """Test rate limit wait calculation with Retry-After header."""
        response = MagicMock()
        response.headers = {"Retry-After": "30", "X-RateLimit-Reset": "0"}
        assert gh_client._calc_rate_limit_wait(response) == 30

    def test_calc_rate_limit_wait_reset_timestamp(self, gh_client: GitHubClient) -> None:
        """Test rate limit wait calculation with reset timestamp."""
        import time

        future_time = int(time.time()) + 60
        response = MagicMock()
        response.headers = {"Retry-After": "0", "X-RateLimit-Reset": str(future_time)}
        wait = gh_client._calc_rate_limit_wait(response)
        assert 59 <= wait <= 61  # Allow for timing variance

    def test_calc_rate_limit_wait_fallback(self, gh_client: GitHubClient) -> None:
        """Test rate limit wait fallback to 60 seconds."""
        response = MagicMock()
        response.headers = {"Retry-After": "0", "X-RateLimit-Reset": "0"}
        assert gh_client._calc_rate_limit_wait(response) == 60

    def test_is_rate_limited_403_no_remaining(self, gh_client: GitHubClient) -> None:
        """Test rate limit detection with 403 and no remaining."""
        response = MagicMock()
        response.status_code = 403
        response.headers = {"X-RateLimit-Remaining": "0"}
        assert gh_client._is_rate_limited(response) is True

    def test_is_rate_limited_429(self, gh_client: GitHubClient) -> None:
        """Test rate limit detection with 429."""
        response = MagicMock()
        response.status_code = 429
        response.headers = {"X-RateLimit-Remaining": "100"}
        assert gh_client._is_rate_limited(response) is True

    def test_is_rate_limited_200(self, gh_client: GitHubClient) -> None:
        """Test non-rate-limited response."""
        response = MagicMock()
        response.status_code = 200
        response.headers = {"X-RateLimit-Remaining": "1000"}
        assert gh_client._is_rate_limited(response) is False

    def test_is_rate_limited_403_with_remaining(self, gh_client: GitHubClient) -> None:
        """Test 403 with remaining quota is not rate limited."""
        response = MagicMock()
        response.status_code = 403
        response.headers = {"X-RateLimit-Remaining": "100"}
        assert gh_client._is_rate_limited(response) is False


class TestGitHubClientAPI:
    """Tests for GitHubClient API methods."""

    def test_get_user_repos(self, memory_cache: diskcache.Cache, respx_mock: respx.Router) -> None:
        """Test fetching user repositories."""
        # First page with data
        respx_mock.get(
            "https://api.github.com/users/testuser/repos",
            params={"type": "owner", "per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"full_name": "testuser/repo1", "fork": False},
                    {"full_name": "testuser/repo2", "fork": False},
                    {"full_name": "testuser/forked", "fork": True},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        # Second page empty to stop pagination
        respx_mock.get(
            "https://api.github.com/users/testuser/repos",
            params={"type": "owner", "per_page": "100", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            repos = gh.get_user_repos("testuser")

        assert repos == ["testuser/repo1", "testuser/repo2"]

    @staticmethod
    def _contributions(prs: list[str], commits: list[str]) -> httpx.Response:
        def by_repo(names: list[str]) -> list[dict[str, Any]]:
            return [{"repository": {"nameWithOwner": name}} for name in names]

        collection = {
            "pullRequestContributionsByRepository": by_repo(prs),
            "commitContributionsByRepository": by_repo(commits),
        }
        return httpx.Response(
            200,
            json={"data": {"user": {"contributionsCollection": collection}}},
            headers={"X-RateLimit-Remaining": "5000"},
        )

    def test_get_contributed_repos(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        """Repos the user contributed to, including ones owned by others."""
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=self._contributions(["org/a", "testuser/b"], ["org/a", "org/c"])
        )

        repos = gh_client.get_contributed_repos(
            "testuser", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )

        assert repos == ["org/a", "org/c", "testuser/b"]
        # Looks back a year before `since`, as PR contributions count at creation time
        assert route.call_count == 2
        first, last = (json.loads(call.request.content)["variables"] for call in route.calls)
        assert first["login"] == "testuser"
        assert first["from"] == _utc(datetime(2023, 6, 2))
        assert last["to"] == _utc(datetime(2024, 7, 1))

    def test_get_contributed_repos_splits_long_ranges(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """contributionsCollection spans at most one year, so longer ranges are split."""
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=self._contributions(["org/a"], [])
        )

        gh_client.get_contributed_repos("testuser", datetime(2022, 1, 1), datetime(2024, 7, 1))

        windows = [json.loads(call.request.content)["variables"] for call in route.calls]
        assert len(windows) == 4  # 2.5 years plus a year of lookback
        for window in windows:
            start = datetime.fromisoformat(window["from"].rstrip("Z"))
            end = datetime.fromisoformat(window["to"].rstrip("Z"))
            assert end - start <= timedelta(days=365)
        assert windows[-1]["to"] == _utc(datetime(2024, 7, 1))

    def test_get_contributed_repos_bisects_when_truncated(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """At most 100 repos are returned per query; bisect the range when that is hit."""
        many = [f"org/repo{i}" for i in range(100)]
        respx_mock.post("https://api.github.com/graphql").mock(
            side_effect=[
                self._contributions([], []),  # Lookback window
                self._contributions(many, []),
                self._contributions(["org/early"], []),
                self._contributions(["org/late"], []),
            ]
        )

        repos = gh_client.get_contributed_repos(
            "testuser", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )

        assert set(repos) == {*many, "org/early", "org/late"}

    def test_get_contributed_repos_unknown_user(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"user": None},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}],
                },
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        assert (
            gh_client.get_contributed_repos("nobody", datetime(2024, 6, 1), datetime(2024, 7, 1))
            == []
        )

    def test_get_contributed_repos_graphql_error_warns(
        self,
        gh_client: GitHubClient,
        respx_mock: respx.Router,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Non-NOT_FOUND GraphQL errors must not look like "no contributions"."""
        respx_mock.post("https://api.github.com/graphql").mock(
            side_effect=[
                self._contributions(["org/a"], []),
                httpx.Response(
                    200,
                    json={
                        "data": {"user": None},
                        "errors": [{"type": "VALIDATION", "message": "x"}],
                    },
                    headers={"X-RateLimit-Remaining": "5000"},
                ),
            ]
        )
        repos = gh_client.get_contributed_repos(
            "testuser", datetime(2023, 6, 1), datetime(2024, 7, 1)
        )
        # Repos found before the error are kept, and the failure is reported
        assert repos == ["org/a"]
        assert "Skipping discovering contributed repos for testuser" in capsys.readouterr().err

    def test_get_contributed_repos_http_error_warns(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(502, headers={"X-RateLimit-Remaining": "5000"})
        )
        repos = gh_client.get_contributed_repos(
            "testuser", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )
        assert repos == []

    def test_get_default_branch(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test fetching default branch."""
        respx_mock.get("https://api.github.com/repos/user/repo").mock(
            return_value=httpx.Response(
                200,
                json={"default_branch": "main"},
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            branch = gh.get_default_branch("user/repo")

        assert branch == "main"

    def test_get_commit_stats(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test fetching commit stats."""
        respx_mock.get("https://api.github.com/repos/user/repo/commits/abc123").mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"filename": "test.py", "additions": 10, "deletions": 5},
                        {"filename": "test.js", "additions": 20, "deletions": 10},
                    ]
                },
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            additions, deletions, by_ext = gh.get_commit_stats("user/repo", "abc123")

        assert additions == 30
        assert deletions == 15
        assert by_ext[".py"].additions == 10
        assert by_ext[".js"].additions == 20


class TestGitHubClientPRCaching:
    """Tests for GitHubClient PR caching."""

    def test_filter_prs_since(self, gh_client: GitHubClient) -> None:
        """Test filtering PRs by date."""
        prs = [
            {"merged_at": "2024-01-10T10:00:00Z"},
            {"merged_at": "2024-01-15T10:00:00Z"},
            {"merged_at": "2024-01-20T10:00:00Z"},
        ]
        since = datetime(2024, 1, 12)
        filtered = gh_client._filter_prs_since(prs, since)
        assert len(filtered) == 2

    def test_save_and_load_pr_cache(
        self, gh_client: GitHubClient, memory_cache: diskcache.Cache
    ) -> None:
        """Test saving and loading PR cache."""
        cache_key = "test_key"
        since = datetime(2024, 1, 1)
        prs = [{"number": 1, "merged_at": "2024-01-15T10:00:00Z"}]

        until = datetime(2024, 2, 1)
        gh_client._save_pr_cache(cache_key, since, until, prs)

        cached = memory_cache.get(cache_key)
        assert cached is not None
        assert cached["cached_since"] == since.isoformat()
        assert cached["cached_until"] == until.isoformat()
        assert cached["prs"] == prs

    def test_get_merged_prs_caches_on_first_call(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that first call fetches from API and caches."""
        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "merged_at": "2024-01-10T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            since = datetime(2024, 1, 5)
            prs = gh.get_merged_prs("user/repo", "testuser", since)

            assert len(prs) == 1
            assert route.call_count == 1

            # Verify cache was populated
            cache_key = "merged_prs_v2:user/repo:testuser"
            cached = memory_cache.get(cache_key)
            assert cached is not None
            assert cached["cached_since"] == since.isoformat()

    def test_get_merged_prs_filters_locally_for_narrower_range(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that narrower date range uses cache without API call."""
        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "merged_at": "2024-01-10T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                    {
                        "number": 2,
                        "merged_at": "2024-01-08T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # First call with wide range - fetches from API
            since_wide = datetime(2024, 1, 5)
            prs1 = gh.get_merged_prs("user/repo", "testuser", since_wide)
            assert len(prs1) == 2
            assert route.call_count == 1

            # Second call with narrower range - should NOT hit API
            since_narrow = datetime(2024, 1, 9)
            prs2 = gh.get_merged_prs("user/repo", "testuser", since_narrow)
            assert len(prs2) == 1
            assert prs2[0]["number"] == 1
            # API should NOT have been called again
            assert route.call_count == 1

    def test_get_merged_prs_fetches_gap_for_wider_range(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that requesting older data fetches only the gap."""
        # Pre-populate cache with data from Jan 8 onwards
        cache_key = "merged_prs_v2:user/repo:testuser"
        memory_cache.set(
            cache_key,
            {
                "cached_since": "2024-01-08T00:00:00",
                "cached_until": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "prs": [
                    {
                        "number": 1,
                        "merged_at": "2024-01-10T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                ],
            },
        )

        # Mock the gap fetch (Jan 5 to Jan 8)
        gap_route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 2,
                        "merged_at": "2024-01-06T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # Request older data (Jan 5) - should fetch gap and merge
            since_older = datetime(2024, 1, 5)
            prs = gh.get_merged_prs("user/repo", "testuser", since_older)

            # Should have both PRs now
            assert len(prs) == 2
            assert gap_route.call_count == 1

            # Cache should be updated with new watermark
            cached = memory_cache.get(cache_key)
            assert cached["cached_since"] == since_older.isoformat()

    def test_consecutive_non_overlapping_ranges(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test scenario: -s 6m -u 5m then -s 5m -u 4m uses cache correctly.

        The cache stores PRs from `since` to NOW, not from `since` to `until`.
        So when querying a later range, it should use cached data.
        """
        # PRs spanning a wide range
        all_prs = [
            {"number": 1, "merged_at": "2024-01-25T10:00:00Z", "user": {"login": "testuser"}},
            {"number": 2, "merged_at": "2024-01-20T10:00:00Z", "user": {"login": "testuser"}},
            {"number": 3, "merged_at": "2024-01-15T10:00:00Z", "user": {"login": "testuser"}},
            {"number": 4, "merged_at": "2024-01-10T10:00:00Z", "user": {"login": "testuser"}},
            {"number": 5, "merged_at": "2024-01-05T10:00:00Z", "user": {"login": "testuser"}},
        ]

        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=all_prs,
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # First query: equivalent to -s Jan 5 -u Jan 11 (end of Jan 10)
            # (simulating -s 6m -u 5m)
            since_6m = datetime(2024, 1, 5)
            until_5m = datetime(2024, 1, 11)  # End of day Jan 10
            prs_6m_5m = [
                pr
                for pr in gh.get_merged_prs("user/repo", "testuser", since_6m)
                if datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None) < until_5m
            ]
            # Should get PR #5 (Jan 5) and PR #4 (Jan 10)
            assert len(prs_6m_5m) == 2
            assert {pr["number"] for pr in prs_6m_5m} == {4, 5}
            assert route.call_count == 1

            # Second query: equivalent to -s Jan 10 -u Jan 16 (end of Jan 15)
            # (simulating -s 5m -u 4m)
            since_5m = datetime(2024, 1, 10)
            until_4m = datetime(2024, 1, 16)  # End of day Jan 15
            prs_5m_4m = [
                pr
                for pr in gh.get_merged_prs("user/repo", "testuser", since_5m)
                if datetime.fromisoformat(pr["merged_at"]).replace(tzinfo=None) < until_4m
            ]
            # Should get PR #4 (Jan 10), PR #3 (Jan 15)
            assert len(prs_5m_4m) == 2
            assert {pr["number"] for pr in prs_5m_4m} == {3, 4}
            # API should NOT have been called again - data comes from cache
            assert route.call_count == 1

    def test_get_merged_prs_refreshes_stale_cache(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """PRs merged after the cache was written must show up on later runs."""
        cache_key = "merged_prs_v2:user/repo:testuser"
        cached_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
        old_pr = {"number": 1, "merged_at": "2024-01-10T10:00:00Z", "user": {"login": "testuser"}}
        memory_cache.set(
            cache_key,
            {
                "cached_since": "2024-01-01T00:00:00",
                "cached_until": cached_until.isoformat(),
                "prs": [old_pr],
            },
        )
        merged_at = (cached_until + timedelta(days=1)).isoformat() + "Z"
        new_pr = {
            "number": 2,
            "merged_at": merged_at,
            "updated_at": merged_at,
            "user": {"login": "testuser"},
        }
        stale = {**old_pr, "updated_at": "2024-01-10T10:00:00Z"}
        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200, json=[new_pr, stale], headers={"X-RateLimit-Remaining": "5000"}
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            prs = gh.get_merged_prs("user/repo", "testuser", datetime(2024, 1, 5))
            assert {pr["number"] for pr in prs} == {1, 2}
            # Stopped at the first PR updated before the watermark, no page 2 request
            assert route.call_count == 1

            # Cache is fresh now, so a second call does not hit the API
            prs = gh.get_merged_prs("user/repo", "testuser", datetime(2024, 1, 5))
            assert {pr["number"] for pr in prs} == {1, 2}
            assert route.call_count == 1

    def test_get_merged_prs_refreshes_legacy_cache_and_dedups(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Entries without cached_until are refreshed from cached_since, deduped by number."""
        cache_key = "merged_prs_v2:user/repo:testuser"
        pr1 = {"number": 1, "merged_at": "2024-01-10T10:00:00Z", "user": {"login": "testuser"}}
        memory_cache.set(cache_key, {"cached_since": "2024-01-08T00:00:00", "prs": [pr1]})
        refreshed_pr1 = {**pr1, "title": "refreshed", "updated_at": "2024-01-12T10:00:00Z"}
        pr2 = {
            "number": 2,
            "merged_at": "2024-02-01T10:00:00Z",
            "updated_at": "2024-02-01T10:00:00Z",
            "user": {"login": "testuser"},
        }
        old = {
            "number": 0,
            "merged_at": "2024-01-01T10:00:00Z",
            "updated_at": "2024-01-01T10:00:00Z",
            "user": {"login": "testuser"},
        }
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200, json=[pr2, refreshed_pr1, old], headers={"X-RateLimit-Remaining": "5000"}
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            prs = gh.get_merged_prs("user/repo", "testuser", datetime(2024, 1, 8))

        assert sorted(pr["number"] for pr in prs) == [1, 2]
        assert next(pr for pr in prs if pr["number"] == 1)["title"] == "refreshed"
        assert "cached_until" in memory_cache.get(cache_key)

    def test_fetch_prs_stops_at_prs_updated_before_since(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Pagination stops once PRs were last updated before `since`."""
        page1 = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 2,
                        "merged_at": "2024-01-10T10:00:00Z",
                        "updated_at": "2024-01-10T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                    {
                        "number": 1,
                        "merged_at": "2023-12-01T10:00:00Z",
                        "updated_at": "2023-12-01T10:00:00Z",
                        "user": {"login": "testuser"},
                    },
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        page2 = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls",
            params__contains={"state": "closed", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "5000"}))

        prs = gh_client._fetch_prs_in_range("user/repo", "testuser", datetime(2024, 1, 1))

        assert [pr["number"] for pr in prs] == [2]
        assert page1.call_count == 1
        assert page2.call_count == 0


class TestGitHubClientRequest:
    """Tests for GitHubClient._request method."""

    def test_request_success(self, memory_cache: diskcache.Cache, respx_mock: respx.Router) -> None:
        """Test successful request."""
        respx_mock.get("https://api.github.com/test").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok"},
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            response = gh._request("/test")
            assert response.status_code == 200

    def test_request_rate_limited_then_success(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test request that gets rate limited then succeeds."""
        # First call: rate limited, second call: success
        route = respx_mock.get("https://api.github.com/test")
        route.side_effect = [
            httpx.Response(
                429,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": "1",
                },
            ),
            httpx.Response(
                200,
                json={"status": "ok"},
                headers={"X-RateLimit-Remaining": "5000"},
            ),
        ]

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            # Patch _show_wait_progress to avoid actual waiting
            gh._show_wait_progress = lambda *args: None  # type: ignore[method-assign]
            response = gh._request("/test")
            assert response.status_code == 200


class TestGitHubClientCachedFetch:
    """Tests for GitHubClient._cached_fetch method."""

    def test_cached_fetch_returns_cached(
        self, gh_client: GitHubClient, memory_cache: diskcache.Cache
    ) -> None:
        """Test that cached values are returned."""
        memory_cache.set("test_key", "cached_value")
        result = gh_client._cached_fetch("test_key", lambda: "new_value")
        assert result == "cached_value"

    def test_cached_fetch_fetches_when_not_cached(self, gh_client: GitHubClient) -> None:
        """Test that fetcher is called when not cached."""
        result = gh_client._cached_fetch("new_key", lambda: "fetched_value")
        assert result == "fetched_value"

    def test_cached_fetch_handles_exception(self, gh_client: GitHubClient) -> None:
        """Test that exceptions in fetcher return None."""

        def bad_fetcher() -> str:
            raise httpx.TimeoutException("timeout")

        result = gh_client._cached_fetch("error_key", bad_fetcher)
        assert result is None


class TestGitHubClientBranchCommits:
    """Tests for GitHubClient branch commits methods."""

    def test_get_branch_commits(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test fetching branch commits."""
        since = datetime(2024, 1, 1)
        until = datetime(2024, 1, 31)
        # First page
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": since.isoformat(),
                "until": until.isoformat(),
                "per_page": "100",
                "page": "1",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"sha": "abc123", "commit": {"author": {"date": "2024-01-15"}}},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        # Second page empty
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": since.isoformat(),
                "until": until.isoformat(),
                "per_page": "100",
                "page": "2",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            commits = gh.get_branch_commits(
                "user/repo",
                "main",
                "testuser",
                since,
                until,
            )

        assert len(commits) == 1
        assert commits[0]["sha"] == "abc123"

    def test_get_branch_commits_filters_locally_for_narrower_range(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that narrower date range uses cache without API call."""
        since_wide = datetime(2024, 1, 1)
        until = datetime(2024, 1, 31)

        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": since_wide.isoformat(),
                "until": until.isoformat(),
                "per_page": "100",
                "page": "1",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"sha": "abc123", "commit": {"author": {"date": "2024-01-20T10:00:00Z"}}},
                    {"sha": "def456", "commit": {"author": {"date": "2024-01-10T10:00:00Z"}}},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": since_wide.isoformat(),
                "until": until.isoformat(),
                "per_page": "100",
                "page": "2",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # First call with wide range - fetches from API
            commits1 = gh.get_branch_commits("user/repo", "main", "testuser", since_wide, until)
            assert len(commits1) == 2
            assert route.call_count == 1

            # Second call with narrower range - should NOT hit API
            since_narrow = datetime(2024, 1, 15)
            commits2 = gh.get_branch_commits("user/repo", "main", "testuser", since_narrow, until)
            assert len(commits2) == 1
            assert commits2[0]["sha"] == "abc123"
            # API should NOT have been called again
            assert route.call_count == 1

    def test_get_branch_commits_fetches_gap_for_older_range(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that requesting older data fetches only the gap."""
        # Pre-populate cache with data from Jan 10 to Jan 31
        cache_key = "branch_commits_v2:user/repo:main:testuser"
        memory_cache.set(
            cache_key,
            {
                "cached_since": "2024-01-10T00:00:00",
                "cached_until": "2024-01-31T00:00:00",
                "commits": [
                    {"sha": "abc123", "commit": {"author": {"date": "2024-01-15T10:00:00Z"}}},
                ],
            },
        )

        # Mock the gap fetch (Jan 1 to Jan 10)
        gap_route = respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": "2024-01-01T00:00:00",
                "until": "2024-01-10T00:00:00",
                "per_page": "100",
                "page": "1",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"sha": "older123", "commit": {"author": {"date": "2024-01-05T10:00:00Z"}}},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": "2024-01-01T00:00:00",
                "until": "2024-01-10T00:00:00",
                "per_page": "100",
                "page": "2",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # Request older data (Jan 1) - should fetch gap and merge
            since_older = datetime(2024, 1, 1)
            until = datetime(2024, 1, 31)
            commits = gh.get_branch_commits("user/repo", "main", "testuser", since_older, until)

            # Should have both commits now
            assert len(commits) == 2
            assert gap_route.call_count == 1

            # Cache should be updated with new watermark
            cached = memory_cache.get(cache_key)
            assert cached["cached_since"] == since_older.isoformat()

    def test_get_branch_commits_fetches_gap_for_newer_range(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test that requesting newer data fetches only the gap."""
        # Pre-populate cache with data from Jan 1 to Jan 20
        cache_key = "branch_commits_v2:user/repo:main:testuser"
        memory_cache.set(
            cache_key,
            {
                "cached_since": "2024-01-01T00:00:00",
                "cached_until": "2024-01-20T00:00:00",
                "commits": [
                    {"sha": "abc123", "commit": {"author": {"date": "2024-01-15T10:00:00Z"}}},
                ],
            },
        )

        # Mock the gap fetch (Jan 20 to Jan 31)
        gap_route = respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": "2024-01-20T00:00:00",
                "until": "2024-01-31T00:00:00",
                "per_page": "100",
                "page": "1",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"sha": "newer123", "commit": {"author": {"date": "2024-01-25T10:00:00Z"}}},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params={
                "sha": "main",
                "author": "testuser",
                "since": "2024-01-20T00:00:00",
                "until": "2024-01-31T00:00:00",
                "per_page": "100",
                "page": "2",
            },
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)

            # Request newer data (to Jan 31) - should fetch gap and merge
            since = datetime(2024, 1, 1)
            until_newer = datetime(2024, 1, 31)
            commits = gh.get_branch_commits("user/repo", "main", "testuser", since, until_newer)

            # Should have both commits now
            assert len(commits) == 2
            assert gap_route.call_count == 1

            # Cache should be updated with new watermark
            cached = memory_cache.get(cache_key)
            assert cached["cached_until"] == until_newer.isoformat()

    def test_get_pr_commits(self, memory_cache: diskcache.Cache, respx_mock: respx.Router) -> None:
        """Test fetching PR commits."""
        # First page
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"sha": "abc123"}, {"sha": "def456"}],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        # Second page empty
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "2"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            shas = gh.get_pr_commits("user/repo", 123)

        assert shas == ["abc123", "def456"]


class TestGitHubClientPRStatsPerCommit:
    """Tests for PR stats per commit."""

    def test_get_pr_stats_per_commit(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test fetching PR stats aggregated per commit."""
        # Mock PR commits
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"sha": "abc123"}],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "5000"}))
        # Mock commit stats
        respx_mock.get("https://api.github.com/repos/user/repo/commits/abc123").mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"filename": "test.py", "additions": 10, "deletions": 5},
                    ]
                },
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            adds, dels, by_ext = gh.get_pr_stats_per_commit("user/repo", 123)

        assert adds == 10
        assert dels == 5
        assert by_ext[".py"].additions == 10


class TestGitHubClientPRStatsNet:
    """Tests for PR stats net diff."""

    def test_get_pr_stats_net(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Test fetching PR net stats."""
        # First page
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/files",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"filename": "test.py", "additions": 100, "deletions": 50},
                ],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        # Second page empty
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/files",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "5000"}))

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            adds, dels, by_ext = gh.get_pr_stats_net("user/repo", 123)

        assert adds == 100
        assert dels == 50
        assert by_ext[".py"].additions == 100


class TestGitHubClientFailures:
    """Failed API calls must not be cached as empty results."""

    HEADERS = {"X-RateLimit-Remaining": "5000"}  # noqa: RUF012

    def test_pr_stats_net_not_cached_on_error(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/files",
            params={"per_page": "100", "page": "1"},
        ).mock(return_value=httpx.Response(500, headers=self.HEADERS))

        assert gh_client.get_pr_stats_net("user/repo", 1) == (0, 0, {})

        route.mock(
            return_value=httpx.Response(
                200,
                json=[{"filename": "a.py", "additions": 5, "deletions": 2}],
                headers=self.HEADERS,
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/files",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers=self.HEADERS))

        adds, dels, _ = gh_client.get_pr_stats_net("user/repo", 1)
        assert (adds, dels) == (5, 2)

    def test_pr_stats_per_commit_not_cached_on_error(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(return_value=httpx.Response(200, json=[{"sha": "abc"}], headers=self.HEADERS))
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/commits",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers=self.HEADERS))
        route = respx_mock.get("https://api.github.com/repos/user/repo/commits/abc").mock(
            return_value=httpx.Response(500, headers=self.HEADERS)
        )

        assert gh_client.get_pr_stats_per_commit("user/repo", 1)[:2] == (0, 0)

        route.mock(
            return_value=httpx.Response(
                200,
                json={"files": [{"filename": "a.py", "additions": 5, "deletions": 2}]},
                headers=self.HEADERS,
            )
        )
        assert gh_client.get_pr_stats_per_commit("user/repo", 1)[:2] == (5, 2)

    def test_pr_stats_per_commit_not_cached_when_commit_list_fails(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(return_value=httpx.Response(500, headers=self.HEADERS))

        assert gh_client.get_pr_stats_per_commit("user/repo", 1)[:2] == (0, 0)
        assert "pr_stats_per_commit:user/repo:1" not in gh_client.cache

    def test_branch_commits_error_skips_repo(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """An empty repository returns 409; that must not abort the whole run."""
        respx_mock.get("https://api.github.com/repos/user/repo/commits").mock(
            return_value=httpx.Response(409, headers=self.HEADERS)
        )
        commits = gh_client.get_branch_commits(
            "user/repo", "main", "user", datetime(2024, 1, 1), datetime(2024, 2, 1)
        )
        assert commits == []
        assert "branch_commits_v2:user/repo:main:user" not in gh_client.cache

    def test_merged_prs_error_skips_repo(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get("https://api.github.com/repos/user/repo/pulls").mock(
            return_value=httpx.Response(404, headers=self.HEADERS)
        )
        assert gh_client.get_merged_prs("user/repo", "user", datetime(2024, 1, 1)) == []
        assert "merged_prs_v2:user/repo:user" not in gh_client.cache

    def test_merged_prs_gap_error_returns_cached(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        cached_pr = {"number": 1, "merged_at": "2024-01-10T10:00:00Z", "user": {"login": "u"}}
        gh_client.cache.set(
            "merged_prs_v2:user/repo:u",
            {"cached_since": "2024-01-08T00:00:00", "prs": [cached_pr]},
        )
        respx_mock.get("https://api.github.com/repos/user/repo/pulls").mock(
            return_value=httpx.Response(500, headers=self.HEADERS)
        )
        assert gh_client.get_merged_prs("user/repo", "u", datetime(2024, 1, 1)) == [cached_pr]
        # Watermark is not moved back, so the gap is retried next run
        cached = gh_client.cache.get("merged_prs_v2:user/repo:u")
        assert cached["cached_since"] == "2024-01-08T00:00:00"

    def test_branch_commits_gap_error_returns_cached(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        commit = {"sha": "abc", "commit": {"author": {"date": "2024-01-10T10:00:00Z"}}}
        gh_client.cache.set(
            "branch_commits_v2:user/repo:main:u",
            {
                "cached_since": "2024-01-05T00:00:00",
                "cached_until": "2024-01-20T00:00:00",
                "commits": [commit],
            },
        )
        respx_mock.get("https://api.github.com/repos/user/repo/commits").mock(
            return_value=httpx.Response(500, headers=self.HEADERS)
        )
        commits = gh_client.get_branch_commits(
            "user/repo", "main", "u", datetime(2024, 1, 1), datetime(2024, 2, 1)
        )
        assert commits == [commit]

    def test_warnings_go_to_stderr(
        self,
        gh_client: GitHubClient,
        respx_mock: respx.Router,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Warnings must not corrupt --json output on stdout."""
        respx_mock.get("https://api.github.com/repos/user/repo/pulls").mock(
            return_value=httpx.Response(404, headers=self.HEADERS)
        )
        gh_client.get_merged_prs("user/repo", "user", datetime(2024, 1, 1))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Skipping PRs for user/repo (HTTP 404)" in captured.err

    def test_warn_skipped_without_response(
        self, gh_client: GitHubClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """_request raises HTTPStatusError(response=None) after exhausting retries."""
        error = httpx.HTTPStatusError("failed", request=None, response=None)  # type: ignore[arg-type]
        gh_client._warn_skipped("user/repo", "PRs", error)
        assert "Skipping PRs for user/repo (HTTPStatusError)" in capsys.readouterr().err


class TestProcessFunctions:
    """Tests for _process_pr and _process_direct_commits."""

    def test_process_pr(self, memory_cache: diskcache.Cache, respx_mock: respx.Router) -> None:
        """Test processing a single PR."""
        from trueloc.cli import _process_pr

        # Mock PR files
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/files",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"filename": "test.py", "additions": 50, "deletions": 25}],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/files",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "5000"}))

        # Mock PR commits (for commit count)
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"sha": "abc123"}, {"sha": "def456"}],
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/123/commits",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "5000"}))

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            aggregator = StatsAggregator()
            pr = {
                "number": 123,
                "title": "Test PR",
                "merged_at": "2024-01-15T10:00:00Z",
            }
            _process_pr(gh, "user/repo", pr, False, aggregator, False)

        assert len(aggregator.prs) == 1
        assert aggregator.total_additions == 50
        assert aggregator.total_deletions == 25
        assert aggregator.prs[0].commit_count == 2

    def _direct_commits_aggregator(
        self,
        memory_cache: diskcache.Cache,
        respx_mock: respx.Router,
        branch_commits: list[dict[str, Any]],
    ) -> StatsAggregator:
        """Run _process_direct_commits after processing one squash-merged PR."""
        from trueloc.cli import _process_direct_commits, _process_pr

        headers = {"X-RateLimit-Remaining": "5000"}
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/commits",
            params={"per_page": "100", "page": "1"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "prcommit",
                        "commit": {
                            "author": {"date": "2024-01-10T10:00:00Z"},
                            "message": "Rebased commit\n\nbody",
                        },
                    }
                ],
                headers=headers,
            )
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/pulls/1/commits",
            params={"per_page": "100", "page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers=headers))
        respx_mock.get("https://api.github.com/repos/user/repo/commits/prcommit").mock(
            return_value=httpx.Response(
                200,
                json={"files": [{"filename": "a.py", "additions": 10, "deletions": 0}]},
                headers=headers,
            )
        )
        respx_mock.get("https://api.github.com/repos/user/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"}, headers=headers)
        )
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params__contains={"page": "1"},
        ).mock(return_value=httpx.Response(200, json=branch_commits, headers=headers))
        respx_mock.get(
            "https://api.github.com/repos/user/repo/commits",
            params__contains={"page": "2"},
        ).mock(return_value=httpx.Response(200, json=[], headers=headers))
        respx_mock.get("https://api.github.com/repos/user/repo/commits/direct").mock(
            return_value=httpx.Response(
                200,
                json={"files": [{"filename": "b.py", "additions": 3, "deletions": 1}]},
                headers=headers,
            )
        )

        with httpx.Client(base_url="https://api.github.com") as client:
            gh = GitHubClient(client, memory_cache)
            aggregator = StatsAggregator()
            pr = {
                "number": 1,
                "title": "PR",
                "merged_at": "2024-01-15T10:00:00Z",
                "merge_commit_sha": "squash",
            }
            _process_pr(gh, "user/repo", pr, True, aggregator, True)
            _process_direct_commits(
                gh, "user/repo", "user", datetime(2024, 1, 1), datetime(2024, 2, 1), aggregator
            )
        return aggregator

    @staticmethod
    def _branch_commit(sha: str, date: str, message: str, n_parents: int = 1) -> dict[str, Any]:
        return {
            "sha": sha,
            "parents": [{"sha": f"p{i}"} for i in range(n_parents)],
            "commit": {"author": {"date": date}, "message": message},
        }

    def test_direct_commits_skip_pr_merge_commits(
        self, memory_cache: diskcache.Cache, respx_mock: respx.Router
    ) -> None:
        """Squash, rebase, and merge commits of PRs must not be counted again."""
        branch_commits = [
            # Squash-merge commit: SHA equals the PR's merge_commit_sha
            self._branch_commit("squash", "2024-01-15T10:00:00Z", "PR (#1)"),
            # Rebase-merge copy of a PR commit: new SHA, same author date and message
            self._branch_commit("rebased", "2024-01-10T10:00:00Z", "Rebased commit\n\nbody"),
            # Merge commit (e.g. merging someone else's PR): diff duplicates the branch
            self._branch_commit("merge", "2024-01-16T10:00:00Z", "Merge pull request #2", 2),
            # Real direct commit
            self._branch_commit("direct", "2024-01-20T10:00:00Z", "Direct commit"),
        ]
        aggregator = self._direct_commits_aggregator(memory_cache, respx_mock, branch_commits)

        assert [c.sha for c in aggregator.direct_commits] == ["direct"]
        assert aggregator.total_additions == 13
        assert aggregator.total_deletions == 1


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_get_github_token(self) -> None:
        """Test getting GitHub token."""
        from trueloc.utils import get_github_token

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test_token\n")
            token = get_github_token()

        assert token == "test_token"

    def test_get_cache_with_no_cache(self) -> None:
        """Test getting in-memory cache."""
        from trueloc.utils import get_cache

        cache = get_cache(no_cache=True)
        assert cache is not None
        cache.set("key", "value")
        cache.close()
        # diskcache has no in-memory mode; ":memory:" would be a directory in the CWD
        assert Path(cache.directory).name != ":memory:"
        # A fresh no-cache instance must not see earlier entries
        fresh = get_cache(no_cache=True)
        assert fresh.get("key") is None
        fresh.close()

    def test_get_cache_with_disk(self) -> None:
        """Test getting disk cache."""
        from trueloc.utils import get_cache

        cache = get_cache(no_cache=False)
        assert cache is not None
        cache.close()


# =============================================================================
# Integration Tests
# =============================================================================


class TestCLI:
    """Tests for CLI commands."""

    def test_app_exists(self) -> None:
        """Test that the Typer app exists."""
        from trueloc import app

        assert app is not None

    def test_clear_cache_command(self) -> None:
        """Test clear-cache command with isolated temp cache."""
        from typer.testing import CliRunner

        from trueloc import app

        runner = CliRunner()
        # Global _isolate_cache fixture ensures we never touch the real cache
        result = runner.invoke(app, ["clear-cache"])
        assert result.exit_code == 0
        assert "Cache cleared" in result.stdout

    def test_cache_isolation_fixture(self) -> None:
        """Verify the global _isolate_cache fixture is working."""
        from trueloc import cli, utils

        real_cache = Path.home() / ".cache" / "trueloc"
        # Access cli.CACHE_DIR to verify the fixture patches it (not exported, so ignore mypy)
        cli_cache = cli.CACHE_DIR  # type: ignore[attr-defined]
        utils_cache = utils.CACHE_DIR
        assert real_cache != cli_cache, f"cli.CACHE_DIR not isolated: {cli_cache}"
        assert real_cache != utils_cache, f"utils.CACHE_DIR not isolated: {utils_cache}"
