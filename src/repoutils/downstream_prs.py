"""The open pull requests from earlier `downstream bump --pr` runs, for
`downstream bump --pr` to close once its new pull request supersedes them,
or to warn on, kept apart from the command for length.

An earlier bump's pull request has a hidden marker at the start of its body,
and comes from a branch of the downstream's own repository named for the
release it bumps to. A test bump, to a dev version, has a marker and a branch
prefix of its own, so a release's bump and a test bump never see each other's
pull requests. It's untouched when every commit on it is one that
`downstream bump` made, marked with TRAILER, apart from merges of the base
branch that GitHub's Update branch button made. Stdlib-only, like every
repoutils module."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import downstream_pins, github, pull_request

# A commit trailer that marks the commits `bump --pr` makes, so that a later
# bump can tell an untouched bump PR from one a person has added to.
TRAILER = "Bumped-By: repoutils downstream bump"
# The committer of the merge commits that GitHub's Update branch button makes.
_WEB_FLOW = "web-flow"
# GitHub lists at most this many of a pull request's commits.
_MAX_COMMITS = 250


@dataclass(frozen=True)
class Bump:
    """An open pull request from an earlier bump."""

    number: int
    url: str
    branch: str
    base: str
    # The commit at the head of its branch when it was listed.
    head: str
    version: str
    body: str


def kind(version: str) -> str:
    """What starts the branch and the marker name of a bump to version:
    `downstream-test` for a dev version, a test bump, else `downstream`."""
    return "downstream-test" if downstream_pins.is_dev(version) else "downstream"


def branch_name(package: str, version: str) -> str:
    return f"{kind(version)}/{package}-{version}"


def marker_name(package: str, version: str) -> str:
    """The name of the marker that starts the body of a bump to version's pull request."""
    return f"{kind(version)}-{package}"


def open_bumps(repo: str, token: str, package: str, version: str, marker: str) -> list[Bump]:
    """The open pull requests in repo whose body starts with marker, from a
    branch of repo itself (not a fork) named for a version of package of the
    same kind as version: a dev version for a test bump, else a release."""
    prefix = branch_name(package, version).removesuffix(version)
    test = downstream_pins.is_dev(version)
    found = []
    page = 1
    while True:
        pulls: list[dict[str, Any]] = github.request(
            "GET", f"{github.API_URL}/repos/{repo}/pulls?state=open&per_page=100&page={page}", token
        )
        for pull in pulls:
            head = pull["head"]
            source = (head.get("repo") or {}).get("full_name", "")
            ref: str = head["ref"]
            body: str = pull.get("body") or ""
            if source.lower() != repo.lower() or not body.startswith(marker):
                continue
            bumped = ref.removeprefix(prefix)
            if ref == bumped:
                continue
            try:
                # Such as a dev version's, from before test bumps had a branch prefix of their own.
                if downstream_pins.is_dev(bumped) != test:
                    continue
            except ValueError:
                continue
            found.append(
                Bump(
                    number=pull["number"],
                    url=pull["html_url"],
                    branch=ref,
                    base=pull["base"]["ref"],
                    head=head["sha"],
                    version=bumped,
                    body=body,
                )
            )
        if len(pulls) < 100:
            return found
        page += 1


def _identity(person: dict[str, Any]) -> tuple[str, str, str]:
    return person["name"], person["email"], person["date"]


def _is_bump_commit(commit: dict[str, Any]) -> bool:
    """Whether commit is one that `downstream bump` made: TRAILER is a line
    of its message's last paragraph, and it was committed by its author, at
    the time it was written. An amend or a rebase keeps the trailer and the
    author, but sets a new committer and commit time, so a rebased bump
    counts as another change, even one by GitHub's Update branch button,
    which can't tell it from an amended one."""
    message: str = commit["commit"]["message"]
    git = commit["commit"]
    return (
        len(commit["parents"]) == 1
        and TRAILER in message.strip().split("\n\n")[-1].splitlines()
        and _identity(git["committer"]) == _identity(git["author"])
    )


def _is_update_merge(commit: dict[str, Any], bump: Bump) -> bool:
    """Whether commit is a merge of bump's base branch that GitHub's Update branch button made."""
    message: str = commit["commit"]["message"]
    committer = commit.get("committer") or {}
    return (
        len(commit["parents"]) == 2
        and committer.get("login") == _WEB_FLOW
        and message.strip() == f"Merge branch '{bump.base}' into {bump.branch}"
    )


def untouched(repo: str, token: str, bump: Bump) -> bool:
    """Whether every commit on bump's branch is one `downstream bump` made,
    or an Update branch merge of its base. Judged by the commits, not the
    diff, as a re-lock or a merge changes uv.lock too."""
    commits: list[dict[str, Any]] = []
    page = 1
    while True:
        batch: list[dict[str, Any]] = github.request(
            "GET", f"{github.API_URL}/repos/{repo}/pulls/{bump.number}/commits?per_page=100&page={page}", token
        )
        commits += batch
        if len(batch) < 100:
            break
        page += 1
    # GitHub lists no more than this, so there could be others it didn't list.
    if len(commits) >= _MAX_COMMITS:
        return False
    return bool(commits) and all(_is_bump_commit(c) or _is_update_merge(c, bump) for c in commits)


def comment_marker(marker: str, version: str) -> str:
    """The marker that starts the comment a bump to version posts on an
    earlier bump's pull request, so that a rerun doesn't post it again."""
    return f"{marker.removesuffix(' -->')}-{version} -->"


def _comment(repo: str, token: str, bump: Bump, marker: str, body: str) -> None:
    """Post body, which starts with marker, on bump's pull request, unless a
    comment there already starts with marker. Anyone's comment counts, as
    the token that posted an earlier one may not be this one."""
    if not github.marked_comments(repo, bump.number, None, marker, token):
        github.request(
            "POST", f"{github.API_URL}/repos/{repo}/issues/{bump.number}/comments", token, json_body={"body": body}
        )


def supersede(directory: Path, repo: str, token: str, bump: Bump, marker: str, number: int) -> None:
    """Delete bump's branch from the remote of the downstream's checkout in
    directory, unless the branch has moved on from bump.head, then close its
    pull request, and comment that pull request number supersedes it. If the
    branch has moved on, the pull request stays open, for a rerun to judge
    again."""
    # The lease fails the push if someone pushed to the branch after it was judged untouched.
    lease = f"--force-with-lease=refs/heads/{bump.branch}:{bump.head}"
    try:
        pull_request.git(directory, "push", lease, pull_request.REMOTE, f":refs/heads/{bump.branch}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Deleting the branch {bump.branch} failed, so it's still open:\n{e.stderr}") from e
    # Deleting the branch closes the pull request too, but not at once.
    github.request("PATCH", f"{github.API_URL}/repos/{repo}/pulls/{bump.number}", token, json_body={"state": "closed"})
    _comment(repo, token, bump, marker, f"{marker}\nSuperseded by #{number}.")


def warn(repo: str, token: str, bump: Bump, marker: str, number: int, package: str, version: str) -> None:
    """Comment on bump's pull request that pull request number bumps package
    to version, and that its other changes probably need moving across."""
    _comment(
        repo,
        token,
        bump,
        marker,
        f"{marker}\n#{number} bumps {package} to {version}. "
        "This pull request has commits that `repoutils downstream bump` didn't make, so it stays open, "
        f"but its changes probably need moving across to #{number}.",
    )
