"""The version of repoutils that is running, and the commit it comes from.

A build has the commit in ``build_info.json``, which ``build_info_hook`` writes,
and the version in its package metadata. Without the file, repoutils is
running from a source checkout, such as the justfile's ``python -m repoutils``
from ``src/`` or an editable install. The version then comes from the
checkout's ``pyproject.toml``, and the commit and nearest ``v`` tag from Git.
If there is no checkout either, it's a build that didn't run the hook, with
only a version.

A build is a release if it's at a tag with no uncommitted changes. A build
from a later commit has the release's version too, so only the distance from
the tag tells them apart.
"""

import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

from repoutils.checkout import head_commit, nearest_tag

FILE = "build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    version: str | None
    commit: str | None
    # Whether a file differed from the commit, or None if that's unknown.
    dirty: bool | None
    # The source checkout repoutils runs from, or None for a build.
    checkout: Path | None
    # The nearest tag the commit comes from, and how many commits it is after
    # it, or None if that's unknown, as for a build by an older hook.
    tag: str | None = None
    distance: int | None = None


def read() -> BuildInfo:
    try:
        built = json.loads(files("repoutils").joinpath(FILE).read_text())
    except FileNotFoundError:
        return _from_checkout(Path(__file__).resolve().parents[2])
    return BuildInfo(_installed_version(), built["commit"], built["dirty"], None, *tag_and_distance(built))


def tag_and_distance(built: dict[str, object]) -> tuple[str | None, int | None]:
    """The tag and distance in build_info.json, which builds by older hooks lack."""
    tag, distance = built.get("tag"), built.get("distance")
    if isinstance(tag, str) and isinstance(distance, int) and not isinstance(distance, bool) and distance >= 0:
        return tag, distance
    return None, None


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
    nearest = nearest_tag(root, "v") if head is not None else None
    tag, distance = nearest if nearest is not None else (None, None)
    return BuildInfo(project_version if isinstance(project_version, str) else None, commit, dirty, root, tag, distance)


def _installed_version() -> str | None:
    try:
        return version("repoutils")
    except PackageNotFoundError:
        return None


def is_release(info: BuildInfo) -> bool | None:
    """Whether the build is at a tag with no uncommitted changes, or None if that's unknown."""
    if info.commit is None or info.tag is None or info.distance is None or info.dirty is None:
        return None
    return info.distance == 0 and not info.dirty


def after_tag(info: BuildInfo) -> str | None:
    """How far the commit is after its tag, such as ``3 commits after v1.0``,
    or None if it's at the tag, or that's unknown."""
    if info.tag is None or not info.distance:
        return None
    return f"{info.distance} commit{'s' if info.distance != 1 else ''} after {info.tag}"


def short(info: BuildInfo, name: str = "repoutils") -> str:
    """One line, as ``repoutils --version`` prints it. A project that builds
    with the ``repoutils-build-info`` hook prints the same, with its own name.
    A build from a commit after its tag says how far after."""
    line = f"{name} {info.version or 'unknown'}"
    if info.commit is not None:
        after = after_tag(info)
        line += f" ({info.commit[:12]}{' with uncommitted changes' if info.dirty else ''}"
        line += f", {after})" if after is not None else ")"
    return line
