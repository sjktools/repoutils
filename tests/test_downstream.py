import email.message
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any, NoReturn

import pytest

from repoutils import downstream_pins, github, pypi_provenance
from repoutils.commands import downstream

API = f"{github.API_URL}/repos/d/down"
BRANCH = "downstream/pkg-0.4.0"
UPSTREAM = '[project]\nname = "pkg"\n\n[tool.repoutils]\ncomment-prefix = "up"\n'
DOWNSTREAM = """\
[build-system]
requires = ["hatchling", "pkg==0.3.0"]

[project]
name = "down"

[dependency-groups]
pkg = ["pkg>=0.3.0"]
"""


def lock_text(version: str) -> str:
    return f'version = 1\n\n[[package]]\nname = "pkg"\nversion = "{version}"\n'


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("o/r", ["o/r"]), (" a-b/c.d\n\to2/R_e  ", ["a-b/c.d", "o2/R_e"])],
)
def test_list(text: str, expected: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert downstream.run(["list", "--repositories", text]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_list_reads_the_variable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DOWNSTREAM_REPOS", "o/r")
    assert downstream.run(["list"]) == 0
    assert capsys.readouterr().out == '["o/r"]\n'


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("  ", "names no repositories"),
        ("o", "has 'o', which isn't of the form owner/name"),
        ("o/r/x", "has 'o/r/x'"),
        ("-o/r", "has '-o/r'"),
        ("o_x/r", "has 'o_x/r'"),
        ("o--x/r", "has 'o--x/r'"),
        ("o/..", "has 'o/..'"),
        ("o/r O/R", "names O/R more than once"),
    ],
)
def test_list_refuses(text: str, error: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert downstream.run(["list", f"--repositories={text}"]) == 1
    assert error in capsys.readouterr().err


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The upstream's checkout, the current directory, with the downstream's
    checkout in down/, committed on main, with a bare repository as its origin."""
    root = tmp_path / "up"
    root.mkdir()
    (root / "pyproject.toml").write_text(UPSTREAM)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "u/up")
    # Git variables, such as a hook's, that would point Git at another repository.
    for name in ("GITHUB_RUN_ID", "GITHUB_SERVER_URL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "GIT_AUTHOR_NAME": "a",
        "GIT_COMMITTER_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_EMAIL": "a@b",
    }.items():
        monkeypatch.setenv(name, value)
    down = root / "down"
    down.mkdir()
    (down / "pyproject.toml").write_text(DOWNSTREAM)
    (down / "uv.lock").write_text(lock_text("0.3.0"))
    origin = tmp_path / "down-origin.git"
    for args in (
        ["init", "--quiet", "--bare", str(origin)],
        ["init", "--quiet", "--initial-branch=main"],
        ["remote", "add", "origin", str(origin)],
        ["add", "--all"],
        ["commit", "--quiet", "--message", "start"],
    ):
        subprocess.run(["git", *args], cwd=down, check=True)
    return root


class Fakes:
    """Stands in for PyPI's provenance, uv lock and GitHub's pull requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, open_pulls: list[dict[str, Any]] | None = None) -> None:
        self.checked: list[tuple[str, str, str]] = []
        self.locked: list[tuple[Path, str, str]] = []
        self.open_pulls = open_pulls or []
        self.created: list[Any] = []
        monkeypatch.setattr(pypi_provenance, "check", self.check)
        monkeypatch.setattr(downstream_pins, "lock", self.lock)
        monkeypatch.setattr(github, "request", self.request)

    def check(self, package: str, version: str, repository: str) -> list[str]:
        self.checked.append((package, version, repository))
        return ["pkg-0.4.0.tar.gz"]

    def lock(self, root: Path, package: str, version: str) -> bool:
        self.locked.append((root, package, version))
        (root / "uv.lock").write_text(lock_text(version))
        return True

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        assert token == "tok"
        if (method, url) == ("GET", f"{API}/pulls?state=open&head=d%3Adownstream%2Fpkg-0.4.0"):
            return self.open_pulls
        assert (method, url) == ("POST", f"{API}/pulls")
        self.created.append(json_body)
        return {"html_url": "https://github.com/d/down/pull/5"}


