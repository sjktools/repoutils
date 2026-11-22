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

``autofix resolve`` and ``autofix check-branch`` are the checks the two
workflows make of the fix run and its PR (see ``repoutils.autofix_runs``).
Stdlib-only, like every repoutils module."""

import argparse
import os
import subprocess
import sys
import urllib.error
from time import monotonic, sleep
from typing import Any

from repoutils import autofix_runs, github, workflow_summaries
from repoutils.docstrings import summary

FIX_WORKFLOW = autofix_runs.FIX_WORKFLOW
PUSH_WORKFLOW = autofix_runs.PUSH_WORKFLOW
push_run_name = autofix_runs.push_run_name
# How long to wait for GitHub to start the push run before starting it here.
PUSH_GRACE = 120.0
# GitHub starts no workflow_run workflow for a run this account started.
GITHUB_ACTIONS_BOT = "github-actions[bot]"
SUBCOMMANDS = ("resolve", "check-branch")


def check_pr(repo: str, pr: int, token: str) -> tuple[str, str]:
    """The PR's branch and head commit. Raises ValueError if PR Fixes can't
    push to it: it's closed, from a fork, or from the default branch."""
    data = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr}", token)
    autofix_runs.check_pr_data(data, pr)
    return str(data["head"]["ref"]), str(data["head"]["sha"])


def find_push_run(
    repo: str, push_workflow: str, fix_run_id: int, token: str, created: str | None = None
) -> dict[str, Any] | None:
    """The newest run of push_workflow for fix run fix_run_id, if there is one
    yet, looking only at runs created at or after created (a timestamp) if given."""
    return next(autofix_runs.push_runs(repo, push_workflow, fix_run_id, token, created), None)


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


def _repo_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )


def _workflow_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workflow", default=FIX_WORKFLOW, help=f"the fix workflow's file name (default: {FIX_WORKFLOW})"
    )
    parser.add_argument(
        "--push-workflow", default=PUSH_WORKFLOW, help=f"the push workflow's file name (default: {PUSH_WORKFLOW})"
    )


def _number(text: str) -> int:
    """A run or pull request number given as decimal digits only, as a run's name gives one."""
    if not text.isascii() or not text.isdigit():
        raise argparse.ArgumentTypeError(f"{text!r} isn't a number")
    return int(text)


def _optional_number(text: str) -> int | None:
    """A number, or None for an empty string, as a workflow passes an input it wasn't given."""
    return _number(text) if text else None


def _resolve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoutils autofix resolve",
        description="Check a finished PR Fixes run and find its pull request, for the PR Fixes Push workflow.",
        epilog="Prints run_id, pr_number, branch, start_sha, conclusion, url, number, head_sha and base, "
        "then, if the run succeeded, fixes and already_pushed, as name=value lines for $GITHUB_OUTPUT. "
        "Each is printed once known, so a failed check keeps the lines before it. "
        "Fails if the run isn't a finished workflow_dispatch run of the fix workflow on a branch of this "
        "repository other than the default, if its pull request can't be found, "
        "or if the run succeeded but the branch has moved on and no other push run pushed its fixes. "
        "Run it on the default branch: it fails if $GITHUB_REF is another ref. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs Actions read and Pull requests read.",
    )
    parser.add_argument("--run-id", type=_number, required=True, help="the ID of the PR Fixes run")
    _repo_option(parser)
    _workflow_options(parser)
    return parser


def _check_branch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoutils autofix check-branch",
        description="Check a PR Fixes run is on a pull request's branch, and find that pull request.",
        epilog="Prints base_branch, the pull request's base, as a name=value line for $GITHUB_OUTPUT. "
        "Fails if $GITHUB_REF isn't a branch other than the default, "
        "if $GITHUB_SHA isn't --head-sha (when given), "
        "or if the branch has no open pull request from this repository: "
        "--pr-number, or without it, exactly one. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs Pull requests read.",
    )
    parser.add_argument("--head-sha", default="", help="the commit the branch must be at (optional)")
    parser.add_argument(
        "--pr-number",
        type=_optional_number,
        help="the pull request (optional): needed only when the branch has more than one open pull request",
    )
    _repo_option(parser)
    return parser


def _autofix_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoutils autofix",
        usage="%(prog)s [-h] [options] pr\n       %(prog)s {resolve,check-branch} ...",
        description=summary(__doc__),
        epilog="Prints the branch's new head commit on standard output if fixes were pushed. "
        "Fails if either run fails. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions write permission, "
        "and Pull requests read. "
        "The workflows must be repoutils' own, or work as they do: "
        "the push workflow's run-name must be 'PR Fixes Push for run <fix run ID>'. "
        "`repoutils autofix resolve` and `repoutils autofix check-branch` are the checks the two workflows make "
        "(see their --help).",
    )
    parser.add_argument("pr", type=int, help="the number of the pull request to fix")
    _repo_option(parser)
    _workflow_options(parser)
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
    return parser


def _actions_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"${name} isn't set: run this in a GitHub Actions job.")
    return value


def _run_command(command: str, args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
    repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if command == "resolve":
        autofix_runs.resolve(
            repo,
            args.run_id,
            token,
            ref=os.environ.get("GITHUB_REF"),
            this_run=os.environ.get("GITHUB_RUN_ID"),
            workflow=args.workflow,
            push_workflow=args.push_workflow,
        )
        return 0
    if command == "check-branch":
        autofix_runs.check_branch(
            repo,
            token,
            ref=_actions_env("GITHUB_REF"),
            sha=_actions_env("GITHUB_SHA"),
            head_sha=args.head_sha,
            pr_number=args.pr_number,
        )
        return 0
    return autofix(
        repo,
        args.pr,
        token,
        workflow=args.workflow,
        push_workflow=args.push_workflow,
        interval=args.interval,
        timeout=args.timeout * 60,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv and argv[0] in SUBCOMMANDS else ""
    if command == "resolve":
        args = _resolve_parser().parse_args(argv[1:])
    elif command == "check-branch":
        args = _check_branch_parser().parse_args(argv[1:])
    else:
        args = _autofix_parser().parse_args(argv)
    # The subcommands run in a workflow, where an ::error:: line is shown as an annotation.
    error = "::error::" if command else ""
    try:
        return _run_command(command, args)
    except subprocess.CalledProcessError as e:
        print(f"{error}{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"{error}HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(f"{error}{e}", file=sys.stderr)
        return 1


run = main
