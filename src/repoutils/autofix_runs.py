"""`autofix resolve` and `autofix check-branch`: the checks that the PR Fixes
workflow (``fix.yml``) and the PR Fixes Push workflow (``fix-push.yml``) make
of a fix run and its pull request, kept apart from the command for length.

Both print their results as ``name=value`` lines, for a step to append to
``$GITHUB_OUTPUT``. Standard library only, like every repoutils module."""

import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from repoutils import github, workflow_summaries

FIX_WORKFLOW = "fix.yml"
PUSH_WORKFLOW = "fix-push.yml"
# fix.yml's run-name: "PR Fixes #<number>", or "PR Fixes" without one.
FIX_RUN_NAME = "PR Fixes"
_FIX_RUN_NAME_WITH_NUMBER = re.compile(r"PR Fixes #([0-9]+)")
# fix.yml's artifact, which holds the bundle.
FIXES_ARTIFACT = "fixes"
# fix-push.yml's job that pushes the bundle.
PUSH_JOB = "push"
PER_PAGE = 100

Output = Callable[[str, str], None]


def print_output(name: str, value: str) -> None:
    """Print name=value, for $GITHUB_OUTPUT, where a newline would start another output."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name}'s value has a line break in it")
    print(f"{name}={value}", flush=True)


def push_run_name(fix_run_id: int) -> str:
    """The name fix-push.yml's run-name gives its run for fix run fix_run_id."""
    return f"PR Fixes Push for run {fix_run_id}"


def push_runs(
    repo: str, push_workflow: str, fix_run_id: int, token: str, created: str | None = None
) -> Iterator[dict[str, Any]]:
    """The runs of push_workflow for fix run fix_run_id, newest first, looking
    only at runs created at or after created (a timestamp) if given. There's
    no status filter, as GitHub caps a filtered listing at 1,000 runs."""
    url = f"{github.API_URL}/repos/{repo}/actions/workflows/{push_workflow}/runs?per_page={PER_PAGE}"
    if created:
        # A push run starts after its fix run, so older runs needn't be read.
        url += f"&created=%3E%3D{urllib.parse.quote(created)}"
    name = push_run_name(fix_run_id)
    page = 1
    while True:
        batch = github.request("GET", f"{url}&page={page}", token)["workflow_runs"]
        yield from (run for run in batch if run.get("display_title") == name)
        if len(batch) < PER_PAGE:
            return
        page += 1


def check_pr_data(data: dict[str, Any], pr: int, branch: str | None = None) -> None:
    """Raise ValueError if PR Fixes can't push to the PR whose API data this
    is: it's closed, from a fork, from the default branch, or (if branch is
    given) from another branch."""
    head = data["head"]
    if data["state"] != "open":
        raise ValueError(f"Pull request #{pr} is not open.")
    # By ID, as the repository's name may differ from GitHub's in case.
    if (head.get("repo") or {}).get("id") != data["base"]["repo"]["id"]:
        raise ValueError(f"Pull request #{pr} is from a fork, which PR Fixes can't push to.")
    if branch is not None and head["ref"] != branch:
        raise ValueError(f"Pull request #{pr} is from branch {head['ref']}, not {branch}.")
    if head["ref"] == data["base"]["repo"]["default_branch"]:
        raise ValueError(f"Pull request #{pr}'s branch is {head['ref']}, which PR Fixes doesn't push to.")


def find_pr(repo: str, branch: str, pr: int | None, token: str, *, which: str) -> dict[str, Any]:
    """The API data of pull request pr from branch, or with no pr, of the
    branch's only open pull request from this repository. A fork's pull
    request can have a head branch of the same name, so only those from this
    repository count, and every page is read, so none goes uncounted. Raises
    ValueError otherwise, ending a message about several with which, which
    says what to do."""
    if pr is not None:
        data: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr}", token)
        check_pr_data(data, pr, branch)
        return data
    head = urllib.parse.quote(f"{repo.split('/')[0]}:{branch}", safe="")
    url = f"{github.API_URL}/repos/{repo}/pulls?state=open&head={head}&per_page={PER_PAGE}"
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github.request("GET", f"{url}&page={page}", token)
        found += [p for p in batch if (p["head"].get("repo") or {}).get("id") == p["base"]["repo"]["id"]]
        if len(batch) < PER_PAGE:
            break
        page += 1
    if not found:
        raise ValueError(f"Branch {branch} has no open pull request in this repository.")
    if len(found) > 1:
        raise ValueError(f"Branch {branch} has {len(found)} open pull requests in this repository; {which}.")
    return found[0]


def _check_branch_name(branch: str) -> None:
    if (
        branch.startswith("-")
        or subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True, check=False).returncode
    ):
        raise ValueError(f"{branch} isn't a valid branch name.")


def _already_pushed(
    repo: str, push_workflow: str, fix_run: dict[str, Any], this_run: str | None, token: str
) -> int | None:
    """Another push run for fix_run whose push job succeeded, if there is one."""
    for run in push_runs(repo, push_workflow, int(fix_run["id"]), token, fix_run.get("created_at")):
        if str(run["id"]) == this_run:
            continue
        jobs = github.request(
            "GET", f"{github.API_URL}/repos/{repo}/actions/runs/{run['id']}/jobs?per_page={PER_PAGE}", token
        )["jobs"]
        if any(job.get("name") == PUSH_JOB and job.get("conclusion") == "success" for job in jobs):
            return int(run["id"])
    return None