def test_bump(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert capsys.readouterr().out == (
        "PyPI's provenance names u/up as the publisher of pkg-0.4.0.tar.gz\n"
        "Bumped pkg from 0.3.0 to 0.4.0 in pyproject.toml, uv.lock\n"
    )
    assert fakes.checked == [("pkg", "0.4.0", "u/up")]
    assert fakes.locked == [(Path("down"), "pkg", "0.4.0")]
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM.replace("0.3.0", "0.4.0")


def test_bump_without_a_lock(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (upstream / "down/uv.lock").unlink()
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        fakes.locked.append((root, package, version))
        return False

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert fakes.locked == [(Path("down"), "pkg", "0.4.0")]
    assert not (upstream / "down/uv.lock").exists()


def test_bump_nothing_to_do(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "down/pyproject.toml").write_text(DOWNSTREAM.replace("0.3.0", "0.4"))
    fakes = Fakes(monkeypatch)
    # An old lock file is ignored, as there's nothing to bump.
    (upstream / "old.lock").write_text(lock_text("0.3.0"))
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down", "--lock-file", "old.lock"]
    assert downstream.run(args) == 0
    assert "already pins pkg 0.4.0, so there's nothing to bump" in capsys.readouterr().out
    assert fakes.checked == []


def test_bump_pr(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fakes = Fakes(monkeypatch)
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    (upstream / "new.lock").write_text(lock_text("0.4.0"))
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down", "--lock-file", "new.lock"]
    assert downstream.run(args) == 0
    assert capsys.readouterr().out.endswith(
        "Bumped pkg from 0.3.0 to 0.4.0 in pyproject.toml, uv.lock\nOpened https://github.com/d/down/pull/5\n"
    )
    # It copies the lock file rather than re-lock.
    assert fakes.locked == []
    down = upstream / "down"
    assert fakes.created == [
        {
            "title": "Bump pkg to 0.4.0",
            "head": BRANCH,
            "base": "main",
            "body": "<!-- up-downstream-pkg -->\n"
            "Bumps [pkg](https://pypi.org/project/pkg/0.4.0/) from 0.3.0 to 0.4.0, "
            "which [u/up](https://github.com/u/up) released.\n\n"
            "- `build-system.requires`: `pkg==0.3.0` to `pkg==0.4.0`\n"
            "- `dependency-groups.pkg`: `pkg>=0.3.0` to `pkg>=0.4.0`\n\n"
            "PyPI's provenance names u/up as the trusted publisher of each of the release's files.\n\n"
            "Opened by `repoutils downstream bump --pr` in https://github.com/u/up/actions/runs/77.",
        }
    ]
    assert git(down, "log", "-1", "--format=%B", f"origin/{BRANCH}") == (
        "Bump pkg to 0.4.0\n\nBumps pkg from 0.3.0 to 0.4.0.\n\n" + downstream.TRAILER
    )
    assert git(down, "log", "-1", "--format=%(trailers:key=Bumped-By,valueonly)", f"origin/{BRANCH}") == (
        "repoutils downstream bump"
    )
    assert git(down, "diff", "--name-only", "main", f"origin/{BRANCH}").splitlines() == ["pyproject.toml", "uv.lock"]
    assert git(down, "show", f"origin/{BRANCH}:uv.lock") + "\n" == lock_text("0.4.0")
    assert git(down, "branch", "--show-current") == "main"
    assert not git(down, "status", "--porcelain")


def test_bump_pr_already_open(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch, [{"html_url": "https://github.com/d/down/pull/4"}])
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 0
    assert capsys.readouterr().out.endswith(
        "Pull request https://github.com/d/down/pull/4 already bumps pkg to 0.4.0\n"
    )
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM


def test_bump_pr_refuses_other_changes(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        (root / "build.log").write_text("x")
        return fakes.lock(root, package, version)

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 1
    assert "The bump changed build.log, pyproject.toml, uv.lock, not just pyproject.toml, uv.lock" in (
        capsys.readouterr().err
    )
    assert fakes.created == []
    # pyproject.toml and uv.lock are put back.
    assert git(upstream / "down", "status", "--porcelain") == "?? build.log"


def test_bump_puts_files_back_if_locking_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> NoReturn:
        (root / "uv.lock").write_text("half")
        raise subprocess.CalledProcessError(2, ["uv", "lock"])

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == "uv lock failed\n"
    assert not git(upstream / "down", "status", "--porcelain")


@pytest.mark.parametrize(
    ("lock", "error"),
    [
        (lock_text("0.3.0"), "new.lock locks pkg at 0.3.0, not 0.4.0"),
        (None, "There's no down/uv.lock for new.lock to replace"),
    ],
)
def test_bump_refuses_lock_file(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], lock: str | None, error: str
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_text(lock_text("0.3.0"))
    if lock is None:
        (upstream / "down/uv.lock").unlink()
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert error in capsys.readouterr().err


def test_bump_refuses_a_lock_file_that_isnt_utf8(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_bytes(b"\xff")
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert capsys.readouterr().err == "new.lock isn't valid UTF-8\n"


def test_bump_refuses_a_lock_file_without_the_package(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_text("version = 1\n")
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert "new.lock doesn't lock pkg, which the project depends on" in capsys.readouterr().err


def test_bump_pr_needs_repo(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr"])
    assert "bump --pr needs --repo" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "project = 1\n", "[project]\nname = ''\n"])
def test_bump_needs_a_package_name(upstream: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
    (upstream / "pyproject.toml").write_text(text)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert "[project] name is missing, so pass --package" in capsys.readouterr().err


def test_bump_takes_package_and_asks_gh(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "pyproject.toml").unlink()
    monkeypatch.delenv("GITHUB_REPOSITORY")
    monkeypatch.delenv("GITHUB_TOKEN")
    fakes = Fakes(monkeypatch, [{"html_url": "https://github.com/d/down/pull/4"}])
    asked: list[tuple[str, ...]] = []

    def gh_output(*args: str) -> str:
        asked.append(args)
        return "tok" if args[0] == "auth" else "g/h"

    monkeypatch.setattr(github, "gh_output", gh_output)
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--package", "pkg", "--pr", "--repo", "d/down"]
    assert downstream.run(args) == 0
    assert fakes.checked == [("pkg", "0.4.0", "g/h")]
    assert [command[0] for command in asked] == ["repo", "auth"]
    # The default comment-prefix, as there's no pyproject.toml here.
    assert "already bumps pkg" in capsys.readouterr().out


def test_bump_http_error(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    Fakes(monkeypatch)

    def request(method: str, url: str, token: str) -> NoReturn:
        raise urllib.error.HTTPError(url, 403, "no", email.message.Message(), io.BytesIO(b"denied"))

    monkeypatch.setattr(github, "request", request)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 1
    assert capsys.readouterr().err == f"HTTP 403 from {API}/pulls?state=open&head=d%3Adownstream%2Fpkg-0.4.0:\ndenied\n"


@pytest.mark.parametrize(("version", "error"), [("v1", "isn't a release version"), ("0.2", "never lowers a pin")])
def test_bump_refuses_version(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str, error: str
) -> None:
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", version, "--directory", "down"]) == 1
    assert error in capsys.readouterr().err
    assert fakes.checked == []


def test_bump_provenance_failure_changes_nothing(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)

    def check(package: str, version: str, repository: str) -> NoReturn:
        raise RuntimeError("bad provenance")

    monkeypatch.setattr(pypi_provenance, "check", check)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == "bad provenance\n"
    assert fakes.locked == []
    assert not git(upstream / "down", "status", "--porcelain")
