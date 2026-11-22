"""`autofix resolve` and `autofix check-branch`: the checks that the PR Fixes
workflow (``fix.yml``) and the PR Fixes Push workflow (``fix-push.yml``) make
of a fix run and its pull request, kept apart from the command for length.

The PR Fixes Push workflow also pushes the commit of a relock run: a run of
``autofix-comment.yml`` whose ``relock`` job relocked ``actions.lock``, after
GitHub refused to start PR Fixes because the lock didn't list every action
``fix.yml`` uses.

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
RELOCK_WORKFLOW = "autofix-comment.yml"
# fix.yml's run-name: "PR Fixes #<number>", or "PR Fixes" without one.
FIX_RUN_NAME = "PR Fixes"
_FIX_RUN_NAME_WITH_NUMBER = re.compile(r"PR Fixes #([0-9]+)")
# autofix-comment.yml's run-name: "Comment on #<number>".
_RELOCK_RUN_NAME = re.compile(r"Comment on #([0-9]+)")
# autofix-comment.yml's job that relocks actions.lock.
RELOCK_JOB = "relock"
# fix.yml's artifact, which holds the bundle.
FIXES_ARTIFACT = "fixes"
# The relock job's artifact is this and the commit it relocked: the run is on
# the default branch, so its own head commit isn't the one the bundle builds on.
RELOCK_ARTIFACT_PREFIX = "fixes-"
_COMMIT = re.compile(r"[0-9a-f]{40}")
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


def run_jobs(repo: str, run_id: int, token: str) -> list[dict[str, Any]]:
    """The jobs of the run's latest attempt."""
    url = f"{github.API_URL}/repos/{repo}/actions/runs/{run_id}/jobs?per_page={PER_PAGE}"
    jobs: list[dict[str, Any]] = github.request("GET", url, token)["jobs"]
    return jobs


