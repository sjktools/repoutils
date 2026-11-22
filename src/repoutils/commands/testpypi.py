"""Start a TestPyPI dev release of a pull request's branch.

It does what a ``/testpypi`` comment does, for a caller that can start a
workflow but can't post that comment as it is, such as Claude Code on the web,
whose comments get a footer. It starts the Publish workflow
(``publish-pypi.yml``) on the PR's branch, as Actions > Publish > Run workflow
would. A run started that way publishes a dev version of the branch to
TestPyPI, checks it installs from there, and never reaches PyPI. This doesn't
wait for the run.

The ``testpypi-dispatch`` action runs it for a ``/testpypi`` comment, with
``--comment-id`` to react to the comment and reply with the run's link.
Stdlib-only, like every repoutils module."""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from repoutils import github, workflow_summaries
from repoutils.docstrings import summary

WORKFLOW = "publish-pypi.yml"


def check_pr(repo: str, pr: int, token: str) -> str:
    """The PR's branch. Raises ValueError if the Publish workflow can't run on
    it: the PR is closed, or from a fork, whose branch isn't in this repository."""
    data: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{pr}", token)
    if data["state"] != "open":
        raise ValueError(f"Pull request #{pr} is {data['state']}, so it gets no TestPyPI release.")
    # By ID, as the repository's name may differ from GitHub's in case.
    if (data["head"].get("repo") or {}).get("id") != data["base"]["repo"]["id"]:
        raise ValueError(f"Pull request #{pr} is from a fork, whose branch isn't in {repo}.")
    branch: str = data["head"]["ref"]
    return branch


def reply(repo: str, pr: int, body: str, token: str) -> None:
    """Comment body on pr. The run has started by then, so a failure only warns."""
    try:
        github.request("POST", f"{github.API_URL}/repos/{repo}/issues/{pr}/comments", token, json_body={"body": body})
    # RuntimeError is github.request()'s error for too many redirects.
    except (OSError, RuntimeError) as e:
        github.close_error(e)
        print(f"{github.annotation('warning')}Couldn't reply on pull request #{pr}: {e}", file=sys.stderr)


def testpypi(
    repo: str,
    pr: int,
    token: str,
    *,
    workflow: str = WORKFLOW,
    comment_id: int | None = None,
    step_summary: Path | None = None,
) -> int:
    """Start workflow on pr's branch and print the run's page. Appends that to
    step_summary if given, and reacts to comment_id and replies on pr if given."""
    branch = check_pr(repo, pr, token)
    run_id = workflow_summaries.dispatch(repo, workflow, branch, token)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    started = f"Started a TestPyPI dev release of `{branch}`"
    print(f"Started {workflow} on {branch}: {run_url}", file=sys.stderr)
    # The run's page on its own, for a script to read.
    print(run_url)
    if comment_id is not None:
        github.react(repo, comment_id, token)
        reply(repo, pr, f"{started}: {run_url}", token)
    if step_summary is not None:
        with step_summary.open("a") as f:
            f.write(f"{started} (PR #{pr}).\n\nPublish run: {run_url}\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoutils testpypi",
        description=summary(__doc__),
        epilog="Prints the run's page on standard output. "
        "Fails if the pull request is closed or from a fork, or if the run can't be started. "
        "In GitHub Actions, its errors and warnings are annotations. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions write permission, "
        "and Pull requests read, or write with --comment-id.",
    )
    parser.add_argument("pr", type=int, help="the number of the pull request whose branch to release")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument("--workflow", default=WORKFLOW, help=f"the Publish workflow's file name (default: {WORKFLOW})")
    parser.add_argument(
        "--comment-id",
        type=int,
        help="the ID of the /testpypi comment to react to with a rocket, and to answer with the run's link "
        "on the pull request, once the run starts",
    )
    parser.add_argument(
        "--step-summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        metavar="FILE",
        help="append a Markdown note linking to the run to this file (default: $GITHUB_STEP_SUMMARY)",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
    repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    return testpypi(
        repo, args.pr, token, workflow=args.workflow, comment_id=args.comment_id, step_summary=args.step_summary
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    return github.guarded(github.annotation("error"), lambda: _run(args))


run = main
