"""Check that a built wheel, or a released version, installs with uvx and runs,
and that its `--version` gives the commit it was built from.

`--wheel` takes a wheel, or the directory (such as dist/) that holds it, and
expects `<name> --version` to print `<name> <version> (<commit>)` exactly, as
the `repoutils-build-info` hook records it: the first 12 hex digits of the
commit this checkout has, followed by ` with uncommitted changes` if a file in
the current directory differs from it.

`--version` installs that version from the index that `--index` names
(default PyPI), retrying with backoff while the index catches up with a new
upload. It expects the same line with any 12-hex commit, as the checkout it
runs in need not be the one the release was built from.

Each `--run` then runs the command with those arguments, which must exit 0.
It installs nothing beyond the package it tests, which uvx installs in an
isolated environment with no cache.
"""

import argparse
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from repoutils import build_info, config
from repoutils.checkout import head_commit
from repoutils.docstrings import summary

DEFAULT_INDEX = "https://pypi.org/simple/"
# Seconds to wait before each try, while the index catches up with an upload.
DELAYS = (0, 10, 20, 40, 80, 160)

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


class CheckError(Exception):
    """The check can't run, such as for a missing wheel or uvx."""


def wheel_name(name: str) -> str:
    """The distribution name as a wheel's file name gives it (PEP 427, as normalized by PEP 503)."""
    return re.sub(r"[-_.]+", "_", name).lower()


def find_wheel(path: Path, name: str, version: str | None) -> Path:
    """path if it's a file, or else the one wheel of name (at version, if given) in the directory path."""
    if path.is_file():
        return path
    if not path.is_dir():
        raise CheckError(f"{path.as_posix()} is neither a wheel nor a directory")
    pattern = f"{wheel_name(name)}-{version if version is not None else '*'}-*.whl"
    wheels = sorted(path.glob(pattern))
    if len(wheels) != 1:
        found = ", ".join(wheel.name for wheel in wheels) or "none"
        raise CheckError(f"expected one wheel matching {path.as_posix()}/{pattern}, found {found}")
    return wheels[0]


def wheel_version(wheel: Path) -> str:
    """The version in a wheel's file name, `<name>-<version>-...whl`."""
    parts = wheel.name.split("-")
    if len(parts) < 5 or not wheel.name.endswith(".whl"):
        raise CheckError(f"{wheel.as_posix()} is not named as a wheel is")
    return parts[1]


def uvx(source: str, command: str, args: Sequence[str], index: str | None = None) -> list[str]:
    """The uvx command line that installs source in an isolated environment, with no cache, and runs command."""
    index_args = ["--default-index", index] if index is not None else []
    return ["uvx", "--isolated", "--no-cache", *index_args, "--from", source, command, *args]


def release_line(name: str, version: str) -> re.Pattern[str]:
    """What `<name> --version` prints for a release built by the hook, from any commit."""
    return re.compile(rf"{re.escape(name)} {re.escape(version)} \([0-9a-f]{{12}}( with uncommitted changes)?\)")


def _call(runner: Runner, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args)
    except FileNotFoundError as error:
        raise CheckError(f"can't run {args[0]}: {error}") from error


def _failed(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout + result.stderr).rstrip()
    return f"`{shlex.join(args)}` exited {result.returncode}" + (f":\n{output}" if output else "")


def _run_extra(runner: Runner, source: str, command: str, runs: Sequence[str], index: str | None) -> list[str]:
    problems: list[str] = []
    for run_args in runs:
        args = uvx(source, command, shlex.split(run_args), index)
        result = _call(runner, args)
        if result.returncode != 0:
            problems.append(_failed(args, result))
    return problems


