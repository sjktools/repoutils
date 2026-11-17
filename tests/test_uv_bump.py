import subprocess
from pathlib import Path
from typing import Any

import pytest

from repoutils import uv_bump

WORKFLOW = """\
jobs:
  build:
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.13.6"  # keep in step
          enable-cache: false
      - uses: extractions/setup-just@v3
        with:
          just-version: '1.58.0'
      - run: echo version: "0.13.6"
"""
PYPROJECT = """\
[project]
name = "p"

[tool.uv]
required-version = ">=0.13.6, <0.14"
"""
DOCKERFILE = "FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.13.6 AS uv\nFROM python:3.12\n"


def make_project(root: Path, *, dockerfile: str | None = DOCKERFILE) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    (root / ".github" / "workflows" / "other.yml").write_text("jobs:\n  x:\n    steps:\n      - run: echo\n")
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "justfile").write_text("set minimum-version := '1.58.0'\n")
    if dockerfile is not None:
        (root / "Dockerfile").write_text(dockerfile)
    return root


def test_version_key() -> None:
    assert uv_bump.version_key("0.13.10") > uv_bump.version_key("0.13.9")
    for version in ("v1", "0.13", "0.13.7.1"):
        with pytest.raises(ValueError, match=f"uv {version} isn't a version"):
            uv_bump.version_key(version)


def test_bump(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    changed, relocked = uv_bump.bump(root, "0.13.6", "0.13.7")
    assert not relocked
    assert changed == sorted([root / "pyproject.toml", root / ".github/workflows/ci.yml", root / "Dockerfile"])
    assert 'required-version = ">=0.13.7, <0.14"' in (root / "pyproject.toml").read_text()
    # Only the setup-uv input changes, keeping its quotes and comment.
    assert (root / ".github/workflows/ci.yml").read_text() == WORKFLOW.replace(
        'version: "0.13.6"  #', 'version: "0.13.7"  #'
    )
    assert (root / "Dockerfile").read_text() == DOCKERFILE.replace("uv:0.13.6", "uv:0.13.7")


def test_bump_without_dockerfile(tmp_path: Path) -> None:
    root = make_project(tmp_path, dockerfile=None)
    assert root / "Dockerfile" not in uv_bump.bump(root, "0.13.6", "0.13.7")[0]


def test_bump_refuses_when_check_pins_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "Dockerfile").write_text(DOCKERFILE.replace("0.13.6", "0.13.5"))
    with pytest.raises(RuntimeError, match=r"check-pins fails, so fix these first:\nDockerfile: FROM"):
        uv_bump.bump(root, "0.13.6", "0.13.7")
    assert (root / "pyproject.toml").read_text() == PYPROJECT


def test_bump_refuses_a_dockerfile_digest(tmp_path: Path) -> None:
    root = make_project(tmp_path, dockerfile="FROM ghcr.io/astral-sh/uv:0.13.6@sha256:abc AS uv\n")
    with pytest.raises(RuntimeError, match="by digest"):
        uv_bump.bump(root, "0.13.6", "0.13.7")
    assert (root / "pyproject.toml").read_text() == PYPROJECT


def test_bump_refuses_a_second_required_version(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "pyproject.toml").write_text(PYPROJECT + '\n[tool.other]\nrequired-version = ">=0.13.6"\n')
    with pytest.raises(RuntimeError, match=r"has 2 `required-version"):
        uv_bump.bump(root, "0.13.6", "0.13.7")


def test_bump_puts_files_back_if_check_pins_then_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    # The bump finds [tool.other]'s, as it doesn't read [tool.uv]'s multi-line string.
    (root / "pyproject.toml").write_text(
        "[tool.other]\nrequired-version = '>=0.13.6'\n\n[tool.uv]\nrequired-version = '''>=0.13.6'''\n"
    )
    before = {path: path.read_text() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(RuntimeError, match=r"After the bump, \[tool.uv\] required-version's minimum is 0\.13\.6"):
        uv_bump.bump(root, "0.13.6", "0.13.7")
    assert {path: path.read_text() for path in root.rglob("*") if path.is_file()} == before


def test_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\nrevision = 2\n")
    calls: list[tuple[list[str], Any]] = []

    def fake_run(args: list[str], *, cwd: str, check: bool) -> None:
        calls.append((args, cwd))
        assert check
        assert Path(cwd) != tmp_path
        (tmp_path / "uv.lock").write_text("version = 1\nrevision = 3\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert uv_bump.lock(tmp_path, "0.13.7")
    assert calls[0][0] == ["uv", "tool", "run", "uv@0.13.7", "lock", "--directory", str(tmp_path.resolve())]


def test_lock_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    assert not uv_bump.lock(tmp_path, "0.13.7")


def test_lock_without_lockfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("ran uv")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert not uv_bump.lock(tmp_path, "0.13.7")
