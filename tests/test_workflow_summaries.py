import io
import json
import urllib.error
import zipfile
from email.message import Message
from typing import Any, override

import pytest

from repoutils import github, workflow_summaries
from repoutils.commands import secrets

API = github.API_URL
RUN = f"{API}/repos/o/r/actions/runs/7"
PAGE = "https://github.com/o/r/actions/runs/7"

# The secret names the repository o/r has at each level, as the API lists them.
NAME_PAGES = {
    f"{API}/repos/o/r/environments?per_page=100&page=1": {"environments": [{"name": "release"}]},
    f"{API}/repos/o/r/actions/secrets?per_page=100&page=1": {"secrets": [{"name": "REPO_TOKEN"}]},
    f"{API}/repos/o/r/environments/release/secrets?per_page=100&page=1": {"secrets": [{"name": "DEPLOY_KEY"}]},
}


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class FakeGitHub:
    """Answers requests from a table, giving each run poll the next answer in turn."""

    def __init__(self, runs: list[dict[str, Any]], artifacts: list[dict[str, Any]], zips: dict[str, bytes]) -> None:
        self.runs = runs
        self.artifacts = artifacts
        self.zips = zips
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        self.calls.append((method, url, json_body))
        assert token == "tok"
        if method == "POST":
            return {"workflow_run_id": 7, "html_url": PAGE}
        if url == f"{API}/repos/o/r":
            return {"default_branch": "trunk"}
        if url in NAME_PAGES:
            return NAME_PAGES[url]
        if url == RUN:
            return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
        if url.startswith(f"{RUN}/artifacts?per_page={workflow_summaries.PER_PAGE}&page="):
            page = int(url.rsplit("=", 1)[1])
            size = workflow_summaries.PER_PAGE
            return {"artifacts": self.artifacts[(page - 1) * size : page * size]}
        raise AssertionError(url)

    def request_bytes(self, method: str, url: str, token: str) -> bytes:
        self.calls.append((method, url, None))
        return self.zips[url]


def _artifact(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "archive_download_url": f"{API}/zip/{name}", **extra}


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(workflow_summaries, "sleep", slept.append)
    return slept


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    monkeypatch.setattr(github, "request", fake.request)
    monkeypatch.setattr(github, "request_bytes", fake.request_bytes)


def _done(conclusion: str = "success") -> dict[str, Any]:
    return {"status": "completed", "conclusion": conclusion, "html_url": PAGE}


def _remote(**overrides: Any) -> int:
    options: dict[str, Any] = {
        "workflow": "inspect-secrets.yml",
        "ref": None,
        "run_id": None,
        "interval": 5,
        "timeout": 600,
    }
    options.update(overrides)
    return workflow_summaries.inspect_remote("o/r", "tok", **options)


def test_dispatches_waits_through_approval_and_prints_summaries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    held = {"status": "completed", "conclusion": "action_required", "html_url": PAGE}
    runs: list[dict[str, Any]] = [
        held,
        held,
        {"status": "waiting", "conclusion": None, "html_url": PAGE},
        {"status": "in_progress"},
        _done(),
    ]
    artifacts = [_artifact("step-summaries-release"), _artifact("other"), _artifact("step-summaries-autofix")]
    zips = {
        f"{API}/zip/step-summaries-release": _zip({"inspect.md": "## Release\n"}),
        f"{API}/zip/step-summaries-autofix": _zip({"b.md": "B", "dir/": "", "a.md": "A\n\n"}),
    }
    fake = FakeGitHub(runs, artifacts, zips)
    _install(monkeypatch, fake)
    assert _remote() == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "<!-- step-summaries-autofix/a.md -->\nA\n\n"
        "<!-- step-summaries-autofix/b.md -->\nB\n\n"
        "<!-- step-summaries-release/inspect.md -->\n## Release\n\n"
    )
    # Each hold is reported once, with where to approve the run.
    assert captured.err == (
        f"Started inspect-secrets.yml on trunk: {PAGE}\n"
        f"Waiting: GitHub is holding the run for approval. Approve it at {PAGE}\n"
        f"Waiting: a job is waiting for approval to deploy to an environment. Approve it at {PAGE}\n"
        f"Run 7 succeeded: {PAGE}\n"
    )
    assert fake.calls[1] == (
        "POST",
        f"{API}/repos/o/r/actions/workflows/inspect-secrets.yml/dispatches",
        {"ref": "trunk", "return_run_details": True},
    )
    assert no_sleep == [5, 5, 5, 5]


