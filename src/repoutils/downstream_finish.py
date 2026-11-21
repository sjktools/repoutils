"""`downstream finish`, which waits for the checks on a test bump's pull
request in a downstream, and closes it without merging it if they all pass,
kept apart from the command for length.

It first waits for the checks that the downstream's rulesets require on the
pull request's base branch to appear, as a check registers some time after
the pull request opens, and then for every check run and commit status on
its head to finish. Stdlib-only, like every repoutils module."""

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
        found.append(Check(run["name"], app, state, detail, run.get("html_url") or ""))
    # The combined status holds only the latest status of each context.
    for status in _pages(f"{commit}/status", token, "statuses"):
        state = {"success": "passed", "pending": "pending"}.get(status["state"], "failed")
        found.append(Check(status["context"], None, state, status["state"], status.get("target_url") or ""))
    return found


def missing(required: list[Required], found: list[Check]) -> list[Required]:
    """The checks in required that nothing in found is. A required check that
    names an app is only a check run from that app, as the API doesn't say
    which app posted a commit status."""
    return [r for r in required if not any(c.name == r.context and r.app in (None, c.app) for c in found)]


def find(repo: str, token: str, package: str, version: str, marker: str) -> downstream_prs.Bump:
    """The open test pull request in repo that bumps package to dev version
    version, whose body starts with marker, raising ValueError if version
    isn't a dev version, and RuntimeError if there's no such pull request."""
    if not downstream_pins.is_dev(version):
        raise ValueError(
            f"{version} isn't a dev version, and finish closes only the test pull requests of dev versions"
        )
    for bump in downstream_prs.open_bumps(repo, token, package, version, marker):
        if bump.version == version:
            return bump
    raise RuntimeError(
        f"{repo} has no open test pull request that bumps {package} to {version}: did its bump or push fail, "
        "or was there nothing to bump?"
    )


def _pull(repo: str, token: str, bump: downstream_prs.Bump) -> dict[str, Any]:
    """bump's pull request, raising RuntimeError if it's no longer open."""
    pull: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{bump.number}", token)
    if pull["state"] != "open":
        raise RuntimeError(f"{bump.url} is no longer open, so there's nothing to finish")
    return pull


def wait(
    repo: str,
    token: str,
    bump: downstream_prs.Bump,
    *,
    interval: float,
    timeout: float,
) -> tuple[str, list[Check]]:
    """The head commit of bump's pull request, and its checks, once the
    checks that the rulesets require on its base branch are there, at least
    one check is, and every check has finished, on two reads in a row that
    find the same checks, as a workflow can add its check after another's
    finished. Raises TimeoutError if that takes over timeout
    seconds."""
    deadline = monotonic() + timeout
    required = required_checks(repo, token, bump.base)
    print(f"Waiting for the checks on {bump.url}")
    if required:
        print(f"{bump.base}'s rulesets require {', '.join(r.context for r in required)}")
    # The head and checks of the last read that found every check finished.
    settled: tuple[str, set[Check]] | None = None
    while True:
        # Its latest head, as someone may have pushed to it.
        sha: str = _pull(repo, token, bump)["head"]["sha"]
        found = checks(repo, token, sha)
        waiting = [r.context for r in missing(required, found)] + [c.name for c in found if c.state == "pending"]
        # Each check's URL too, so that a check run replaced by another of the same name counts as a change.
        done = (sha, set(found)) if found and not waiting else None
        # Read the head again, so that it closes only the commit whose checks it read.
        if done is not None and done == settled and _pull(repo, token, bump)["head"]["sha"] == sha:
            return sha, found
        settled = done
        left = deadline - monotonic()
        if left <= 0:
            what = ", ".join(waiting) if waiting else "any check to start"
            raise TimeoutError(f"Gave up after {timeout:g} seconds waiting for {what} on {bump.url}, so it stays open")
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


def finish(repo: str, token: str, bump: downstream_prs.Bump, *, interval: float, timeout: float) -> int:
    """Wait for the checks on bump's pull request, then close it and delete
    its branch if they all passed, and return 0. If any failed, leave it
    open, and return 1."""
    sha, found = wait(repo, token, bump, interval=interval, timeout=timeout)
    if failed := [c for c in found if c.state == "failed"]:
        print(f"Left {bump.url} open, as these checks failed on {sha}:", file=sys.stderr)
        for check in failed:
            print(f"- {check.name}: {check.detail}" + (f" {check.url}" if check.url else ""), file=sys.stderr)
        return 1
    close(repo, token, bump, len(found))
    print(f"All {len(found)} checks passed on {sha}, so closed {bump.url} and deleted its branch {bump.branch}")
    return 0
