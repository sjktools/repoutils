"""Check for, or bump to, a newer GitHub release of a project whose release
assets this project pins by digest.

Dependabot doesn't track release assets, so nothing else says when such a
project has a new release. Each ``[tool.repoutils.github-releases.<name>]``
table in pyproject.toml names a repository, the glob patterns of the asset
names to pin, and the pins file to write them to. A release counts once it's
neither a draft nor a prerelease, and was published at least ``cooldown-days``
ago (default 7, as with Dependabot's cooldown), and the newest such release is
the one to pin. Stdlib-only, like every repoutils module."""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from repoutils import config, github, pull_request, release_pins
from repoutils.docstrings import summary

SECTION = "github-releases"
# `bump --pr` pushes its commit to BRANCH_PREFIX + <name>/<tag>.
BRANCH_PREFIX = f"{SECTION}/"
DEFAULT_COOLDOWN_DAYS = 7
PER_PAGE = 100
# A name `bump --pr` can put in a branch name, with no / so that the branch's tag can't be confused with it.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Upstream:
    """One ``[tool.repoutils.github-releases.<name>]`` table."""

    name: str
    repo: str
    file: Path
    assets: list[str]
    cooldown_days: int


@dataclass(frozen=True)
class Release:
    tag: str
    published: datetime
    url: str
    assets: list[dict[str, Any]]


def upstreams(root: Path = Path()) -> list[Upstream]:
    """Every table under ``[tool.repoutils.github-releases]``, in order."""
    found: list[Upstream] = []
    for name, table in config.section(SECTION, root).items():
        where = f"[tool.repoutils.{SECTION}.{config.toml_key(name)}]"
        if not isinstance(table, dict):
            raise config.ConfigError(f"{where} must be a table")
        if not _NAME_RE.fullmatch(name):
            raise config.ConfigError(f"{where}: the name must be letters, digits, '_', '.' and '-' only")
        unknown = sorted(set(table) - {"repo", "file", "assets", "cooldown-days"})
        if unknown:
            raise config.ConfigError(f"{where} has unknown setting(s): {', '.join(unknown)}")
        repo, file = table.get("repo"), table.get("file")
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
            raise config.ConfigError(f"{where} repo must be the repository's owner/name")
        if not isinstance(file, str) or not file:
            raise config.ConfigError(f"{where} file must be the path of the pins file")
        assets = config.string_list(table, "assets", [], where)
        if not assets:
            raise config.ConfigError(f"{where} assets must list at least one glob pattern")
        cooldown = config.integer(table, "cooldown-days", DEFAULT_COOLDOWN_DAYS, where)
        if not (root / file).resolve().is_relative_to(root.resolve()):
            raise config.ConfigError(f"{where} file must be inside the project, not {file}")
        found.append(Upstream(name, repo, root / file, assets, cooldown))
    files = [upstream.file.resolve() for upstream in found]
    if len(set(files)) < len(files):
        raise config.ConfigError(f"Two [tool.repoutils.{SECTION}] tables have the same file")
    return found


def _releases(repo: str, token: str) -> Iterator[dict[str, Any]]:
    """Every release of repo the token can see, fetching each page only when it's reached."""
    page = 1
    while True:
        url = f"{github.API_URL}/repos/{repo}/releases?per_page={PER_PAGE}&page={page}"
        batch: list[dict[str, Any]] = github.request("GET", url, token)
        yield from batch
        if len(batch) < PER_PAGE:
            return
        page += 1


def _release(data: dict[str, Any]) -> Release:
    published = datetime.fromisoformat(str(data["published_at"]))
    return Release(str(data["tag_name"]), published, str(data["html_url"]), list(data.get("assets", [])))


@dataclass(frozen=True)
class Status:
    """An upstream's pins, its newest release past its cooldown, if it has
    one, and whether that's newer than the release the pins file has in use."""

    pins: release_pins.Pins
    newest: Release | None
    newer: bool


