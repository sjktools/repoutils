"""The checks on a commit, for the commands that wait for them: `downstream
finish` and `dependabot-merge`. It reads the checks that a branch's
rulesets require, lists the check runs and commit statuses on a commit,
waits for them to finish, and re-runs failed GitHub Actions jobs.
Stdlib-only, like every repoutils module."""

import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep, time
from typing import Any

from repoutils import github

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
    # When the check run started or finished, or the status was posted, in seconds since the epoch.
    since: float | None = None


def branch_rules(repo: str, token: str, branch: str) -> list[dict[str, Any]]:
    """Every rule that repo's rulesets apply to branch, from the rules API."""
    url = f"{github.API_URL}/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}"
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        rules: list[dict[str, Any]] = github.request("GET", f"{url}?per_page={_PER_PAGE}&page={page}", token)
        found += rules
        if len(rules) < _PER_PAGE:
            return found
        page += 1


def required(rules: list[dict[str, Any]]) -> list[Required]:
    """The checks that the required_status_checks rules in rules require."""
    found: dict[Required, None] = {}
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        for check in (rule.get("parameters") or {}).get("required_status_checks", []):
            found[Required(check["context"], check.get("integration_id"))] = None
    return list(found)


def required_checks(repo: str, token: str, branch: str) -> list[Required]:
    """The checks that repo's rulesets require on branch, from the rules API."""
    return required(branch_rules(repo, token, branch))


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


def _when(timestamp: str | None) -> float | None:
    return datetime.fromisoformat(timestamp).timestamp() if timestamp else None


def _age(found: list[Check]) -> float:
    """How long ago a check in found last started or finished, or 0 if none says."""
    times = [c.since for c in found if c.since is not None]
    return max(0.0, time() - max(times)) if times else 0.0


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
        found.append(
            Check(
                run["name"],
                app,
                state,
                detail,
                run.get("html_url") or "",
                run["id"],
                _when(run.get("completed_at") or run.get("started_at")),
            )
        )
    # The combined status holds only the latest status of each context.
    for status in _pages(f"{commit}/status", token, "statuses"):
        state = {"success": "passed", "pending": "pending"}.get(status["state"], "failed")
        found.append(
            Check(
                status["context"],
                None,
                state,
                status["state"],
                status.get("target_url") or "",
                status["id"],
                _when(status.get("created_at")),
            )
        )
    return found


def missing(required: list[Required], found: list[Check]) -> list[Required]:
    """The checks in required that nothing in found is. A required check that
    names an app is only a check run from that app, as the API doesn't say
    which app posted a commit status."""
    return [r for r in required if not any(c.name == r.context and r.app in (None, c.app) for c in found)]


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


class HeadChanged(RuntimeError):
    """The commit that settle() waited on is no longer the head it reads."""


@dataclass(frozen=True)
class Outcome:
    """What settle() found on commit sha: its checks, the ones that failed,
    and, if it gave up at its deadline, what it was still waiting for."""

    sha: str
    checks: list[Check]
    failed: list[Check]
    waiting: list[str]

    @property
    def timed_out(self) -> bool:
        return bool(self.waiting)


def settle(
    repo: str,
    token: str,
    sha: str,
    head: Callable[[], str],
    *,
    required: list[Required],
    expected: set[str],
    interval: float,
    quiet: float,
    deadline: float,
    stop_on_failure: bool = True,
    replaced: frozenset[int] = frozenset(),
    allow_none: bool = False,
) -> Outcome:
    """Wait for the checks on commit sha, the commit that head() returns, to
    finish, as GitHub can't say which checks a commit will get. They have finished once:

    - at least one check is there, unless allow_none;
    - every check in required is there;
    - a check of every name in expected is there, such as the names on an
      earlier commit that ran the same workflows;
    - none is pending, nor has an ID in replaced (a failed check that a
      re-run will replace);
    - no check has appeared, finished or been replaced for quiet seconds,
      counting from the last of those that GitHub gives a time for.

    With stop_on_failure, it returns as soon as a check fails, without
    waiting for the rest. It reads head() again after each read of the
    checks, and raises HeadChanged if it returns another commit. At deadline
    (from monotonic()), it returns what it was still waiting for."""
    seen: frozenset[tuple[str, int, str]] | None = None
    changed = monotonic()
    while True:
        found = checks(repo, token, sha)
        if (now := head()) != sha:
            raise HeadChanged(f"the head moved from {sha} to {now}")
        failed = [c for c in found if c.state == "failed" and c.id not in replaced]
        if failed and stop_on_failure:
            return Outcome(sha, found, failed, [])
        names = {c.name for c in found}
        waiting = [r.context for r in missing(required, found)]
        waiting += sorted(expected - names - set(waiting))
        waiting += [c.name for c in found if c.state == "pending" or c.id in replaced]
        # Whole IDs, so that a check run replaced by another of the same name counts as a change, and states, so
        # that a check finishing does too, as it can start others.
        if (now_seen := frozenset((c.name, c.id, c.state) for c in found)) != seen:
            # The time since the last change GitHub knows of counts, such as on a commit that's been there for hours.
            seen, changed = now_seen, monotonic() - _age(found)
        quiet_left = changed + quiet - monotonic()
        if (found or allow_none) and not waiting and quiet_left <= 0:
            return Outcome(sha, found, failed, [])
        left = deadline - monotonic()
        if left <= 0:
            if not found and not allow_none:
                waiting = ["any check to start"]
            elif not waiting:
                waiting = [f"{quiet:g} seconds with no new check"]
            return Outcome(sha, found, failed, waiting)
        # Once only the quiet period is left, wait just until it ends.
        sleep(min(interval, left) if waiting or not (found or allow_none) else min(interval, left, quiet_left))
