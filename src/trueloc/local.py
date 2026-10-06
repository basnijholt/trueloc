"""Local git repository analysis functions."""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from trueloc.models import FileStats
from trueloc.utils import get_file_extension

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


def run_git(
    repo_path: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a git command in the specified repository.

    Output that isn't valid UTF-8 (e.g. Latin-1 file names) is decoded with replacements.
    """
    cmd = ["git", "-C", str(repo_path), *args]
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        check=False,
        input=None if stdin is None else stdin.encode(),
        env=env,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise subprocess.CalledProcessError(result.returncode, cmd, stdout, stderr)
    return stdout


def _isolated_env(**extra: str) -> dict[str, str]:
    """Environment ignoring the user's git config (e.g. log.showSignature, log.showRoot).

    For read-only commands whose output is parsed.
    """
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **extra}


def get_local_commits(
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
    output = run_git(repo_path, *args)

    commits = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:  # noqa: PLR2004
            commits.append(
                {
                    "sha": parts[0],
                    "date": parts[1][:10],  # Just the date part
                    "message": parts[2],
                }
            )
    return commits


def get_commit_numstat(repo_path: Path, sha: str) -> tuple[int, int, dict[str, FileStats]]:
    """Get additions/deletions for a commit using git show --numstat.

    Returns (total_additions, total_deletions, by_extension).
    """
    try:
        output = run_git(repo_path, "show", "--numstat", "--format=", sha)
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
        ext = get_file_extension(filename)

        total_add += additions
        total_del += deletions
        by_extension[ext].additions += additions
        by_extension[ext].deletions += deletions

    return total_add, total_del, dict(by_extension)


def parse_numstat_z(output: str) -> dict[str, tuple[int, int, dict[str, FileStats]]]:
    """Parse `git log --numstat -z --format=%x01%H` output into stats per commit."""
    stats: dict[str, tuple[int, int, dict[str, FileStats]]] = {}
    for block in output.split("\x01")[1:]:
        sha, _, files = block.partition("\0")
        by_extension: dict[str, FileStats] = defaultdict(FileStats)
        total_add = 0
        total_del = 0
        tokens = iter(files.split("\0"))
        for raw_token in tokens:
            token = raw_token.lstrip("\n")
            if not token:
                continue
            add_str, del_str, path = token.split("\t", 2)
            if not path:  # Rename: the old and new paths follow
                next(tokens)
                path = next(tokens)
            # Binary files show "-" for additions/deletions
            if add_str == "-" or del_str == "-":
                continue
            additions = int(add_str)
            deletions = int(del_str)
            ext = get_file_extension(path)
            total_add += additions
            total_del += deletions
            by_extension[ext].additions += additions
            by_extension[ext].deletions += deletions
        stats[sha] = (total_add, total_del, dict(by_extension))
    return stats


def get_commits_numstat(
    repo_path: Path, shas: list[str]
) -> dict[str, tuple[int, int, dict[str, FileStats]]]:
    """Get additions/deletions per commit for many commits with a single git call.

    Merge commits get no stats (git log shows no diff for them).
    """
    if not shas:
        return {}
    output = run_git(
        repo_path,
        "log",
        "--no-walk=unsorted",
        "--stdin",
        "--numstat",
        "-z",
        "-M",
        "--no-ext-diff",
        "--no-textconv",
        "--format=%x01%H",
        stdin="\n".join(shas),
        env=_isolated_env(),
    )
    return parse_numstat_z(output)


def get_existing_commits(repo_path: Path, shas: list[str]) -> set[str]:
    """Return the subset of SHAs that are commits in the local repository."""
    if not shas:
        return set()
    output = run_git(
        repo_path,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        stdin="\n".join(shas),
        env=_isolated_env(),
    )
    return {line.split()[0] for line in output.splitlines() if line.endswith(" commit")}


def get_pr_commits_local(
    repo_path: Path, pr_number: int, merge_commit_sha: str
) -> list[dict[str, Any]] | None:
    """Get a merged PR's commits from refs/pull/N/head, shaped like GET /pulls/{n}/commits.

    The PR's commits are those on its head that the base branch did not have when it
    was merged (the merge commit's first parent), for merge, squash, and rebase merges.
    Returns None if the PR head or merge commit is not available locally.
    """
    try:
        output = run_git(
            repo_path,
            "log",
            "--reverse",
            "--topo-order",
            "--date=format-local:%Y-%m-%dT%H:%M:%SZ",
            "--format=%H%x00%P%x00%ad%x00%B%x01",
            f"refs/pull/{pr_number}/head",
            "--not",
            f"{merge_commit_sha}^1",
            env=_isolated_env(TZ="UTC"),
        )
    except subprocess.CalledProcessError:
        return None
    commits = []
    for record in output.split("\x01"):
        record = record.lstrip("\n")  # noqa: PLW2901
        if not record:
            continue
        sha, parents, date, message = record.split("\0", 3)
        commits.append(
            {
                "sha": sha,
                "parents": [{"sha": parent} for parent in parents.split()],
                # GitHub strips trailing whitespace from messages
                "commit": {"author": {"date": date}, "message": message.rstrip()},
            }
        )
    return commits
