"""
Check the commits in a Git bundle and push them to a branch.

It's for a push job, which pushes with a write-capable token the commits that an
earlier job made from a branch's code and handed over as a bundle. Run it in a
checkout of the commit the bundle builds on, with history enough for the bundle's
prerequisites, and the token in `$GITHUB_TOKEN`, never in an argument.

The command verifies the bundle and fetches its head, and fails unless the head adds
commits to `--start`. Each option adds a check:

- `--expected-head`: the head is that commit.
- `--single-commit`: the head is one commit on `--start`.
- `--clean-merge-of BASE`: the head is at most one merge of a commit on BASE, which
  must be exactly what Git's merge of it gives, or a fast-forward to one, and then at
  most one other commit. The path checks then apply to that other commit only.
- `--allow-path` and `--deny-path` (each repeatable): the change touches no path under
  a `--deny-path` that isn't under an `--allow-path`. With `--allow-path` and no
  `--deny-path`, every path outside the `--allow-path`s is denied.

It then pushes the head to the branch with a lease on `--start`, so the push fails if
the branch has moved, and with no hooks.
"""

import argparse
import base64
import os
import posixpath
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from repoutils.checkout import git_env
from repoutils.docstrings import summary

PROG = "repoutils push-bundle"


class Refused(Exception):
    """The bundle breaks a check, or Git failed."""


