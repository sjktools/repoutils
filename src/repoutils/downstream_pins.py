"""`downstream bump`'s changes to a downstream's pins of the upstream package,
and its re-lock of `uv.lock`, kept apart from the command for length.

A downstream pins the package in up to three places, and bump moves the first
two by editing `pyproject.toml`'s text:

- `==` pins in `[build-system] requires`, such as a build hook's, which
  `uv.lock` doesn't lock.
- `>=` minimums in `[project] dependencies`, optional dependencies and
  dependency groups.
- `uv.lock`, which `lock` re-locks with the downstream's own uv.

Stdlib-only, like every repoutils module."""

import copy
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import config
from repoutils.commands import check_pins
from repoutils.commands.requirements_min_versions import (
    dependency_groups,
    normalize_package_name,
    replace_dependency,
)

BUILD_SYSTEM = "build-system.requires"
# How often, and how far apart, lock() tries `uv lock`, as PyPI's index can lag
# behind a release for a minute or two.
LOCK_ATTEMPTS = 5
LOCK_DELAY = 30.0

_NAME_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^]]*\])?")
# The version after each operator, which ends at a comma, space, marker or
# closing parenthesis. `===` is left alone, as an arbitrary-equality pin isn't
# a release version.
_VERSION_RES = {
    "==": re.compile(r"(?<![=!~<>])(==(?!=)\s*)([^,\s;)]+)"),
    ">=": re.compile(r"(>=\s*)([^,\s;)]+)"),
}
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")
# A clause of a version specifier that bound() can check.
_CLAUSE_RE = re.compile(r"(?P<op><=|>=|==|!=|<|>)\s*(?P<version>\d+(?:\.\d+)*)")
_COMPARE = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def release_key(version: str) -> tuple[int, ...]:
    """A release version (X.Y.Z, with any number of parts) as numbers for
    comparing, with trailing zeros dropped, so 1.0 and 1.0.0 compare equal."""
    if not _RELEASE_RE.fullmatch(version):
        raise ValueError(f"{version} isn't a release version of the form X.Y.Z")
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


@dataclass(frozen=True)
class Pin:
    """A requirement in pyproject.toml that pins the package."""

    where: str
    requirement: str
    operator: str
    version: str

    def moved(self, version: str) -> str:
        """The requirement with its version set to version."""
        return _VERSION_RES[self.operator].sub(rf"\g<1>{version}", self.requirement, count=1)

    def excludes(self, version: str) -> str | None:
        """The clause of the moved requirement, such as `<0.4`, that version
        doesn't meet, if there is one. A clause this can't read (`~=`, a
        wildcard) is left to uv."""
        moved = self.moved(version)
        name = _NAME_RE.match(moved)
        specifier = moved.split(";", 1)[0][name.end() if name else 0 :].strip(" ()")
        new = release_key(version)
        for clause in (part.strip() for part in specifier.split(",")):
            match = _CLAUSE_RE.fullmatch(clause)
            if match is not None and not _COMPARE[match["op"]](new, release_key(match["version"])):
                return clause
        return None


def _lists(pyproject: dict[str, Any]) -> Iterator[tuple[str, list[Any], str]]:
    """Each list of requirements that bump moves, with where it is and the operator it moves."""
    build_system = pyproject.get("build-system", {})
    requires = build_system.get("requires", []) if isinstance(build_system, dict) else []
    if isinstance(requires, list):
        yield BUILD_SYSTEM, requires, "=="
    for where, requirements in dependency_groups(pyproject):
        yield where, requirements, ">="


def _pin(requirement: object, package: str, operator: str, where: str) -> Pin | None:
    """The pin requirement makes, if it's a requirement on package with operator."""
    if not isinstance(requirement, str):
        return None
    name = _NAME_RE.match(requirement)
    if name is None or normalize_package_name(name["name"]) != package:
        return None
    # The markers after ; aren't part of the version specifier.
    match = _VERSION_RES[operator].search(requirement.split(";", 1)[0])
    if match is None:
        return None
    return Pin(where, requirement, operator, match[2])


def pins(pyproject: dict[str, Any], package: str) -> list[Pin]:
    """Every pin of package that bump moves, in the order pyproject.toml gives them."""
    package = normalize_package_name(package)
    return [
        pin
        for where, requirements, operator in _lists(pyproject)
        for requirement in requirements
        if (pin := _pin(requirement, package, operator, where)) is not None
    ]


def depends(root: Path, package: str) -> bool:
    """Whether root/pyproject.toml's dependencies or dependency groups name
    package, so that its uv.lock must lock it."""
    package = normalize_package_name(package)
    for _, requirements in dependency_groups(config.read_pyproject(root / config.PYPROJECT)):
        for requirement in requirements:
            name = _NAME_RE.match(requirement) if isinstance(requirement, str) else None
            if name is not None and normalize_package_name(name["name"]) == package:
                return True
    return False


