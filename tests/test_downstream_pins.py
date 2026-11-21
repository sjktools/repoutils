import subprocess
import tomllib
from pathlib import Path

import pytest

from repoutils import downstream_pins
from repoutils.downstream_pins import Pin

PYPROJECT = """\
[build-system]
requires = ["hatchling", "Rep.Utils==0.3.0"]

[project]
name = "down"
dependencies = ["other>=1.0", 'rep-utils[x] >= 0.3.0, <1 ; python_version >= "3.12"']

[project.optional-dependencies]
extra = ["rep-utils>=0.2"]

[dependency-groups]
dev = ["rep-utils>=0.3.0", {include-group = "other"}]
other = ["rep-utils==0.3.0", "rep-utils"]

[tool.notes]
copy = ["rep-utils>=0.2"]

[tool.uv]
required-version = ">=0.13.6"
"""


def lock_text(version: str, name: str = "rep-utils") -> str:
    return f'version = 1\n\n[[package]]\nname = "{name}"\nversion = "{version}"\n'


def test_release_key() -> None:
    assert downstream_pins.release_key("1.0") == downstream_pins.release_key("1.0.0") == (1,)
    assert downstream_pins.release_key("0.10.0") > downstream_pins.release_key("0.9.1")
    assert downstream_pins.release_key("0") == (0,)
    for version in ("v1", "1.0rc1", "1.*", ""):
        with pytest.raises(ValueError, match="isn't a release version"):
            downstream_pins.release_key(version)


def test_pins() -> None:
    found = downstream_pins.pins(tomllib.loads(PYPROJECT), "rep-utils")
    assert found == [
        Pin("build-system.requires", "Rep.Utils==0.3.0", "==", "0.3.0"),
        Pin("project.dependencies", 'rep-utils[x] >= 0.3.0, <1 ; python_version >= "3.12"', ">=", "0.3.0"),
        Pin("project.optional-dependencies.extra", "rep-utils>=0.2", ">=", "0.2"),
        Pin("dependency-groups.dev", "rep-utils>=0.3.0", ">=", "0.3.0"),
    ]
    assert found[1].moved("0.4.0") == 'rep-utils[x] >= 0.4.0, <1 ; python_version >= "3.12"'


@pytest.mark.parametrize(
    ("requires", "expected"),
    [({"build-system": "x"}, []), ({"build-system": {"requires": "x"}}, []), ({}, [])],
)
def test_pins_skip_malformed_build_system(requires: dict[str, object], expected: list[Pin]) -> None:
    assert downstream_pins.pins(requires, "rep-utils") == expected


def test_pins_ignore_arbitrary_equality() -> None:
    assert downstream_pins.pins({"build-system": {"requires": ["rep-utils===0.3.0"]}}, "rep-utils") == []


