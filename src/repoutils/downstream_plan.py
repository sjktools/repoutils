"""`downstream plan`, which works out what a Bump downstream run bumps to,
from the event that started it, kept apart from the command for length.

A release's Publish run, on the tag v<version>, bumps to that version. A
Publish run that someone started by hand published a dev version of the next
patch release of the commit it ran on, numbered by its run ID, which plan
works out from that commit's pyproject.toml, read with `git show`, with the
same code as `bump-version --dev`. A Bump downstream run started by hand
bumps to the version, and the commit, that its inputs give.
Stdlib-only, like every repoutils module."""

import json
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from repoutils import downstream_actions, downstream_pins
from repoutils.checkout import git_env
from repoutils.commands import bump_version
from repoutils.config import PYPROJECT

# The keys of what plan works out, in the order it prints them.
OUTPUTS = ("repositories", "version", "commit", "test")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, env=git_env(), capture_output=True, text=True, check=False)


def published_dev_version(root: Path, commit: str, run_id: int) -> str:
    """The dev version that a Publish run started by hand, with run_id,
    published from commit, from commit's pyproject.toml in root's history."""
    shown = _git(root, "show", f"{commit}:{PYPROJECT}")
    if shown.returncode:
        raise RuntimeError(f"Reading {PYPROJECT} at {commit} failed: {shown.stderr.strip()}")
    where = Path(f"{commit}:{PYPROJECT}")
    try:
        pyproject = tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{where} isn't valid TOML: {e}") from e
    _, version = bump_version.name_and_version(pyproject, where)
    return bump_version.dev_version(version, "patch", run_id)


def check_reviewed(root: Path, commit: str) -> None:
    """Raise ValueError unless commit is in the history of root's HEAD, the
    default branch, so that it's reviewed code."""
    found = _git(root, "merge-base", "--is-ancestor", commit, "HEAD")
    if found.returncode == 1:
        raise ValueError(f"{commit} isn't a commit on the checked-out branch, so it may be unreviewed code")
    if found.returncode:
        raise RuntimeError(f"Checking {commit} is on the checked-out branch failed: {found.stderr.strip()}")


def _text(value: object, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"The event's {what} isn't a string")
    return value


def version_and_commit(root: Path, event_name: str, event: Mapping[str, object]) -> tuple[str, str]:
    """The version a run bumps to, and for a dev version, the commit its
    build came from (or empty), from the event that started the run."""
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise ValueError("The workflow_dispatch event's inputs aren't an object")
        version = _text(inputs.get("version"), "version input").strip()
        if not version:
            raise ValueError("The version input is empty")
        return version, _text(inputs.get("commit"), "commit input").strip()
    if event_name != "workflow_run":
        raise ValueError(f"A {event_name or 'missing'} event doesn't say what to bump to")
    run = event.get("workflow_run")
    if not isinstance(run, dict):
        raise ValueError("The workflow_run event has no workflow_run")
    if run.get("event") == "workflow_dispatch":
        run_id, commit = run.get("id"), _text(run.get("head_sha"), "head_sha")
        if not isinstance(run_id, int) or not commit:
            raise ValueError("The workflow_run event has no run ID or head_sha")
        return published_dev_version(root, commit, run_id), commit
    if run.get("event") == "push":
        tag = _text(run.get("head_branch"), "head_branch")
        if not tag.startswith("v") or len(tag) == 1:
            raise ValueError(f"The run was on {tag!r}, which isn't a release tag, v<version>")
        return tag.removeprefix("v"), ""
    raise ValueError(f"The run was started by a {run.get('event')} event, not by a tag push or by hand")


def plan(root: Path, event_name: str, event: Mapping[str, object], repositories: list[str]) -> dict[str, str]:
    """What a run bumps to, keyed by OUTPUTS: the repositories as a JSON
    list, the version, the commit (or empty), and whether it's a test run,
    `true` or `false`, for a dev version. Raises ValueError if the version
    is neither a release nor a dev version, the commit is malformed or given
    for a release, or it isn't on root's checked-out branch."""
    version, commit = version_and_commit(root, event_name, event)
    test = downstream_pins.is_dev(version)
    downstream_actions.target(version, commit or None)
    if commit:
        # The downstream's CI runs the upstream's actions at the commit. A
        # Publish run on a tag named main would have main as its head
        # branch too, so check the commit rather than trust the branch.
        check_reviewed(root, commit)
    return {
        "repositories": json.dumps(repositories),
        "version": version,
        "commit": commit,
        "test": "true" if test else "false",
    }


def read_event(path: str) -> dict[str, object]:
    """The event in the JSON file at path, such as $GITHUB_EVENT_PATH."""
    if not path:
        raise ValueError("No event file: pass --event-path, or run it in GitHub Actions")
    with open(path, encoding="utf-8") as f:
        event: object = json.load(f)
    if not isinstance(event, dict):
        raise ValueError(f"{path} isn't a JSON object")
    return event
