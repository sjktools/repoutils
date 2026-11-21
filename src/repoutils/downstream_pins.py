"""`downstream bump`'s changes to a downstream's pins of the upstream package,
and its re-lock of `uv.lock`, kept apart from the command for length.

A downstream pins the package in up to three places, and bump moves the first
two by editing `pyproject.toml`'s text:

- `==` pins in `[build-system] requires`, such as a build hook's, which
  `uv.lock` doesn't lock.
- `>=` minimums in `[project] dependencies`, optional dependencies and
  dependency groups.
- `uv.lock`, which `lock` re-locks with the downstream's own uv.

A dev version, such as `0.4.1.dev123`, is a test build on TestPyPI, so for
one, `add_source` also tells uv to take the package from TestPyPI.

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

from repoutils import config, pypi_provenance
from repoutils.commands import check_pins
from repoutils.commands.requirements_min_versions import (
    dependency_groups,
    normalize_package_name,
    replace_dependency,
)

BUILD_SYSTEM = "build-system.requires"
# How often, and how far apart, lock() tries `uv lock`, as the index can lag
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
# A release version, or a dev version of one.
_RELEASE_RE = re.compile(r"(?P<release>\d+(?:\.\d+)*)(?:\.dev(?P<dev>\d+))?")
# A clause of a version specifier that excludes() can check.
_CLAUSE_RE = re.compile(r"(?P<op><=|>=|==|!=|<|>)\s*(?P<version>\d+(?:\.\d+)*(?:\.dev\d+)?)")
_COMPARE = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


# The index a dev version's test build is on, and the name add_source gives it.
TEST_INDEX = "testpypi"
TEST_INDEX_URL = f"{pypi_provenance.TESTPYPI_URL}/simple/"


def release_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """A release version (X.Y.Z, with any number of parts), or a dev version
    of one (X.Y.Z.devN), as numbers for comparing. Trailing zeros are dropped,
    so 1.0 and 1.0.0 compare equal, and X.Y.Z.devN sorts before X.Y.Z."""
    match = _RELEASE_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"{version} isn't a release version of the form X.Y.Z, or a dev version X.Y.Z.devN")
    parts = [int(part) for part in match["release"].split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return (tuple(parts), 0, int(match["dev"])) if match["dev"] is not None else (tuple(parts), 1, 0)


def is_dev(version: str) -> bool:
    """Whether version is a dev version, X.Y.Z.devN, raising ValueError if it's neither that nor a release."""
    return release_key(version)[1] == 0


def index_url(version: str) -> str:
    """The URL of the index that version's files are on: TestPyPI for a dev version, else PyPI."""
    return pypi_provenance.TESTPYPI_URL if is_dev(version) else pypi_provenance.PYPI_URL


def _meets(op: str, version: str, clause: str) -> bool:
    """Whether version meets the clause op clause."""
    new, bound = release_key(version), release_key(clause)
    # PEP 440: <V excludes V's dev versions, unless V is one itself.
    if op == "<" and new[0] == bound[0] and not is_dev(clause):
        return False
    return bool(_COMPARE[op](new, bound))


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
        for clause in (part.strip() for part in specifier.split(",")):
            match = _CLAUSE_RE.fullmatch(clause)
            if match is not None and not _meets(match["op"], version, match["version"]):
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


def _uv_table(pyproject: dict[str, Any], path: Path) -> dict[str, Any]:
    tool = pyproject.get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else None
    if not isinstance(uv, dict):
        raise RuntimeError(f"{path} [tool.uv] isn't a table")
    return uv


def _test_index(uv: dict[str, Any], path: Path) -> str | None:
    """The name of the uv index in uv, a [tool.uv] table, at TEST_INDEX_URL,
    if there is one, raising RuntimeError if it isn't explicit, or another
    index has TEST_INDEX's name."""
    indexes = uv.get("index", [])
    if not isinstance(indexes, list):
        raise RuntimeError(f"{path} [tool.uv] index isn't a list of tables, so bump it by hand")
    found = None
    for index in indexes:
        if not isinstance(index, dict):
            raise RuntimeError(f"{path} [tool.uv] index isn't a list of tables, so bump it by hand")
        if str(index.get("url", "")).rstrip("/") == TEST_INDEX_URL.rstrip("/"):
            # An index that isn't explicit would let any package on it win.
            if index.get("explicit") is not True:
                raise RuntimeError(
                    f"{path} has a [[tool.uv.index]] for {TEST_INDEX_URL} that isn't `explicit = true`, "
                    "so any package there could win over PyPI's: make it explicit"
                )
            name = index.get("name")
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    f"{path} has a [[tool.uv.index]] for {TEST_INDEX_URL} with no name, so bump it by hand"
                )
            found = found or name
        elif index.get("name") == TEST_INDEX:
            raise RuntimeError(
                f"{path} has a [[tool.uv.index]] named {TEST_INDEX} at {index.get('url')!r}, "
                f"not {TEST_INDEX_URL}, so bump it by hand"
            )
    return found


