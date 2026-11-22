import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, NoReturn

import pytest

from repoutils import github
from repoutils.commands import bump_version

API = github.API_URL
PYPROJECT = """\
[project]
name = "My_Pkg"
version = "1.2.3"
dependencies = ["other==1.2.3"]

[tool.other]
version = "1.2.3"
"""
LOCK = """\
version = 1
revision = 3

[[package]]
name = "my-pkg"
version = "1.2.3"
source = { editable = "." }

[[package]]
name = "other"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
"""


@pytest.mark.parametrize(
    ("level", "new"),
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")],
)
def test_bumped(level: str, new: str) -> None:
    assert bump_version.bumped("1.2.3", level) == new


@pytest.mark.parametrize("version", ["1.2", "1.2.3.dev1", "v1.2.3"])
def test_bumped_refuses_other_forms(version: str) -> None:
    with pytest.raises(ValueError, match="isn't of the form X.Y.Z"):
        bump_version.bumped(version, "patch")


@pytest.mark.parametrize(
    ("level", "new"),
    [("patch", "1.2.4.dev99"), ("minor", "1.3.0.dev0"), ("major", "2.0.0.dev7")],
)
def test_dev_version(level: str, new: str) -> None:
    number = int(new.rsplit("dev", 1)[1])
    assert bump_version.dev_version("1.2.3", level, number) == new


def test_dev_version_refuses_a_negative_number() -> None:
    with pytest.raises(ValueError, match="can't be negative"):
        bump_version.dev_version("1.2.3", "patch", -1)


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "uv.lock").write_text(LOCK)
    return tmp_path