def status(upstream: Upstream, token: str, now: datetime) -> Status:
    """Whether upstream has a newer release past its cooldown than the one its
    pins file has in use. Never counts an older release as newer, so a release
    pinned by hand during its cooldown stays."""
    pins = release_pins.read(upstream.file, upstream.repo)
    cutoff = now - timedelta(days=upstream.cooldown_days)
    newest: Release | None = None
    current: Release | None = None
    for data in _releases(upstream.repo, token):
        if data.get("draft") or not data.get("published_at"):
            continue
        if data.get("tag_name") == pins.current:
            current = _release(data)
        if data.get("prerelease"):
            continue
        # The listing is in order of creation, which a draft published later breaks, so this checks them all.
        release = _release(data)
        if release.published <= cutoff and (newest is None or release.published > newest.published):
            newest = release
    if pins.current is not None and current is None:
        raise RuntimeError(
            f"{upstream.file} has release {pins.current} in use, but {upstream.repo} has no published release "
            "with that tag"
        )
    newer = newest is not None and (current is None or newest.published > current.published)
    return Status(pins, newest, newer)


def assets(upstream: Upstream, release: Release) -> dict[str, str]:
    """The digest of each of release's assets that upstream's patterns match,
    by name. Raises RuntimeError if a pattern matches none, or an asset has no
    sha256 digest, which GitHub gives only for assets uploaded since June 2025."""
    found: dict[str, str] = {}
    for pattern in upstream.assets:
        matched = [asset for asset in release.assets if fnmatch.fnmatchcase(str(asset.get("name")), pattern)]
        if not matched:
            raise RuntimeError(
                f"Release {release.tag} of {upstream.repo} has no asset matching {pattern}: {release.url}"
            )
        for asset in matched:
            digest = asset.get("digest")
            if not isinstance(digest, str) or not release_pins.DIGEST_RE.fullmatch(digest):
                raise RuntimeError(
                    f"GitHub gives no sha256 digest for asset {asset['name']} of release {release.tag} "
                    f"of {upstream.repo}, so pin it by hand: {release.url}"
                )
            found[str(asset["name"])] = digest
    return found


def _describe(upstream: Upstream, release: Release) -> str:
    return f"{upstream.repo} {release.tag}, released on {release.published.date()}: {release.url}"


def check(upstream: Upstream, token: str, now: datetime) -> bool:
    """Report whether upstream has a newer release past its cooldown, and return whether it's up to date."""
    found = status(upstream, token, now)
    in_use = (
        f"{upstream.file} has {found.pins.current} in use"
        if found.pins.current
        else f"{upstream.file} pins no release yet"
    )
    if found.newer and found.newest is not None:
        print(
            f"{upstream.name}: {in_use}, but there's a newer release: {_describe(upstream, found.newest)}",
            file=sys.stderr,
        )
        return False
    if found.newest is None:
        print(
            f"{upstream.name}: {in_use}, and {upstream.repo} has no release "
            f"past its {upstream.cooldown_days}-day cooldown"
        )
    else:
        print(f"{upstream.name}: {in_use}, which is up to date")
    return True