def test_run_id_reads_an_existing_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    fake = FakeGitHub([_done()], [], {})
    _install(monkeypatch, fake)
    # A run that kept no summaries, such as one whose jobs all skipped, inspected nothing.
    assert _remote(run_id=7, ref="ignored") == 1
    assert all(method == "GET" for method, _, _ in fake.calls)
    assert f"Run 7 kept no step summaries (step-summaries-* artifacts), so nothing was inspected: {PAGE}" in (
        capsys.readouterr().err
    )


def test_reads_every_page_of_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    monkeypatch.setattr(workflow_summaries, "PER_PAGE", 1)
    artifacts = [_artifact("other"), _artifact("step-summaries-x")]
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "second page"})}
    fake = FakeGitHub([_done()], artifacts, zips)
    _install(monkeypatch, fake)
    assert _remote(run_id=7) == 0
    assert "second page" in capsys.readouterr().out
    pages = [url for _, url, _ in fake.calls if "/artifacts?" in url]
    assert pages == [f"{RUN}/artifacts?per_page=1&page={page}" for page in (1, 2, 3)]


def test_failed_run_still_prints_its_summaries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "failed here"})}
    _install(monkeypatch, FakeGitHub([_done("failure")], [_artifact("step-summaries-x")], zips))
    assert _remote(ref="main") == 1
    captured = capsys.readouterr()
    assert "failed here" in captured.out
    assert f"Run 7 finished with conclusion failure: {PAGE}" in captured.err


def test_expired_artifact_is_an_error(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    _install(monkeypatch, FakeGitHub([_done()], [_artifact("step-summaries-x", expired=True)], {}))
    with pytest.raises(RuntimeError, match="step-summaries-x of run 7 has expired"):
        _remote(run_id=7)


def test_times_out(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    clock = iter([0.0, 3.0, 11.0])
    monkeypatch.setattr(workflow_summaries, "monotonic", lambda: next(clock))
    _install(monkeypatch, FakeGitHub([{"status": "queued", "html_url": PAGE}], [], {}))
    with pytest.raises(TimeoutError, match=f"Run 7 hadn't finished after 10 seconds: {PAGE}"):
        _remote(run_id=7, timeout=10)
    assert no_sleep == [5]


def _not_found() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(RUN, 404, "Not Found", Message(), io.BytesIO(b"{}"))


def test_retries_a_new_run_that_github_doesnt_serve_yet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "S"})}
    fake = FakeGitHub([_done()], [_artifact("step-summaries-x")], zips)
    _install(monkeypatch, fake)
    misses = [_not_found(), _not_found()]

    def request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if url == RUN and misses:
            raise misses.pop()
        return fake.request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", request)
    assert _remote() == 0
    assert no_sleep == [workflow_summaries.NOT_FOUND_RETRY] * 2


