"""Post a Markdown file to a PR as a comment, editing or hiding the one an
earlier run posted.

Each comment starts with a hidden marker, ``<!-- <comment-prefix>-<name> -->``,
where ``name`` is ``--name`` and ``comment-prefix`` comes from
``[tool.repoutils]`` (default ``repoutils``), so the next run can find it. By
default the next run edits that comment in place; with ``--hide-previous`` it
posts a new comment and hides the earlier ones as outdated instead, which suits
reports whose history is worth keeping, such as exolot's findings. With
``--update-only`` it edits the newest earlier comment but never posts a first
one, so a run can mark a problem an earlier run reported as resolved without
adding a comment where there was none. Without ``--hide-previous``, it deletes
any older ones. With it, it edits the newest one that isn't hidden, and hides
the older ones instead. So a report can post a new
comment for a problem, which notifies the PR's subscribers as an edit doesn't,
and edit that comment once the problem is gone, as ``/autofix``'s does.

CI runs this in a job with a write-capable token that runs nothing else, so it
uses only the standard library."""

import argparse
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

from repoutils import config
from repoutils import github as github_comments
from repoutils.docstrings import summary


def _author(args: argparse.Namespace, token: str) -> str:
    """The login the token comments as, to find the earlier comment by."""
    if args.author:
        return str(args.author)
    login: str = github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
    return login


def _post(args: argparse.Namespace, marker: str) -> str:
    """Posts the comment, returning what it did."""
    body = f"{marker}\n{args.body.read_text()}"
    token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
    repo = args.repo or github_comments.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if args.commit:
        # An older commit's run can finish after a newer one's, and mustn't
        # post over its report.
        head = github_comments.pr_head(repo, args.pr, token)
        if not head.startswith(args.commit):
            return f"Not posting: the report is for {args.commit}, but PR #{args.pr}'s head is now {head}."
    if args.hide_previous and args.update_only:
        # The newest one still showing, as someone may have hidden a newer one.
        shown = [c for c in github_comments.own_comments(repo, args.pr, marker, token) if not c["isMinimized"]]
        if not shown:
            return f"Not posting: PR #{args.pr} has no earlier comment to update."
        newest = shown[-1]
        github_comments.request(
            "PATCH",
            f"{github_comments.API_URL}/repos/{repo}/issues/comments/{newest['databaseId']}",
            token,
            json_body={"body": body},
        )
        hidden = github_comments.hide_comments(repo, args.pr, marker, token, keep=newest["id"])
        return f"Posted {args.body} to PR #{args.pr} in its latest comment, and hid {hidden} earlier comment(s)."
    if args.hide_previous:
        # Post first, so a failure leaves the old report showing rather than none.
        posted = github_comments.request(
            "POST", f"{github_comments.API_URL}/repos/{repo}/issues/{args.pr}/comments", token, json_body={"body": body}
        )
        hidden = github_comments.hide_comments(repo, args.pr, marker, token, keep=posted["node_id"])
        return f"Posted {args.body} to PR #{args.pr}, and hid {hidden} earlier comment(s)."
    author = _author(args, token)
    if not github_comments.upsert_comment(repo, args.pr, author, marker, body, token, create=not args.update_only):
        return f"Not posting: PR #{args.pr} has no earlier comment to update."
    return f"Posted {args.body} to PR #{args.pr}."


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils post-comment",
        description=summary(__doc__),
        epilog="Comments with $GITHUB_TOKEN, defaulting to `gh auth token`.",
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("body", type=Path, help="the Markdown file to post")
    parser.add_argument("--name", required=True, help="names the comment's marker, e.g. exolot-findings")
    parser.add_argument(
        "--hide-previous",
        action="store_true",
        help="post a new comment and hide earlier ones, rather than edit the earlier one",
    )
    parser.add_argument(
        "--update-only",
        action="store_true",
        help="edit the newest earlier comment, but post nothing if there's none "
        "(with --hide-previous, hide the older ones rather than delete them)",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--commit",
        help="commit the report is for; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    parser.add_argument(
        "--author",
        help="login $GITHUB_TOKEN comments as, e.g. github-actions[bot], to find the comment to edit "
        "(default: looked up, which only works for a user token; "
        "not used with --hide-previous, which asks GitHub which comments the token's user posted)",
    )
    args = parser.parse_args(argv)
    try:
        print(_post(args, config.marker(args.name)))
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
    return 0


run = main
