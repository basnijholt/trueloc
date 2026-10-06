"""Tests for computing commit stats from local git clones."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import diskcache  # type: ignore[import-untyped]
import httpx
import pytest

from trueloc.github import GitHubClient
from trueloc.local import get_commits_numstat, get_pr_commits_local, parse_numstat_z, run_git
from trueloc.mirror import RepoMirrors, git_supports_env_config

if TYPE_CHECKING:
    from collections.abc import Generator

    import respx


@pytest.fixture
def gh_client(tmp_path: Path) -> Generator[GitHubClient, None, None]:
    cache = diskcache.Cache(tmp_path / "cache")
    with httpx.Client(base_url="https://api.github.com") as client:
        yield GitHubClient(client, cache)
    cache.close()


def git(
    repo: Path, *args: str, date: str = "2024-01-10T10:00:00-08:00", stdin: bytes | None = None
) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        # Ignore the user's config (e.g. commit signing)
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        check=True,
        env=env,
        input=stdin,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def remote(tmp_path: Path) -> dict[str, str]:
    """A repo with a squash-merged PR #1 whose branch merged main back in.

    Returns the repo path and the SHAs of interest.
    """
    repo = tmp_path / "gh" / "owner" / "proj"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Base")

    git(repo, "switch", "-q", "-c", "feature")
    (repo / "b.py").write_text("1\n2\n3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Add b\n\nWith a body", date="2024-01-10T10:00:00-08:00")
    c1 = git(repo, "rev-parse", "HEAD")

    git(repo, "mv", "b.py", "c.py")
    (repo / "c.py").write_text("1\n2\n3\n4\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Rename b to c", date="2024-01-11T10:00:00+02:00")
    c2 = git(repo, "rev-parse", "HEAD")

    git(repo, "switch", "-q", "main")
    (repo / "d.txt").write_text("x\ny\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Main work")
    direct = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    merge = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pull/1/head", merge)

    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--squash", "feature")
    git(repo, "commit", "-q", "-m", "PR (#1)")
    squash = git(repo, "rev-parse", "HEAD")
    return {
        "path": str(repo),
        "c1": c1,
        "c2": c2,
        "merge": merge,
        "squash": squash,
        "direct": direct,
    }


class TestParseNumstatZ:
    def test_parses_files_renames_and_binaries(self) -> None:
        output = (
            "\x01" + "a" * 40 + "\0\n"
            "3\t1\tsrc/x.py\0"
            "2\t0\t\0old.md\0new/doc.md\0"
            "-\t-\tlogo.png\0"
            "\x01" + "b" * 40 + "\0"  # merge commit: no file lines
        )

        stats = parse_numstat_z(output)

        add, dels, by_ext = stats["a" * 40]
        assert (add, dels) == (5, 1)
        assert by_ext[".py"].to_tuple() == (3, 1)
        assert by_ext[".md"].to_tuple() == (2, 0)  # renamed file counts under its new path
        assert ".png" not in by_ext  # binary files have no line counts
        assert stats["b" * 40] == (0, 0, {})


class TestLocalGit:
    def test_get_commits_numstat(self, remote: dict[str, str]) -> None:
        stats = get_commits_numstat(Path(remote["path"]), [remote["c1"], remote["c2"]])

        assert stats[remote["c1"]][:2] == (3, 0)
        assert stats[remote["c2"]][:2] == (1, 0)  # rename detected: only the added line
        assert stats[remote["c2"]][2][".py"].to_tuple() == (1, 0)

    def test_get_pr_commits_local_matches_rest_shape(self, remote: dict[str, str]) -> None:
        commits = get_pr_commits_local(Path(remote["path"]), 1, remote["squash"])

        assert commits is not None
        # Oldest first, like GET /pulls/{n}/commits; includes the merge of main
        assert [c["sha"] for c in commits] == [remote["c1"], remote["c2"], remote["merge"]]
        first = commits[0]
        # Author dates in UTC with a Z suffix, like the REST API
        assert first["commit"]["author"]["date"] == "2024-01-10T18:00:00Z"
        assert commits[1]["commit"]["author"]["date"] == "2024-01-11T08:00:00Z"
        assert first["commit"]["message"] == "Add b\n\nWith a body"
        assert len(first["parents"]) == 1
        assert len(commits[2]["parents"]) == 2

    def test_rebase_merged_pr(self, remote: dict[str, str]) -> None:
        """Rebase merges copy PR commits to the base branch with new SHAs."""
        repo = Path(remote["path"])
        git(repo, "switch", "-q", "-c", "rebased", f"{remote['squash']}^")
        git(repo, "cherry-pick", remote["c1"], remote["c2"], date="2024-02-01T00:00:00Z")
        last_copy = git(repo, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/pull/2/head", remote["c2"])
        # The two copies: commits on the rebased branch after its base
        git(repo, "update-ref", "refs/pull/9/head", last_copy)
        copies = get_pr_commits_local(repo, 9, f"{last_copy}^")

        commits = get_pr_commits_local(repo, 2, last_copy)

        assert commits is not None
        assert [c["sha"] for c in commits] == [remote["c1"], remote["c2"]]
        # The copies keep author date and message, which the direct-commit dedup relies on
        assert copies is not None
        fingerprint = [(c["commit"]["author"]["date"], c["commit"]["message"]) for c in commits]
        assert fingerprint == [
            (c["commit"]["author"]["date"], c["commit"]["message"]) for c in copies
        ]
        assert {c["sha"] for c in copies}.isdisjoint({remote["c1"], remote["c2"]})

    def test_merge_commit_merged_pr(self, remote: dict[str, str]) -> None:
        repo = Path(remote["path"])
        git(repo, "switch", "-q", "-c", "merged", f"{remote['squash']}^")
        git(repo, "update-ref", "refs/pull/3/head", remote["c2"])
        git(repo, "merge", "-q", "--no-ff", "--no-edit", remote["c2"])
        merge = git(repo, "rev-parse", "HEAD")

        commits = get_pr_commits_local(repo, 3, merge)

        assert commits is not None
        assert [c["sha"] for c in commits] == [remote["c1"], remote["c2"]]

    def test_crlf_message_and_non_utf8_path(self, remote: dict[str, str]) -> None:
        """Bytes that aren't UTF-8 must not crash, and messages must match REST."""
        repo = Path(remote["path"])
        blob = git(repo, "hash-object", "-w", "--stdin", stdin=b"1\n2\n")
        git(
            repo,
            "update-index",
            "--index-info",
            stdin=f"100644 {blob}\t".encode() + b"caf\xe9.py\n",
        )
        message = repo / "message.txt"
        message.write_bytes(b"Fix\r\n\r\nBody  \r\n")
        git(repo, "commit", "-q", "--cleanup=verbatim", "-F", str(message))
        sha = git(repo, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/pull/4/head", sha)

        stats = get_commits_numstat(repo, [sha])
        commits = get_pr_commits_local(repo, 4, remote["squash"])

        assert stats[sha][2][".py"].to_tuple() == (2, 0)
        assert commits is not None
        # GitHub keeps CRLF inside messages but strips trailing whitespace
        assert commits[-1]["commit"]["message"] == "Fix\r\n\r\nBody"

    def test_ignores_user_git_config(
        self, tmp_path: Path, remote: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings like log.showSignature or log.showRoot=false must not change results."""
        config = tmp_path / "gitconfig"
        config.write_text(
            "[log]\n\tshowSignature = true\n\tshowRoot = false\n"
            "[diff]\n\trenames = copies\n[color]\n\tui = always\n"
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        repo = Path(remote["path"])
        root = git(repo, "rev-list", "--max-parents=0", "HEAD")

        stats = get_commits_numstat(repo, [root, remote["c1"]])
        commits = get_pr_commits_local(repo, 1, remote["squash"])

        assert stats[root][:2] == (1, 0)
        assert stats[remote["c1"]][:2] == (3, 0)
        assert commits is not None
        assert [c["sha"] for c in commits] == [remote["c1"], remote["c2"], remote["merge"]]

    def test_get_pr_commits_local_missing_ref(self, remote: dict[str, str]) -> None:
        assert get_pr_commits_local(Path(remote["path"]), 2, remote["squash"]) is None


def mirrors_for(tmp_path: Path, token: str | None = None) -> RepoMirrors:
    return RepoMirrors(tmp_path / "mirrors", token, url_template=f"{tmp_path}/gh/{{repo}}")


class TestRepoMirrors:
    def test_sync_clones_and_fetches_pr_heads(self, tmp_path: Path, remote: dict[str, str]) -> None:
        mirrors = mirrors_for(tmp_path)

        path = mirrors.sync("owner/proj", pr_numbers=[1])

        assert path == tmp_path / "mirrors" / "owner" / "proj.git"
        assert path is not None
        assert get_pr_commits_local(path, 1, remote["squash"]) is not None
        assert git(path, "rev-parse", "refs/heads/main") == remote["squash"]

    def test_sync_fetches_new_commits(self, tmp_path: Path, remote: dict[str, str]) -> None:
        mirrors_for(tmp_path).sync("owner/proj")
        repo = Path(remote["path"])
        (repo / "e.py").write_text("e\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "New")
        new = git(repo, "rev-parse", "HEAD")

        # A new instance (a later run) fetches; within one run a repo is synced once
        path = mirrors_for(tmp_path).sync("owner/proj")

        assert path is not None
        assert git(path, "rev-parse", "refs/heads/main") == new

    def test_sync_skips_missing_pr_refs(self, tmp_path: Path, remote: dict[str, str]) -> None:
        path = mirrors_for(tmp_path).sync("owner/proj", pr_numbers=[1, 999])

        assert path is not None
        assert get_pr_commits_local(path, 1, remote["squash"]) is not None

    def test_failed_update_keeps_existing_clone(
        self, tmp_path: Path, remote: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A transient fetch error must not delete a clone that took long to make."""
        mirrors_for(tmp_path).sync("owner/proj")
        shutil.rmtree(tmp_path / "gh")  # The remote is unreachable now

        path = mirrors_for(tmp_path).sync("owner/proj")

        assert path is not None
        assert git(path, "rev-parse", "refs/heads/main") == remote["squash"]
        assert "owner/proj" in capsys.readouterr().err

    @pytest.mark.usefixtures("remote")
    def test_failed_update_skips_pr_heads(self, tmp_path: Path) -> None:
        """Fetching PR heads one by one from an unreachable remote would take minutes."""
        mirrors_for(tmp_path).sync("owner/proj")
        shutil.rmtree(tmp_path / "gh")
        fetches = []

        def counting_run_git(*args: Any, **kwargs: Any) -> str:
            if "fetch" in args:
                fetches.append(args)
            return run_git(*args, **kwargs)

        with patch("trueloc.mirror.run_git", counting_run_git):
            mirrors_for(tmp_path).sync("owner/proj", pr_numbers=range(2, 50))

        assert len(fetches) == 1  # Only the failed branch update

    def test_sync_failure_returns_none(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mirrors_for(tmp_path).sync("owner/missing") is None
        assert "owner/missing" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("version", "supported"),
        [("git version 2.31.9", False), ("git version 2.32.0", True), ("git version 2.55.0", True)],
    )
    def test_git_supports_env_config(self, version: str, supported: bool) -> None:  # noqa: FBT001
        """GIT_CONFIG_COUNT (token) needs git 2.31, GIT_CONFIG_GLOBAL (isolation) 2.32."""
        completed = subprocess.CompletedProcess([], 0, stdout=version)
        with patch("trueloc.mirror.subprocess.run", return_value=completed):
            assert git_supports_env_config() is supported

    def test_token_passed_via_environment(self, tmp_path: Path) -> None:
        env = mirrors_for(tmp_path, token="secret").git_env()  # noqa: S106

        # Scoped to GitHub, so URL rewrites to other hosts can't receive the token
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        # base64 of "x-access-token:secret"
        assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic eC1hY2Nlc3MtdG9rZW46c2VjcmV0"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_git_never_asks_for_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An askpass helper (e.g. a GUI dialog) would hang the run on a rejected token."""
        monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ssh-askpass")
        monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/ssh-askpass")

        env = mirrors_for(tmp_path).git_env()

        assert env["GIT_ASKPASS"] == "echo"
        assert "SSH_ASKPASS" not in env


def search_response(
    nodes: list[dict[str, Any] | None],
    issue_count: int | None = None,
    cursor: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "data": {
            "search": {
                "issueCount": len(nodes) if issue_count is None else issue_count,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload, headers={"X-RateLimit-Remaining": "5000"})


def pr_node(
    number: int, repo: str = "owner/proj", merged_at: str = "2024-06-10T00:00:00Z"
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "mergedAt": merged_at,
        "updatedAt": merged_at,
        "author": {"login": "testuser"},
        "mergeCommit": {"oid": f"merge{number}"},
        "repository": {"nameWithOwner": repo, "diskUsage": 1234},
        "commits": {"totalCount": 3},
    }


def graphql_variables(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content)["variables"] for call in route.calls]


class TestSearchMergedPRs:
    def test_returns_rest_shaped_prs_and_skips_inaccessible(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """PRs in orgs the token can't access (e.g. SAML) come back as null nodes."""
        respx_mock.post("https://api.github.com/graphql").mock(
            return_value=search_response(
                [pr_node(1), None, pr_node(2, repo="org/other")],
                errors=[{"type": "FORBIDDEN", "message": "SAML enforcement"}],
            )
        )

        prs = gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))

        assert prs is not None
        assert prs[0] == {
            "number": 1,
            "title": "PR 1",
            "merged_at": "2024-06-10T00:00:00Z",
            "updated_at": "2024-06-10T00:00:00Z",
            "merge_commit_sha": "merge1",
            "user": {"login": "testuser"},
            "repo": "owner/proj",
            "commit_count": 3,
            "disk_usage": 1234,
        }
        assert [pr["repo"] for pr in prs] == ["owner/proj", "org/other"]

    def test_query(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=search_response([pr_node(1)])
        )

        gh_client.search_merged_prs(
            "testuser", datetime(2024, 6, 1), datetime(2024, 7, 1), repo="owner/proj"
        )

        query = graphql_variables(route)[0]["q"]
        assert query.startswith("is:pr is:merged author:testuser repo:owner/proj merged:")
        assert query.endswith("Z")

    def test_paginates(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        route = respx_mock.post("https://api.github.com/graphql").mock(
            side_effect=[
                search_response([pr_node(1)], issue_count=2, cursor="c1"),
                search_response([pr_node(2)], issue_count=2),
            ]
        )

        prs = gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))

        assert prs is not None
        assert [pr["number"] for pr in prs] == [1, 2]
        assert graphql_variables(route)[1]["cursor"] == "c1"

    def test_bisects_windows_over_search_limit(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """Search returns at most 1000 results per query, so busy windows are split."""
        route = respx_mock.post("https://api.github.com/graphql").mock(
            side_effect=[
                search_response([], issue_count=1500),
                search_response([pr_node(1)]),
                search_response([pr_node(2)]),
            ]
        )

        prs = gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))

        assert prs is not None
        assert sorted(pr["number"] for pr in prs) == [1, 2]
        whole, first, second = (v["q"].split("merged:")[1] for v in graphql_variables(route))
        assert first.split("..")[0] == whole.split("..")[0]
        assert first.split("..")[1] == second.split("..")[0]

    def test_dedupes_prs_at_window_edges(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        """`merged:A..B` includes both ends, so a PR at a split point is found twice."""
        respx_mock.post("https://api.github.com/graphql").mock(
            side_effect=[
                search_response([], issue_count=1500),
                search_response([pr_node(1)]),
                search_response([pr_node(1)]),
            ]
        )

        prs = gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))

        assert prs is not None
        assert [pr["number"] for pr in prs] == [1]

    def test_null_disk_usage(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        node = pr_node(1)
        node["repository"]["diskUsage"] = None
        respx_mock.post("https://api.github.com/graphql").mock(return_value=search_response([node]))

        prs = gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))

        assert prs is not None
        assert prs[0]["disk_usage"] == 0

    def test_cached_within_refresh_interval(
        self, gh_client: GitHubClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post("https://api.github.com/graphql").mock(
            return_value=search_response([pr_node(1)])
        )
        since = datetime.now() - timedelta(days=30)  # noqa: DTZ005

        gh_client.search_merged_prs("testuser", since, datetime.now())  # noqa: DTZ005
        prs = gh_client.search_merged_prs("testuser", since, datetime.now())  # noqa: DTZ005

        assert route.call_count == 1
        assert prs is not None

    def test_failure_returns_none(self, gh_client: GitHubClient, respx_mock: respx.Router) -> None:
        """None (not []) so callers can fall back to listing PRs per repo."""
        respx_mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"search": None}, "errors": [{"type": "TIMEOUT", "message": "x"}]},
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )

        assert (
            gh_client.search_merged_prs("testuser", datetime(2024, 6, 1), datetime(2024, 7, 1))
            is None
        )


