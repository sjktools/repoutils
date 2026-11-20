"""Run the PR Fixes workflow on a pull request, wait for its fixes to be
pushed, and say whether they were.

It does what a ``/autofix`` comment does, for a caller that can start a
workflow but can't post that comment as it is, such as Claude Code on the web,
whose comments get a footer. It starts the PR Fixes workflow (``fix.yml``) on
the PR's branch, with the PR's head commit and number, and waits for it. Then
it waits for the PR Fixes Push workflow (``fix-push.yml``) that pushes the
fixes, found by its run name, which names the fix run.

GitHub starts that push run itself, through its ``workflow_run`` trigger,
unless the fix run was started with a GITHUB_TOKEN. Then, or if no push run
appears within two minutes, this starts it on the default branch, as
autofix-comment.yml does. The push run, not this command, pushes the fixes,
and needs a token this command never sees.
Stdlib-only, like every repoutils module."""

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.parse
from time import monotonic, sleep
from typing import Any

from repoutils import github, workflow_summaries
from repoutils.docstrings import summary

FIX_WORKFLOW = "fix.yml"
PUSH_WORKFLOW = "fix-push.yml"
# How long to wait for GitHub to start the push run before starting it here.
PUSH_GRACE = 120.0
PER_PAGE = 100
# GitHub starts no workflow_run workflow for a run this account started.
GITHUB_ACTIONS_BOT = "github-actions[bot]"


def push_run_name(fix_run_id: int) -> str:
    """The name fix-push.yml's run-name gives its run for fix run fix_run_id."""
    return f"PR Fixes Push for run {fix_run_id}"


def check_pr(repo: str, pr: int, token: str) -> tuple[str, str]:
    """The PR's branch and head commit. Raises ValueError if PR Fixes can't
    push to it: it's closed, from a fork, or from the default branch."""
    data = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr}", token)
    head = data["head"]
    branch = str(head["ref"])
    if data["state"] != "open":
        raise ValueError(f"pull request #{pr} is not open")
    # By ID, as --repo may differ from GitHub's name in case.
    if (head.get("repo") or {}).get("id") != data["base"]["repo"]["id"]:
        raise ValueError(f"pull request #{pr} is from a fork, which PR Fixes can't push to")
    if branch == data["base"]["repo"]["default_branch"]:
        raise ValueError(f"pull request #{pr}'s branch is {branch}, which PR Fixes doesn't push to")
    return branch, str(head["sha"])


def find_push_run(
    repo: str, push_workflow: str, fix_run_id: int, token: str, created: str | None = None
) -> dict[str, Any] | None:
    """The newest run of push_workflow for fix run fix_run_id, if there is one
    yet, looking only at runs created at or after created (a timestamp) if given."""
    url = f"{github.API_URL}/repos/{repo}/actions/workflows/{push_workflow}/runs?per_page={PER_PAGE}"
    if created:
        # A push run starts after its fix run, so older runs needn't be read.
        url += f"&created=%3E%3D{urllib.parse.quote(created)}"
    name = push_run_name(fix_run_id)
    page = 1
    while True:
        batch = github.request("GET", f"{url}&page={page}", token)["workflow_runs"]
        for run in batch:
            if run.get("display_title") == name:
                found: dict[str, Any] = run
                return found
        if len(batch) < PER_PAGE:
            return None
        page += 1


def start_push(
    repo: str,
    push_workflow: str,
    fix_run: dict[str, Any],
    token: str,
    *,
    interval: float,
    deadline: float,
) -> int:
    """The ID of the push run for fix_run: the one GitHub started, or else one this starts."""
    fix_run_id = int(fix_run["id"])
    if (fix_run.get("triggering_actor") or {}).get("login") != GITHUB_ACTIONS_BOT:
        give_up = min(monotonic() + PUSH_GRACE, deadline)
        while True:
            run = find_push_run(repo, push_workflow, fix_run_id, token, fix_run.get("created_at"))
            if run is not None:
                return int(run["id"])
            left = give_up - monotonic()
            if left <= 0:
                break
            sleep(min(interval, left))
        print(f"GitHub didn't start {push_workflow} for run {fix_run_id}, so starting it here", file=sys.stderr)
    # Rather than start a push nobody waits for, which could push after the command gave up.
    if monotonic() >= deadline:
        raise TimeoutError(f"Gave up before starting {push_workflow} for run {fix_run_id}")
    default_branch = workflow_summaries.default_branch(repo, token)
    return workflow_summaries.dispatch(repo, push_workflow, default_branch, token, {"run-id": str(fix_run_id)})


