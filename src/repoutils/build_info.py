"""The version of repoutils that is running, and the commit it comes from.

A build has the commit in ``build_info.json``, which ``build_info_hook`` writes,
and the version in its package metadata. Without the file, repoutils is
running from a source checkout, such as the justfile's ``python -m repoutils``
from ``src/`` or an editable install. The version then comes from the
checkout's ``pyproject.toml``, and the commit from Git. If there is no
checkout either, it's a build that didn't run the hook, with only a version.
"""

import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

from repoutils.checkout import head_commit

FILE = "build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    version: str | None
    commit: str | None
    # Whether a file differed from the commit, or None if that's unknown.
    dirty: bool | None
    # The source checkout repoutils runs from, or None for a build.
    checkout: Path | None


def read() -> BuildInfo:
    try:
        built = json.loads(files("repoutils").joinpath(FILE).read_text())
    except FileNotFoundError:
        return _from_checkout(Path(__file__).resolve().parents[2])
    return BuildInfo(_installed_version(), built["commit"], built["dirty"], None)


def _from_checkout(root: Path) -> BuildInfo:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        project = None
    if not isinstance(project, dict) or project.get("name") != "repoutils":
        # Neither a build nor a checkout of repoutils, such as a build by a
        # build backend that didn't run the hook.
        return BuildInfo(_installed_version(), None, None, None)
    project_version = project.get("version")
    head = head_commit(root)
    commit, dirty = head if head is not None else (None, None)
    return BuildInfo(project_version if isinstance(project_version, str) else None, commit, dirty, root)


def _installed_version() -> str | None:
    try:
        return version("repoutils")
    except PackageNotFoundError:
        return None


def short(info: BuildInfo) -> str:
    """One line, as ``repoutils --version`` prints it."""
    line = f"repoutils {info.version or 'unknown'}"
    if info.commit is not None:
        line += f" ({info.commit[:12]}{' with uncommitted changes' if info.dirty else ''})"
    return line