def test_stale_and_move(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    moving = downstream_pins.stale(tmp_path, "rep-utils", "0.4")
    assert [pin.where for pin in moving] == [
        "build-system.requires",
        "project.dependencies",
        "project.optional-dependencies.extra",
        "dependency-groups.dev",
    ]
    downstream_pins.move(tmp_path, "rep-utils", "0.4", moving)
    text = (tmp_path / "pyproject.toml").read_text()
    assert text == (
        PYPROJECT.replace("Rep.Utils==0.3.0", "Rep.Utils==0.4")
        .replace("rep-utils[x] >= 0.3.0", "rep-utils[x] >= 0.4")
        .replace('extra = ["rep-utils>=0.2"]', 'extra = ["rep-utils>=0.4"]')
        .replace('dev = ["rep-utils>=0.3.0"', 'dev = ["rep-utils>=0.4"')
    )
    # The same string elsewhere in the file is left alone.
    assert 'copy = ["rep-utils>=0.2"]' in text
    assert downstream_pins.stale(tmp_path, "rep-utils", "0.4.0") == []


def test_move_refuses_a_change_elsewhere(tmp_path: Path) -> None:
    # The [tool.notes] string comes first, so replacing the first match would change it instead.
    text = '[tool.notes]\ncopy = ["rep-utils>=0.2"]\n\n[project]\ndependencies = ["rep-utils>=0.2"]\n'
    (tmp_path / "pyproject.toml").write_text(text)
    moving = downstream_pins.stale(tmp_path, "rep-utils", "0.4")
    with pytest.raises(RuntimeError, match="would change more than them"):
        downstream_pins.move(tmp_path, "rep-utils", "0.4", moving)
    assert (tmp_path / "pyproject.toml").read_text() == text


def test_move_refuses_a_missing_requirement(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["rep-utils>=0.2"]\n')
    with pytest.raises(RuntimeError, match="Couldn't find 'rep-utils>=0.1'"):
        downstream_pins.move(tmp_path, "rep-utils", "0.4", [Pin("x", "rep-utils>=0.1", ">=", "0.1")])


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('[project]\ndependencies = ["other>=1"]\n', "doesn't pin rep-utils"),
        ('[project]\ndependencies = ["rep-utils>=0.5"]\n', "newer than 0.4, and bump never lowers a pin"),
        ('[project]\ndependencies = ["rep-utils>=0.5rc1"]\n', "which bump can't move"),
    ],
)
def test_stale_refuses(tmp_path: Path, text: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(text)
    with pytest.raises(RuntimeError, match=error):
        downstream_pins.stale(tmp_path, "rep-utils", "0.4")


def test_check_locked() -> None:
    downstream_pins.check_locked(lock_text("0.4.0"), "rep-utils", "0.4", "uv.lock")
    # A downstream that only builds with the package doesn't lock it.
    downstream_pins.check_locked(lock_text("1.0", "other"), "rep-utils", "0.4", "uv.lock")
    with pytest.raises(RuntimeError, match=r"uv.lock locks rep-utils at 0.3.0, not 0.4"):
        downstream_pins.check_locked(lock_text("0.3.0"), "rep-utils", "0.4", "uv.lock")
    with pytest.raises(RuntimeError, match="F isn't a valid lock file"):
        downstream_pins.check_locked("[[package", "rep-utils", "0.4", "F")


def test_uv_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.13.7, <0.14"\n')
    assert downstream_pins.uv_command(tmp_path) == ["uv", "tool", "run", "uv@0.13.7"]
    for text in ("", "tool = 1\n", "[tool]\nuv = 1\n", "[tool.uv]\n"):
        (tmp_path / "pyproject.toml").write_text(text)
        assert downstream_pins.uv_command(tmp_path) == ["uv"]


class FakeUv:
    """Stands in for `uv lock`, failing the first `failures` times."""

    def __init__(self, root: Path, failures: int, version: str = "0.4.0") -> None:
        self.root, self.failures, self.version = root, failures, version
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, cwd: str, check: bool) -> subprocess.CompletedProcess[bytes]:
        assert not check
        assert not Path(cwd).is_relative_to(self.root)
        self.commands.append(command)
        if len(self.commands) <= self.failures:
            return subprocess.CompletedProcess(command, 1)
        (self.root / "uv.lock").write_text(lock_text(self.version))
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.13.7"\n')
    (tmp_path / "uv.lock").write_text(lock_text("0.3.0"))
    monkeypatch.setattr(downstream_pins, "LOCK_DELAY", 0.0)
    return tmp_path


