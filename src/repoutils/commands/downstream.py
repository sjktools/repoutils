"""List the downstream repositories a release is pushed to, or bump a
downstream's pins of this project's package to a release, and open a pull
request with the bump.

`list` checks the repositories that the DOWNSTREAM_REPOS Actions variable
names, and prints them as JSON for a job matrix. `bump` runs in the
upstream's checkout, which gives the package's name, and changes the
downstream checkout that `--directory` names. Before it changes anything, it
checks that PyPI's provenance for the release names the upstream's repository
as its trusted publisher. Stdlib-only, like every repoutils module."""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

from repoutils import config, downstream_pins, downstream_prs, github, pull_request, pypi_provenance
from repoutils.docstrings import summary

# An owner is letters, digits and single hyphens, and a repository's name is
# letters, digits, `.`, `_` and `-`.
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9])*/[A-Za-z0-9._-]+")
TRAILER = downstream_prs.TRAILER


def repositories(text: str) -> list[str]:
    """The whitespace-separated owner/name repositories in text, raising
    ValueError if there are none, or one is malformed or repeated."""
    found = text.split()
    if not found:
        raise ValueError("DOWNSTREAM_REPOS names no repositories: set it to owner/name repositories, space-separated")
    seen: set[str] = set()
    for repository in found:
        if not _REPOSITORY_RE.fullmatch(repository) or repository.split("/")[1] in (".", ".."):
            raise ValueError(f"DOWNSTREAM_REPOS has {repository!r}, which isn't of the form owner/name")
        # GitHub's names are case-insensitive.
        if repository.lower() in seen:
            raise ValueError(f"DOWNSTREAM_REPOS names {repository} more than once")
        seen.add(repository.lower())
    return found


def list_repositories(text: str) -> int:
    print(json.dumps(repositories(text)))
    return 0


def package_name(root: Path) -> str:
    """The [project] name in root/pyproject.toml."""
    path = root / config.PYPROJECT
    project = config.read_pyproject(path).get("project", {})
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name:
        raise config.ConfigError(f"{path}: [project] name is missing, so pass --package")
    return name