def _sources(uv: dict[str, Any], path: Path) -> dict[str, Any]:
    sources = uv.get("sources", {})
    if not isinstance(sources, dict):
        raise RuntimeError(f"{path} [tool.uv.sources] isn't a table, so bump it by hand")
    return sources


def needs_source(root: Path, package: str) -> bool:
    """Whether root/pyproject.toml must gain a [tool.uv.sources] entry that
    takes package from TestPyPI, as a dev version is only there. Raises
    RuntimeError if it has another source for package, or an index for
    TestPyPI that add_source can't use."""
    path = root / config.PYPROJECT
    uv = _uv_table(config.read_pyproject(path), path)
    index = _test_index(uv, path)
    normalized = normalize_package_name(package)
    for name, source in _sources(uv, path).items():
        if normalize_package_name(name) != normalized:
            continue
        if index is not None and source == {"index": index}:
            return False
        raise RuntimeError(
            f"{path} [tool.uv.sources] already has a source for {package}, {source!r}, "
            f"rather than TestPyPI's index, so bump it by hand"
        )
    return True


def test_source(root: Path, package: str) -> bool:
    """Whether root/pyproject.toml's [tool.uv.sources] takes package from an
    index at TEST_INDEX_URL, as a dev version's bump makes it, so that a
    release's bump would still resolve it from TestPyPI."""
    tool = config.read_pyproject(root / config.PYPROJECT).get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    if not isinstance(uv, dict):
        return False
    indexes = uv.get("index", [])
    sources = uv.get("sources", {})
    if not isinstance(indexes, list) or not isinstance(sources, dict):
        return False
    test = {
        index["name"]
        for index in indexes
        if isinstance(index, dict)
        and isinstance(index.get("name"), str)
        and str(index.get("url", "")).rstrip("/") == TEST_INDEX_URL.rstrip("/")
    }
    normalized = normalize_package_name(package)
    return any(
        normalize_package_name(name) == normalized
        and isinstance(source, dict)
        and isinstance(source.get("index"), str)
        and source["index"] in test
        for name, source in sources.items()
    )


_SOURCES_HEADER_RE = re.compile(
    r"^[ \t]*\[[ \t]*tool[ \t]*\.[ \t]*uv[ \t]*\.[ \t]*sources[ \t]*\][ \t]*(?:#[^\r\n]*)?(?=\r?$)", re.M
)


def _table(text: str, eol: str, *lines: str) -> str:
    """lines, as a table to append to text, after a blank line unless text is empty."""
    return (eol if text else "") + "".join(line + eol for line in lines)


def add_source(root: Path, package: str) -> None:
    """Make root/pyproject.toml take package from TestPyPI: add an explicit
    [[tool.uv.index]] for TestPyPI, unless it has one, and a [tool.uv.sources]
    entry for package that names it. Changes nothing else, and raises
    RuntimeError, with the file unchanged, if it can't. uv applies the source
    to [build-system] requires too."""
    path = root / config.PYPROJECT
    with path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    before = tomllib.loads(text)
    uv = _uv_table(before, path)
    eol = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith("\n"):
        text += eol
    expected = copy.deepcopy(before)
    expected_uv = expected.setdefault("tool", {}).setdefault("uv", {})
    index = _test_index(uv, path)
    if index is None:
        index = TEST_INDEX
        text += _table(
            text, eol, "[[tool.uv.index]]", f'name = "{index}"', f'url = "{TEST_INDEX_URL}"', "explicit = true"
        )
        expected_uv.setdefault("index", []).append({"name": index, "url": TEST_INDEX_URL, "explicit": True})
    entry = f"{config.toml_key(package)} = {{ index = {config.toml_string(index)} }}"
    if "sources" not in uv:
        text += _table(text, eol, "[tool.uv.sources]", entry)
    else:
        headers = list(_SOURCES_HEADER_RE.finditer(text))
        if len(headers) != 1:
            raise RuntimeError(
                f"{path} doesn't have one [tool.uv.sources] header to add {package} under, so bump it by hand"
            )
        end = headers[0].end()
        text = text[:end] + eol + entry + text[end:]
    expected_uv.setdefault("sources", {})[package] = {"index": index}
    # An inline table or dotted keys elsewhere would make these lines
    # redefine a table, or add to another one.
    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        after = None
    if after != expected:
        raise RuntimeError(f"Taking {package} from TestPyPI in {path} would change more than that, so bump it by hand")
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
    tries LOCK_ATTEMPTS times, LOCK_DELAY seconds apart, in case the index
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
        print(f"uv lock failed, so trying again in {LOCK_DELAY:g}s, as the index may lag", file=sys.stderr)
        time.sleep(LOCK_DELAY)
        attempt += 1
    check_locked(path.read_text(encoding="utf-8"), package, version, str(path), required=depends(root, package))
    return path.read_bytes() != before
