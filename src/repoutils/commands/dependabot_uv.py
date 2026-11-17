"""Report the uv version Dependabot updates this repository with, check that
pyproject.toml's [tool.uv] required-version minimum is that version, or bump
the project's uv to it.

Dependabot's own uv changes when GitHub updates Dependabot, at irregular
intervals, and a project has the fewest problems when it bumps its uv at the
same time. The version comes from the log of the newest successful uv run of
the repository's "Dependabot Updates" workflow, which prints the output of
``uv --version``. Only the newest run counts, as GitHub seems to roll a new
Dependabot out in phases, and an older or newer run could have used another.
Stdlib-only, like every repoutils module."""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import github, uv_bump
from repoutils.commands.check_pins import uv_minimum
from repoutils.docstrings import summary

# The path GitHub gives the dynamic workflow that runs Dependabot's updates.
WORKFLOW_PATH = "dynamic/dependabot/dependabot-updates"
# Runs are named e.g. "uv in /. - Update #1710872725".
RUN_PREFIX = "uv in "
JOB_NAME = "Dependabot"
PER_PAGE = 100
# `bump --pr` pushes its commit to BRANCH_PREFIX + the new version, on REMOTE.
BRANCH_PREFIX = "uv-"
REMOTE = "origin"
# The updater logs `pyenv exec uv --version`, e.g.
# "INFO <job_1710872725> Package manager uv, Info : uv 0.13.7 (x86_64-unknown-linux-gnu)".
# The log format isn't documented, so a log without this line is an error, never a pass.
_VERSION_RE = re.compile(r"Package manager uv, Info : uv (?P<version>\S+)")


@dataclass(frozen=True)
class Found:
    """The uv version Dependabot's newest successful uv run used, and that run."""

    version: str
    run_url: str
    date: str


def _pages(url: str, key: str, token: str) -> Iterator[Any]:
    """Each item of the list under key in a paginated listing, newest first
    for workflow runs, fetching each page only when it's reached."""
    page = 1
    while True:
        batch = github.request("GET", f"{url}{'&' if '?' in url else '?'}per_page={PER_PAGE}&page={page}", token)[key]
        yield from batch
        if len(batch) < PER_PAGE:
            return
        page += 1


def workflow_id(repo: str, token: str) -> int:
    """The ID of the repository's Dependabot Updates workflow. GitHub doesn't
    find a dynamic workflow by its path, so this lists them all."""
    for workflow in _pages(f"{github.API_URL}/repos/{repo}/actions/workflows", "workflows", token):
        if workflow.get("path") == WORKFLOW_PATH:
            found: int = workflow["id"]
            return found
    raise RuntimeError(
        f"{repo} has no Dependabot Updates workflow, so Dependabot has never run there "
        "(or the token can't read its Actions)"
    )


def newest_uv_run(repo: str, token: str) -> dict[str, Any]:
    """The newest successful run of Dependabot Updates that updated uv."""
    # Not ?status=success, as GitHub returns at most 1,000 runs for a filtered listing.
    url = f"{github.API_URL}/repos/{repo}/actions/workflows/{workflow_id(repo, token)}/runs"
    for run in _pages(url, "workflow_runs", token):
        if run.get("conclusion") == "success" and str(run.get("name", "")).startswith(RUN_PREFIX):
            found: dict[str, Any] = run
            return found
    raise RuntimeError(
        f"{repo} has no successful Dependabot uv run: check .github/dependabot.yml has the uv ecosystem, "
        "and that Dependabot has run since"
    )


def job_log(repo: str, run: dict[str, Any], token: str) -> str:
    """The log of the run's Dependabot job."""
    url = f"{github.API_URL}/repos/{repo}/actions/runs/{run['id']}/jobs"
    jobs = [job for job in _pages(url, "jobs", token) if job.get("name") == JOB_NAME]
    if len(jobs) != 1:
        raise RuntimeError(f"Run {run['html_url']} has {len(jobs)} jobs named {JOB_NAME}, not 1")
    try:
        # The API redirects to blob storage, which request_bytes() sends no token to.
        log = github.request_bytes("GET", f"{github.API_URL}/repos/{repo}/actions/jobs/{jobs[0]['id']}/logs", token)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410):
            raise
        e.close()
        raise RuntimeError(
            f"GitHub has no log for run {run['html_url']} (HTTP {e.code}): "
            "it may have expired, as GitHub deletes logs after the repository's retention period"
        ) from e
    return log.decode(errors="replace")


