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
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from repoutils import downstream_pins, downstream_prs, github

# The check run conclusions that GitHub counts as passing a required check.
_PASSING = {"success", "neutral", "skipped"}
_PER_PAGE = 100
# The ID of the GitHub Actions app, whose check run for a job has the job's ID.
ACTIONS_APP = 15368


@dataclass(frozen=True)
class Required:
    """A check that a ruleset requires, from the app whose ID is app, if given."""

    context: str
    app: int | None


@dataclass(frozen=True)
class Check:
    """A check run, or a commit status if app is None, on a commit."""

    name: str
    app: int | None
    # pending, passed or failed.
    state: str
    # Its status or conclusion, as GitHub gives it.
    detail: str
    url: str
    # The check run's or status's own ID, which a rerun's new check run doesn't share.
    id: int


def required_checks(repo: str, token: str, branch: str) -> list[Required]:
    """The checks that repo's rulesets require on branch, from the rules API."""
    url = f"{github.API_URL}/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}"
    found: dict[Required, None] = {}
    page = 1
    while True:
        rules: list[dict[str, Any]] = github.request("GET", f"{url}?per_page={_PER_PAGE}&page={page}", token)
        for rule in rules:
            if rule.get("type") != "required_status_checks":
                continue
            for check in (rule.get("parameters") or {}).get("required_status_checks", []):
                found[Required(check["context"], check.get("integration_id"))] = None
        if len(rules) < _PER_PAGE:
            return list(found)
        page += 1


def _pages(url: str, token: str, key: str) -> list[dict[str, Any]]:
    """The items under key in each page of url's response."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch: list[dict[str, Any]] = github.request("GET", f"{url}?per_page={_PER_PAGE}&page={page}", token)[key]
        items += batch
        if len(batch) < _PER_PAGE:
            return items
        page += 1


def checks(repo: str, token: str, sha: str) -> list[Check]:
    """The latest check run of each name, and the latest commit status of
    each context, on commit sha in repo."""
    commit = f"{github.API_URL}/repos/{repo}/commits/{sha}"
    found = []
    for run in _pages(f"{commit}/check-runs", token, "check_runs"):
        conclusion = run.get("conclusion")
        if run.get("status") != "completed":
            state, detail = "pending", str(run.get("status"))
        else:
            state, detail = ("passed" if conclusion in _PASSING else "failed"), str(conclusion)
        app = (run.get("app") or {}).get("id")
        found.append(Check(run["name"], app, state, detail, run.get("html_url") or "", run["id"]))
    # The combined status holds only the latest status of each context.
    for status in _pages(f"{commit}/status", token, "statuses"):
        state = {"success": "passed", "pending": "pending"}.get(status["state"], "failed")
        found.append(
            Check(status["context"], None, state, status["state"], status.get("target_url") or "", status["id"])
        )
    return found


def missing(required: list[Required], found: list[Check]) -> list[Required]:
    """The checks in required that nothing in found is. A required check that
    names an app is only a check run from that app, as the API doesn't say
    which app posted a commit status."""
    return [r for r in required if not any(c.name == r.context and r.app in (None, c.app) for c in found)]


class Closed(RuntimeError):
    """The pull request closed, or merged, while finish waited for its checks."""


def find(repo: str, token: str, package: str, version: str, marker: str) -> downstream_prs.Bump | None:
    """The open pull request in repo that bumps package to version, whose
    body starts with marker: a test pull request for a dev version, or a
    release's pull request. None if a release has no such pull request, as a
    bump that had nothing to do, or a merged pull request, leaves none.
    Raises RuntimeError if a dev version has no such test pull request,
    which a test run always opens."""
    for bump in downstream_prs.open_bumps(repo, token, package, version, marker):
        if bump.version == version:
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


def rerun(repo: str, token: str, failed: list[Check]) -> bool:
    """Re-run the failed jobs of the workflow runs whose jobs are the check
    runs in failed, and return True. Re-run nothing and return False if a
    check in failed isn't a GitHub Actions job, as it couldn't pass then, or
    print a warning and return False if GitHub refuses a re-run."""
    if any(c.app != ACTIONS_APP for c in failed):
        return False
    api = f"{github.API_URL}/repos/{repo}/actions"
    try:
        runs = {github.request("GET", f"{api}/jobs/{c.id}", token)["run_id"]: None for c in failed}
        for run_id in runs:
            github.request("POST", f"{api}/runs/{run_id}/rerun-failed-jobs", token)
            print(f"Re-ran the failed jobs of https://github.com/{repo}/actions/runs/{run_id}")
    # RuntimeError is github.request()'s error for too many redirects.
    except (OSError, RuntimeError) as e:
        github.close_error(e)
        print(
            f"warning: couldn't re-run the failed jobs on {repo}, which needs Actions write: {e}",
            file=sys.stderr,
        )
        return False
    return True


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