def resolve(
    repo: str,
    run_id: int,
    token: str,
    *,
    ref: str | None,
    this_run: str | None,
    workflow: str = FIX_WORKFLOW,
    push_workflow: str = PUSH_WORKFLOW,
    output: Output = print_output,
) -> None:
    """Check fix run run_id and find its pull request, for fix-push.yml,
    giving each output as soon as it's known, so a later failure keeps them.

    Everything about the run comes from the API, which GitHub sets, rather
    than from the event that started the push run. ref is the push run's own
    ref, which must be the default branch, and this_run its ID, which isn't
    another push run. Raises ValueError if a check fails."""
    api = f"{github.API_URL}/repos/{repo}"
    default_branch = workflow_summaries.default_branch(repo, token)
    # A branch's copy of fix-push.yml can't deploy to autofix, so this only
    # fails early with a clearer message.
    if ref is not None and ref != f"refs/heads/{default_branch}":
        raise ValueError(f"Run this workflow on {default_branch}, not on {ref}.")
    run = github.request("GET", f"{api}/actions/runs/{run_id}", token)
    # A branch could add another workflow named PR Fixes; only fix.yml's runs
    # count, though nothing here trusts them more for it.
    if run["workflow_id"] != github.request("GET", f"{api}/actions/workflows/{workflow}", token)["id"]:
        raise ValueError(f"Run {run_id} isn't a run of {workflow}.")
    if (run.get("head_repository") or {}).get("id") != run["repository"]["id"]:
        raise ValueError(f"Run {run_id} ran on {(run.get('head_repository') or {}).get('full_name')}, not {repo}.")
    if run["event"] != "workflow_dispatch":
        raise ValueError(f"Run {run_id} was started by {run['event']}, not workflow_dispatch.")
    if run["status"] != "completed":
        raise ValueError(f"Run {run_id} hasn't finished; it's {run['status']}.")
    branch = str(run["head_branch"])
    _check_branch_name(branch)
    if branch == default_branch:
        raise ValueError(f"PR Fixes doesn't push to {default_branch}.")
    title = str(run["display_title"])
    if match := _FIX_RUN_NAME_WITH_NUMBER.fullmatch(title):
        pr_number: int | None = int(match.group(1))
    elif title == FIX_RUN_NAME:
        pr_number = None
    else:
        raise ValueError(f"Run {run_id}'s name, {title}, doesn't give a pull request.")
    start_sha = str(run["head_sha"])
    conclusion = str(run["conclusion"] or "")
    output("run_id", str(run_id))
    output("pr_number", "" if pr_number is None else str(pr_number))
    output("branch", branch)
    output("start_sha", start_sha)
    output("conclusion", conclusion)
    output("url", str(run["html_url"]))

    # Whatever the fix run's conclusion, so the report job knows which pull
    # request to report to.
    pr = find_pr(repo, branch, pr_number, token, which=f"run {run_id}'s name doesn't say which one it was for")
    head_sha = str(pr["head"]["sha"])
    output("number", str(pr["number"]))
    output("head_sha", head_sha)
    output("base", str(pr["base"]["ref"]))
    if conclusion != "success":
        return

    if head_sha != start_sha:
        # Another push run for this fix run may have pushed its fixes already.
        # That's not a failure, and the other run reports it.
        other = _already_pushed(repo, push_workflow, run, this_run, token)
        if other is None:
            raise ValueError(
                f"Branch {branch} is at {head_sha}, not {start_sha}, where PR Fixes started; "
                "it has moved on, so run PR Fixes again."
            )
        print(f"Run {other} already pushed PR Fixes run {run_id}'s fixes to {branch}.", file=sys.stderr)
        output("fixes", "false")
        output("already_pushed", "true")
        return
    artifacts = workflow_summaries.artifacts(repo, run_id, token, FIXES_ARTIFACT)
    fixes = any(artifact["name"] == FIXES_ARTIFACT for artifact in artifacts)
    if not fixes:
        print(f"PR Fixes found nothing to push to {branch}.", file=sys.stderr)
    output("fixes", str(fixes).lower())
    output("already_pushed", "false")


def check_branch(
    repo: str,
    token: str,
    *,
    ref: str,
    sha: str,
    head_sha: str | None,
    pr_number: int | None,
    output: Output = print_output,
) -> None:
    """Check fix.yml's own run, at ref and commit sha, for fix.yml: it must be
    on a branch other than the default, at head_sha if given, with an open
    pull request from this repository (pr_number, if given). Gives the pull
    request's base branch as base_branch. Raises ValueError if a check fails.

    fix-push.yml checks the run again, and trusts nothing it does; this only
    fails early with a clearer message."""
    branch = ref.removeprefix("refs/heads/")
    if branch == ref or branch == workflow_summaries.default_branch(repo, token):
        raise ValueError(f"Run this workflow on a pull request's branch (Use workflow from), not on {ref}.")
    if head_sha and head_sha != sha:
        raise ValueError(
            f"Branch {branch} is at {sha}, not {head_sha} as expected; it has moved on, so run PR Fixes again."
        )
    pr = find_pr(repo, branch, pr_number, token, which="run PR Fixes again with the number of the one to fix")
    output("base_branch", str(pr["base"]["ref"]))
