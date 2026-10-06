"""Local bare clones of GitHub repos, to compute commit stats without API requests."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from rich.console import Console

from trueloc.local import run_git

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

console = Console(stderr=True)  # Keep stdout clean for --json

FETCH_CHUNK_SIZE = 500  # PR refs per git fetch, keeping command lines short


class RepoMirrors:
    """Bare clones of repos, including the PR heads (refs/pull/N/head) GitHub keeps."""

    def __init__(
        self,
        root: Path,
        token: str | None,
        url_template: str = "https://github.com/{repo}.git",
    ) -> None:
        self.root = root
        self.token = token
        self.url_template = url_template
        self._synced: set[str] = set()

    def path(self, repo: str) -> Path:
        """Local path of the bare clone of a repo."""
        return self.root / f"{repo}.git"

    def git_env(self) -> dict[str, str]:
        """Environment for git, passing the token via config (not visible in `ps`)."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self.token:
            credentials = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
            }
        return env

    def sync(self, repo: str, pr_numbers: Iterable[int] = ()) -> Path | None:
        """Clone or update a repo's branches once per run, and fetch missing PR heads.

        Returns the path of the bare clone, or None if it could not be cloned or fetched.
        """
        path = self.path(repo)
        url = self.url_template.format(repo=repo)
        env = self.git_env()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_git(path.parent, "clone", "-q", "--bare", "--no-tags", url, path.name, env=env)
            except subprocess.CalledProcessError as e:
                console.print(
                    f"[yellow]Could not clone {repo}, using the API instead: {e.stderr}[/yellow]"
                )
                shutil.rmtree(path, ignore_errors=True)
                return None
        elif repo not in self._synced:
            refspec = "+refs/heads/*:refs/heads/*"
            try:
                run_git(path, "fetch", "-q", "--prune", "--no-tags", url, refspec, env=env)
            except subprocess.CalledProcessError as e:
                # Keep the existing clone; commits missing from it fall back to the API
                console.print(f"[yellow]Could not update {repo}: {e.stderr}[/yellow]")
        self._synced.add(repo)
        self._fetch_pr_heads(path, url, pr_numbers, env)
        return path

    def _fetch_pr_heads(
        self, path: Path, url: str, pr_numbers: Iterable[int], env: dict[str, str]
    ) -> None:
        """Fetch refs/pull/N/head for PRs that are not available locally yet."""
        present = set(run_git(path, "for-each-ref", "--format=%(refname)", "refs/pull/").split())
        refspecs = [
            f"+refs/pull/{n}/head:refs/pull/{n}/head"
            for n in sorted(set(pr_numbers))
            if f"refs/pull/{n}/head" not in present
        ]
        for i in range(0, len(refspecs), FETCH_CHUNK_SIZE):
            chunk = refspecs[i : i + FETCH_CHUNK_SIZE]
            try:
                run_git(path, "fetch", "-q", "--no-tags", url, *chunk, env=env)
            except subprocess.CalledProcessError:
                # One missing ref fails the whole fetch; fetch the chunk one by one
                for refspec in chunk:
                    try:
                        run_git(path, "fetch", "-q", "--no-tags", url, refspec, env=env)
                    except subprocess.CalledProcessError:
                        continue