def bump(upstream: Upstream, token: str, now: datetime, *, root: Path, repo: str, pr: bool) -> None:
    """Pin upstream's newest release past its cooldown and make it current,
    if it's newer than the one in use, and with pr, commit that to a new
    branch, push it and open a pull request."""
    found = status(upstream, token, now)
    if not found.newer or found.newest is None:
        print(f"{upstream.name}: {upstream.file} is up to date, so there's nothing to bump")
        return
    release, previous = found.newest, found.pins.current
    digests = assets(upstream, release)
    branch, base = f"{BRANCH_PREFIX}{upstream.name}/{release.tag}", ""
    if pr:
        if existing := pull_request.open_pr(repo, branch, token):
            print(f"{upstream.name}: pull request {existing} already bumps it to {release.tag}")
            return
        base = pull_request.prepare(root, branch)
    release_pins.add(found.pins, release.tag, digests)
    upstream.file.parent.mkdir(parents=True, exist_ok=True)
    upstream.file.write_text(release_pins.render(found.pins))
    change = f"from {previous} to {release.tag}" if previous else f"to {release.tag}"
    print(f"{upstream.name}: bumped {upstream.file} {change}")
    if not pr:
        return
    source = f"Bumps {upstream.name} {change}: {_describe(upstream, release)}"
    path = upstream.file.resolve().relative_to(root.resolve()).as_posix()
    url = pull_request.submit(
        root,
        repo,
        token,
        branch=branch,
        base=base,
        paths=[path],
        title=f"Bump {upstream.name} to {release.tag}",
        message=source,
        body=f"{source}\n\nPins {len(digests)} asset(s) in `{path}`.\n\n"
        "Opened by `repoutils github-releases bump --pr`.",
    )
    print(f"{upstream.name}: opened {url}")


def main(
    mode: str,
    names: list[str],
    *,
    root: Path = Path(),
    repo: str | None = None,
    pr: bool = False,
    exit_zero: bool = False,
    now: datetime | None = None,
) -> int:
    configured = upstreams(root)
    if not configured:
        print(f"pyproject.toml has no [tool.repoutils.{SECTION}.<name>] tables", file=sys.stderr)
        return 1
    unknown = sorted(set(names) - {upstream.name for upstream in configured})
    if unknown:
        print(f"pyproject.toml has no [tool.repoutils.{SECTION}] table for: {', '.join(unknown)}", file=sys.stderr)
        return 1
    chosen = [upstream for upstream in configured if not names or upstream.name in names]
    now = now or datetime.now(UTC)
    token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
    if mode == "check":
        up_to_date = [check(upstream, token, now) for upstream in chosen]
        return 0 if all(up_to_date) or exit_zero else 1
    if pr and repo is None:
        repo = github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    for upstream in chosen:
        bump(upstream, token, now, root=root, repo=repo or "", pr=pr)
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"repoutils {SECTION}",
        description=summary(__doc__),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs to read the releases. "
        "bump --pr pushes with Git's own credentials, and its token also needs the Pull requests write permission.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,bump}")
    check_parser = subparsers.add_parser(
        "check",
        help="fail if a project has a newer release past its cooldown than its pins file has in use",
        description="Fail if a project has a newer release past its cooldown than the one its pins file has in use.",
    )
    bump_parser = subparsers.add_parser(
        "bump",
        help="pin a project's newest release past its cooldown, if it's newer than the one in use",
        description="Pin a project's newest release past its cooldown in its pins file, and make it the release "
        "in use, if it's newer than the one in use. It keeps the earlier releases in the file. It fails if an "
        "asset pattern matches no asset of the release, or GitHub gives no sha256 digest for a matching asset, "
        "as it never downloads an asset to find its digest.",
    )
    for sub in (check_parser, bump_parser):
        sub.add_argument(
            "names",
            nargs="*",
            metavar="NAME",
            help=f"the [tool.repoutils.{SECTION}.<name>] tables to use (default: all of them)",
        )
    bump_parser.add_argument(
        "--pr",
        action="store_true",
        help=f"for each project, commit the bump to a new branch, {BRANCH_PREFIX}<name>/<tag>, "
        f"push it to {pull_request.REMOTE}"
        " and open a pull request into the current branch; if one from that branch is already open, change nothing",
    )
    bump_parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="with --pr, the owner/name of this project's repository, to open the pull request in "
        "(default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if there's a newer release (still exit 1 on an error, unlike `|| true`)",
    )
    args = parser.parse_args(argv)
    try:
        if args.mode == "bump":
            return main("bump", args.names, repo=args.repo, pr=args.pr)
        return main("check", args.names, exit_zero=args.exit_zero)
    except subprocess.CalledProcessError as e:
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
