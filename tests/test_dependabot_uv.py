import email.message
import io
import subprocess
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import dependabot_uv

API = github.API_URL
REPO = f"{API}/repos/o/r"
WORKFLOWS = f"{REPO}/actions/workflows"
RUNS = f"{WORKFLOWS}/9/runs?status=success"
LOG = f"{REPO}/actions/jobs/55/logs"
PAGE = "https://github.com/o/r/actions/runs/7"
# Copied from a real Dependabot Updates job log.
LINE = (
    "2026-11-17T01:44:54.9481977Z 2026/11/17 01:44:54 INFO <job_1710872725> "
    "Package manager uv, Info : uv 0.13.7 (x86_64-unknown-linux-gnu)\n"
)
Install = Callable[["FakeGitHub"], "FakeGitHub"]
PYPROJECT = '[tool.uv]\nrequired-version = ">=0.13.7"\n'


def _run(name: str, run_id: int = 7) -> dict[str, Any]:
    return {"id": run_id, "name": name, "html_url": PAGE, "created_at": "2026-11-17T01:44:22Z"}


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(LOG, code, "error", email.message.Message(), io.BytesIO(b"gone"))


class FakeGitHub:
    """Answers each listing from a table, a page at a time."""

    def __init__(
        self,
        *,
        workflows: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        log: bytes | urllib.error.HTTPError = LINE.encode(),
    ) -> None:
        self.lists: dict[str, tuple[str, list[dict[str, Any]]]] = {
            WORKFLOWS: (
                "workflows",
                [{"id": 3, "path": ".github/workflows/ci.yml"}, {"id": 9, "path": dependabot_uv.WORKFLOW_PATH}]
                if workflows is None
                else workflows,
            ),
            RUNS: ("workflow_runs", [_run("uv in /. - Update #2")] if runs is None else runs),
            f"{REPO}/actions/runs/7/jobs": ("jobs", [{"id": 55, "name": "Dependabot"}] if jobs is None else jobs),
        }
        self.log = log
        self.urls: list[str] = []

    def request(self, method: str, url: str, token: str) -> Any:
        assert (method, token) == ("GET", "tok")
        self.urls.append(url)
        base, _, query = url.rpartition("per_page=")
        size, page = (int(part) for part in query.split("&page="))
        key, items = self.lists[base.removesuffix("?").removesuffix("&")]
        return {key: items[(page - 1) * size : page * size]}

    def request_bytes(self, method: str, url: str, token: str) -> bytes:
        assert (method, url, token) == ("GET", LOG, "tok")
        if isinstance(self.log, urllib.error.HTTPError):
            raise self.log
        return self.log


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Install:
    def install(fake: FakeGitHub) -> FakeGitHub:
        monkeypatch.setattr(github, "request", fake.request)
        monkeypatch.setattr(github, "request_bytes", fake.request_bytes)
        return fake

    return install


def test_log_version() -> None:
    assert dependabot_uv.log_version(f"before\n{LINE}after\n", PAGE) == "0.13.7"
    # The same version twice is fine.
    assert dependabot_uv.log_version(LINE * 2, PAGE) == "0.13.7"


def test_log_version_missing() -> None:
    with pytest.raises(RuntimeError, match="has no `Package manager uv"):
        dependabot_uv.log_version("updater | uv --version\n", PAGE)


def test_log_version_conflicting() -> None:
    with pytest.raises(RuntimeError, match=r"more than one uv version: 0\.13\.7, 0\.14\.0"):
        dependabot_uv.log_version(LINE + LINE.replace("0.13.7", "0.14.0"), PAGE)


def test_newest_uv_run_skips_other_ecosystems_and_pages(install: Install) -> None:
    other = [_run("github_actions in /. - Update #1", run_id=n) for n in range(dependabot_uv.PER_PAGE)]
    fake = install(FakeGitHub(runs=[*other, _run("uv in /. - Update #2"), _run("uv in /. - Update #1", 6)]))
    found = dependabot_uv.dependabot_uv("o/r", "tok")
    assert found == dependabot_uv.Found("0.13.7", PAGE, "2026-11-17")
    assert f"{RUNS}&per_page=100&page=2" in fake.urls


