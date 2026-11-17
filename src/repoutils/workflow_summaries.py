"""``repoutils secrets inspect-remote``: run the workflow that inspects the
repository's secrets, wait for it, and print the step summaries its jobs keep
as ``step-summaries-*`` artifacts. It's the remote counterpart of ``secrets
inspect``, which only runs inside a workflow. Kept apart from the secrets
command for length.

It dispatches the workflow on a branch (by default the repository's default
branch), or reads a run that already exists with ``--run-id``. While GitHub
holds the run for approval, it prints the run's page, where a maintainer can
approve it, and keeps waiting.
Stdlib-only, like every repoutils module."""

import argparse
import io
import math
import os
import sys
import zipfile
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from repoutils import github

DEFAULT_WORKFLOW = "inspect-secrets.yml"
# The prefix of the artifacts that hold a run's step summaries (see AGENTS.md).
ARTIFACT_PREFIX = "step-summaries-"
PER_PAGE = 100


@dataclass(frozen=True)
class Summary:
    """One step summary file from a run's artifacts."""

    artifact: str
    path: str
    text: str


def dispatch(repo: str, workflow: str, ref: str, token: str) -> int:
    """Start workflow on ref, and return the new run's ID."""
    url = f"{github.API_URL}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    # return_run_details makes GitHub answer with the new run's ID rather than an empty 204.
    response = github.request("POST", url, token, json_body={"ref": ref, "return_run_details": True})
    run_id = response.get("workflow_run_id") if isinstance(response, dict) else None
    if not isinstance(run_id, int):
        raise RuntimeError(f"Started {workflow} on {ref}, but GitHub didn't give its run ID")
    return run_id


def _held(run: dict[str, Any]) -> str | None:
    """Why the run is waiting for a person, or None if it isn't."""
    if run.get("conclusion") == "action_required":
        # GitHub holds a run it thinks may be malicious, showing it as completed
        # until a maintainer approves it, when the same run starts.
        return "GitHub is holding the run for approval"
    if run.get("status") == "waiting":
        return "a job is waiting for approval to deploy to an environment"
    return None


def wait(repo: str, run_id: int, token: str, *, interval: float, timeout: float) -> dict[str, Any]:
    """Poll the run until it completes, and return it. Raises TimeoutError
    after timeout seconds."""
    url = f"{github.API_URL}/repos/{repo}/actions/runs/{run_id}"
    deadline = monotonic() + timeout
    told = None
    while True:
        run: dict[str, Any] = github.request("GET", url, token)
        held = _held(run)
        if held is not None and held != told:
            print(f"Waiting: {held}. Approve it at {run.get('html_url')}", file=sys.stderr)
        told = held
        if run.get("status") == "completed" and held is None:
            return run
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError(f"Run {run_id} hadn't finished after {timeout:g} seconds: {run.get('html_url')}")
        sleep(min(interval, left))


def summaries(repo: str, run_id: int, token: str) -> list[Summary]:
    """The step summaries in the run's step-summaries-* artifacts, by artifact
    name and then path."""
    url = f"{github.API_URL}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page={PER_PAGE}"
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github.request("GET", f"{url}&page={page}", token)["artifacts"]
        artifacts += batch
        if len(batch) < PER_PAGE:
            break
        page += 1
    found: list[Summary] = []
    for artifact in sorted(artifacts, key=lambda a: str(a["name"])):
        name = str(artifact["name"])
        if not name.startswith(ARTIFACT_PREFIX):
            continue
        if artifact.get("expired"):
            raise RuntimeError(f"Artifact {name} of run {run_id} has expired")
        # The API redirects to blob storage, which request_bytes() sends no token to.
        data = github.request_bytes("GET", str(artifact["archive_download_url"]), token)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if not info.is_dir():
                    text = archive.read(info).decode(errors="replace")
                    found.append(Summary(name, info.filename, text))
    return found


def default_branch(repo: str, token: str) -> str:
    branch: str = github.request("GET", f"{github.API_URL}/repos/{repo}", token)["default_branch"]
    return branch


def inspect_remote(
    repo: str,
    token: str,
    *,
    workflow: str,
    ref: str | None,
    run_id: int | None,
    interval: float,
    timeout: float,
) -> int:
    """Run (or read) the workflow and print its step summaries; exit 0 only
    if the run succeeded and kept some."""
    if run_id is None:
        ref = ref or default_branch(repo, token)
        run_id = dispatch(repo, workflow, ref, token)
        print(f"Started {workflow} on {ref}: https://github.com/{repo}/actions/runs/{run_id}", file=sys.stderr)
    run = wait(repo, run_id, token, interval=interval, timeout=timeout)
    found = summaries(repo, run_id, token)
    for summary in found:
        print(f"<!-- {summary.artifact}/{summary.path} -->")
        print(summary.text.rstrip("\n"))
        print()
    conclusion = run.get("conclusion")
    if not found:
        # Such as when every job was skipped: inspect-secrets.yml's jobs only run on main.
        print(
            f"Run {run_id} kept no step summaries ({ARTIFACT_PREFIX}* artifacts), so nothing was inspected: "
            f"{run.get('html_url')}",
            file=sys.stderr,
        )
        return 1
    if conclusion != "success":
        print(f"Run {run_id} finished with conclusion {conclusion}: {run.get('html_url')}", file=sys.stderr)
        return 1
    print(f"Run {run_id} succeeded: {run.get('html_url')}", file=sys.stderr)
    return 0


def _positive(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a number more than 0")
    return value


# argparse has no public name for the type add_subparsers() returns.
def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    """Add the inspect-remote subcommand and its options."""
    parser = subparsers.add_parser(
        "inspect-remote",
        help="run the workflow that inspects the secrets, and print its step summaries",
        description=f"Dispatch the workflow that runs `secrets inspect` (default {DEFAULT_WORKFLOW}), "
        f"wait for it, and print the step summaries its jobs keep as {ARTIFACT_PREFIX}* artifacts. "
        "While GitHub holds the run for approval, print where to approve it and keep waiting. "
        "Fails unless the run succeeds and keeps some step summaries. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, "
        "which needs the Actions write permission to dispatch, and Actions read for --run-id.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help=f"file name (default: {DEFAULT_WORKFLOW})")
    parser.add_argument("--ref", help="branch to run it on (default: the repository's default branch)")
    parser.add_argument("--run-id", type=int, help="read this run instead of starting one")
    parser.add_argument(
        "--timeout",
        type=_positive,
        default=30,
        metavar="MINUTES",
        help="give up if the run hasn't finished after this long (default: 30)",
    )
    parser.add_argument(
        "--interval",
        type=_positive,
        default=10,
        metavar="SECONDS",
        help="how often to check on the run (default: 10)",
    )