def stale(root: Path, package: str, version: str) -> list[Pin]:
    """The pins of package in root/pyproject.toml that aren't at version,
    raising RuntimeError if there are none at all, or one is newer than
    version, as bump never lowers a pin."""
    path = root / config.PYPROJECT
    found = pins(config.read_pyproject(path), package)
    if not found:
        raise RuntimeError(
            f"{path} doesn't pin {package}: it has no `==` pin in [build-system] requires, "
            "and no `>=` minimum in its dependencies or dependency groups"
        )
    new = release_key(version)
    result: list[Pin] = []
    for pin in found:
        try:
            old = release_key(pin.version)
        except ValueError:
            raise RuntimeError(f"{path} {pin.where} has {pin.requirement!r}, which bump can't move, so bump it by hand")
        if old > new:
            raise RuntimeError(
                f"{path} {pin.where} has {pin.requirement!r}, newer than {version}, and bump never lowers a pin"
            )
        if old != new:
            if (clause := pin.excludes(version)) is not None:
                raise RuntimeError(
                    f"{path} {pin.where} has {pin.requirement!r}, whose `{clause}` excludes {version}, "
                    "so bump it by hand"
                )
            result.append(pin)
    return result


def move(root: Path, package: str, version: str, moving: list[Pin]) -> None:
    """Set each of the pins moving in root/pyproject.toml to version, changing
    nothing else, and raising RuntimeError, with the file unchanged, if it
    can't."""
    path = root / config.PYPROJECT
    # newline="" keeps the file's own line endings.
    with path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    before = tomllib.loads(text)
    for pin in moving:
        text, count = replace_dependency(text, pin.requirement, pin.moved(version))
        if count == 0:
            # A basic string with escapes in it, such as a marker's \" quotes.
            escaped = config.toml_string(pin.requirement)[1:-1]
            text, count = replace_dependency(text, escaped, config.toml_string(pin.moved(version))[1:-1])
        if count == 0:
            raise RuntimeError(f"Couldn't find {pin.requirement!r} in {path}, so bump it by hand")
    # A string elsewhere in the file that matched one of the requirements
    # would have been changed instead, so check the file now reads as the
    # same TOML with just those pins moved.
    expected = copy.deepcopy(before)
    normalized = normalize_package_name(package)
    for where, requirements, operator in _lists(expected):
        for i, requirement in enumerate(requirements):
            found = _pin(requirement, normalized, operator, where)
            if found is not None and found in moving:
                requirements[i] = found.moved(version)
    if tomllib.loads(text) != expected:
        raise RuntimeError(f"Moving the pins in {path} would change more than them, so bump it by hand")
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def locked_versions(text: str, package: str) -> set[str]:
    """The versions of package a uv.lock's text locks, raising ValueError if
    its [[package]] tables aren't tables."""
    package = normalize_package_name(package)
    entries = tomllib.loads(text).get("package", [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("its package entries aren't [[package]] tables")
    return {
        str(entry["version"])
        for entry in entries
        if normalize_package_name(str(entry.get("name", ""))) == package and "version" in entry
    }


def check_locked(text: str, package: str, version: str, where: str, *, required: bool = False) -> None:
    """Raise RuntimeError unless a uv.lock's text locks package at version.
    Unless required, it may not lock package at all, as a build requirement
    alone isn't locked."""
    try:
        versions = locked_versions(text, package)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise RuntimeError(f"{where} isn't a valid lock file: {e}") from e
    if not versions and required:
        raise RuntimeError(f"{where} doesn't lock {package}, which the project depends on")
    if versions and {release_key(found) for found in versions} != {release_key(version)}:
        raise RuntimeError(f"{where} locks {package} at {', '.join(sorted(versions))}, not {version}")


def uv_command(root: Path) -> list[str]:
    """The command that runs the uv root's [tool.uv] required-version names,
    through `uv tool run`, or the uv on PATH if it names none."""
    tool = config.read_pyproject(root / config.PYPROJECT).get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    if not isinstance(uv, dict) or "required-version" not in uv:
        return ["uv"]
    return ["uv", "tool", "run", f"uv@{check_pins.uv_minimum(root)}"]


def _lock_once(command: list[str]) -> int:
    """Run `uv lock` once, from a directory outside both projects, so that
    neither's uv settings pick which uv `uv tool run` runs."""
    with tempfile.TemporaryDirectory() as outside:
        return subprocess.run(command, cwd=outside, check=False).returncode


def lock(root: Path, package: str, version: str) -> bool:
    """Re-lock root's uv.lock, if it has one, upgrading only package, to
    version, with the uv root requires, and say whether uv.lock changed. It
    tries LOCK_ATTEMPTS times, LOCK_DELAY seconds apart, in case PyPI's index
    lags behind the release."""
    path = root / "uv.lock"
    if not path.is_file():
        return False
    before = path.read_bytes()
    command = [
        *uv_command(root),
        "lock",
        "--directory",
        str(root.resolve()),
        "--upgrade-package",
        f"{package}=={version}",
        "--refresh-package",
        package,
    ]
    attempt = 1
    while (code := _lock_once(command)) != 0:
        if attempt >= LOCK_ATTEMPTS:
            raise subprocess.CalledProcessError(code, command)
        print(f"uv lock failed, so trying again in {LOCK_DELAY:g}s, as PyPI's index may lag", file=sys.stderr)
        time.sleep(LOCK_DELAY)
        attempt += 1
    check_locked(path.read_text(encoding="utf-8"), package, version, str(path), required=depends(root, package))
    return path.read_bytes() != before
