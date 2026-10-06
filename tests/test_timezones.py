"""Dates are naive UTC everywhere, whatever the local timezone.

GitHub returns UTC dates and reads timestamps without a timezone as US Pacific time,
so these tests run in another timezone (New York, UTC-4 in summer) to catch both.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import diskcache  # type: ignore[import-untyped]
import httpx
import pytest
from typer.testing import CliRunner

from trueloc.cli import app
from trueloc.github import GitHubClient
from trueloc.utils import to_utc

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    import respx

HEADERS = {"X-RateLimit-Remaining": "5000"}


@pytest.fixture(autouse=True)
def _new_york(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def gh_client(tmp_path: Path) -> Generator[GitHubClient, None, None]:
    cache = diskcache.Cache(tmp_path / "cache")
    with httpx.Client(base_url="https://api.github.com") as client:
        yield GitHubClient(client, cache)
    cache.close()


def commit(sha: str, date: str) -> dict[str, Any]:
    return {"sha": sha, "parents": [{"sha": "p"}], "commit": {"author": {"date": date}}}


def test_to_utc() -> None:
    assert to_utc(datetime(2024, 6, 1)) == datetime(2024, 6, 1, 4)


class TestCountRange:
    def run_count(self, *args: str) -> tuple[datetime, datetime]:
        """Run `count` and return the (since, until) it passes to repo discovery."""
        calls = []

        def discover(*a: Any) -> tuple[list[str], set[str]]:
            calls.append(a)
            return [], set()

        with (
            patch("trueloc.cli.get_github_token", return_value="token"),
            patch("trueloc.cli._discover_repos", discover),
            patch("trueloc.cli._merged_prs_by_repo", return_value={}),
        ):
            result = CliRunner().invoke(app, ["count", "testuser", *args, "--json"])
        assert result.exit_code == 0, result.output
        _gh, _username, _repo, since, until = calls[0]
        return since, until

    def test_dates_are_converted_to_utc(self) -> None:
        since, until = self.run_count("--since", "2024-06-01", "--until", "2024-06-02")

        assert since == datetime(2024, 6, 1, 4)  # Local midnight in New York
        assert until == datetime(2024, 6, 2, 4)

    def test_default_until_is_now_in_utc(self) -> None:
        _since, until = self.run_count("--since", "1d")

        assert abs(until - datetime.now(UTC).replace(tzinfo=None)) < timedelta(minutes=1)


class TestBranchCommits:
    def test_requests_send_utc_timestamps(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """GitHub reads timestamps without a timezone as US Pacific time."""
        route = respx_mock.get("https://api.github.com/repos/u/r/commits").mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )

        gh_client.get_branch_commits("u/r", "main", "u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        params = route.calls[0].request.url.params
        assert params["since"] == "2024-06-01T00:00:00Z"
        assert params["until"] == "2024-07-01T00:00:00Z"

    def test_first_fetch_filters_by_author_date(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """GitHub filters by committer date; cached lookups filter by author date."""
        url = "https://api.github.com/repos/u/r/commits"
        respx_mock.get(url, params__contains={"page": "1"}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    commit("rebased", "2024-05-01T00:00:00Z"),
                    commit("new", "2024-06-10T00:00:00Z"),
                ],
                headers=HEADERS,
            )
        )
        respx_mock.get(url, params__contains={"page": "2"}).mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )

        commits = gh_client.get_branch_commits(
            "u/r", "main", "u", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )

        assert [c["sha"] for c in commits] == ["new"]

    def test_newer_fetch_dedupes_boundary_commit(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Fetched ranges share their endpoint, so a commit there is returned twice."""
        boundary = commit("boundary", "2024-06-15T00:00:00Z")
        gh_client.cache.set(
            "branch_commits_v3:u/r:main:u",
            {
                "cached_since": "2024-06-01T00:00:00",
                "cached_until": "2024-06-15T00:00:00",
                "commits": [boundary],
            },
        )
        url = "https://api.github.com/repos/u/r/commits"
        respx_mock.get(url, params__contains={"page": "1"}).mock(
            return_value=httpx.Response(200, json=[boundary], headers=HEADERS)
        )
        respx_mock.get(url, params__contains={"page": "2"}).mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )

        commits = gh_client.get_branch_commits(
            "u/r", "main", "u", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )

        assert [c["sha"] for c in commits] == ["boundary"]

    def test_ignores_cache_with_local_time_watermarks(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Entries written by earlier versions have watermarks in local time."""
        gh_client.cache.set(
            "branch_commits_v2:u/r:main:u",
            {
                "cached_since": "2024-01-01T00:00:00",
                "cached_until": "2025-01-01T00:00:00",
                "commits": [commit("stale", "2024-06-10T00:00:00Z")],
            },
        )
        respx_mock.get("https://api.github.com/repos/u/r/commits").mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )

        commits = gh_client.get_branch_commits(
            "u/r", "main", "u", datetime(2024, 6, 1), datetime(2024, 7, 1)
        )

        assert commits == []


class TestGraphQLRanges:
    def test_search_treats_range_as_utc(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        search = {"issueCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []}
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"search": search}}, headers=HEADERS)
        )

        gh_client.search_merged_prs("u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        query = json.loads(route.calls[0].request.content)["variables"]["q"]
        assert "merged:2024-06-01T00:00:00Z.." in query

    def test_contributions_treat_range_as_utc(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        collection: dict[str, list[Any]] = {
            "pullRequestContributionsByRepository": [],
            "commitContributionsByRepository": [],
        }
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"user": {"contributionsCollection": collection}}},
                headers=HEADERS,
            )
        )

        gh_client.get_contributed_repos("u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        variables = json.loads(route.calls[-1].request.content)["variables"]
        assert variables["to"] == "2024-07-01T00:00:00Z"
