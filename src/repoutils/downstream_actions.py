"""`downstream bump`'s changes to a downstream's references to the upstream's
GitHub Actions, and its regeneration of `actions.lock`, kept apart from the
command for length.

A downstream references the upstream's actions by release tag, such as
`owner/name/.github/actions/downstream-bump@v0.4.0`, so that the tag matches
the version it pins on PyPI. `move` sets the ref of each `uses:` that names
the upstream's repository, in the workflows and local actions that check-pins
reads, to `v<version>`, and changes nothing else in those files. `lock` then
regenerates `.github/workflows/actions.lock` with `gh actions-lock`, which
also locks the actions that the upstream's actions use in turn. Stdlib-only,
like every repoutils module."""

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
# A key whose value is a block scalar, such as `run: |`, whose lines follow,
# indented further than the key's line.
_BLOCK_RE = re.compile(r"""^(?P<indent>\s*)(?:-\s+)?[^\s#'"][^#]*?:\s*[|>][-+0-9]*\s*(?:#.*)?$""")


def tag(version: str) -> str:
    """The upstream's release tag for version."""
    return f"v{version}"


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


def stale(root: Path, upstream: str, version: str) -> list[Use]:
    """The `uses:` of the upstream's actions in root that aren't at its tag
    for version, raising RuntimeError if one isn't at a release tag, or is at
    a newer one, as bump never lowers a ref."""
    new, result = downstream_pins.release_key(version), []
    for use in uses(root, upstream):
        if use.ref == tag(version):
            continue
        where = f"{root / use.file}:{use.line}"
        try:
            old = downstream_pins.release_key(use.ref.removeprefix("v")) if use.ref.startswith("v") else None
        except ValueError:
            old = None
        if old is None:
            raise RuntimeError(
                f"{where} uses {use.action}@{use.ref}, which bump can't move, as {use.ref} isn't a release tag "
                "of the form vX.Y.Z, so bump it by hand"
            )
        if old > new:
            raise RuntimeError(
                f"{where} uses {use.action}@{use.ref}, newer than {tag(version)}, and bump never lowers a ref"
            )
        result.append(use)
    return result


def move(root: Path, version: str, moving: list[Use]) -> list[str]:
    """Set the ref of each of the `uses:` in moving to the tag for version,
    changing nothing else, and return the files that changed, relative to
    root. Raises RuntimeError, with no file changed, if a line has changed
    since stale() read it."""
    files: dict[str, list[str]] = {}
    for use in moving:
        lines = files.setdefault(use.file, _lines(root / use.file))
        line = lines[use.line - 1] if use.line <= len(lines) else ""
        body = line.rstrip("\r\n")
        match = _USES_RE.match(body)
        if match is None or (match["action"], match["ref"]) != (use.action, use.ref):
            raise RuntimeError(f"{root / use.file}:{use.line} changed while bump ran, so the bump stops here")
        start, end = match.span("ref")
        lines[use.line - 1] = body[:start] + tag(version) + body[end:] + line[len(body) :]
    for file, lines in files.items():
        with (root / file).open("w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
    return sorted(files)


def _gh_actions_lock(root: Path, *args: str) -> None:
    subprocess.run(["gh", "actions-lock", *args], cwd=root, check=True, capture_output=True, text=True)


def lock(root: Path) -> bool:
    """Regenerate root's actions.lock, if it has one, with `gh actions-lock`,
    and say whether it changed."""
    path = root / ACTIONS_LOCK
    if not path.is_file():
        return False
    before = path.read_bytes()
    _gh_actions_lock(root)
    return path.read_bytes() != before


def verify(root: Path) -> None:
    """Check, with `gh actions-lock --verify`, that root's actions.lock locks
    every action its workflows use, at a commit that its ref still resolves
    to, raising CalledProcessError if not."""
    _gh_actions_lock(root, "--verify")
