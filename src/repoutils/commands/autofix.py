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
appears within two minutes, this starts it on the default branch. It hands
every fix run to a push run, even a failed one, as the push run's report job
tells the PR how the fix run went. The push run, not this command, pushes the
fixes, and needs a token this command never sees.

The ``autofix-start`` action runs it for a ``/autofix`` comment, with
``--comment-id`` to react to the comment and ``--report`` to say on the PR
what went wrong when the push run couldn't.

``autofix resolve`` and ``autofix check-branch`` are the checks the two
workflows make of the fix run and its PR (see ``repoutils.autofix_runs``).
Stdlib-only, like every repoutils module."""

import argparse
import os
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
# fix-push.yml's job that comments on the PR.
REPORT_JOB = "report"


@dataclass
class Progress:
    """How far autofix() got, for the report a workflow posts when it fails."""

    fix_url: str | None = None
    push_url: str | None = None
    # Whether the push run's report job commented on the PR.
    reported: bool = False


def _annotation(kind: str) -> str:
    """The prefix that makes GitHub Actions show a line as an annotation, or
    nothing outside Actions."""
    return f"::{kind}::" if os.environ.get("GITHUB_ACTIONS") == "true" else ""


def _run_page(repo: str, run_id: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def check_pr(repo: str, pr: int, token: str) -> tuple[str, str]:
    """The PR's branch and head commit. Raises ValueError if PR Fixes can't
    push to it: it's closed, from a fork, or from the default branch."""
    data = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr}", token)
    autofix_runs.check_pr_data(data, pr)
    return str(data["head"]["ref"]), str(data["head"]["sha"])


def react(repo: str, comment_id: int, token: str) -> None:
    """React to the comment with a rocket. The fix run has started by then,
    so a failure only warns: a failed run would suggest commenting again."""
    try:
        github.request(
            "POST",
            f"{github.API_URL}/repos/{repo}/issues/comments/{comment_id}/reactions",
            token,
            json_body={"content": "rocket"},
        )
    except OSError as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        print(f"{_annotation('warning')}Couldn't react to comment {comment_id}: {e}", file=sys.stderr)


def report_succeeded(repo: str, run_id: int, token: str) -> bool:
    """Whether the push run's report job succeeded, and so commented on the PR."""
    url = f"{github.API_URL}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    jobs = github.request("GET", url, token)["jobs"]
    return any(job.get("name") == REPORT_JOB and job.get("conclusion") == "success" for job in jobs)


def report(progress: Progress, run_url: str) -> str | None:
    """The Markdown to post on the PR for a run that failed, or None if the
    push run's report job already said how it went."""
    if progress.reported:
        return None
    if progress.fix_url is None:
        return f"/autofix [couldn't start PR Fixes]({run_url}).\n"
    if progress.push_url is None:
        return f"/autofix started [PR Fixes]({progress.fix_url}), but [couldn't start pushing its fixes]({run_url}).\n"
    return (
        f"/autofix started [PR Fixes]({progress.fix_url}), "
        f"but [PR Fixes Push]({progress.push_url}) didn't report whether it pushed the fixes.\n"
    )


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
    comment_id: int | None = None,
    progress: Progress | None = None,
) -> int:
    """Run PR Fixes on pr and wait for its fixes to be pushed. Exits 0 if
    they were, or there was nothing to fix. Reacts to comment_id if given,
    and records how far it got in progress if given."""
    progress = progress or Progress()
    deadline = monotonic() + timeout
    branch, head_sha = check_pr(repo, pr, token)
    # The head commit makes the run fail if the branch moves on before it
    # starts, and the number goes into its name, where fix-push.yml reads it.
    inputs = {"head-sha": head_sha, "pr-number": str(pr)}
    fix_run_id = workflow_summaries.dispatch(repo, workflow, branch, token, inputs)
    progress.fix_url = _run_page(repo, fix_run_id)
    print(f"Started {workflow} on {branch}: {progress.fix_url}", file=sys.stderr)
    if comment_id is not None:
        react(repo, comment_id, token)
    fix_run = workflow_summaries.wait(
        repo, fix_run_id, token, interval=interval, timeout=deadline - monotonic(), just_started=True
    )
    fixed = fix_run.get("conclusion") == "success"
    if not fixed:
        print(
            f"{workflow} run {fix_run_id} finished with conclusion {fix_run.get('conclusion')}, "
            f"so it has nothing to push: {fix_run.get('html_url')}",
            file=sys.stderr,
        )
    # Even a failed fix run, as the push run reports it on the PR.
    push_run_id = start_push(repo, push_workflow, fix_run, token, interval=interval, deadline=deadline)
    progress.push_url = _run_page(repo, push_run_id)
    print(f"Waiting for {push_workflow}: {progress.push_url}", file=sys.stderr)
    try:
        push_run = workflow_summaries.wait(
            repo, push_run_id, token, interval=interval, timeout=max(deadline - monotonic(), 0.0), just_started=True
        )
    except OSError:
        # A failed poll or a timeout says nothing about the report job, which
        # may have commented already. If this fails too, the wait's error is
        # still the one to show.
        try:
            progress.reported = report_succeeded(repo, push_run_id, token)
        except OSError:
            pass
        raise
    pushed = push_run.get("conclusion") == "success"
    if not fixed or not pushed:
        progress.reported = report_succeeded(repo, push_run_id, token)
    if not fixed:
        return 1
    if not pushed:
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
        "In GitHub Actions, its errors and warnings are annotations. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions write permission, "
        "and Pull requests read, or write with --comment-id. "
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
    parser.add_argument(
        "--comment-id",
        type=_number,
        help="the ID of the /autofix comment to react to with a rocket once the fix run starts",
    )
    parser.add_argument(
        "--report",
        type=Path,
        metavar="FILE",
        help="if this fails and the push workflow didn't comment on the pull request, "
        "write the Markdown to post there to FILE, linking to this run (for a GitHub Actions job)",
    )
    return parser


def _actions_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"${name} isn't set: run this in a GitHub Actions job.")
    return value


def _run_command(command: str, args: argparse.Namespace, progress: Progress) -> int:
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
        comment_id=args.comment_id,
        progress=progress,
    )


def _write_report(path: Path, progress: Progress) -> None:
    run_url = (
        f"{_actions_env('GITHUB_SERVER_URL')}/{_actions_env('GITHUB_REPOSITORY')}"
        f"/actions/runs/{_actions_env('GITHUB_RUN_ID')}"
    )
    body = report(progress, run_url)
    if body is None:
        print("The push run reported on the pull request, so there's nothing more to report.", file=sys.stderr)
    else:
        path.write_text(body)


def _guarded(error: str, action: Callable[[], int | None]) -> int:
    """Run action, printing an exception it raises and returning 1 for it."""
    try:
        return action() or 0
    except subprocess.CalledProcessError as e:
        print(f"{error}{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"{error}HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"{error}{e}", file=sys.stderr)
    return 1


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
    error = "::error::" if command else _annotation("error")
    progress = Progress()
    code = _guarded(error, lambda: _run_command(command, args, progress))
    if code and not command and args.report:
        _guarded(error, lambda: _write_report(args.report, progress))
    return code


run = main
