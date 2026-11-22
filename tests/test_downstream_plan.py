import json
import subprocess
from pathlib import Path

import pytest

from repoutils import downstream_plan
from repoutils.commands import downstream

REPOS = ["sjktools/exolot", "skissane/other"]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, version: str) -> str:
    (root / "pyproject.toml").write_text(f'[project]\nname = "pkg"\nversion = "{version}"\n')
    _git(root, "add", "pyproject.toml")
    _git(root, "commit", "--quiet", "-m", f"Version {version}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout of main, with pyproject.toml at 0.4.0 then 0.5.0."""
    for name in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{name}_NAME", "a")
        monkeypatch.setenv(f"{name}_EMAIL", "a@b")
    _git(tmp_path, "init", "--quiet", "-b", "main")
    _commit(tmp_path, "0.4.0")
    _commit(tmp_path, "0.5.0")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _manual_publish(sha: str, run_id: int = 123) -> dict[str, object]:
    return {"workflow_run": {"event": "workflow_dispatch", "id": run_id, "head_sha": sha, "head_branch": "main"}}


def test_release_tag(checkout: Path) -> None:
    event = {"workflow_run": {"event": "push", "id": 1, "head_sha": "0" * 40, "head_branch": "v0.4.0"}}
    assert downstream_plan.plan(checkout, "workflow_run", event, REPOS) == {
        "repositories": json.dumps(REPOS),
        "version": "0.4.0",
        "commit": "",
        "test": "false",
    }


def test_manual_publish_reads_the_runs_commit(checkout: Path) -> None:
    # The version at the run's commit, not at HEAD.
    sha = _git(checkout, "rev-parse", "HEAD~1")
    assert downstream_plan.plan(checkout, "workflow_run", _manual_publish(sha, 41908943359), REPOS) == {
        "repositories": json.dumps(REPOS),
        "version": "0.4.1.dev41908943359",
        "commit": sha,
        "test": "true",
    }


@pytest.mark.parametrize(
    ("inputs", "version", "commit", "test"),
    [
        ({"version": "0.4.0"}, "0.4.0", "", "false"),
        ({"version": " 0.4.0 ", "commit": ""}, "0.4.0", "", "false"),
        ({"version": "0.5.1.dev9", "commit": ""}, "0.5.1.dev9", "", "true"),
    ],
)
def test_dispatched(inputs: dict[str, str], version: str, commit: str, test: str, checkout: Path) -> None:
    planned = downstream_plan.plan(checkout, "workflow_dispatch", {"inputs": inputs}, REPOS)
    assert (planned["version"], planned["commit"], planned["test"]) == (version, commit, test)


def test_dispatched_dev_version_with_a_commit(checkout: Path) -> None:
    sha = _git(checkout, "rev-parse", "HEAD")
    event = {"inputs": {"version": "0.5.1.dev9", "commit": sha}}
    assert downstream_plan.plan(checkout, "workflow_dispatch", event, REPOS)["commit"] == sha


def test_refuses_a_commit_off_the_branch(checkout: Path) -> None:
    _git(checkout, "checkout", "--quiet", "-b", "other")
    sha = _commit(checkout, "9.9.9")
    _git(checkout, "checkout", "--quiet", "main")
    with pytest.raises(ValueError, match=f"{sha} isn't a commit on the checked-out branch"):
        downstream_plan.plan(checkout, "workflow_run", _manual_publish(sha), REPOS)


def test_unknown_commit(checkout: Path) -> None:
    with pytest.raises(RuntimeError, match=f"Reading pyproject.toml at {'a' * 40} failed"):
        downstream_plan.plan(checkout, "workflow_run", _manual_publish("a" * 40), REPOS)
    event = {"inputs": {"version": "0.5.1.dev9", "commit": "a" * 40}}
    with pytest.raises(RuntimeError, match="Checking .* is on the checked-out branch failed"):
        downstream_plan.plan(checkout, "workflow_dispatch", event, REPOS)


def test_bad_pyproject_at_the_commit(checkout: Path) -> None:
    (checkout / "pyproject.toml").write_text("[project\n")
    _git(checkout, "commit", "--quiet", "-am", "Break it")
    with pytest.raises(ValueError, match="isn't valid TOML"):
        downstream_plan.plan(checkout, "workflow_run", _manual_publish(_git(checkout, "rev-parse", "HEAD")), REPOS)
    sha = _commit(checkout, "0.6.0.dev1")
    with pytest.raises(ValueError, match="isn't of the form X.Y.Z"):
        downstream_plan.plan(checkout, "workflow_run", _manual_publish(sha), REPOS)


@pytest.mark.parametrize(
    ("event_name", "event", "error"),
    [
        ("push", {}, "A push event doesn't say what to bump to"),
        ("", {}, "A missing event doesn't say"),
        ("workflow_dispatch", {"inputs": {"version": ""}}, "The version input is empty"),
        ("workflow_dispatch", {}, "The version input is empty"),
        ("workflow_dispatch", {"inputs": {"version": 4}}, "version input isn't a string"),
        ("workflow_dispatch", {"inputs": {"version": "0.4.0", "commit": "0" * 40}}, "0.4.0 is a release"),
        ("workflow_dispatch", {"inputs": {"version": "0.4.1.dev1", "commit": "abc"}}, "isn't a full commit hash"),
        ("workflow_dispatch", {"inputs": {"version": "0.4.0rc1"}}, "0.4.0rc1"),
        ("workflow_dispatch", {"inputs": ["0.4.0"]}, "inputs aren't an object"),
        ("workflow_run", {}, "has no workflow_run"),
        ("workflow_run", {"workflow_run": {"event": "workflow_dispatch", "id": "1"}}, "no run ID or head_sha"),
        ("workflow_run", {"workflow_run": {"event": "push", "head_branch": "main"}}, "isn't a release tag"),
        ("workflow_run", {"workflow_run": {"event": "push", "head_branch": "v"}}, "isn't a release tag"),
        ("workflow_run", {"workflow_run": {"event": "schedule"}}, "started by a schedule event"),
    ],
)
def test_refuses(event_name: str, event: dict[str, object], error: str, checkout: Path) -> None:
    with pytest.raises(ValueError, match=error):
        downstream_plan.plan(checkout, event_name, event, REPOS)


def test_command(checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sha = _git(checkout, "rev-parse", "HEAD")
    event_path = checkout.parent / "event.json"
    event_path.write_text(json.dumps(_manual_publish(sha, 7)))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("DOWNSTREAM_REPOS", " ".join(REPOS))
    assert downstream.run(["plan"]) == 0
    assert capsys.readouterr().out == (
        f"repositories={json.dumps(REPOS)}\nversion=0.5.1.dev7\ncommit={sha}\ntest=true\n"
    )


def test_command_options(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    event_path = checkout.parent / "event.json"
    event_path.write_text(json.dumps({"inputs": {"version": "0.4.0"}}))
    args = ["plan", "--repositories", "a/b", "--event-name", "workflow_dispatch", "--event-path", str(event_path)]
    assert downstream.run(args) == 0
    assert capsys.readouterr().out == 'repositories=["a/b"]\nversion=0.4.0\ncommit=\ntest=false\n'


@pytest.mark.parametrize(
    ("args", "event", "error"),
    [
        (["--repositories", ""], {"inputs": {"version": "0.4.0"}}, "DOWNSTREAM_REPOS names no repositories"),
        (["--repositories", "a/b"], ["not", "an", "object"], "isn't a JSON object"),
        (["--repositories", "a/b"], {"inputs": {"version": "0.4.0x"}}, "0.4.0x"),
        (["--repositories", "a/b", "--event-path", ""], {}, "No event file"),
    ],
)
def test_command_fails(
    args: list[str], event: object, error: str, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    event_path = checkout.parent / "event.json"
    event_path.write_text(json.dumps(event))
    argv = ["plan", "--event-name", "workflow_dispatch", "--event-path", str(event_path), *args]
    assert downstream.run(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert error in captured.err
