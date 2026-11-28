"""`downstream finish`, which waits for the checks on a bump's pull request
in a downstream. For a test bump, to a dev version, it closes the pull
request without merging it if they all pass. For a release, it leaves the
pull request open for review. Kept apart from the command for length.

It first waits for the checks that the downstream's rulesets require on the
pull request's base branch to appear, as a check registers some time after
the pull request opens, and then for every check run and commit status on
its head to finish. If only GitHub Actions jobs failed, it re-runs them
once (or --retries times), as a downstream's CI can fail for a moment's
reason, such as a package index server that doesn't yet list the version.
Stdlib-only, like every repoutils module."""

import os
import sys
import urllib.parse
from time import monotonic, sleep
from typing import Any

from repoutils import downstream_pins, downstream_prs, github
from repoutils.pr_checks import Check, Required, checks, missing, required_checks, rerun


class Closed(RuntimeError):
    """The pull request closed, or merged, while finish waited for its checks."""


def find(repo: str, token: str, package: str, version: str, marker: str) -> downstream_prs.Bump | None:
    """The open pull request in repo that bumps package to version, whose
    body starts with marker: a test pull request for a dev version, or a
    release's pull request. None if a release has no such pull request, as a
    bump that had nothing to do, or a merged pull request, leaves none.
    Raises RuntimeError if a dev version has no such test pull request,
    which a test run always opens."""
    key = downstream_pins.release_key(version)
    # As bump does, so that 0.4 finds the pull request of 0.4.0.
    for bump in downstream_prs.open_bumps(repo, token, package, version, marker):
        if downstream_pins.release_key(bump.version) == key:
            return bump
    if not downstream_pins.is_dev(version):
        return None
    raise RuntimeError(
        f"{repo} has no open test pull request that bumps {package} to {version}: did its bump or push fail, "
        "or was there nothing to bump?"
    )


def _pull(repo: str, token: str, bump: downstream_prs.Bump) -> dict[str, Any]:
    """bump's pull request, raising Closed if it's no longer open."""
    pull: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{bump.number}", token)
    if pull["state"] != "open":
        raise Closed(f"{bump.url} is no longer open, so there's nothing to finish")
    return pull


def wait(
    repo: str,
    token: str,
    bump: downstream_prs.Bump,
    *,
    interval: float,
    deadline: float,
    required: list[Required],
    replaced: frozenset[int] = frozenset(),
) -> tuple[str, list[Check]]:
    """The head commit of bump's pull request, and its checks, once the
    checks in required are there, at least one check is, and every check
    has finished, on two reads in a row that find the same checks, as a
    workflow can add its check after another's finished. A check whose ID is
    in replaced counts as unfinished, as it's one that a re-run will replace.
    Raises TimeoutError, saying what it waited for, if that takes until
    after deadline (from monotonic())."""
    # The head and checks of the last read that found every check finished.
    settled: tuple[str, set[Check]] | None = None
    while True:
        # Its latest head, as someone may have pushed to it.
        sha: str = _pull(repo, token, bump)["head"]["sha"]
        found = checks(repo, token, sha)
        waiting = [r.context for r in missing(required, found)]
        waiting += [c.name for c in found if c.state == "pending" or c.id in replaced]
        # Whole checks, IDs included, so that a check run replaced by another of the same name counts as a change.
        done = (sha, set(found)) if found and not waiting else None
        # Read the head again, so that it closes only the commit whose checks it read.
        if done is not None and done == settled and _pull(repo, token, bump)["head"]["sha"] == sha:
            return sha, found
        settled = done
        left = deadline - monotonic()
        if left <= 0:
            what = ", ".join(waiting) if waiting else "any check to start"
            raise TimeoutError(f"waiting for {what} on {bump.url}, so it stays open")
        sleep(min(interval, left))


def close(repo: str, token: str, bump: downstream_prs.Bump, count: int) -> None:
    """Comment that count checks passed, close bump's pull request without merging it, and delete its branch."""
    run_id = os.environ.get("GITHUB_RUN_ID")
    upstream = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run = f" in {server}/{upstream}/actions/runs/{run_id}" if run_id and upstream else ""
    api = f"{github.API_URL}/repos/{repo}"
    github.request(
        "POST",
        f"{api}/issues/{bump.number}/comments",
        token,
        json_body={"body": f"All {count} checks passed, so `repoutils downstream finish`{run} closes this test."},
    )
    github.request("PATCH", f"{api}/pulls/{bump.number}", token, json_body={"state": "closed"})
    github.request("DELETE", f"{api}/git/refs/heads/{urllib.parse.quote(bump.branch, safe='/')}", token)


def finish(
    repo: str, token: str, bump: downstream_prs.Bump, *, interval: float, timeout: float, retries: int = 1
) -> int:
    """Wait for the checks on bump's pull request, and return 0 if they all
    passed, after closing it and deleting its branch if it's a test bump, to
    a dev version. If only GitHub Actions jobs failed, re-run them and wait
    again, up to retries times while time is left. If any still failed,
    leave it open, and return 1. Return 0 if a release's pull request closes,
    or merges, while it waits, as there's nothing left to watch then."""
    test = downstream_pins.is_dev(bump.version)
    deadline = monotonic() + timeout
    required = required_checks(repo, token, bump.base)
    print(f"Waiting for the checks on {bump.url}")
    if required:
        print(f"{bump.base}'s rulesets require {', '.join(r.context for r in required)}")
    try:
        sha, found = wait(repo, token, bump, interval=interval, deadline=deadline, required=required)
        failed = [c for c in found if c.state == "failed"]
        # With no time left, a re-run couldn't finish, so it reports the failures instead.
        while failed and retries > 0 and monotonic() < deadline and rerun(repo, token, failed):
            retries -= 1
            replaced = frozenset(c.id for c in failed)
            sha, found = wait(
                repo, token, bump, interval=interval, deadline=deadline, required=required, replaced=replaced
            )
            failed = [c for c in found if c.state == "failed"]
    except TimeoutError as e:
        raise TimeoutError(f"Gave up after {timeout:g} seconds {e}") from None
    except Closed:
        if test:
            raise
        print(f"{bump.url} is no longer open, so there's nothing left to wait for")
        return 0
    if failed:
        print(f"Left {bump.url} open, as these checks failed on {sha}:", file=sys.stderr)
        for check in failed:
            print(f"- {check.name}: {check.detail}" + (f" {check.url}" if check.url else ""), file=sys.stderr)
        return 1
    if not test:
        print(f"All {len(found)} checks passed on {sha}, so left {bump.url} open for review")
        return 0
    close(repo, token, bump, len(found))
    print(f"All {len(found)} checks passed on {sha}, so closed {bump.url} and deleted its branch {bump.branch}")
    return 0