def bump(
    directory: Path,
    package: str,
    version: str,
    upstream: str,
    *,
    pr: bool = False,
    repo: str = "",
    token: str = "",
    lock_file: Path | None = None,
    marker: str = "",
) -> int:
    """Move directory's pins of package to version, and re-lock its uv.lock,
    or copy lock_file over it if given. With pr, commit that to a new branch
    of repo, push it and open a pull request whose body starts with marker."""
    moving = downstream_pins.stale(directory, package, version)
    if not moving:
        print(f"{directory / config.PYPROJECT} already pins {package} {version}, so there's nothing to bump")
        return 0
    lockfile = directory / "uv.lock"
    new_lock = None
    if lock_file is not None:
        if not lockfile.is_file():
            raise RuntimeError(f"There's no {lockfile} for {lock_file} to replace")
        new_lock = lock_file.read_bytes()
        try:
            lock_text = new_lock.decode()
        except UnicodeDecodeError as e:
            raise RuntimeError(f"{lock_file} isn't valid UTF-8") from e
        required = downstream_pins.depends(directory, package)
        downstream_pins.check_locked(lock_text, package, version, str(lock_file), required=required)
    files = pypi_provenance.check(package, version, upstream)
    print(f"PyPI's provenance names {upstream} as the publisher of {', '.join(files)}")

    branch, base = downstream_prs.branch_name(package, version), ""
    key = downstream_pins.release_key(version)
    earlier: list[downstream_prs.Bump] = []
    kept: list[downstream_prs.Bump] = []
    if pr:
        bumps = downstream_prs.open_bumps(repo, token, package, marker)
        if newer := [b for b in bumps if downstream_pins.release_key(b.version) > key]:
            newest = newer[0]
            print(f"Pull request {newest.url} already bumps {package} to {newest.version}, so there's nothing to do")
            return 0
        earlier = [b for b in bumps if downstream_pins.release_key(b.version) < key]
        # Judged before anything changes, so the new pull request's body can link the ones it leaves open.
        kept = [b for b in earlier if not downstream_prs.untouched(repo, token, b)]
        if same := [b for b in bumps if downstream_pins.release_key(b.version) == key]:
            # A rerun, after an earlier run opened it, finishes with the earlier ones.
            current = same[0]
            print(f"Pull request {current.url} already bumps {package} to {version}")
            linked = _link_kept(repo, token, current, package, kept)
            return linked | _follow_up(directory, repo, token, earlier, kept, same[0].number, package, version, marker)
        # One whose body has lost the marker.
        if existing := pull_request.open_pr(repo, branch, token):
            print(f"Pull request {existing} already bumps {package} to {version}")
            return 0
        base = pull_request.prepare(directory, branch)

    pyproject = directory / config.PYPROJECT
    texts = {pyproject: pyproject.read_bytes()}
    if lockfile.is_file():
        texts[lockfile] = lockfile.read_bytes()
    try:
        downstream_pins.move(directory, package, version, moving)
        if new_lock is None:
            moved = pyproject.read_bytes()
            relocked = downstream_pins.lock(directory, package, version)
            # uv lock runs the downstream's build backends, which could edit it.
            if pyproject.read_bytes() != moved:
                raise RuntimeError(f"{pyproject} changed while uv lock ran, so the bump stops here")
        else:
            lockfile.write_bytes(new_lock)
            relocked = new_lock != texts[lockfile]
        names = [config.PYPROJECT] + (["uv.lock"] if relocked else [])
        if pr:
            # prepare() saw a clean tree, so anything else changed came from this run.
            listed = pull_request.git(directory, "ls-files", "--modified", "--others", "--exclude-standard")
            changed = sorted(set(listed.splitlines()))
            if changed != sorted(names):
                raise RuntimeError(
                    f"The bump changed {', '.join(changed)}, not just {', '.join(names)}, so it stops here"
                )
    except BaseException:
        for path, text in texts.items():
            path.write_bytes(text)
        raise
    old = ", ".join(sorted({pin.version for pin in moving}, key=downstream_pins.release_key))
    print(f"Bumped {package} from {old} to {version} in {', '.join(names)}")
    if not pr:
        return 0

    pins = "\n".join(f"- `{pin.where}`: `{pin.requirement}` to `{pin.moved(version)}`" for pin in moving)
    # The run this is part of, in a workflow of the upstream's.
    run_id = os.environ.get("GITHUB_RUN_ID")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run = f" in {server}/{upstream}/actions/runs/{run_id}" if run_id else ""
    left_open = "".join(f"{note}\n\n" for note in _left_open(package, kept))
    url = pull_request.submit(
        directory,
        repo,
        token,
        branch=branch,
        base=base,
        paths=names,
        title=f"Bump {package} to {version}",
        message=f"Bumps {package} from {old} to {version}.\n\n{downstream_prs.TRAILER}",
        body=f"{marker}\n"
        f"Bumps [{package}](https://pypi.org/project/{package}/{version}/) from {old} to {version}, "
        f"which [{upstream}](https://github.com/{upstream}) released.\n\n"
        f"{pins}\n\n"
        f"PyPI's provenance names {upstream} as the trusted publisher of each of the release's files.\n\n"
        f"{left_open}"
        f"Opened by `repoutils downstream bump --pr`{run}.",
    )
    print(f"Opened {url}")
    return _follow_up(directory, repo, token, earlier, kept, int(url.rsplit("/", 1)[1]), package, version, marker)


def _link_kept(
    repo: str, token: str, current: downstream_prs.Bump, package: str, kept: list[downstream_prs.Bump]
) -> int:
    """Add a paragraph to current's body for each pull request in kept that it
    doesn't link yet, as one may have gained other commits since the run that
    opened current. Returns 1 if that failed, after reporting it."""
    if not (missing := [note for note in _left_open(package, kept) if note not in current.body]):
        return 0
    body = current.body.rstrip() + "\n\n" + "\n\n".join(missing)
    try:
        github.request(
            "PATCH", f"{github.API_URL}/repos/{repo}/pulls/{current.number}", token, json_body={"body": body}
        )
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(
            f"Linking the pull requests it leaves open from {current.url} failed: HTTP {e.code}:\n{detail}",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError) as e:
        print(f"Linking the pull requests it leaves open from {current.url} failed: {e}", file=sys.stderr)
        return 1
    return 0


def _left_open(package: str, kept: list[downstream_prs.Bump]) -> list[str]:
    """A paragraph for the new pull request's body on each earlier one in kept, which it leaves open."""
    return [
        f"#{b.number} bumps {package} to {b.version}, and stays open, as it has commits that "
        "`repoutils downstream bump` didn't make. Its changes probably need moving across."
        for b in kept
    ]