def test_bump(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert bump_version.run(["minor"]) == 0
    assert capsys.readouterr().out == "Bumped the version from 1.2.3 to 1.3.0 in pyproject.toml, uv.lock\n"
    assert (cwd / "pyproject.toml").read_text() == PYPROJECT.replace('version = "1.2.3"\ndep', 'version = "1.3.0"\ndep')
    assert (cwd / "uv.lock").read_text() == LOCK.replace('my-pkg"\nversion = "1.2.3"', 'my-pkg"\nversion = "1.3.0"')


def test_bump_dev(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert bump_version.run(["patch", "--dev", "41908943359"]) == 0
    new = "1.2.4.dev41908943359"
    assert capsys.readouterr().out == f"Bumped the version from 1.2.3 to {new} in pyproject.toml, uv.lock\n"
    assert (cwd / "pyproject.toml").read_text() == PYPROJECT.replace(
        'version = "1.2.3"\ndep', f'version = "{new}"\ndep'
    )
    assert (cwd / "uv.lock").read_text() == LOCK.replace('my-pkg"\nversion = "1.2.3"', f'my-pkg"\nversion = "{new}"')


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["patch", "--dev", "-1"], "isn't a dev number"),
        (["patch", "--dev", "x"], "isn't a dev number"),
        (["patch", "--dev", "1", "--pr"], "--dev can't be used with --pr"),
    ],
)
def test_bump_dev_refuses(args: list[str], error: str, cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        bump_version.run(args)
    assert raised.value.code == 2
    assert error in capsys.readouterr().err
    assert (cwd / "pyproject.toml").read_text() == PYPROJECT


def test_bump_keeps_line_endings_and_quotes(cwd: Path) -> None:
    (cwd / "pyproject.toml").write_bytes(b"[project]\r\nname = 'p'\r\nversion = '0.1.0'\r\n")
    (cwd / "uv.lock").unlink()
    assert bump_version.run(["patch"]) == 0
    assert (cwd / "pyproject.toml").read_bytes() == b"[project]\r\nname = 'p'\r\nversion = '0.1.1'\r\n"
    assert not (cwd / "uv.lock").exists()


def test_bump_virtual_project(cwd: Path) -> None:
    (cwd / "uv.lock").write_text(LOCK.replace('{ editable = "." }', '{ virtual = "." }'))
    assert bump_version.run(["patch"]) == 0
    assert 'name = "my-pkg"\nversion = "1.2.4"\nsource = { virtual' in (cwd / "uv.lock").read_text()


@pytest.mark.parametrize(
    ("pyproject", "lock", "error"),
    [
        ('[project]\nname = "p"\ndynamic = ["version"]\n', None, "must be a string, and not dynamic"),
        ('[tool.x]\nversion = "1.0.0"\n', None, "has no [project] table"),
        ('[project]\nversion = "1.0.0"\n', None, "[project] name must be a string"),
        ('[project]\nname = "p"\nversion = "1.0"\n', None, "isn't of the form X.Y.Z"),
        # A version this can't find as a line of its own.
        ('project = { name = "p", version = "1.0.0" }\n', None, "has 0 lines that set it to 1.0.0"),
        (PYPROJECT, LOCK.replace('my-pkg"\nversion = "1.2.3"', 'my-pkg"\nversion = "1.2.2"'), "doesn't lock My_Pkg"),
        (PYPROJECT, LOCK.replace('{ editable = "." }', '{ editable = "sub" }'), "doesn't lock My_Pkg 1.2.3"),
        (PYPROJECT, "not toml", "uv.lock isn't a valid lock file"),
        ("[project", None, "pyproject.toml: "),
    ],
)
def test_bump_refuses(
    cwd: Path, capsys: pytest.CaptureFixture[str], pyproject: str, lock: str | None, error: str
) -> None:
    (cwd / "pyproject.toml").write_text(pyproject)
    (cwd / "uv.lock").write_text(LOCK if lock is None else lock)
    assert bump_version.run(["patch"]) == 1
    assert error in capsys.readouterr().err
    # It wrote nothing.
    assert (cwd / "pyproject.toml").read_text() == pyproject
    assert (cwd / "uv.lock").read_text() == (LOCK if lock is None else lock)


def test_bump_puts_files_back_if_a_write_fails(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write = bump_version._write

    def fail_on_lock(path: Path, text: str) -> None:
        if path.name == "uv.lock" and "1.2.4" in text:
            raise OSError("disk full")
        write(path, text)

    monkeypatch.setattr(bump_version, "_write", fail_on_lock)
    assert bump_version.run(["patch"]) == 1
    assert "disk full" in capsys.readouterr().err
    assert (cwd / "pyproject.toml").read_text() == PYPROJECT
    assert (cwd / "uv.lock").read_text() == LOCK


def test_bump_puts_back_what_it_can(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write = bump_version._write
    pyproject_writes: list[str] = []

    def fail(path: Path, text: str) -> None:
        if path.name == "pyproject.toml":
            pyproject_writes.append(text)
            # The bump goes through, but putting the file back fails.
            if len(pyproject_writes) == 2:
                raise OSError("read-only")
        elif "1.2.4" in text:
            # A write that fails part way leaves the file cut short.
            write(path, text[:10])
            raise OSError("disk full")
        write(path, text)

    monkeypatch.setattr(bump_version, "_write", fail)
    assert bump_version.run(["patch"]) == 1
    err = capsys.readouterr().err
    assert "Couldn't put back pyproject.toml: read-only" in err
    assert err.endswith("disk full\n")
    assert (cwd / "uv.lock").read_text() == LOCK


def test_auto_merge_needs_pr(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        bump_version.run(["patch", "--auto-merge", "squash"])
    assert raised.value.code == 2
    assert "--auto-merge needs --pr" in capsys.readouterr().err


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """cwd committed on main, with a bare repository as its origin."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    for name in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{name}_NAME", "a")
        monkeypatch.setenv(f"{name}_EMAIL", "a@b")
    origin = cwd.parent / f"{cwd.name}-origin.git"
    _git(cwd.parent, "init", "--quiet", "--bare", str(origin))
    _git(cwd, "init", "--quiet", "--initial-branch=main")
    _git(cwd, "remote", "add", "origin", str(origin))
    _git(cwd, "add", "--all")
    _git(cwd, "commit", "--quiet", "--message", "start")
    return cwd


class FakeGitHub:
    def __init__(self, *, open_pulls: list[dict[str, Any]] | None = None, default: str = "main") -> None:
        self.open_pulls = open_pulls or []
        self.default = default
        self.created: list[Any] = []
        self.mutations: list[dict[str, Any]] = []
        self.auto_merge_error: Exception | None = None

    def request(self, method: str, url: str, token: str, *, json_body: Any = None) -> Any:
        assert token == "tok"
        if url == f"{API}/repos/o/r/pulls?state=open&head=o%3Arelease-version-1.2.4":
            return self.open_pulls
        if url == f"{API}/repos/o/r":
            return {"default_branch": self.default}
        if (method, url) == ("POST", f"{API}/repos/o/r/pulls"):
            self.created.append(json_body)
            return {"html_url": "https://github.com/o/r/pull/9"}
        if (method, url) == ("GET", f"{API}/repos/o/r/pulls/9"):
            return {"node_id": "PR_9"}
        raise AssertionError(f"unexpected {method} {url}")

    def graphql(self, query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
        assert "enablePullRequestAutoMerge" in query
        if self.auto_merge_error:
            raise self.auto_merge_error
        self.mutations.append(variables)
        return {}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "request", fake.request)
    monkeypatch.setattr(github, "graphql", fake.graphql)
    return fake


def test_bump_asks_github_nothing(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("asked GitHub or gh")

    for name in ("request", "graphql", "gh_output"):
        monkeypatch.setattr(github, name, unexpected)
    assert bump_version.run(["patch"]) == 0


def test_bump_pr(project: Path, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    assert bump_version.run(["patch", "--pr", "--repo", "o/r", "--auto-merge", "squash"]) == 0
    assert capsys.readouterr().out == (
        "Bumped the version from 1.2.3 to 1.2.4 in pyproject.toml, uv.lock\n"
        "Opened https://github.com/o/r/pull/9\n"
        "Turned on auto-merge (squash)\n"
    )
    assert fake.created == [
        {
            "title": "Release version 1.2.4",
            "head": "release-version-1.2.4",
            "base": "main",
            "body": "Bumps the version from 1.2.3 to 1.2.4 (patch bump).\n\n"
            "Changes `pyproject.toml`, `uv.lock`.\n\n"
            "Opened by `repoutils bump-version --pr`.",
        }
    ]
    assert fake.mutations == [{"id": "PR_9", "method": "SQUASH"}]
    assert _git(project, "log", "-1", "--format=%B", "origin/release-version-1.2.4") == (
        "Bump version to 1.2.4\n\nBumps the version from 1.2.3 to 1.2.4 (patch bump)."
    )
    assert _git(project, "diff", "--name-only", "main", "origin/release-version-1.2.4").splitlines() == [
        "pyproject.toml",
        "uv.lock",
    ]
    assert _git(project, "branch", "--show-current") == "main"
    assert not _git(project, "status", "--porcelain")


def test_bump_pr_without_auto_merge(project: Path, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    assert bump_version.run(["patch", "--pr", "--repo", "o/r"]) == 0
    assert capsys.readouterr().out.endswith("Opened https://github.com/o/r/pull/9\n")
    assert not fake.mutations


def test_bump_pr_already_open(project: Path, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.open_pulls = [{"html_url": "https://github.com/o/r/pull/8"}]
    assert bump_version.run(["patch", "--pr", "--repo", "o/r"]) == 0
    assert capsys.readouterr().out == "Pull request https://github.com/o/r/pull/8 already bumps the version to 1.2.4\n"
    assert not fake.created
    assert not _git(project, "status", "--porcelain")


def test_bump_pr_refuses_another_branch(project: Path, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.default = "master"
    assert bump_version.run(["patch", "--pr", "--repo", "o/r"]) == 1
    assert "The checkout is on main, not o/r's default branch, master" in capsys.readouterr().err
    assert not fake.created
    assert not _git(project, "status", "--porcelain")


def test_bump_pr_auto_merge_failure(project: Path, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.auto_merge_error = urllib.error.HTTPError("u", 403, "Forbidden", Message(), None)
    assert bump_version.run(["patch", "--pr", "--repo", "o/r", "--auto-merge", "merge"]) == 1
    assert (
        "Opened https://github.com/o/r/pull/9, but turning on its auto-merge failed: HTTP Error 403: Forbidden"
        in capsys.readouterr().err
    )
    assert fake.created


def test_bump_pr_uses_gh(project: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    answers = {"auth": "tok", "repo": "o/r"}
    monkeypatch.setattr(github, "gh_output", lambda *args, **kwargs: answers[args[0]])
    assert bump_version.run(["patch", "--pr"]) == 0
    assert fake.created