def log_version(log: str, run_url: str) -> str:
    """The uv version the log's ``uv --version`` line gives."""
    versions = {m["version"] for m in _VERSION_RE.finditer(log)}
    if not versions:
        raise RuntimeError(
            f"The log of run {run_url} has no `Package manager uv, Info : uv <version>` line: "
            "Dependabot's log format may have changed"
        )
    if len(versions) > 1:
        raise RuntimeError(f"The log of run {run_url} gives more than one uv version: {', '.join(sorted(versions))}")
    return versions.pop()


def dependabot_uv(repo: str, token: str) -> Found:
    """The uv version Dependabot's newest successful uv run in repo used."""
    run = newest_uv_run(repo, token)
    run_url = str(run["html_url"])
    version = log_version(job_log(repo, run, token), run_url)
    return Found(version, run_url, str(run.get("created_at", ""))[:10])


def show(repo: str, token: str) -> int:
    found = dependabot_uv(repo, token)
    print(f"Dependabot used uv {found.version} in its newest uv run, on {found.date}: {found.run_url}")
    return 0


def check(repo: str, token: str, minimum: str, *, exit_zero: bool = False) -> int:
    """Fail unless minimum, [tool.uv] required-version's, is Dependabot's uv version."""
    found = dependabot_uv(repo, token)
    if minimum == found.version:
        print(f"[tool.uv] required-version's minimum is uv {found.version}, which Dependabot uses: {found.run_url}")
        return 0
    print(
        f"[tool.uv] required-version's minimum is uv {minimum}, but Dependabot used uv {found.version} "
        f"in its newest uv run, on {found.date}: {found.run_url}",
        file=sys.stderr,
    )
    return 0 if exit_zero else 1


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _open_pr(repo: str, branch: str, token: str) -> str | None:
    """The URL of the open pull request from branch, if there is one."""
    owner = repo.split("/", 1)[0]
    head = urllib.parse.quote(f"{owner}:{branch}", safe="")
    url = f"{github.API_URL}/repos/{repo}/pulls?state=open&head={head}"
    pulls: list[dict[str, Any]] = github.request("GET", url, token)
    return str(pulls[0]["html_url"]) if pulls else None


