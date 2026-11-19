"""Open a pull request with a change a command makes, for the commands whose
``bump --pr`` does that (``dependabot-uv`` and ``github-releases``).

``prepare`` checks, before the command changes anything, that it can commit
to a new branch. ``submit`` then commits the change there, pushes the branch
and opens the pull request into the branch the checkout was on, and switches
back to that branch. The push uses Git's own credentials, and the API
requests the token. Stdlib-only, like every repoutils module."""

import subprocess
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from repoutils import github

# The remote that `bump --pr` pushes its branch to.
REMOTE = "origin"


def git(root: Path, *args: str) -> str:
    """The output of a Git command run in root, raising CalledProcessError if it fails."""
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def open_pr(repo: str, branch: str, token: str) -> str | None:
    """The URL of the open pull request from branch, if there is one."""
    owner = repo.split("/", 1)[0]
    head = urllib.parse.quote(f"{owner}:{branch}", safe="")
    url = f"{github.API_URL}/repos/{repo}/pulls?state=open&head={head}"
    pulls: list[dict[str, Any]] = github.request("GET", url, token)
    return str(pulls[0]["html_url"]) if pulls else None


def prepare(root: Path, branch: str) -> str:
    """The branch the checkout is on, which the pull request from branch goes
    into, raising RuntimeError if the working tree has changes, HEAD isn't on
    a branch, or branch already exists here or on REMOTE."""
    if git(root, "status", "--porcelain"):
        raise RuntimeError("The working tree has changes, so commit or stash them before --pr")
    base = git(root, "branch", "--show-current")
    if not base:
        raise RuntimeError("HEAD isn't on a branch, so --pr has no base branch for the pull request")
    if git(root, "branch", "--list", branch) or git(root, "ls-remote", "--heads", REMOTE, f"refs/heads/{branch}"):
        raise RuntimeError(
            f"Branch {branch} already exists, here or on {REMOTE}, so delete it, or open its pull request by hand"
        )
    return base


def submit(
    root: Path,
    repo: str,
    token: str,
    *,
    branch: str,
    base: str,
    paths: list[str],
    title: str,
    message: str,
    body: str,
) -> str:
    """Commit paths (relative to root) to a new branch with title and message
    as the commit message, push it to REMOTE, switch back to base, and open a
    pull request from branch into base. Returns the pull request's URL."""
    git(root, "switch", "--create", branch)
    try:
        # Literal, so a path with a glob character in it stages only itself.
        git(root, "--literal-pathspecs", "add", "--all", "--", *paths)
        git(root, "commit", "--message", f"{title}\n\n{message}")
        git(root, "push", "--set-upstream", REMOTE, branch)
    finally:
        git(root, "switch", base)
    try:
        created = github.request(
            "POST",
            f"{github.API_URL}/repos/{repo}/pulls",
            token,
            json_body={"title": title, "head": branch, "base": base, "body": body},
        )
    except OSError as e:
        reason, detail = str(e), ""
        if isinstance(e, urllib.error.HTTPError):
            with e:
                reason, detail = f"HTTP {e.code}", f":\n{e.read().decode(errors='replace')}"
        # A rerun would stop at the pushed branch, so say how to finish by hand.
        raise RuntimeError(
            f"Pushed {branch}, but opening its pull request failed ({reason}){detail}\n"
            f"Open it at https://github.com/{repo}/compare/{base}...{branch}"
        ) from None
    return str(created["html_url"])
