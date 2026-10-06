"""Repeated runs avoid requests for data that cannot have changed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import diskcache  # type: ignore[import-untyped]
import httpx
import pytest

from trueloc.github import GitHubClient

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    import respx

HEADERS = {"X-RateLimit-Remaining": "5000"}


@pytest.fixture
def gh_client(tmp_path: Path) -> Generator[GitHubClient, None, None]:
    cache = diskcache.Cache(tmp_path / "cache")
    with httpx.Client(base_url="https://api.github.com") as client:
        yield GitHubClient(client, cache)
    cache.close()


def test_paginate_stops_at_short_page(gh_client: GitHubClient, respx_mock: respx.Router) -> None:
    """A page with fewer items than requested is the last; no extra request for an empty one."""
    respx_mock.get("https://api.github.com/items", params__contains={"page": "1"}).mock(
        return_value=httpx.Response(200, json=[{"id": 1}], headers=HEADERS)
    )

    assert list(gh_client._paginate("/items")) == [{"id": 1}]


def contributions(names: list[str]) -> httpx.Response:
    collection = {
        "pullRequestContributionsByRepository": [
            {"repository": {"nameWithOwner": n}} for n in names
        ],
        "commitContributionsByRepository": [],
    }
    return httpx.Response(
        200, json={"data": {"user": {"contributionsCollection": collection}}}, headers=HEADERS
    )


def windows(route: respx.Route) -> list[tuple[str, str]]:
    variables = [json.loads(call.request.content)["variables"] for call in route.calls]
    return [(v["from"], v["to"]) for v in variables]


class TestContributedRepos:
    def test_queries_calendar_years(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Year-aligned windows are the same on every run, so past years can be cached."""
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=contributions(["org/a"])
        )

        gh_client.get_contributed_repos("u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        # A year of lookback before `since`: 2023 and 2024, both complete years
        assert windows(route) == [
            ("2023-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
            ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ]

    def test_past_years_are_cached(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=contributions(["org/a"])
        )

        first = gh_client.get_contributed_repos("u", datetime(2024, 6, 1), datetime(2024, 7, 1))
        second = gh_client.get_contributed_repos("u", datetime(2024, 6, 2), datetime(2024, 7, 2))

        assert first == second == ["org/a"]
        assert route.call_count == 2
        # Not forever: repos get renamed or transferred, and access changes
        _value, expire_time = gh_client.cache.get("contributed_repos:u:2023", expire_time=True)
        assert expire_time is not None

    def test_current_year_is_refetched(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=contributions(["org/a"])
        )

        gh_client.get_contributed_repos("u", now - timedelta(days=1), now)
        calls = route.call_count
        gh_client.get_contributed_repos("u", now - timedelta(days=1), now)

        # Only years that may still change are queried again (on Jan 1 also the last one)
        again = windows(route)[calls:]
        assert again
        assert all(int(start[:4]) >= (now - timedelta(days=1)).year for start, _end in again)


def commit(sha: str, date: str) -> dict[str, Any]:
    return {"sha": sha, "parents": [{"sha": "p"}], "commit": {"author": {"date": date}}}


def cache_branch_commits(gh_client: GitHubClient, commits: list[dict[str, Any]]) -> None:
    gh_client.cache.set(
        "branch_commits_v3:u/r:main:u",
        {
            "cached_since": "2024-06-01T00:00:00",
            "cached_until": "2024-06-15T00:00:00",
            "commits": commits,
        },
    )


class TestSkipUnchangedRepos:
    def test_get_pushed_at_batches_repos(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        def graphql(request: httpx.Request) -> httpx.Response:
            query = json.loads(request.content)["query"]
            n = query.count("repository(")
            data: dict[str, dict[str, str] | None] = {
                f"r{i}": {"pushedAt": "2024-06-10T00:00:00Z"} for i in range(n)
            }
            data["r1"] = None  # Not accessible
            return httpx.Response(200, json={"data": data, "errors": [{}]}, headers=HEADERS)

        route = respx_mock.post("https://api.github.com/graphql").mock(side_effect=graphql)
        repos = [f"org/repo-{i}.x" for i in range(150)]

        pushed = gh_client.get_pushed_at(repos)

        assert route.call_count == 2  # 100 repos per query
        assert len(pushed) == 148
        assert "org/repo-1.x" not in pushed
        assert pushed["org/repo-0.x"] == datetime(2024, 6, 10)

    def test_branch_commits_not_refetched_without_push(self, gh_client: GitHubClient) -> None:
        """No push since the last fetch: the branch cannot have new commits."""
        cache_branch_commits(gh_client, [commit("old", "2024-06-10T00:00:00Z")])

        # No HTTP routes are mocked: a request would fail the test
        commits = gh_client.get_branch_commits(
            "u/r",
            "main",
            "u",
            datetime(2024, 6, 1),
            datetime(2024, 7, 1),
            pushed_at=datetime(2024, 6, 14),
        )

        assert [c["sha"] for c in commits] == ["old"]
        assert (
            gh_client.cache.get("branch_commits_v3:u/r:main:u")["cached_until"]
            == "2024-07-01T00:00:00"
        )

    def test_branch_commits_refetched_after_push(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        cache_branch_commits(gh_client, [])
        route = respx_mock.get("https://api.github.com/repos/u/r/commits").mock(
            return_value=httpx.Response(
                200, json=[commit("new", "2024-06-20T00:00:00Z")], headers=HEADERS
            )
        )

        commits = gh_client.get_branch_commits(
            "u/r",
            "main",
            "u",
            datetime(2024, 6, 1),
            datetime(2024, 7, 1),
            pushed_at=datetime(2024, 6, 20),
        )

        assert route.call_count == 1
        assert [c["sha"] for c in commits] == ["new"]

    def test_push_just_before_watermark_is_fetched(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Within the clock-skew margin of the watermark, fetch to be safe."""
        cache_branch_commits(gh_client, [])
        route = respx_mock.get("https://api.github.com/repos/u/r/commits").mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )

        gh_client.get_branch_commits(
            "u/r",
            "main",
            "u",
            datetime(2024, 6, 1),
            datetime(2024, 7, 1),
            pushed_at=datetime(2024, 6, 14, 23, 55),
        )

        assert route.call_count == 1

    def test_future_until_is_not_cached_as_fetched(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """The cached range must end when it was fetched, not at a future --until."""
        respx_mock.get("https://api.github.com/repos/u/r/commits").mock(
            return_value=httpx.Response(200, json=[], headers=HEADERS)
        )
        now = datetime.now(UTC).replace(tzinfo=None)

        gh_client.get_branch_commits(
            "u/r", "main", "u", now - timedelta(days=30), now + timedelta(days=30)
        )

        cached_until = datetime.fromisoformat(
            gh_client.cache.get("branch_commits_v3:u/r:main:u")["cached_until"]
        )
        assert cached_until <= datetime.now(UTC).replace(tzinfo=None)

    def test_get_pushed_at_warns_on_failure(
        self,
        gh_client: GitHubClient,
        respx_mock: respx.Router,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]},
                headers=HEADERS,
            )
        )

        assert gh_client.get_pushed_at(["org/a"]) == {}
        assert "slow down" in capsys.readouterr().err


class TestForkCommitsCache:
    def mock_fork(self, respx_mock: respx.Router) -> tuple[respx.Route, respx.Route]:
        info = respx_mock.get("https://api.github.com/repos/u/fork").mock(
            return_value=httpx.Response(
                200,
                json={
                    "default_branch": "main",
                    "parent": {"full_name": "up/proj", "default_branch": "main"},
                },
                headers=HEADERS,
            )
        )
        mine = {**commit("mine", "2024-06-10T00:00:00Z"), "author": {"login": "u"}}
        compare = respx_mock.get(
            "https://api.github.com/repos/u/fork/compare/up:proj:main...main"
        ).mock(return_value=httpx.Response(200, json={"commits": [mine]}, headers=HEADERS))
        return info, compare

    def test_cached_while_fork_not_pushed(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        info, compare = self.mock_fork(respx_mock)
        args = ("u/fork", "u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        first = gh_client.get_fork_commits(*args, pushed_at=datetime(2024, 6, 10))
        second = gh_client.get_fork_commits(*args, pushed_at=datetime(2024, 6, 10))

        assert [c["sha"] for c in first] == [c["sha"] for c in second] == ["mine"]
        assert info.call_count == compare.call_count == 1

    def test_refetched_after_fork_push(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        _info, compare = self.mock_fork(respx_mock)
        args = ("u/fork", "u", datetime(2024, 6, 1), datetime(2024, 7, 1))

        gh_client.get_fork_commits(*args, pushed_at=datetime(2024, 6, 10))
        gh_client.get_fork_commits(*args, pushed_at=datetime(2024, 6, 11))

        assert compare.call_count == 2


def test_direct_commit_in_two_repos_counted_once(
    gh_client: GitHubClient, respx_mock: respx.Router
) -> None:
    """A repo copied or forked from another shares its commits; count each once."""
    from trueloc.cli import _process_direct_commits
    from trueloc.models import StatsAggregator

    shared = {
        **commit("shared", "2024-06-10T00:00:00Z"),
        "commit": {"author": {"date": "2024-06-10T00:00:00Z"}, "message": "Work"},
    }
    for repo in ("a/proj", "b/proj"):
        respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"}, headers=HEADERS)
        )
        respx_mock.get(f"https://api.github.com/repos/{repo}/commits").mock(
            return_value=httpx.Response(200, json=[shared], headers=HEADERS)
        )
    respx_mock.get("https://api.github.com/repos/a/proj/commits/shared").mock(
        return_value=httpx.Response(
            200,
            json={"files": [{"filename": "x.py", "additions": 5, "deletions": 0}]},
            headers=HEADERS,
        )
    )
    aggregator = StatsAggregator()

    for repo in ("a/proj", "b/proj"):
        _process_direct_commits(
            gh_client, repo, "u", datetime(2024, 6, 1), datetime(2024, 7, 1), aggregator
        )

    assert [c.repo for c in aggregator.direct_commits] == ["a/proj"]
    assert aggregator.total_additions == 5


def test_count_passes_pushed_at_to_direct_commits() -> None:
    """Forks use their listing's pushed_at; other repos one batched lookup."""
    from typer.testing import CliRunner

    from trueloc.cli import app

    fork_pushed, repo_pushed = datetime(2024, 6, 10), datetime(2024, 6, 11)
    lookups = []
    calls = {}

    def get_pushed_at(_self: GitHubClient, repos: list[str]) -> dict[str, datetime]:
        lookups.append(repos)
        return {"u/repo": repo_pushed}

    def process(*args: Any, **kwargs: Any) -> None:
        calls[args[1]] = kwargs

    with (
        patch("trueloc.cli.get_github_token", return_value="token"),
        patch(
            "trueloc.cli._discover_repos",
            return_value=(["u/fork", "u/repo"], {"u/fork": fork_pushed}),
        ),
        patch("trueloc.cli._merged_prs_by_repo", return_value={}),
        patch.object(GitHubClient, "get_pushed_at", get_pushed_at),
        patch("trueloc.cli._process_direct_commits", process),
    ):
        result = CliRunner().invoke(app, ["count", "u", "--since", "1m", "--json"])

    assert result.exit_code == 0, result.output
    assert lookups == [["u/repo"]]  # Forks are not looked up again
    assert calls["u/fork"] == {"is_owned_fork": True, "pushed_at": fork_pushed}
    assert calls["u/repo"] == {"is_owned_fork": False, "pushed_at": repo_pushed}


def test_api_client_follows_redirects() -> None:
    """Renamed or transferred repos answer with a redirect to their new name."""
    from typer.testing import CliRunner

    from trueloc.cli import app

    clients = []
    real_client = httpx.Client

    def capture(*args: Any, **kwargs: Any) -> httpx.Client:
        clients.append(kwargs)
        return real_client(*args, **kwargs)

    with (
        patch("trueloc.cli.get_github_token", return_value="token"),
        patch("trueloc.cli.httpx.Client", capture),
        patch("trueloc.cli._discover_repos", return_value=([], {})),
        patch("trueloc.cli._merged_prs_by_repo", return_value={}),
    ):
        result = CliRunner().invoke(app, ["count", "u", "--since", "1m", "--json"])

    assert result.exit_code == 0, result.output
    assert clients[0]["follow_redirects"] is True
