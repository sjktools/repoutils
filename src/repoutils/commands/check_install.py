"""Check that a built wheel, or a released version, installs and records the
commit it was built from.

It installs the package with `uv run --isolated --no-cache`, and reads the
`build_info.json` that the `repoutils-build-info` hook added to it, through
the installed distribution's metadata, so the package needs no command.

- `--wheel` takes a wheel, or the directory (such as dist/) that holds it.
  The file's commit, and whether it had uncommitted changes, must match the
  checkout in the current directory.
- `--version` installs that version from the index that `--index` names
  (default PyPI), retrying with backoff while an install fails, such as while
  the index catches up with a new upload. Each command it runs installs the
  version again, so it also retries while one of them exits non-zero, as the
  index can serve the version to one install and not yet to the next. Any
  commit will do, as the checkout it runs in need not be the one the release
  was built from.

Options for a package that installs a command (by default, named after the
package):

- `--version-line` requires `<command> --version` to print
  `<name> <version> (<12-hex commit>[ with uncommitted changes])` exactly,
  with the commit the file records, as `repoutils --version` does.
- Each `--run` runs the command with those arguments, which must exit 0.
- Each `--run-with-commit` does the same, and its output must also give the
  whole commit the file records.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from repoutils import build_info, config
from repoutils.checkout import head_commit
from repoutils.docstrings import summary

DEFAULT_INDEX = "https://pypi.org/simple/"
# Seconds to wait before each try, while the index catches up with an upload.
DELAYS = (0, 10, 20, 40, 80, 160)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")

# Run in the installed package's environment, with -I so that neither the
# current directory nor PYTHONPATH can put another copy of the package first.
# It prints the installed version and each build_info.json the distribution
# has (or only the one that argv[2] names), outside its .dist-info, and the
# commands it installs: its console scripts and the files it puts in the
# environment's scripts directory (bin/, or Scripts\\ on Windows).
PROBE = """
import json, sys, sysconfig
from importlib.metadata import distribution
from pathlib import Path
scripts = Path(sysconfig.get_path("scripts")).resolve()
dist = distribution(sys.argv[1])
found = {}
commands = {entry.name for entry in dist.entry_points if entry.group == "console_scripts"}
for file in dist.files or ():
    path = file.as_posix()
    wanted = path == sys.argv[2] if len(sys.argv) > 2 else file.name == "build_info.json"
    if wanted and ".dist-info/" not in path:
        found[path] = file.read_text()
    if Path(file.locate()).resolve().parent == scripts:
        commands.add(file.name.removesuffix(".exe"))
print(json.dumps({"version": dist.version, "files": found, "commands": sorted(commands)}))
"""

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Without PYTHONPATH, which the justfile's `repoutils` variable sets to this
    # checkout's src/, so that the installed copy of a package runs, not that one.
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    return subprocess.run(args, capture_output=True, text=True, check=False, env=env)


class CheckError(Exception):
    """The check can't run, such as for a missing wheel or uv."""


@dataclass(frozen=True)
class Run:
    """A `--run` (or with `with_commit`, a `--run-with-commit`): arguments to run the command with."""

    args: list[str]
    with_commit: bool = False


@dataclass(frozen=True)
class Options:
    """What to check beyond the build info."""

    command: str
    version_line: bool = False
    runs: Sequence[Run] = ()
    build_info_path: str | None = None


@dataclass(frozen=True)
class Installed:
    """The installed version, and the commit its build info records."""

    version: str
    commit: str
    dirty: bool
    # The commands the distribution installs.
    commands: tuple[str, ...] = ()


def wheel_name(name: str) -> str:
    """The distribution name as a wheel's file name gives it (PEP 427, as normalized by PEP 503)."""
    return re.sub(r"[-_.]+", "_", name).lower()