@pytest.fixture
def mirrored_client(
    tmp_path: Path,
    remote: dict[str, str],  # noqa: ARG001 (creates the repo to clone)
) -> Generator[GitHubClient, None, None]:
    cache = diskcache.Cache(tmp_path / "cache")
    with httpx.Client(base_url="https://api.github.com") as client:
        yield GitHubClient(client, cache, mirrors=mirrors_for(tmp_path))
    cache.close()


def squash_pr(remote: dict[str, str], commit_count: int = 3) -> dict[str, Any]:
    return {
        "number": 1,
        "merge_commit_sha": remote["squash"],
        "commit_count": commit_count,
        "disk_usage": 100,
    }


class TestLocalPrefetch:
    """Stats computed from a local clone fill the same cache the API path uses."""

    def test_prefetch_pr_commits(
        self, mirrored_client: GitHubClient, remote: dict[str, str]
    ) -> None:
        with patch("trueloc.github.LOCAL_MIN_COMMITS", 1):
            mirrored_client.prefetch_pr_commits("owner/proj", [squash_pr(remote)])

        # No HTTP routes are mocked: any API request would fail the test
        commits = mirrored_client.get_pr_commits("owner/proj", 1)
        adds, dels, by_ext = mirrored_client.get_pr_stats_per_commit("owner/proj", 1)
        assert commits == [remote["c1"], remote["c2"], remote["merge"]]
        assert (adds, dels) == (4, 0)  # c1 +3, c2 +1 (rename); merge commit skipped
        assert by_ext[".py"].to_tuple() == (4, 0)

    def test_prefetch_pr_commits_count_mismatch_uses_api(
        self, mirrored_client: GitHubClient, remote: dict[str, str]
    ) -> None:
        """If git disagrees with GitHub's commit count, leave the PR to the API."""
        with patch("trueloc.github.LOCAL_MIN_COMMITS", 1):
            mirrored_client.prefetch_pr_commits("owner/proj", [squash_pr(remote, commit_count=5)])

        assert "pr_commits_raw:owner/proj:1" not in mirrored_client.cache

    def test_prefetch_pr_commits_skips_small_repos(
        self, tmp_path: Path, mirrored_client: GitHubClient, remote: dict[str, str]
    ) -> None:
        """Cloning costs more than a few API requests."""
        mirrored_client.prefetch_pr_commits("owner/proj", [squash_pr(remote)])

        assert not (tmp_path / "mirrors" / "owner" / "proj.git").exists()

    def test_prefetch_pr_commits_skips_huge_repos(
        self, tmp_path: Path, mirrored_client: GitHubClient, remote: dict[str, str]
    ) -> None:
        pr = {**squash_pr(remote), "disk_usage": 10_000_000}
        with patch("trueloc.github.LOCAL_MIN_COMMITS", 1):
            mirrored_client.prefetch_pr_commits("owner/proj", [pr])

        assert not (tmp_path / "mirrors" / "owner" / "proj.git").exists()

    def test_prefetch_commit_stats(
        self, mirrored_client: GitHubClient, remote: dict[str, str], respx_mock: respx.Router
    ) -> None:
        respx_mock.get("https://api.github.com/repos/owner/proj").mock(
            return_value=httpx.Response(
                200,
                json={"default_branch": "main", "size": 100},
                headers={"X-RateLimit-Remaining": "5000"},
            )
        )
        with patch("trueloc.github.LOCAL_MIN_COMMITS", 1):
            mirrored_client.prefetch_commit_stats("owner/proj", [remote["c1"]])

        assert mirrored_client.get_commit_stats("owner/proj", remote["c1"])[:2] == (3, 0)