def autofix(
    repo: str,
    pr: int,
    token: str,
    *,
    workflow: str = FIX_WORKFLOW,
    push_workflow: str = PUSH_WORKFLOW,
    interval: float,
    timeout: float,
) -> int:
    """Run PR Fixes on pr and wait for its fixes to be pushed. Exits 0 if
    they were, or there was nothing to fix."""
    deadline = monotonic() + timeout
    branch, head_sha = check_pr(repo, pr, token)
    # The head commit makes the run fail if the branch moves on before it
    # starts, and the number goes into its name, where fix-push.yml reads it.
    inputs = {"head-sha": head_sha, "pr-number": str(pr)}
    fix_run_id = workflow_summaries.dispatch(repo, workflow, branch, token, inputs)
    print(f"Started {workflow} on {branch}: https://github.com/{repo}/actions/runs/{fix_run_id}", file=sys.stderr)
    fix_run = workflow_summaries.wait(
        repo, fix_run_id, token, interval=interval, timeout=deadline - monotonic(), just_started=True
    )
    if fix_run.get("conclusion") != "success":
        print(
            f"{workflow} run {fix_run_id} finished with conclusion {fix_run.get('conclusion')}, "
            f"so nothing was pushed: {fix_run.get('html_url')}",
            file=sys.stderr,
        )
        return 1
    push_run_id = start_push(repo, push_workflow, fix_run, token, interval=interval, deadline=deadline)
    print(f"Waiting for {push_workflow}: https://github.com/{repo}/actions/runs/{push_run_id}", file=sys.stderr)
    push_run = workflow_summaries.wait(
        repo, push_run_id, token, interval=interval, timeout=max(deadline - monotonic(), 0.0), just_started=True
    )
    if push_run.get("conclusion") != "success":
        print(
            f"{push_workflow} run {push_run_id} finished with conclusion {push_run.get('conclusion')}, "
            f"so the fixes may not have been pushed: {push_run.get('html_url')}",
            file=sys.stderr,
        )
        return 1
    new_head = github.pr_head(repo, pr, token)
    if new_head == head_sha:
        print(f"PR Fixes found nothing to fix on {branch} at {head_sha}", file=sys.stderr)
    else:
        print(f"PR Fixes pushed its fixes to {branch}, which is now at {new_head}", file=sys.stderr)
        # The new head on its own, for a script to read.
        print(new_head)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils autofix",
        description=summary(__doc__),
        epilog="Prints the branch's new head commit on standard output if fixes were pushed. "
        "Fails if either run fails. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions write permission, "
        "and Pull requests read. "
        "The workflows must be repoutils' own, or work as they do: "
        "the push workflow's run-name must be 'PR Fixes Push for run <fix run ID>'.",
    )
    parser.add_argument("pr", type=int, help="the number of the pull request to fix")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--workflow", default=FIX_WORKFLOW, help=f"the fix workflow's file name (default: {FIX_WORKFLOW})"
    )
    parser.add_argument(
        "--push-workflow", default=PUSH_WORKFLOW, help=f"the push workflow's file name (default: {PUSH_WORKFLOW})"
    )
    parser.add_argument(
        "--timeout",
        type=workflow_summaries.positive,
        default=45,
        metavar="MINUTES",
        help="give up if the fixes haven't been pushed after this long (default: 45)",
    )
    parser.add_argument(
        "--interval",
        type=workflow_summaries.positive,
        default=10,
        metavar="SECONDS",
        help="how often to check on the runs (default: 10)",
    )
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        return autofix(
            repo,
            args.pr,
            token,
            workflow=args.workflow,
            push_workflow=args.push_workflow,
            interval=args.interval,
            timeout=args.timeout * 60,
        )
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main
