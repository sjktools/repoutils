"""Bump the project's version, and with ``--pr``, open a pull request for it.

It bumps ``[project] version`` in ``pyproject.toml``, of the form X.Y.Z, by
one patch, minor or major level, and the project's own entry in ``uv.lock`` to
match. It edits both files as text and runs no uv, so a job that holds a token
that can push runs nothing but repoutils. With ``--pr``, it commits the bump to
a new branch, ``release-version-<version>``, pushes it and opens a pull request
into the repository's default branch, which the checkout must be on.
Stdlib-only, like every repoutils module."""

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from repoutils import github, pull_request
from repoutils.commands.requirements_min_versions import normalize_package_name
from repoutils.config import PYPROJECT, ConfigError, read_pyproject
from repoutils.docstrings import summary

PROG = "repoutils bump-version"
LEVELS = ("patch", "minor", "major")
# `--pr` pushes its commit to BRANCH_PREFIX + the new version.
BRANCH_PREFIX = "release-version-"
LOCK_FILE = "uv.lock"
MERGE_METHODS = ("merge", "squash", "rebase")

_ENABLE_AUTO_MERGE = """
mutation($id: ID!, $method: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) { clientMutationId }
}"""


def bumped(version: str, level: str) -> str:
    """version, of the form X.Y.Z, bumped by level, as `uv version --bump` does."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"[project] version {version} isn't of the form X.Y.Z, so bump it by hand")
    major, minor, patch = (int(part) for part in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _read(path: Path) -> str:
    # newline="" keeps the file's line endings when it's written back.
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _replace_one(text: str, old: str, new: str, expected: dict[str, Any], what: str) -> str:
    """text with the one `version = "old"` line whose change to new makes
    the TOML parse to expected, raising RuntimeError unless exactly one does."""
    pattern = re.compile(rf"""^(?P<head>[ \t]*version[ \t]*=[ \t]*)(?P<q>["']){re.escape(old)}(?P=q)""", re.MULTILINE)
    found: list[str] = []
    for match in pattern.finditer(text):
        candidate = f"{text[: match.start()]}{match['head']}{match['q']}{new}{match['q']}{text[match.end() :]}"
        if tomllib.loads(candidate) == expected:
            found.append(candidate)
    if len(found) != 1:
        raise RuntimeError(f"{what} has {len(found)} lines that set it to {old}, not 1, so bump it by hand")
    return found[0]


def _project(pyproject: dict[str, Any], path: Path) -> tuple[str, str]:
    """The project's name and version, from [project]."""
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ConfigError(f"{path} has no [project] table")
    name, version = project.get("name"), project.get("version")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}: [project] name must be a string")
    if not isinstance(version, str) or not version:
        raise ConfigError(f"{path}: [project] version must be a string, and not dynamic")
    return name, version


def _is_root(entry: object, name: str) -> bool:
    """Whether a uv.lock [[package]] entry is the project's own."""
    return (
        isinstance(entry, dict)
        and normalize_package_name(str(entry.get("name", ""))) == normalize_package_name(name)
        and entry.get("source") in ({"editable": "."}, {"virtual": "."})
    )


def _lock_text(text: str, name: str, old: str, new: str, path: Path) -> str:
    """uv.lock's text with the project's own entry at version new."""
    try:
        lock = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"{path} isn't a valid lock file: {e}") from e
    entries = lock.get("package", [])
    roots = [entry for entry in entries if _is_root(entry, name)] if isinstance(entries, list) else []
    if len(roots) != 1 or roots[0].get("version") != old:
        raise RuntimeError(f"{path} doesn't lock {name} {old} from the project itself, so re-lock it first")
    roots[0]["version"] = new
    return _replace_one(text, old, new, lock, f"{path}'s entry for {name}")