def relock_job(repo: str, run_id: int, token: str) -> dict[str, Any]:
    """The relock job of run run_id. Raises ValueError if the run has none,
    or it hasn't finished. The run itself is still going, as the job after
    the relock job starts the push run."""
    job = next((job for job in run_jobs(repo, run_id, token) if job.get("name") == RELOCK_JOB), None)
    if job is None:
        raise ValueError(f"Run {run_id} has no {RELOCK_JOB} job.")
    if job.get("status") != "completed":
        raise ValueError(f"Run {run_id}'s {RELOCK_JOB} job hasn't finished; it's {job.get('status')}.")
    return job


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
        if any(
            job.get("name") == PUSH_JOB and job.get("conclusion") == "success"
            for job in run_jobs(repo, int(run["id"]), token)
        ):
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
    relock_workflow: str = RELOCK_WORKFLOW,
    output: Output = print_output,
) -> None:
    """Check fix run or relock run run_id and find its pull request, for
    fix-push.yml, giving each output as soon as it's known, so a later
    failure keeps them. kind says which it is: fix or relock.

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
    # count, though nothing here trusts them more for it. A relock run is of
    # the default branch's copy of relock_workflow, which a branch can't change.
    if run["workflow_id"] == github.request("GET", f"{api}/actions/workflows/{workflow}", token)["id"]:
        kind = "fix"
    elif run["workflow_id"] == github.request("GET", f"{api}/actions/workflows/{relock_workflow}", token)["id"]:
        kind = "relock"
    else:
        raise ValueError(f"Run {run_id} isn't a run of {workflow} or {relock_workflow}.")
    if (run.get("head_repository") or {}).get("id") != run["repository"]["id"]:
        raise ValueError(f"Run {run_id} ran on {(run.get('head_repository') or {}).get('full_name')}, not {repo}.")
    if kind == "relock":
        _resolve_relock(
            repo,
            run,
            token,
            default_branch=default_branch,
            this_run=this_run,
            push_workflow=push_workflow,
            output=output,
        )
    else:
        _resolve_fix(
            repo,
            run,
            token,
            default_branch=default_branch,
            this_run=this_run,
            push_workflow=push_workflow,
            output=output,
        )


def _resolve_fix(
    repo: str,
    run: dict[str, Any],
    token: str,
    *,
    default_branch: str,
    this_run: str | None,
    push_workflow: str,
    output: Output,
) -> None:
    """resolve() for a run of the fix workflow."""
    run_id = int(run["id"])
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
    output("kind", "fix")
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

    if _moved_on(
        repo,
        run,
        token,
        branch=branch,
        start_sha=start_sha,
        head_sha=head_sha,
        what="PR Fixes",
        this_run=this_run,
        push_workflow=push_workflow,
        output=output,
    ):
        return
    artifacts = workflow_summaries.artifacts(repo, run_id, token, FIXES_ARTIFACT)
    if any(artifact["name"] == FIXES_ARTIFACT for artifact in artifacts):
        _output_fixes(FIXES_ARTIFACT, output)
    else:
        print(f"PR Fixes found nothing to push to {branch}.", file=sys.stderr)
        _output_fixes("", output)


def _moved_on(
    repo: str,
    run: dict[str, Any],
    token: str,
    *,
    branch: str,
    start_sha: str,
    head_sha: str,
    what: str,
    this_run: str | None,
    push_workflow: str,
    output: Output,
) -> bool:
    """Whether another push run already pushed what run made, which
    started from start_sha, as the branch is now at head_sha. Outputs that
    if so. Raises ValueError if the branch has moved on and nothing pushed."""
    if head_sha == start_sha:
        return False
    # Another push run for this run may have pushed already. That's not a
    # failure, and the other run reports it.
    other = _already_pushed(repo, push_workflow, run, this_run, token)
    if other is None:
        raise ValueError(
            f"Branch {branch} is at {head_sha}, not {start_sha}, where {what} started; "
            "it has moved on, so run PR Fixes again."
        )
    print(f"Run {other} already pushed {what} run {run['id']}'s fixes to {branch}.", file=sys.stderr)
    output("fixes", "false")
    output("already_pushed", "true")
    output("artifact", "")
    return True


def _resolve_relock(
    repo: str,
    run: dict[str, Any],
    token: str,
    *,
    default_branch: str,
    this_run: str | None,
    push_workflow: str,
    output: Output,
) -> None:
    """resolve() for a relock run, whose relock job has finished, though the
    run hasn't. The run is on the default branch, so its pull request comes
    from its name, and the commit the relock started from from its
    artifact's name, which the default branch's relock job sets. Without
    an artifact, there's nothing to push, and start_sha is the pull
    request's head."""
    run_id = int(run["id"])
    if run["event"] != "issue_comment":
        raise ValueError(f"Run {run_id} was started by {run['event']}, not issue_comment.")
    if run["head_branch"] != default_branch:
        raise ValueError(f"Run {run_id} ran on {run['head_branch']}, not {default_branch}.")
    title = str(run["display_title"])
    match = _RELOCK_RUN_NAME.fullmatch(title)
    if match is None:
        raise ValueError(f"Run {run_id}'s name, {title}, doesn't give a pull request.")
    pr_number = int(match.group(1))
    job = relock_job(repo, run_id, token)
    conclusion = str(job.get("conclusion") or "")
    output("kind", "relock")
    output("run_id", str(run_id))
    output("pr_number", str(pr_number))

    pr = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr_number}", token)
    check_pr_data(pr, pr_number)
    branch = str(pr["head"]["ref"])
    _check_branch_name(branch)
    head_sha = str(pr["head"]["sha"])
    output("branch", branch)
    artifact = relock_artifact(repo, run_id, token) if conclusion == "success" else None
    start_sha = head_sha if artifact is None else artifact.removeprefix(RELOCK_ARTIFACT_PREFIX)
    output("start_sha", start_sha)
    output("conclusion", conclusion)
    output("url", str(job["html_url"]))
    output("number", str(pr_number))
    output("head_sha", head_sha)
    output("base", str(pr["base"]["ref"]))
    if conclusion != "success":
        return
    if artifact is None:
        print(f"Relocking actions.lock found nothing to push to {branch}.", file=sys.stderr)
        _output_fixes("", output)
        return
    if not _moved_on(
        repo,
        run,
        token,
        branch=branch,
        start_sha=start_sha,
        head_sha=head_sha,
        what="relock",
        this_run=this_run,
        push_workflow=push_workflow,
        output=output,
    ):
        _output_fixes(artifact, output)


def relock_artifact(repo: str, run_id: int, token: str) -> str | None:
    """The name of the relock job's artifact in run run_id, fixes-<commit>,
    or None if it uploaded none. Raises ValueError if there's more than one,
    or the name doesn't end in a full commit hash."""
    names = [str(a["name"]) for a in workflow_summaries.artifacts(repo, run_id, token, RELOCK_ARTIFACT_PREFIX)]
    if len(names) > 1:
        raise ValueError(f"Run {run_id} has {len(names)} artifacts named {RELOCK_ARTIFACT_PREFIX}*, not one.")
    if not names:
        return None
    if not _COMMIT.fullmatch(names[0].removeprefix(RELOCK_ARTIFACT_PREFIX)):
        raise ValueError(f"Run {run_id}'s artifact {names[0]} doesn't name a commit.")
    return names[0]


def _output_fixes(artifact: str, output: Output) -> None:
    """Output that there's a bundle to push in artifact, or, if artifact is empty, that there's none."""
    output("fixes", str(bool(artifact)).lower())
    output("already_pushed", "false")
    output("artifact", artifact)


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