# PEP 440's version pattern (its appendix B), for normalize_version.
_VERSION_RE = re.compile(
    r"""v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}


def normalize_version(version: str) -> str | None:
    """version as PEP 440 normalizes it, and wheel file names give it (`1.0-RC1` is `1.0rc1`),
    or None if it isn't a PEP 440 version."""
    match = _VERSION_RE.fullmatch(version.strip())
    if match is None:
        return None
    parts = match.groupdict()
    text = f"{int(parts['epoch'])}!" if parts["epoch"] and int(parts["epoch"]) else ""
    text += ".".join(str(int(number)) for number in parts["release"].split("."))
    if parts["pre_l"] is not None:
        label = parts["pre_l"].lower()
        text += f"{_PRE.get(label, label)}{int(parts['pre_n'] or 0)}"
    if parts["post_n1"] is not None or parts["post_l"] is not None:
        text += f".post{int(parts['post_n1'] or parts['post_n2'] or 0)}"
    if parts["dev_l"] is not None:
        text += f".dev{int(parts['dev_n'] or 0)}"
    if parts["local"] is not None:
        local = re.split(r"[-_.]", parts["local"].lower())
        text += "+" + ".".join(str(int(part)) if part.isdigit() else part for part in local)
    return text


def find_wheel(path: Path, name: str, version: str | None) -> Path:
    """path if it's a file, or else the one wheel of name (at version, if given) in the directory path.
    A version that isn't a PEP 440 version matches any one wheel of name."""
    if path.is_file():
        return path
    if not path.is_dir():
        raise CheckError(f"{path.as_posix()} is neither a wheel nor a directory")
    normalized = normalize_version(version) if version is not None else None
    pattern = f"{wheel_name(name)}-{normalized or '*'}-*.whl"
    wheels = sorted(path.glob(pattern))
    if len(wheels) != 1:
        found = ", ".join(wheel.name for wheel in wheels) or "none"
        raise CheckError(f"expected one wheel matching {path.as_posix()}/{pattern}, found {found}")
    return wheels[0]


def uv_run(source: str, args: Sequence[str], index: str | None = None) -> list[str]:
    """The uv command line that installs source in an isolated environment, with no cache, and runs args there."""
    index_args = ["--default-index", index] if index is not None else []
    return ["uv", "run", "--quiet", "--isolated", "--no-project", "--no-cache", *index_args, "--with", source, *args]


def _call(runner: Runner, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args)
    except FileNotFoundError as error:
        raise CheckError(f"can't run {args[0]}: {error}") from error


def shown(url: str) -> str:
    """url without the user name and password it may hold, to print."""
    parts = urllib.parse.urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc="***@" + parts.netloc.rpartition("@")[2]))


def _shown_args(args: list[str]) -> str:
    return shlex.join([shown(arg) if arg.lower().startswith(("http://", "https://")) else arg for arg in args])


def _failed(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout + result.stderr).rstrip()
    return f"`{_shown_args(args)}` exited {result.returncode}" + (f":\n{output}" if output else "")


def read_probe(name: str, source: str, stdout: str) -> tuple[Installed | None, list[str]]:
    """The installed version and build info from what PROBE printed, or the problems with them."""
    try:
        probe = json.loads(stdout)
        version, files = str(probe["version"]), dict(probe["files"])
        commands = tuple(str(command) for command in probe["commands"])
    except (ValueError, KeyError, TypeError):
        return None, [f"can't read what the probe of {source} printed: {stdout!r}"]
    if len(files) != 1:
        found = ", ".join(sorted(files)) or "none"
        return None, [f"expected one build_info.json in {name} from {source}, found {found} (see --build-info)"]
    [(path, text)] = files.items()
    try:
        info = json.loads(text)
        commit, dirty = info["commit"], info["dirty"]
    except (ValueError, KeyError, TypeError):
        return None, [f"{path} in {source} is not the build info that the repoutils-build-info hook writes"]
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None or not isinstance(dirty, bool):
        return None, [f"{path} in {source} records no commit, as its build had no Git checkout: {text.strip()}"]
    return Installed(version, commit, dirty, commands), []


def _probe_args(name: str, source: str, options: Options, index: str | None) -> list[str]:
    extra = [options.build_info_path] if options.build_info_path is not None else []
    return uv_run(source, ["python", "-I", "-c", PROBE, name, *extra], index)


def _check_command(
    runner: Runner, name: str, source: str, options: Options, installed: Installed, index: str | None
) -> tuple[list[str], bool]:
    """The problems with the command's --version line and runs, which run once the build info passes,
    and whether any of them exited non-zero, which may mean that installing it again failed."""
    if (options.version_line or options.runs) and options.command not in installed.commands:
        # uv run would find a command of that name elsewhere on PATH, which isn't the one under test.
        installs = ", ".join(installed.commands) or "none"
        return [f"{name} from {source} installs no command {options.command!r} (it installs: {installs})"], False
    problems: list[str] = []
    exited = False
    if options.version_line:
        args = uv_run(source, [options.command, "--version"], index)
        info = build_info.BuildInfo(installed.version, installed.commit, installed.dirty, None)
        expected = build_info.short(info, name)
        result = _call(runner, args)
        if result.returncode != 0:
            problems.append(_failed(args, result))
            exited = True
        elif result.stdout.strip() != expected:
            problems.append(f"expected `{_shown_args(args)}` to print {expected!r}, got {result.stdout.strip()!r}")
    whole = re.compile(rf"\b{installed.commit}\b")
    for run in options.runs:
        args = uv_run(source, [options.command, *run.args], index)
        result = _call(runner, args)
        if result.returncode != 0:
            problems.append(_failed(args, result))
            exited = True
        elif run.with_commit and whole.search(result.stdout) is None:
            problems.append(f"expected `{_shown_args(args)}` to print the commit {installed.commit}")
    return problems, exited