def test_lock_retries(locked: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeUv(locked, failures=2)
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert downstream_pins.lock(locked, "rep-utils", "0.4.0")
    assert len(fake.commands) == 3
    assert fake.commands[0] == [
        "uv",
        "tool",
        "run",
        "uv@0.13.7",
        "lock",
        "--directory",
        str(locked.resolve()),
        "--upgrade-package",
        "rep-utils==0.4.0",
        "--refresh-package",
        "rep-utils",
    ]
    assert capsys.readouterr().err.count("uv lock failed, so trying again in 0s") == 2


def test_lock_gives_up(locked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeUv(locked, failures=downstream_pins.LOCK_ATTEMPTS)
    monkeypatch.setattr(subprocess, "run", fake.run)
    with pytest.raises(subprocess.CalledProcessError):
        downstream_pins.lock(locked, "rep-utils", "0.4.0")
    assert len(fake.commands) == downstream_pins.LOCK_ATTEMPTS


def test_lock_checks_the_version(locked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeUv(locked, failures=0, version="0.5.0").run)
    with pytest.raises(RuntimeError, match="locks rep-utils at 0.5.0, not 0.4.0"):
        downstream_pins.lock(locked, "rep-utils", "0.4.0")


def test_lock_unchanged_and_no_lock(locked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (locked / "uv.lock").write_text(lock_text("0.4.0"))
    monkeypatch.setattr(subprocess, "run", FakeUv(locked, failures=0).run)
    assert not downstream_pins.lock(locked, "rep-utils", "0.4.0")
    (locked / "uv.lock").unlink()
    assert not downstream_pins.lock(locked, "rep-utils", "0.4.0")
    assert not (locked / "uv.lock").exists()


def test_pins_in_parentheses(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["rep-utils (>=0.3)"]\n')
    moving = downstream_pins.stale(tmp_path, "rep-utils", "0.4")
    assert moving == [Pin("project.dependencies", "rep-utils (>=0.3)", ">=", "0.3")]
    downstream_pins.move(tmp_path, "rep-utils", "0.4", moving)
    assert '"rep-utils (>=0.4)"' in (tmp_path / "pyproject.toml").read_text()


@pytest.mark.parametrize(
    ("requirement", "clause"),
    [("rep-utils>=0.3, <0.4", "<0.4"), ("rep-utils (>=0.3,!=0.4)", "!=0.4")],
)
def test_stale_refuses_a_bound_the_version_crosses(tmp_path: Path, requirement: str, clause: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[project]\ndependencies = ["{requirement}"]\n')
    with pytest.raises(RuntimeError, match=f"whose `{clause}` excludes 0.4, so bump it by hand"):
        downstream_pins.stale(tmp_path, "rep-utils", "0.4")


def test_stale_leaves_unreadable_clauses_to_uv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["rep-utils>=0.3, <1, ~=0.3, !=0.3.*"]\n')
    assert len(downstream_pins.stale(tmp_path, "rep-utils", "0.4")) == 1


def test_move_keeps_crlf_and_escapes(tmp_path: Path) -> None:
    text = '[project]\r\ndependencies = ["rep-utils>=0.3; python_version >= \\"3.12\\""]\r\n'
    (tmp_path / "pyproject.toml").write_bytes(text.encode())
    downstream_pins.move(tmp_path, "rep-utils", "0.4", downstream_pins.stale(tmp_path, "rep-utils", "0.4"))
    assert (tmp_path / "pyproject.toml").read_bytes() == text.replace(">=0.3;", ">=0.4;").encode()


def test_depends(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    assert downstream_pins.depends(tmp_path, "Rep_Utils")
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["rep-utils==0.3"]\n[project]\ndependencies = ["other", {x = 1}]\n'
    )
    assert not downstream_pins.depends(tmp_path, "rep-utils")


def test_check_locked_required_and_malformed() -> None:
    with pytest.raises(RuntimeError, match="uv.lock doesn't lock rep-utils, which the project depends on"):
        downstream_pins.check_locked(lock_text("1.0", "other"), "rep-utils", "0.4", "uv.lock", required=True)
    for text in ("package = 1\n", "package = [1]\n"):
        with pytest.raises(RuntimeError, match="F isn't a valid lock file: its package entries aren't"):
            downstream_pins.check_locked(text, "rep-utils", "0.4", "F")