def bump(root: Path, repo: str, token: str, minimum: str, version: str | None, *, pr: bool = False) -> int:
    """Bump the project's uv to Dependabot's, or to version if given, and with
    pr, commit that to a new branch, push it and open a pull request."""
    run_url = ""
    if version is None:
        found = dependabot_uv(repo, token)
        version, run_url = found.version, found.run_url
    if uv_bump.version_key(version) == uv_bump.version_key(minimum):
        print(f"[tool.uv] required-version's minimum is already uv {version}, so there's nothing to bump")
        return 0
    if uv_bump.version_key(version) < uv_bump.version_key(minimum):
        print(
            f"uv {version} is older than [tool.uv] required-version's minimum, {minimum}, and bump never lowers it",
            file=sys.stderr,
        )
        return 1
    branch, base = BRANCH_PREFIX + version, ""
    if pr:
        if existing := _open_pr(repo, branch, token):
            print(f"Pull request {existing} already bumps uv to {version}")
            return 0
        if _git(root, "status", "--porcelain"):
            raise RuntimeError("The working tree has changes, so commit or stash them before --pr")
        base = _git(root, "branch", "--show-current")
        if not base:
            raise RuntimeError("HEAD isn't on a branch, so --pr has no base branch for the pull request")
        if _git(root, "branch", "--list", branch) or _git(root, "ls-remote", "--heads", REMOTE, f"refs/heads/{branch}"):
            raise RuntimeError(
                f"Branch {branch} already exists, here or on {REMOTE}, so delete it, or open its pull request by hand"
            )

    changed, relocked = uv_bump.bump(root, minimum, version)
    names = [path.relative_to(root).as_posix() for path in changed] + (["uv.lock"] if relocked else [])
    print(f"Bumped uv from {minimum} to {version} in {', '.join(names)}")
    if not pr:
        return 0

    source = f", which Dependabot used in its newest uv run: {run_url}" if run_url else ""
    title = f"Bump uv to {version}"
    _git(root, "switch", "--create", branch)
    _git(root, "commit", "--all", "--message", f"{title}\n\nBumps uv from {minimum} to {version}{source}")
    _git(root, "push", "--set-upstream", REMOTE, branch)
    body = (
        f"Bumps uv from {minimum} to {version}{source}\n\n"
        f"Changes {', '.join(f'`{name}`' for name in names)}.\n\n"
        "Opened by `repoutils dependabot-uv bump --pr`."
    )
    try:
        created = github.request(
            "POST",
            f"{github.API_URL}/repos/{repo}/pulls",
            token,
            json_body={"title": title, "head": branch, "base": base, "body": body},
        )
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        # A rerun would stop at the pushed branch, so say how to finish by hand.
        raise RuntimeError(
            f"Pushed {branch}, but opening its pull request failed (HTTP {e.code}):\n{detail}\n"
            f"Open it at https://github.com/{repo}/compare/{base}...{branch}"
        ) from None
    print(f"Opened {created['html_url']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils dependabot-uv",
        description=summary(__doc__),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions read permission. "
        "bump --pr pushes with Git's own credentials, and its token also needs the Pull requests write permission.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{show,check,bump}")
    show_parser = subparsers.add_parser(
        "show",
        help="print the uv version of Dependabot's newest uv run",
        description="Print the uv version of Dependabot's newest successful uv run, with the run's date and URL.",
    )
    check_parser = subparsers.add_parser(
        "check",
        help="fail unless [tool.uv] required-version's minimum is Dependabot's uv version",
        description="Fail unless the minimum in pyproject.toml's [tool.uv] required-version is exactly "
        "the uv version of Dependabot's newest successful uv run. check-pins ties the setup-uv pins to that "
        "minimum, so this covers them too.",
    )
    bump_parser = subparsers.add_parser(
        "bump",
        help="bump the project's uv to Dependabot's uv version",
        description="Bump the project's uv to the uv version of Dependabot's newest successful uv run: "
        "the minimum in pyproject.toml's [tool.uv] required-version, every setup-uv step's version and "
        "any Dockerfile ghcr.io/astral-sh/uv stage, which are what check-pins checks. Then re-lock "
        "uv.lock with that uv, which changes it only if that uv writes it differently. It does nothing "
        "if the minimum is already that version, and fails if it's newer, or if check-pins fails "
        "before the bump.",
    )
    bump_parser.add_argument(
        "--version",
        help="bump to this uv version rather than Dependabot's, without asking GitHub for it",
    )
    bump_parser.add_argument(
        "--pr",
        action="store_true",
        help=f"commit the bump to a new branch, {BRANCH_PREFIX}<version>, push it to {REMOTE} and open a "
        "pull request into the current branch; if one from that branch is already open, change nothing",
    )
    for sub in (show_parser, check_parser, bump_parser):
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if the versions differ (still exit 1 on an error, unlike `|| true`)",
    )
    args = parser.parse_args(argv)

    try:
        # Read pyproject.toml first, so a bad setting fails before anything asks GitHub.
        minimum = uv_minimum(Path()) if args.mode in ("check", "bump") else ""
        token = repo = ""
        # bump --version without --pr has nothing to ask GitHub.
        if args.mode != "bump" or args.version is None or args.pr:
            token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
            repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "show":
            return show(repo, token)
        if args.mode == "bump":
            return bump(Path(), repo, token, minimum, args.version, pr=args.pr)
        return check(repo, token, minimum, exit_zero=args.exit_zero)
    except subprocess.CalledProcessError as e:
        # A command run without capturing its output (uv lock) has already shown its stderr.
        print(f"{' '.join(e.cmd)} failed" + (f":\n{e.stderr}" if e.stderr else ""), file=sys.stderr)
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