def _run_git(*args: str, env: dict[str, str] | None = None, ok: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """Git's exit code, which must be one of `ok`, and its output."""
    result = subprocess.run(
        ["git", *args],
        env=git_env() if env is None else env,
        stdout=subprocess.PIPE,
        text=True,
        # A path needn't be UTF-8; this keeps its bytes, which the path rules compare.
        errors="surrogateescape",
        check=False,
    )
    if result.returncode not in ok:
        raise Refused(f"`git {' '.join(args)}` exited with code {result.returncode}")
    return result.returncode, result.stdout.strip()


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    return _run_git(*args, env=env)[1]


def _commit(ref: str) -> str:
    try:
        return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except Refused:
        raise Refused(f"{ref} isn't a commit in this checkout") from None


def _parents(commit: str) -> list[str]:
    return _git("rev-list", "--parents", "-n", "1", commit).split()[1:]


def _is_ancestor(ancestor: str, commit: str) -> bool:
    return _run_git("merge-base", "--is-ancestor", ancestor, commit, ok=(0, 1))[0] == 0


def _under(path: str, prefixes: Sequence[str]) -> bool:
    # A prefix of "" (from "." or "/") is the whole repository.
    return any(not prefix or path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _normalize(path: str) -> str:
    path = posixpath.normpath(path.strip("/") or ".")
    return "" if path == "." else path


def denied_paths(changed: Sequence[str], allow_paths: Sequence[str], deny_paths: Sequence[str]) -> list[str]:
    """The paths in `changed` that the --allow-path and --deny-path rules refuse."""
    allow = [_normalize(path) for path in allow_paths]
    deny = [_normalize(path) for path in deny_paths]
    if not deny and not allow:
        return []
    return [path for path in changed if (not deny or _under(path, deny)) and not _under(path, allow)]


def _changed(old: str, new: str) -> list[str]:
    # --no-renames, so a file moved out of a denied path is listed under its old name too.
    names = _git("diff", "--no-renames", "--name-only", "-z", old, new)
    return [name for name in names.split("\0") if name]


def _check_clean_merge(start: str, head: str, base: str) -> str:
    """Check the head is the shape --clean-merge-of allows, and return the commit its
    other commit builds on (the head itself when there is none)."""
    base_commit = _commit(base)

    def on_base(commit: str) -> bool:
        return _is_ancestor(commit, base_commit)

    # The other commit, if there is one: a commit with one parent that isn't on the base.
    before_fix = head
    head_parents = _parents(head)
    if len(head_parents) == 1 and not on_base(head):
        before_fix = head_parents[0]
    if before_fix == start or on_base(before_fix):
        return before_fix
    parents = _parents(before_fix)
    if len(parents) != 2 or parents[0] != start or not on_base(parents[1]):
        raise Refused(f"the bundle's head isn't at most one merge of {base} on {start} and one other commit")
    # Its first line is the tree, and exit code 1 means the merge conflicts.
    code, merged = _run_git("merge-tree", "--write-tree", start, parents[1], ok=(0, 1))
    if code == 1:
        raise Refused(f"merging {parents[1]} from {base} into {start} conflicts, so merge {before_fix} isn't clean")
    if merged.split("\n", 1)[0] != _git("rev-parse", f"{before_fix}^{{tree}}"):
        raise Refused(f"merge {before_fix} isn't exactly what merging {parents[1]} from {base} gives")
    return before_fix


def check(
    bundle: Path,
    start: str,
    *,
    expected_head: str | None = None,
    single_commit: bool = False,
    clean_merge_of: str | None = None,
    allow_paths: Sequence[str] = (),
    deny_paths: Sequence[str] = (),
) -> str:
    """Fetch the bundle's head, check it, and return it."""
    _git("bundle", "verify", "--quiet", str(bundle))
    _git("fetch", "--quiet", "--no-tags", str(bundle), "HEAD")
    head = _git("rev-parse", "FETCH_HEAD^{commit}")
    start = _commit(start)
    # The lease makes the push a forced one, so check it only adds commits.
    if head == start or not _is_ancestor(start, head):
        raise Refused(f"the bundle's head, {head}, doesn't add commits to {start}")
    if expected_head is not None and head != expected_head:
        raise Refused(f"the bundle's head is {head}, not {expected_head}")
    if single_commit and _parents(head) != [start]:
        raise Refused(f"the bundle's head, {head}, isn't a single commit on {start}")
    since = start if clean_merge_of is None else _check_clean_merge(start, head, clean_merge_of)
    denied = denied_paths(_changed(since, head), allow_paths, deny_paths)
    if denied:
        raise Refused("the bundle changes paths it may not change: " + ", ".join(denied))
    return head


def push(head: str, branch: str, start: str, remote: str, token: str) -> None:
    """Push `head` to `branch` at `remote`, if the branch is still at `start`."""
    # From the environment, as actions/checkout does, so the token is in no argument.
    env = git_env()
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env |= {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{remote}.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
    }
    _git(
        "-c",
        "core.hooksPath=/dev/null",
        "push",
        f"--force-with-lease=refs/heads/{branch}:{start}",
        remote,
        f"{head}:refs/heads/{branch}",
        env=env,
    )


def main(
    bundle: Path,
    branch: str,
    start: str,
    remote: str,
    token: str,
    *,
    expected_head: str | None = None,
    single_commit: bool = False,
    clean_merge_of: str | None = None,
    allow_paths: Sequence[str] = (),
    deny_paths: Sequence[str] = (),
) -> int:
    try:
        head = check(
            bundle,
            start,
            expected_head=expected_head,
            single_commit=single_commit,
            clean_merge_of=clean_merge_of,
            allow_paths=allow_paths,
            deny_paths=deny_paths,
        )
        push(head, branch, start, remote, token)
    except Refused as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return 1
    print(f"Pushed {head} to {branch}.")
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=summary(__doc__))
    parser.add_argument("bundle", type=Path, help="the bundle file")
    parser.add_argument("--branch", required=True, help="the branch to push to")
    parser.add_argument("--start", required=True, help="the commit the bundle builds on, where the branch must be")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="the repository to push to, as owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--expected-head", help="the commit the bundle's head must be")
    parser.add_argument("--single-commit", action="store_true", help="require one commit on --start")
    parser.add_argument("--clean-merge-of", metavar="BASE", help="allow one clean merge of BASE, then one commit")
    parser.add_argument("--allow-path", action="append", default=[], help="a path the change may touch")
    parser.add_argument("--deny-path", action="append", default=[], help="a path the change may not touch")
    args = parser.parse_args(argv)
    if not args.repository:
        parser.error("give --repository, or set $GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(f"{PROG}: $GITHUB_TOKEN isn't set", file=sys.stderr)
        return 1
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return main(
        args.bundle,
        args.branch,
        args.start,
        f"{server}/{args.repository}.git",
        token,
        expected_head=args.expected_head,
        single_commit=args.single_commit,
        clean_merge_of=args.clean_merge_of,
        allow_paths=args.allow_path,
        deny_paths=args.deny_path,
    )
