"""check-pins' check that a project runs a package and that package's GitHub
Actions from the same commit.

For each package in ``uv.lock`` whose ``Repository`` project URL names a
GitHub repository that ``.github/workflows/actions.lock`` has actions from, it
compares the commit that ``actions.lock`` records for each of those actions
with the commit in the installed wheel's ``build_info.json``, which
``build_info_hook`` writes. A package without that file only has its actions'
refs checked against ``v<version>``, with a warning. It reads only local files.
"""

import csv
import io
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import Distribution
from pathlib import Path, PurePosixPath

ACTIONS_LOCK = Path(".github/workflows/actions.lock")
UV_LOCK = Path("uv.lock")
BUILD_INFO = "build_info.json"

# actions.lock is YAML that `gh actions-lock` writes in a fixed layout, so,
# with no YAML parser in the standard library, it's read line by line.
_SECTION_RE = re.compile(r"^(?P<name>[a-z_]+):\s*(?P<empty>\{\}|\[\])?\s*$")
_VERSION_RE = re.compile(r"^version:\s*'[^']*'\s*$")
SECTIONS = ("workflows", "dependencies")
_ENTRY_RE = re.compile(r"^ {4}'(?P<key>[^']+)':\s*$")
_LIST_ITEM_RE = re.compile(r"^ {8}- '(?P<key>[^']+)'\s*$")
_FIELD_RE = re.compile(r"^ {8}(?P<name>[a-z_]+):\s*'(?P<value>[^']*)'\s*$")
# An action's key: owner/name, an optional path in the repository, and the ref.
_ACTION_RE = re.compile(r"^(?P<repo>[^/@\s]+/[^/@\s]+)(?:/[^@\s]+)?@(?P<ref>[^@\s]+)$")
_GITHUB_URL_RE = re.compile(r"^https?://(?:www\.)?github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


class LockError(ValueError):
    """A lock file or build info this check can't read, so it can't tell
    whether the pins match."""


@dataclass(frozen=True)
class LockedAction:
    key: str
    repo: str
    ref: str
    commit: str


@dataclass
class Result:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # A line for each package whose actions match it.
    matched: list[str] = field(default_factory=list)


def read_actions_lock(text: str, path: Path = ACTIONS_LOCK) -> list[LockedAction]:
    """The actions that a workflow (or local action) in actions.lock uses
    directly. The actions those use in turn are left out, as they can pin
    their own repository's actions at another commit."""
    direct: set[str] = set()
    fields: dict[str, dict[str, str]] = {}
    sections: set[str] = set()
    section = entry = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if match := _SECTION_RE.match(line):
            section, entry = match["name"], None
            sections.add(section)
        elif _VERSION_RE.match(line):
            section = entry = None
        elif match := _ENTRY_RE.match(line):
            entry = match["key"]
            if section == "dependencies":
                fields[entry] = {}
        elif section == "workflows" and entry is not None and (match := _LIST_ITEM_RE.match(line)):
            direct.add(match["key"])
        elif section == "dependencies" and entry is not None and (match := _FIELD_RE.match(line)):
            fields[entry][match["name"]] = match["value"]
        elif section == "dependencies" and entry is not None and line.startswith(" " * 8):
            continue  # The actions this one uses, which aren't checked.
        else:
            raise LockError(f"{path}:{number}: unexpected line {line.strip()!r}")
    if missing := [name for name in SECTIONS if name not in sections]:
        raise LockError(f"{path}: no {' or '.join(missing)} section")
    actions: list[LockedAction] = []
    for key in sorted(direct):
        action = _ACTION_RE.match(key)
        if action is None:
            raise LockError(f"{path}: {key!r} isn't of the form owner/name[/path]@ref")
        if key not in fields:
            raise LockError(f"{path}: {key} has no entry under dependencies")
        entry_fields = fields[key]
        if "ref" not in entry_fields or "commit" not in entry_fields:
            raise LockError(f"{path}: {key} has no ref or no commit")
        # A commit is recorded as e.g. sha1-<hex>.
        commit = entry_fields["commit"].rpartition("-")[2]
        actions.append(LockedAction(key, action["repo"].lower(), entry_fields["ref"], commit))
    return actions


def read_uv_lock(text: str, path: Path = UV_LOCK) -> dict[str, list[str]]:
    """Each package's versions in uv.lock, by name. A package can have more
    than one, for different markers. A package with no version (the project
    itself, when it's virtual) is left out."""
    try:
        lock = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LockError(f"{path}: {e}") from e
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        raise LockError(f"{path}: package isn't a list of tables")
    versions: dict[str, list[str]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise LockError(f"{path}: a package has no name")
        if "version" not in package:
            continue
        if not isinstance(version := package["version"], str):
            raise LockError(f"{path}: {package['name']}'s version isn't a string")
        versions.setdefault(package["name"], []).append(version)
    return versions


def repository(distribution: Distribution) -> str | None:
    """The owner/name of the GitHub repository that a distribution's
    ``Repository`` project URL names, in lower case, or None."""
    for entry in distribution.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        # Labels compare as PEP 753 normalizes them.
        if re.sub(r"[^a-z0-9]", "", label.lower()) == "repository":
            match = _GITHUB_URL_RE.match(url.strip())
            return match["repo"].lower() if match else None
    return None


def build_info(distribution: Distribution, name: str) -> tuple[str, bool] | None:
    """The commit in a distribution's build_info.json, and whether the build
    was dirty, or None if the distribution has none or its commit is null.
    The file is found through the distribution's RECORD, wherever the build
    hook's ``file`` put it. RECORD is read itself, as Distribution.files leaves
    out the files that are missing, and a missing build_info.json is an error."""
    text = distribution.read_text("RECORD")
    if text is None:
        raise LockError(f"{name} has no readable RECORD, so its {BUILD_INFO} can't be found")
    record = csv.reader(io.StringIO(text))
    found = [row[0] for row in record if row and PurePosixPath(row[0]).name == BUILD_INFO]
    if not found:
        return None
    if len(found) > 1:
        raise LockError(f"{name} has more than one {BUILD_INFO}: {', '.join(sorted(found))}")
    path = Path(str(distribution.locate_file(found[0])))
    try:
        info = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LockError(f"{name}'s {path} can't be read: {e}") from e
    commit = info.get("commit") if isinstance(info, dict) else None
    dirty = info.get("dirty") if isinstance(info, dict) else None
    # The hook writes both as null, or a commit and a boolean.
    if isinstance(commit, str) and isinstance(dirty, bool):
        return commit, dirty
    if isinstance(info, dict) and {"commit", "dirty"} <= info.keys() and commit is None and dirty is None:
        return None
    raise LockError(f'{name}\'s {path} isn\'t of the form {{"commit": ..., "dirty": ...}}')


def check_package(name: str, versions: list[str], distribution: Distribution, actions: list[LockedAction]) -> Result:
    """Compare one installed package, at one of uv.lock's versions of it,
    with the actions from its repository."""
    result = Result()
    locked = distribution.version
    if locked not in versions:
        raise LockError(
            f"{name} {locked} is installed, but uv.lock pins {' or '.join(versions)}, so its actions can't be checked "
            "(run uv sync)"
        )
    info = build_info(distribution, name)
    if info is None:
        tag = f"v{locked}"
        result.warnings.extend(
            f"warning: {name} {locked} has no {BUILD_INFO}, so only {action.key}'s ref was checked "
            f"against {tag}, not its commit"
            for action in actions
        )
        result.problems.extend(
            f"actions.lock: {action.key} is at {action.ref}, but uv.lock pins {name} {locked}, so it should be {tag}"
            for action in actions
            if action.ref != tag
        )
        if not result.problems:
            result.matched.append(f"{name} {locked}'s actions are at {tag}")
        return result
    commit, dirty = info
    if dirty:
        result.problems.append(f"{name} {locked} was built with uncommitted changes to {commit[:12]}")
    result.problems.extend(
        f"actions.lock: {action.key} is at commit {action.commit[:12]}, but uv.lock's {name} {locked} "
        f"was built from {commit[:12]}"
        for action in actions
        if action.commit.lower() != commit.lower()
    )
    if not result.problems:
        result.matched.append(f"{name} {locked}'s actions are at its commit {commit[:12]}")
    return result


def check(root: Path, path: list[str] | None = None) -> Result:
    """Check every package in root's uv.lock whose repository has actions in
    its actions.lock. Packages are looked up in path, or else sys.path. A
    package that isn't installed is skipped, as only its installed metadata
    names its repository. Raises LockError if it can't tell whether they
    match."""
    result = Result()
    actions_lock, uv_lock = root / ACTIONS_LOCK, root / UV_LOCK
    if not actions_lock.is_file() or not uv_lock.is_file():
        return result
    try:
        actions_text, uv_text = actions_lock.read_text(), uv_lock.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise LockError(str(e)) from e
    actions = read_actions_lock(actions_text, ACTIONS_LOCK)
    repos = {action.repo for action in actions}
    for name, versions in sorted(read_uv_lock(uv_text, UV_LOCK).items()):
        distribution = _find(name, path)
        repo = repository(distribution) if distribution is not None else None
        if distribution is None or repo not in repos:
            continue
        found = check_package(name, versions, distribution, [action for action in actions if action.repo == repo])
        result.problems += found.problems
        result.warnings += found.warnings
        result.matched += found.matched
    return result


def _find(name: str, path: list[str] | None) -> Distribution | None:
    """The first distribution of that name in path, as importlib.metadata
    would find it."""
    return next(iter(Distribution.discover(name=name, path=sys.path if path is None else path)), None)