def bump(root: Path, level: str) -> tuple[str, str, list[str]]:
    """Bump root's version by level, in pyproject.toml and, if root has one,
    uv.lock. Return the old and new versions, and the files it changed,
    relative to root. It writes nothing unless it can change them all, and
    puts both back if writing either fails."""
    path = root / PYPROJECT
    text = _read(path)
    pyproject = read_pyproject(path)
    name, old = _project(pyproject, path)
    new = bumped(old, level)
    pyproject["project"]["version"] = new
    originals = {path: text}
    texts = {path: _replace_one(text, old, new, pyproject, f"{path}'s [project] version")}
    lockfile = root / LOCK_FILE
    if lockfile.is_file():
        originals[lockfile] = _read(lockfile)
        texts[lockfile] = _lock_text(originals[lockfile], name, old, new, lockfile)
    try:
        for changed, new_text in texts.items():
            _write(changed, new_text)
    except BaseException:
        # So a failed write leaves the two files as they were, and in step.
        for changed, original in originals.items():
            _write(changed, original)
        raise
    return old, new, [changed.relative_to(root).as_posix() for changed in texts]


def default_branch(repo: str, token: str) -> str:
    """The repository's default branch."""
    return str(github.request("GET", f"{github.API_URL}/repos/{repo}", token)["default_branch"])


def enable_auto_merge(repo: str, url: str, method: str, token: str) -> None:
    """Turn on auto-merge, with method, for the pull request at url."""
    number = url.rstrip("/").rsplit("/", 1)[1]
    pull = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{number}", token)
    github.graphql(_ENABLE_AUTO_MERGE, {"id": pull["node_id"], "method": method.upper()}, token)


def bump_pr(root: Path, repo: str, token: str, level: str, auto_merge: str | None = None) -> int:
    """Bump the version by level, commit it to a new branch, push it and open
    a pull request into the default branch, with auto-merge on if given a
    merge method. If a pull request from that branch is already open, change
    nothing."""
    path = root / PYPROJECT
    _, old = _project(read_pyproject(path), path)
    new = bumped(old, level)
    branch = BRANCH_PREFIX + new
    if existing := pull_request.open_pr(repo, branch, token):
        print(f"Pull request {existing} already bumps the version to {new}")
        return 0
    base = pull_request.prepare(root, branch)
    if base != (default := default_branch(repo, token)):
        raise RuntimeError(f"The checkout is on {base}, not {repo}'s default branch, {default}, so switch to it first")
    old, new, paths = bump(root, level)
    print(f"Bumped the version from {old} to {new} in {', '.join(paths)}")
    url = pull_request.submit(
        root,
        repo,
        token,
        branch=branch,
        base=base,
        paths=paths,
        title=f"Bump version to {new}",
        message=f"Bumps the version from {old} to {new} ({level} bump).",
        pr_title=f"Release version {new}",
        body=f"Bumps the version from {old} to {new} ({level} bump).\n\n"
        f"Changes {', '.join(f'`{changed}`' for changed in paths)}.\n\n"
        "Opened by `repoutils bump-version --pr`.",
    )
    print(f"Opened {url}")
    if auto_merge:
        try:
            enable_auto_merge(repo, url, auto_merge, token)
        except (OSError, RuntimeError) as e:
            github.close_error(e)
            raise RuntimeError(f"Opened {url}, but turning on its auto-merge failed: {e}") from None
        print(f"Turned on auto-merge ({auto_merge})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=summary(__doc__),
        epilog="--pr uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Pull requests write "
        "permission, and pushes with Git's own credentials, which need Contents write.",
    )
    parser.add_argument("level", choices=LEVELS, help="the part of the version to bump")
    parser.add_argument(
        "--pr",
        action="store_true",
        help=f"commit the bump to a new branch, {BRANCH_PREFIX}<version>, push it to {pull_request.REMOTE} and open a "
        "pull request into the default branch, which the checkout must be on; if one from that branch is already "
        "open, change nothing",
    )
    parser.add_argument(
        "--auto-merge",
        choices=MERGE_METHODS,
        help="with --pr, turn on auto-merge for the pull request, with this merge method",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name, for --pr (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if not args.pr:
        old, new, paths = bump(Path(), args.level)
        print(f"Bumped the version from {old} to {new} in {', '.join(paths)}")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
    repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    return bump_pr(Path(), repo, token, args.level, args.auto_merge)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.auto_merge and not args.pr:
        parser.error("--auto-merge needs --pr")
    return github.guarded(f"{github.annotation('error')}{PROG}: ", lambda: _run(args))


run = main
