"""Report the uv version Dependabot updates this repository with, or check that
pyproject.toml's [tool.uv] required-version minimum is that version.

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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import github
from repoutils.commands.check_pins import uv_minimum
from repoutils.docstrings import summary

# The path GitHub gives the dynamic workflow that runs Dependabot's updates.
WORKFLOW_PATH = "dynamic/dependabot/dependabot-updates"
# Runs are named e.g. "uv in /. - Update #1710872725".
RUN_PREFIX = "uv in "
JOB_NAME = "Dependabot"
PER_PAGE = 100
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
    url = f"{github.API_URL}/repos/{repo}/actions/workflows/{workflow_id(repo, token)}/runs?status=success"
    for run in _pages(url, "workflow_runs", token):
        if str(run.get("name", "")).startswith(RUN_PREFIX):
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils dependabot-uv",
        description=summary(__doc__),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Actions read permission.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{show,check}")
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
    for sub in (show_parser, check_parser):
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
        minimum = uv_minimum(Path()) if args.mode == "check" else ""
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "show":
            return show(repo, token)
        return check(repo, token, minimum, exit_zero=args.exit_zero)
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