def test_no_workflow(install: Install) -> None:
    install(FakeGitHub(workflows=[{"id": 3, "path": ".github/workflows/ci.yml"}]))
    with pytest.raises(RuntimeError, match="has no Dependabot Updates workflow"):
        dependabot_uv.dependabot_uv("o/r", "tok")


def test_no_uv_run(install: Install) -> None:
    install(FakeGitHub(runs=[_run("github_actions in /. - Update #1")]))
    with pytest.raises(RuntimeError, match="has no successful Dependabot uv run"):
        dependabot_uv.dependabot_uv("o/r", "tok")


def test_no_dependabot_job(install: Install) -> None:
    install(FakeGitHub(jobs=[{"id": 55, "name": "Something else"}]))
    with pytest.raises(RuntimeError, match="has 0 jobs named Dependabot"):
        dependabot_uv.dependabot_uv("o/r", "tok")


@pytest.mark.parametrize("code", [404, 410])
def test_expired_log(install: Install, code: int) -> None:
    install(FakeGitHub(log=_http_error(code)))
    with pytest.raises(RuntimeError, match=f"no log for run {PAGE} \\(HTTP {code}\\): it may have expired"):
        dependabot_uv.dependabot_uv("o/r", "tok")


def test_log_other_http_error(install: Install) -> None:
    install(FakeGitHub(log=_http_error(500)))
    with pytest.raises(urllib.error.HTTPError) as raised:
        dependabot_uv.dependabot_uv("o/r", "tok")
    # Close it, as Python 3.15 warns about an HTTPError left to the garbage collector.
    raised.value.close()


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def test_show(cwd: Path, install: Install, capsys: pytest.CaptureFixture[str]) -> None:
    install(FakeGitHub())
    assert dependabot_uv.run(["show", "--repo", "o/r"]) == 0
    assert capsys.readouterr().out == f"Dependabot used uv 0.13.7 in its newest uv run, on 2026-11-17: {PAGE}\n"


def test_check_matches(cwd: Path, install: Install, capsys: pytest.CaptureFixture[str]) -> None:
    install(FakeGitHub())
    assert dependabot_uv.run(["check", "--repo", "o/r"]) == 0
    assert "minimum is uv 0.13.7, which Dependabot uses" in capsys.readouterr().out


@pytest.mark.parametrize(("args", "status"), [([], 1), (["--exit-zero"], 0)])
def test_check_differs(
    cwd: Path, install: Install, capsys: pytest.CaptureFixture[str], args: list[str], status: int
) -> None:
    (cwd / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.13.6, <0.14"\n')
    install(FakeGitHub())
    assert dependabot_uv.run(["check", "--repo", "o/r", *args]) == status
    assert "minimum is uv 0.13.6, but Dependabot used uv 0.13.7" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--exit-zero"]])
def test_check_error_fails_even_with_exit_zero(
    cwd: Path, install: Install, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    install(FakeGitHub(log=b"no version here\n"))
    assert dependabot_uv.run(["check", "--repo", "o/r", *args]) == 1
    assert "has no `Package manager uv" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--exit-zero"]])
def test_check_http_error_fails_even_with_exit_zero(
    cwd: Path, install: Install, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    install(FakeGitHub(log=_http_error(500)))
    assert dependabot_uv.run(["check", "--repo", "o/r", *args]) == 1
    assert f"HTTP 500 from {LOG}:\ngone" in capsys.readouterr().err


def test_check_bad_pyproject_asks_nothing(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cwd / "pyproject.toml").write_text("[tool.uv]\n")
    # No fake installed and no gh: a request would fail differently.
    assert dependabot_uv.run(["check", "--repo", "o/r", "--exit-zero"]) == 1
    assert "[tool.uv] required-version is missing" in capsys.readouterr().err


def test_gh_failure(cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")

    def fail(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    monkeypatch.setattr(github, "gh_output", fail)
    assert dependabot_uv.run(["show", "--repo", "o/r"]) == 1
    assert "gh auth token failed:\nnot logged in" in capsys.readouterr().err