class TestCountWithLocalGit:
    def test_count_uses_search_and_local_clone(
        self, tmp_path: Path, remote: dict[str, str], respx_mock: respx.Router
    ) -> None:
        """PRs come from one search request; all commit stats from the local clone."""
        from typer.testing import CliRunner

        from trueloc.cli import app

        headers = {"X-RateLimit-Remaining": "5000"}

        def ok(data: Any) -> httpx.Response:
            return httpx.Response(200, json=data, headers=headers)

        def graphql(request: httpx.Request) -> httpx.Response:
            query = json.loads(request.content)["query"]
            if "search(" in query:
                node = {**pr_node(1), "mergeCommit": {"oid": remote["squash"]}}
                return search_response([node])
            collection: dict[str, list[Any]] = {
                "pullRequestContributionsByRepository": [],
                "commitContributionsByRepository": [],
            }
            return ok({"data": {"user": {"contributionsCollection": collection}}})

        respx_mock.post("https://api.github.com/graphql").mock(side_effect=graphql)
        repos_url = "https://api.github.com/users/testuser/repos"
        respx_mock.get(repos_url, params__contains={"page": "1"}).mock(
            return_value=ok(
                [{"full_name": "owner/proj", "fork": False, "pushed_at": "2024-06-10T00:00:00Z"}]
            )
        )
        respx_mock.get(repos_url, params__contains={"page": "2"}).mock(return_value=ok([]))
        respx_mock.get("https://api.github.com/repos/owner/proj").mock(
            return_value=ok({"default_branch": "main", "size": 100})
        )
        branch_commits = [
            {
                "sha": sha,
                "parents": [{"sha": "p"}],
                "commit": {"author": {"date": "2024-01-10T18:00:00Z"}, "message": message},
            }
            for sha, message in [(remote["squash"], "PR (#1)"), (remote["direct"], "Main work")]
        ]
        commits_url = "https://api.github.com/repos/owner/proj/commits"
        respx_mock.get(commits_url, params__contains={"page": "1"}).mock(
            return_value=ok(branch_commits)
        )
        respx_mock.get(commits_url, params__contains={"page": "2"}).mock(return_value=ok([]))

        def local_mirrors(root: Path, token: str | None) -> RepoMirrors:
            return RepoMirrors(root, token, url_template=f"{tmp_path}/gh/{{repo}}")

        with (
            patch("trueloc.cli.get_github_token", return_value="token"),
            patch("trueloc.cli.RepoMirrors", local_mirrors),
            patch("trueloc.github.LOCAL_MIN_COMMITS", 1),
        ):
            result = CliRunner().invoke(
                app,
                ["count", "testuser", "--since", "2024-01-01", "--until", "2024-12-31", "--json"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        # PR: c1 +3, c2 +1 (merge commit skipped); direct commit "Main work": +2
        assert data["summary"]["total_additions"] == 6
        # The clone lives in the cache directory, so clear-cache removes it
        assert (tmp_path / "test_cache" / "repos" / "owner" / "proj.git").exists()
