"""Builds a small project with hatchling and the build hook, in each of the
ways a build can meet Git."""

import json
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

from repoutils import build_info_hook

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "src" / "repoutils" / "build_info_hook.py"
FILE = "src/example/build_info.json"


def _project(directory: Path, file: str | None = FILE) -> Path:
    option = f'file = "{file}"\n' if file is not None else ""
    (directory / "src" / "example").mkdir(parents=True)
    (directory / "src" / "example" / "__init__.py").write_text("")
    (directory / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.0"\n\n'
        f"[tool.hatch.build.hooks.custom]\npath = {json.dumps(str(HOOK))}\n{option}\n"
        # An editable install by a .pth file, without the editables package.
        '[tool.hatch.build.targets.wheel]\ndev-mode-dirs = ["src"]\n'
    )
    return directory


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(directory: Path) -> Path:
    _git(directory, "init", "-q")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "commit")
    return directory


def _wheel(project: Path, out: Path, version: str = "standard") -> Path:
    return Path(next(WheelBuilder(str(project)).build(directory=str(out), versions=[version])))


def _sdist(project: Path, out: Path) -> Path:
    return Path(next(SdistBuilder(str(project)).build(directory=str(out), versions=["standard"])))


def _from_wheel(wheel: Path) -> dict[str, Any] | None:
    with zipfile.ZipFile(wheel) as archive:
        if "example/build_info.json" not in archive.namelist():
            return None
        result: dict[str, Any] = json.loads(archive.read("example/build_info.json"))
        return result


def test_a_git_checkout_records_its_commit(tmp_path: Path) -> None:
    project = _repo(_project(tmp_path / "project"))
    head = _git(project, "rev-parse", "HEAD")
    assert _from_wheel(_wheel(project, tmp_path / "dist")) == {"commit": head, "dirty": False}
    with tarfile.open(_sdist(project, tmp_path / "dist")) as archive:
        member = archive.extractfile(f"example-1.0/{FILE}")
        assert member is not None
        assert json.loads(member.read()) == {"commit": head, "dirty": False}
    # The hook wrote nothing into the project.
    assert not (project / FILE).exists()
    assert _git(project, "status", "--porcelain", "--ignored") == ""


def test_changes_and_untracked_files_are_dirty(tmp_path: Path) -> None:
    project = _repo(_project(tmp_path / "project"))
    (project / "src" / "example" / "new.py").write_text("")
    info = _from_wheel(_wheel(project, tmp_path / "dist"))
    assert info is not None and info["dirty"] is True


def test_changes_outside_the_project_are_not(tmp_path: Path) -> None:
    project = _project(tmp_path / "repo" / "project")
    _repo(tmp_path / "repo")
    (tmp_path / "repo" / "elsewhere.txt").write_text("")
    info = _from_wheel(_wheel(project, tmp_path / "dist"))
    assert info is not None and info["dirty"] is False


def test_an_unpacked_sdist_reuses_its_file_without_running_git(tmp_path: Path) -> None:
    project = _repo(_project(tmp_path / "project"))
    head = _git(project, "rev-parse", "HEAD")
    sdist = _sdist(project, tmp_path / "dist")
    # Unpacked inside another repository, whose commit must not be recorded.
    other = _repo(_project(tmp_path / "other"))
    (project / "src" / "example" / "__init__.py").write_text("changed = True\n")
    _git(project, "commit", "-qam", "another")
    with tarfile.open(sdist) as archive:
        archive.extractall(other / "unpacked", filter="data")
    unpacked = other / "unpacked" / "example-1.0"
    assert _from_wheel(_wheel(unpacked, tmp_path / "wheel")) == {"commit": head, "dirty": False}


def test_an_unpacked_sdist_without_the_file_gets_none(tmp_path: Path) -> None:
    project = _repo(_project(tmp_path / "project"))
    (project / "PKG-INFO").write_text("Metadata-Version: 2.4\nName: example\nVersion: 1.0\n")
    assert _from_wheel(_wheel(project, tmp_path / "dist")) is None


def test_an_editable_build_gets_none(tmp_path: Path) -> None:
    project = _repo(_project(tmp_path / "project"))
    wheel = _wheel(project, tmp_path / "dist", "editable")
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.endswith("build_info.json") for name in archive.namelist())


def test_without_git_the_commit_is_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    assert _from_wheel(_wheel(project, tmp_path / "dist")) == {"commit": None, "dirty": None}


def test_the_hook_cleans_up_its_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tempfile caches its directory, so TMPDIR may be too late.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    project = _repo(_project(tmp_path / "project"))
    _wheel(project, tmp_path / "dist")
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.parametrize("file", [None, ""])
def test_the_file_option_is_required(tmp_path: Path, file: str | None) -> None:
    project = _repo(_project(tmp_path / "project", file))
    with pytest.raises(ValueError, match=r"\[tool.hatch.build.hooks.custom\] file must be"):
        _wheel(project, tmp_path / "dist")


def test_the_entry_point_registers_the_hook() -> None:
    entry_points = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["entry-points"]["hatch"]
    assert entry_points == {build_info_hook.PLUGIN_NAME: "repoutils.build_info_hook"}
    assert build_info_hook.hatch_register_build_hook() is build_info_hook.BuildInfoHook
    assert build_info_hook.BuildInfoHook.PLUGIN_NAME == "repoutils-build-info"


def test_another_project_enables_it_through_the_entry_point(tmp_path: Path) -> None:
    # As exolot does, with repoutils in its build-system.requires.
    project = tmp_path / "project"
    _project(project)
    pyproject = (project / "pyproject.toml").read_text()
    custom = f"[tool.hatch.build.hooks.custom]\npath = {json.dumps(str(HOOK))}\n"
    assert custom in pyproject
    (project / "pyproject.toml").write_text(
        pyproject.replace(custom, "[tool.hatch.build.hooks.repoutils-build-info]\n")
    )
    _repo(project)
    head = _git(project, "rev-parse", "HEAD")
    assert _from_wheel(_wheel(project, tmp_path / "dist")) == {"commit": head, "dirty": False}