def check_wheel(wheel: Path, name: str, options: Options, runner: Runner = _run) -> list[str]:
    """The problems with installing and running wheel, compared with the checkout in the current directory."""
    head = head_commit(Path())
    if head is None:
        raise CheckError("can't find the commit the current directory has checked out, to compare with the wheel's")
    source = wheel.as_posix()
    args = _probe_args(name, source, options, None)
    result = _call(runner, args)
    if result.returncode != 0:
        return [_failed(args, result)]
    installed, problems = read_probe(name, source, result.stdout)
    if installed is None:
        return problems
    if (installed.commit, installed.dirty) != head:
        changes = {True: " with uncommitted changes", False: ""}
        return [
            f"{source} was built from {installed.commit}{changes[installed.dirty]},"
            f" but this checkout has {head[0]}{changes[head[1]]}"
        ]
    return _check_command(runner, name, source, options, installed, None)[0]


def check_release(
    name: str,
    version: str,
    index: str,
    options: Options,
    runner: Runner = _run,
    delays: Sequence[float] = DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """The problems with installing name==version from index, retrying while it fails to install or run."""
    source = f"{name}=={version}"
    args = _probe_args(name, source, options, index)
    failure = ""
    for tries, delay in enumerate(delays, 1):
        sleep(delay)
        result = _call(runner, args)
        if result.returncode != 0:
            failure = _failed(args, result)
            if tries < len(delays):
                print(f"{failure}\nRetrying in {delays[tries]} seconds.", file=sys.stderr)
            continue
        # What an install gives won't change by waiting, so a wrong build info or output fails at once.
        installed, problems = read_probe(name, source, result.stdout)
        if installed is None:
            return problems
        # Each command installs the version again, which can fail while the index catches up,
        # as uv's error can't be told from the command's own exit.
        problems, exited = _check_command(runner, name, source, options, installed, index)
        if not exited or tries == len(delays):
            return problems
        failure = "\n".join(problems)
        print(f"{failure}\nRetrying in {delays[tries]} seconds.", file=sys.stderr)
    return [f"{source} did not install from {shown(index)} after {len(delays)} tries: {failure}"]


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
    runs_with_commit: Sequence[str] = (),
    version_line: bool = False,
    build_info_path: str | None = None,
    runner: Runner = _run,
    delays: Sequence[float] = DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    project = _project()
    # [project] version is this project's, so it says nothing about another that --name names.
    project_name = project.get("name")
    same = name is None or (isinstance(project_name, str) and wheel_name(name) == wheel_name(project_name))
    project_version = project.get("version") if same else None
    if name is None:
        project_name = project.get("name")
        if not isinstance(project_name, str) or not project_name:
            print("check-install: pyproject.toml has no [project] name, so give --name", file=sys.stderr)
            return 2
        name = project_name
    try:
        parsed = [Run(shlex.split(run)) for run in runs]
        parsed += [Run(shlex.split(run), with_commit=True) for run in runs_with_commit]
    except ValueError as error:
        print(f"check-install: can't split a --run's arguments: {error}", file=sys.stderr)
        return 2
    options = Options(command or name, version_line, parsed, build_info_path)
    try:
        if wheel is not None:
            found = find_wheel(wheel, name, project_version if isinstance(project_version, str) else None)
            problems = check_wheel(found, name, options, runner)
            done = f"{found.as_posix()} installs and records this checkout's commit"
        elif version is not None:
            problems = check_release(name, version, index, options, runner, delays, sleep)
            done = f"{name} {version} installs from {shown(index)} and records its commit"
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
    parser.add_argument(
        "--build-info",
        metavar="PATH",
        help="the build info file's path in the installed distribution, such as example/build_info.json"
        " (default: its one file named build_info.json)",
    )
    parser.add_argument(
        "--command", help="the command the package installs, for the options below (default: the distribution's name)"
    )
    parser.add_argument(
        "--version-line",
        action="store_true",
        help="require `<command> --version` to print `<name> <version> (<12-hex commit>)`, as repoutils does",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="ARGS",
        help="also run the command with these arguments, split as a shell would, which must exit 0;"
        " repeatable (use --run=ARGS when ARGS starts with -)",
    )
    parser.add_argument(
        "--run-with-commit",
        action="append",
        default=[],
        metavar="ARGS",
        help="as --run, and the output must also give the whole commit; repeatable",
    )
    args = parser.parse_args(argv)
    if args.index is not None and args.version is None:
        parser.error("--index needs --version")
    return main(
        args.wheel,
        args.version,
        args.index or DEFAULT_INDEX,
        args.name,
        args.command,
        args.run,
        args.run_with_commit,
        args.version_line,
        args.build_info,
    )