def test_gives_up_on_a_404_after_the_grace(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    clock = iter([0.0, 1.0, workflow_summaries.NOT_FOUND_GRACE])
    monkeypatch.setattr(workflow_summaries, "monotonic", lambda: next(clock))

    def request(*args: object, **kwargs: object) -> Any:
        raise _not_found()

    monkeypatch.setattr(github, "request", request)
    with pytest.raises(urllib.error.HTTPError) as raised:
        workflow_summaries.wait("o/r", 7, "tok", interval=5, timeout=600, just_started=True)
    raised.value.close()
    assert no_sleep == [workflow_summaries.NOT_FOUND_RETRY]


def test_timeout_inside_the_grace_is_a_timeout(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(workflow_summaries, "monotonic", lambda: next(clock))

    def request(*args: object, **kwargs: object) -> Any:
        raise _not_found()

    monkeypatch.setattr(github, "request", request)
    with pytest.raises(TimeoutError, match="GitHub didn't serve run 7 within 2 seconds"):
        workflow_summaries.wait("o/r", 7, "tok", interval=5, timeout=2, just_started=True)
    assert no_sleep == [workflow_summaries.NOT_FOUND_RETRY]


def test_timeout_past_the_grace_is_still_a_timeout(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    clock = iter([0.0, 1.0, workflow_summaries.NOT_FOUND_GRACE + 1])
    monkeypatch.setattr(workflow_summaries, "monotonic", lambda: next(clock))

    def request(*args: object, **kwargs: object) -> Any:
        raise _not_found()

    monkeypatch.setattr(github, "request", request)
    with pytest.raises(TimeoutError, match="within 2 seconds"):
        workflow_summaries.wait("o/r", 7, "tok", interval=5, timeout=2, just_started=True)


def test_404_for_an_existing_run_fails_at_once(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise _not_found()

    monkeypatch.setattr(github, "request", request)
    with pytest.raises(urllib.error.HTTPError) as raised:
        _remote(run_id=7)
    raised.value.close()
    assert no_sleep == []


@pytest.mark.parametrize("response", [None, {}, {"workflow_run_id": "7"}])
def test_dispatch_without_a_run_id(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    monkeypatch.setattr(github, "request", lambda *a, **k: response)  # pyright: ignore[reportUnknownLambdaType]
    with pytest.raises(RuntimeError, match="didn't give its run ID"):
        workflow_summaries.dispatch("o/r", "w.yml", "main", "tok")


def test_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "S"})}
    fake = FakeGitHub([_done()], [_artifact("step-summaries-x")], zips)
    _install(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    argv = ["inspect-remote", "--repo", "o/r", "--workflow", "w.yml", "--ref", "dev", "--timeout", "1"]
    assert secrets.main(argv) == 0
    names = '{"actions":["REPO_TOKEN"],"environments":{"release":["DEPLOY_KEY"]}}'
    dispatches = [call for call in fake.calls if call[0] == "POST"]
    assert dispatches == [
        (
            "POST",
            f"{API}/repos/o/r/actions/workflows/w.yml/dispatches",
            {"ref": "dev", "return_run_details": True, "inputs": {"secret-names": names}},
        )
    ]


def test_command_line_no_secret_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "S"})}
    fake = FakeGitHub([_done()], [_artifact("step-summaries-x")], zips)
    _install(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert secrets.main(["inspect-remote", "--repo", "o/r", "--ref", "dev", "--no-secret-names"]) == 0
    # Neither lists a secret nor passes the input.
    assert not any(url in NAME_PAGES for _, url, _ in fake.calls)
    assert fake.calls[0] == (
        "POST",
        f"{API}/repos/o/r/actions/workflows/inspect-secrets.yml/dispatches",
        {"ref": "dev", "return_run_details": True},
    )


def test_command_line_run_id_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_sleep: list[float]
) -> None:
    zips = {f"{API}/zip/step-summaries-x": _zip({"s.md": "S"})}
    fake = FakeGitHub([_done()], [_artifact("step-summaries-x")], zips)
    _install(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert secrets.main(["inspect-remote", "--repo", "o/r", "--run-id", "7"]) == 0
    assert all(method == "GET" for method, _, _ in fake.calls)
    # Nor lists the secret names, which only a new run needs.
    assert not any(url in NAME_PAGES for _, url, _ in fake.calls)


def test_command_line_converts_minutes_to_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_inspect_remote(repo: str, token: str, **options: Any) -> int:
        seen.update(options, repo=repo, token=token)
        return 0

    monkeypatch.setattr(workflow_summaries, "inspect_remote", fake_inspect_remote)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert secrets.main(["inspect-remote", "--repo", "o/r", "--timeout", "1.5", "--interval", "2"]) == 0
    assert seen["timeout"] == 90
    assert seen["interval"] == 2


@pytest.mark.parametrize("option", ["--timeout", "--interval"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_rejects_bad_times(option: str, value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        secrets.main(["inspect-remote", option, value])
    assert "must be a number more than 0" in capsys.readouterr().err


def test_request_bytes_returns_the_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        @override
        def __enter__(self) -> "Response":
            return self

    monkeypatch.setattr(github.no_redirect_opener, "open", lambda *a, **k: Response(b"PK\x03\x04"))  # pyright: ignore[reportUnknownLambdaType]
    assert github.request_bytes("GET", f"{API}/zip", "tok") == b"PK\x03\x04"
    monkeypatch.setattr(github.no_redirect_opener, "open", lambda *a, **k: Response(json.dumps({"a": 1}).encode()))  # pyright: ignore[reportUnknownLambdaType]
    assert github.request("GET", f"{API}/x", "tok") == {"a": 1}
