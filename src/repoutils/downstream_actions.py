"""`downstream bump`'s changes to a downstream's references to the upstream's
GitHub Actions, and its regeneration of `actions.lock`, kept apart from the
command for length.

A downstream references the upstream's actions by release tag, such as
`owner/name/.github/actions/downstream-bump@v0.4.0`, so that the tag matches
the version it pins on PyPI. `move` sets the ref of each `uses:` that names
the upstream's repository, in the workflows and local actions that check-pins
reads, to `v<version>`, and changes nothing else in those files. `lock` then
regenerates `.github/workflows/actions.lock` with `gh actions-lock`, which
also locks the actions that the upstream's actions use in turn, and checks it
with `gh actions-lock --verify`, relocking it if that fails.

A dev version, a test build on TestPyPI, has no tag, so for one, `move` sets
the refs to the commit the build came from instead. Stdlib-only, like every
repoutils module."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repoutils import downstream_pins
from repoutils.commands import check_pins
from repoutils.locked_actions import ACTIONS_LOCK

# A `uses:` line, as a step's key or a job's (a reusable workflow), whose ref
# is followed by nothing but a comment.
_USES_RE = re.compile(
    r"""^\s*(?:-\s+)?uses:\s*(?P<quote>["']?)"""
    r"""(?P<action>(?P<repo>[^/@\s"'#]+/[^/@\s"'#]+)(?:/[^@\s"'#]*)?)@(?P<ref>[^@\s"'#]+)(?P=quote)"""
    r"""\s*(?:#.*)?$"""
)
# A release tag that bump moves. A shorter one, such as v1, is often a tag
# that moves with each release, so moving it to a fixed tag is left to a person.
_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
# actions.lock's sections, its dependencies' entries, and their commits, in
# the fixed layout `gh actions-lock` writes.
_SECTION_RE = re.compile(r"^(?P<name>\S[^:]*):")
_ENTRY_RE = re.compile(r"^ {4}'(?P<key>[^']+)':\s*$")
_COMMIT_RE = re.compile(r"^ {8}commit:\s*'(?P<commit>[^']*)'\s*$")
# A key whose value is a block scalar, such as `run: |`, whose lines follow,
# indented further than the key itself, which is after any `- ` that starts a
# sequence item. A sibling key, such as a step's `uses:`, is at the key's own
# indent.
_BLOCK_RE = re.compile(r"""^(?P<indent>\s*(?:-\s+)?)[^\s#'"][^#]*?:\s*[|>][-+0-9]*\s*(?:#.*)?$""")


_HASH_RE = re.compile(r"[0-9a-f]{40}")


def tag(version: str) -> str:
    """The upstream's release tag for version."""
    return f"v{version}"


def target(version: str, commit: str | None) -> str | None:
    """The ref that bump moves the upstream's actions to: a release's tag, or
    for a dev version, which has no tag, commit, the commit its build came
    from, if given. Raises ValueError if commit isn't a full commit hash, or
    is given for a release."""
    if commit is not None and not _HASH_RE.fullmatch(commit):
        raise ValueError(f"{commit!r} isn't a full commit hash of 40 lowercase hex digits")
    if not downstream_pins.is_dev(version):
        if commit is not None:
            raise ValueError(
                f"{version} is a release, so the upstream's actions move to {tag(version)}, not to a commit"
            )
        return tag(version)
    return commit


@dataclass(frozen=True)
class Use:
    """A `uses:` line that names the upstream's repository."""

    # The file, relative to the downstream's root, and its line, from 1.
    file: str
    line: int
    action: str
    ref: str


def _lines(path: Path) -> list[str]:
    # newline="" keeps the file's own line endings, so the line numbers match move()'s.
    with path.open(encoding="utf-8", newline="") as f:
        return f.read().splitlines(keepends=True)


def _outside_blocks(lines: list[str]) -> list[tuple[int, str]]:
    """Each line, from 1, with its line ending, that isn't in a block scalar,
    such as a `run: |` script, whose text could read as a `uses:`."""
    found: list[tuple[int, str]] = []
    block: int | None = None
    for number, line in enumerate(lines, 1):
        body = line.rstrip("\r\n")
        if block is not None and (not body.strip() or len(body) - len(body.lstrip()) > block):
            continue
        block = None
        if match := _BLOCK_RE.match(body):
            block = len(match["indent"])
        found.append((number, body))
    return found


def uses(root: Path, upstream: str) -> list[Use]:
    """Every `uses:` in root's workflows and local actions that names the
    upstream's repository, in file and line order."""
    found: list[Use] = []
    for path in check_pins.workflow_files(root):
        for number, line in _outside_blocks(_lines(path)):
            match = _USES_RE.match(line)
            # GitHub's names are case-insensitive.
            if match is not None and match["repo"].lower() == upstream.lower():
                found.append(Use(path.relative_to(root).as_posix(), number, match["action"], match["ref"]))
    return found


def stale(root: Path, upstream: str, version: str, ref: str | None) -> list[Use]:
    """The `uses:` of the upstream's actions in root that aren't at ref, the
    tag for version or the commit its dev build came from, raising
    RuntimeError if one isn't at a release tag, or is at a newer one than
    version, as bump never lowers a ref, or if there's one and ref is None."""
    new, result = downstream_pins.release_key(version), []
    for use in uses(root, upstream):
        if use.ref == ref:
            continue
        where = f"{root / use.file}:{use.line}"
        if ref is None:
            raise RuntimeError(
                f"{where} uses {use.action}@{use.ref}, and {version} is a dev version, which has no tag to move it "
                "to, so give the commit the build came from"
            )
        if not _TAG_RE.fullmatch(use.ref):
            raise RuntimeError(
                f"{where} uses {use.action}@{use.ref}, which bump can't move, as {use.ref} isn't a release tag "
                "of the form vX.Y.Z, so bump it by hand"
            )
        if downstream_pins.release_key(use.ref[1:]) > new:
            raise RuntimeError(
                f"{where} uses {use.action}@{use.ref}, newer than {version}, and bump never lowers a ref"
            )
        result.append(use)
    return result


def move(root: Path, ref: str, moving: list[Use]) -> list[str]:
    """Set the ref of each of the `uses:` in moving to ref, changing nothing
    else, and return the files that changed, relative to root. Raises
    RuntimeError, with no file changed, if a line has changed since stale()
    read it."""
    files: dict[str, list[str]] = {}
    for use in moving:
        lines = files.setdefault(use.file, _lines(root / use.file))
        line = lines[use.line - 1] if use.line <= len(lines) else ""
        body = line.rstrip("\r\n")
        match = _USES_RE.match(body)
        if match is None or (match["action"], match["ref"]) != (use.action, use.ref):
            raise RuntimeError(f"{root / use.file}:{use.line} changed while bump ran, so the bump stops here")
        start, end = match.span("ref")
        lines[use.line - 1] = body[:start] + ref + body[end:] + line[len(body) :]
    for file, lines in files.items():
        with (root / file).open("w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
    return sorted(files)


def locked_commits(text: str) -> dict[str, str]:
    """The commit an actions.lock's text records for each action under dependencies."""
    commits: dict[str, str] = {}
    section = key = None
    for line in text.splitlines():
        if match := _SECTION_RE.match(line):
            section, key = match["name"], None
        elif section == "dependencies" and (match := _ENTRY_RE.match(line)):
            key = match["key"]
        elif key is not None and (match := _COMMIT_RE.match(line)):
            commits[key] = match["commit"]
    return commits


def check_kept(old: bytes, new: bytes, where: str) -> None:
    """Raise RuntimeError if the actions.lock new, from where, records
    another commit than old for an action that both lock. Each action's key
    names its ref, so the upstream's moved actions are new keys, and only an
    action whose ref didn't change can fail this, such as one at a branch
    moved back to an older commit, which `gh actions-lock --verify` may allow."""
    try:
        before, after = locked_commits(old.decode()), locked_commits(new.decode())
    except UnicodeDecodeError as e:
        raise RuntimeError(f"{where} isn't valid UTF-8") from e
    if moved := sorted(key for key in before.keys() & after.keys() if before[key] != after[key]):
        raise RuntimeError(
            f"{where} locks {', '.join(moved)} at another commit than {ACTIONS_LOCK} does, though the bump "
            "doesn't change its ref, so the bump stops here: regenerate actions.lock on the default branch first"
        )


def _gh_actions_lock(root: Path, *args: str) -> None:
    subprocess.run(["gh", "actions-lock", *args], cwd=root, check=True, capture_output=True, text=True)


def lock(root: Path) -> bool:
    """Regenerate root's actions.lock, if it has one, with `gh actions-lock`,
    check it with `gh actions-lock --verify`, and say whether it changed.

    A plain run can write an entry for the upstream's new tag that leaves out
    an action that one of its actions uses, which `--verify` then rejects and
    a plain run can't repair. So if `--verify` fails, it regenerates the lock
    with `gh actions-lock --relock` and checks it again. `--relock` also moves
    an action at a ref such as `v4` to the ref's current commit, so it raises
    RuntimeError if the relocked file records another commit for an action
    that the old one locked, as `check_kept` does."""
    path = root / ACTIONS_LOCK
    if not path.is_file():
        return False
    before = path.read_bytes()
    _gh_actions_lock(root)
    try:
        verify(root)
    except subprocess.CalledProcessError:
        _gh_actions_lock(root, "--relock")
        check_kept(before, path.read_bytes(), "gh actions-lock --relock")
        verify(root)
    return path.read_bytes() != before


def verify(root: Path) -> None:
    """Check, with `gh actions-lock --verify`, that root's actions.lock locks
    every action its workflows use, at a commit that its ref still resolves
    to, raising CalledProcessError if not."""
    _gh_actions_lock(root, "--verify")
