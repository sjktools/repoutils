import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from repoutils import build_info
from repoutils.build_info import BuildInfo
from repoutils.commands import version

ROOT = Path(__file__).parents[1]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _checkout_version() -> str:
    result: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return result


def test_from_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tests run repoutils editable, which has no build_info.json.
    monkeypatch.setattr(build_info, "head_commit", lambda root: (COMMIT, False) if root == ROOT else None)
    assert build_info.read() == BuildInfo(_checkout_version(), COMMIT, False, ROOT)


def test_from_a_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / build_info.FILE).write_text(f'{{"commit": "{COMMIT}", "dirty": true}}')
    monkeypatch.setattr(build_info, "files", lambda package: tmp_path)
    monkeypatch.setattr(build_info, "version", lambda name: "9.9")
    assert build_info.read() == BuildInfo("9.9", COMMIT, True, None)


def test_from_a_build_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / build_info.FILE).write_text('{"commit": null, "dirty": null}')
    monkeypatch.setattr(build_info, "files", lambda package: tmp_path)
    assert build_info.read() == BuildInfo(build_info._installed_version(), None, None, None)


@pytest.mark.parametrize(
    "pyproject", [None, "not toml [", "[tool.x]\na = 1\n", '[project]\nname = "other"\nversion = "1.0"\n']
)
def test_neither_a_build_nor_a_checkout_of_repoutils(tmp_path: Path, pyproject: str | None) -> None:
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    assert build_info._from_checkout(tmp_path) == BuildInfo(build_info._installed_version(), None, None, None)


def test_a_checkout_outside_git_with_no_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "repoutils"\ndynamic = ["version"]\n')
    assert build_info._from_checkout(tmp_path) == BuildInfo(None, None, None, tmp_path)


def test_installed_version_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(build_info, "version", missing)
    assert build_info._installed_version() is None


@pytest.mark.parametrize(
    ("info", "line"),
    [
        (BuildInfo("1.0", COMMIT, False, None), "repoutils 1.0 (0123456789ab)"),
        (BuildInfo("1.0", COMMIT, True, None), "repoutils 1.0 (0123456789ab with uncommitted changes)"),
        (BuildInfo(None, None, None, None), "repoutils unknown"),
    ],
)
def test_short(info: BuildInfo, line: str) -> None:
    assert build_info.short(info) == line


def test_short_with_another_name() -> None:
    assert build_info.short(BuildInfo("1.0", COMMIT, False, None), "exolot") == "exolot 1.0 (0123456789ab)"


@pytest.mark.parametrize(("dirty", "changes"), [(None, "unknown"), (True, "yes"), (False, "no")])
def test_describe_a_build(dirty: bool | None, changes: str) -> None:
    lines = version.describe(BuildInfo("1.0", COMMIT if dirty is not None else None, dirty, None))
    assert lines[:3] == [
        "Version: 1.0",
        f"Commit: {COMMIT if dirty is not None else 'unknown'}",
        f"Uncommitted changes: {changes}",
    ]
    assert lines[3] == f"Installed in: {ROOT / 'src' / 'repoutils'}"
    assert lines[4].startswith("Python: ") and sys.executable in lines[4]
    assert len(lines) == 5


@pytest.mark.parametrize(("commit", "source"), [(COMMIT, "Git"), (None, "Git, which gave none")])
def test_describe_a_checkout(commit: str | None, source: str) -> None:
    lines = version.describe(BuildInfo(None, commit, None, ROOT))
    assert lines[0] == "Version: unknown"
    assert lines[-1] == (
        f"Running from the source checkout in {ROOT},"
        f" so the version is from its pyproject.toml and the commit from {source}."
    )


def test_version_command_from_this_checkout(capsys: pytest.CaptureFixture[str]) -> None:
    assert version.run([]) == 0
    out = capsys.readouterr().out
    assert f"Version: {_checkout_version()}\n" in out
    assert f"Running from the source checkout in {ROOT}" in out


def test_version_from_an_installed_build(tmp_path: Path) -> None:
    # Runs the wheel of a real build, so the hook and the reader agree on the file.
    subprocess.run(["uv", "build", "--wheel", "--quiet", "--out-dir", str(tmp_path), str(ROOT)], check=True)
    (wheel,) = tmp_path.glob("repoutils-*.whl")
    # -I and -S keep out this checkout and the editable install, so only the wheel has repoutils.
    code = (
        f"import sys; sys.path.insert(0, {str(wheel)!r}); from repoutils.cli import main; sys.exit(main(['version']))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    assert f"Commit: {commit.strip()}\n" in result.stdout
    assert f"Version: {_checkout_version()}\n" in result.stdout
    assert "source checkout" not in result.stdout