def _follow_up(
    directory: Path,
    repo: str,
    token: str,
    earlier: list[downstream_prs.Bump],
    kept: list[downstream_prs.Bump],
    number: int,
    package: str,
    version: str,
    marker: str,
) -> int:
    """Close each earlier bump pull request that pull request number
    supersedes, and comment on each in kept, which has commits that bump
    didn't make, rather than close it. Goes on to the next one if one fails,
    and returns 1 if any did."""
    comment_marker = downstream_prs.comment_marker(marker, version)
    failed = 0
    for bump in earlier:
        try:
            if bump in kept:
                downstream_prs.warn(repo, token, bump, comment_marker, number, package, version)
                print(f"Left {bump.url} open, as it has other commits, and commented that #{number} bumps {version}")
            else:
                downstream_prs.supersede(directory, repo, token, bump, comment_marker, number)
                print(f"Closed {bump.url}, which #{number} supersedes, and deleted its branch")
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read().decode(errors="replace")
            print(f"Handling {bump.url} failed: HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
            failed = 1
        except (OSError, RuntimeError, ValueError) as e:
            print(f"Handling {bump.url} failed: {e}", file=sys.stderr)
            failed = 1
    return failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils downstream", description=summary(__doc__))
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{list,bump}")
    list_parser = subparsers.add_parser(
        "list",
        help="check the downstream repositories, and print them as JSON for a job matrix",
        description="Check that each entry of the space-separated list is an owner/name repository, named once, "
        "and print them as a JSON list, for a job matrix. Fails if the list is empty.",
    )
    list_parser.add_argument(
        "--repositories",
        default=os.environ.get("DOWNSTREAM_REPOS", ""),
        help="the space-separated list (default: $DOWNSTREAM_REPOS)",
    )
    bump_parser = subparsers.add_parser(
        "bump",
        help="move a downstream's pins of the package to a release, and re-lock it",
        description="Run in the upstream's checkout. In the downstream checkout that --directory names, move "
        "the package's == pins in [build-system] requires and >= minimums in dependencies and dependency groups "
        "to the version, then re-lock uv.lock, upgrading only the package, with the uv the downstream's "
        "[tool.uv] required-version names. It retries the re-lock for a while, in case PyPI's index lags. "
        "First it checks that PyPI's provenance names the upstream's repository as the trusted publisher of "
        "each of the release's files. It does nothing if every pin is already at the version, and fails if "
        "one is newer.",
        epilog="--pr uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs the Pull requests write "
        "permission on the downstream, and pushes with Git's own credentials.",
    )
    bump_parser.add_argument("--version", required=True, help="the release to bump to, such as 0.4.0")
    bump_parser.add_argument(
        "--directory", type=Path, required=True, help="the downstream's checkout, on its default branch"
    )
    bump_parser.add_argument(
        "--package", help="the package to bump (default: [project] name in the current directory's pyproject.toml)"
    )
    bump_parser.add_argument(
        "--upstream",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name of the repository PyPI's provenance must name (default: $GITHUB_REPOSITORY, "
        "else gh's current repo)",
    )
    bump_parser.add_argument(
        "--lock-file",
        type=Path,
        help="copy this file over uv.lock rather than re-lock it, so that bump runs no uv: for a job that holds "
        "a token that can push, given a uv.lock that a job without one re-locked by running bump",
    )
    bump_parser.add_argument(
        "--pr",
        action="store_true",
        help="commit the bump to a new branch, downstream/<package>-<version>, push it to the downstream "
        f"checkout's {pull_request.REMOTE} and open a pull request into its current branch; "
        "if an open pull request from an earlier bump is for a newer version, change nothing; "
        "close one for an older version that has only the commits bump made, and delete its branch, "
        "or comment on one that has other commits and link it from the new pull request; "
        "if one for this version is open, open none, but handle the older ones, and link the ones it leaves open "
        "from that pull request",
    )
    bump_parser.add_argument("--repo", help="the downstream's owner/name, which --pr needs")
    args = parser.parse_args(argv)

    try:
        if args.mode == "list":
            return list_repositories(args.repositories)
        if args.pr and not args.repo:
            parser.error("bump --pr needs --repo")
        # Read pyproject.toml first, so a bad setting fails before anything asks GitHub.
        package = args.package or package_name(Path())
        marker = config.marker(f"downstream-{package}") if args.pr else ""
        upstream = args.upstream or github.gh_output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        )
        token = (os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")) if args.pr else ""
        return bump(
            args.directory,
            package,
            args.version,
            upstream,
            pr=args.pr,
            repo=args.repo or "",
            token=token,
            lock_file=args.lock_file,
            marker=marker,
        )
    except subprocess.CalledProcessError as e:
        # A command run without capturing its output (uv lock) has already shown its stderr.
        print(f"{' '.join(e.cmd)} failed" + (f":\n{e.stderr}" if e.stderr else ""), file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main