def check_wheel(wheel: Path, name: str, command: str, runs: Sequence[str], runner: Runner = _run) -> list[str]:
    """The problems with installing and running wheel, compared with the checkout in the current directory."""
    head = head_commit(Path())
    if head is None:
        raise CheckError("can't find the commit the current directory has checked out, to compare with the wheel's")
    commit, dirty = head
    expected = build_info.short(build_info.BuildInfo(wheel_version(wheel), commit, dirty, None), name)
    source = wheel.as_posix()
    args = uvx(source, command, ["--version"])
    result = _call(runner, args)
    if result.returncode != 0:
        return [_failed(args, result)]
    actual = result.stdout.strip()
    if actual != expected:
        return [f"expected {expected!r} from `{command} --version` of {source}, got {actual!r}"]
    return _run_extra(runner, source, command, runs, None)


def check_release(
    name: str,
    version: str,
    index: str,
    command: str,
    runs: Sequence[str],
    runner: Runner = _run,
    delays: Sequence[float] = DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """The problems with installing and running name==version from index, retrying while it fails to install or run."""
    source = f"{name}=={version}"
    args = uvx(source, command, ["--version"], index)
    pattern = release_line(name, version)
    failure = ""
    for tries, delay in enumerate(delays, 1):
        sleep(delay)
        result = _call(runner, args)
        if result.returncode != 0:
            failure = _failed(args, result)
            if tries < len(delays):
                print(f"{failure}\nRetrying in {delays[tries]} seconds.", file=sys.stderr)
            continue
        actual = result.stdout.strip()
        # A wrong line won't change by waiting, so it fails at once.
        if pattern.fullmatch(actual) is None:
            expected = f"{name} {version} (<12-hex commit>)"
            return [f"expected `{command} --version` of {source} to print {expected!r}, got {actual!r}"]
        return _run_extra(runner, source, command, runs, index)
    return [f"{source} did not install from {index} and run after {len(delays)} tries: {failure}"]


def _project() -> dict[str, object]:
    path = Path(config.PYPROJECT)
    if not path.is_file():
        return {}
    project = config.read_pyproject(path).get("project", {})
    if not isinstance(project, dict):
        raise config.ConfigError(f"{path}: [project] must be a table")
    return {str(key): value for key, value in project.items()}


def main(
    wheel: Path | None = None,
    version: str | None = None,
    index: str = DEFAULT_INDEX,
    name: str | None = None,
    command: str | None = None,
    runs: Sequence[str] = (),
    runner: Runner = _run,
    delays: Sequence[float] = DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    project = _project()
    if name is None:
        project_name = project.get("name")
        if not isinstance(project_name, str) or not project_name:
            print("check-install: pyproject.toml has no [project] name, so give --name", file=sys.stderr)
            return 2
        name = project_name
    command = command or name
    try:
        if wheel is not None:
            project_version = project.get("version")
            found = find_wheel(wheel, name, project_version if isinstance(project_version, str) else None)
            problems = check_wheel(found, name, command, runs, runner)
            done = f"{found.as_posix()} installs and runs"
        elif version is not None:
            problems = check_release(name, version, index, command, runs, runner, delays, sleep)
            done = f"{name} {version} installs from {index} and runs"
        else:
            print("check-install: give --wheel or --version", file=sys.stderr)
            return 2
    except CheckError as error:
        print(f"check-install: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(done)
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils check-install", description=summary(__doc__))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--wheel", type=Path, help="a wheel, or a directory such as dist/ holding one wheel of the package"
    )
    mode.add_argument("--version", help="install this released version from --index")
    parser.add_argument("--index", help=f"the index to install --version from (default: {DEFAULT_INDEX})")
    parser.add_argument("--name", help="the distribution's name (default: [project] name in pyproject.toml)")
    parser.add_argument("--command", help="the command the package installs (default: the distribution's name)")
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="ARGS",
        help="also run the command with these arguments, split as a shell would, which must exit 0;"
        " repeatable (use --run=ARGS when ARGS starts with -)",
    )
    args = parser.parse_args(argv)
    if args.index is not None and args.version is None:
        parser.error("--index needs --version")
    return main(args.wheel, args.version, args.index or DEFAULT_INDEX, args.name, args.command, args.run)
