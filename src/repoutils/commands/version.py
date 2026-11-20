"""Print repoutils' version, the commit it was built from, and where it runs.

From a source checkout, such as the justfile's `python -m repoutils` from
`src/`, it says so, and gives the version in the checkout's `pyproject.toml`
and the commit Git has checked out.
"""

import argparse
import platform
import sys
from pathlib import Path

from repoutils import build_info
from repoutils.docstrings import summary


def describe(info: build_info.BuildInfo) -> list[str]:
    changes = "unknown" if info.dirty is None else "yes" if info.dirty else "no"
    lines = [
        f"Version: {info.version or 'unknown'}",
        f"Commit: {info.commit or 'unknown'}",
        f"Uncommitted changes: {changes}",
        f"Installed in: {Path(build_info.__file__).parent}",
        f"Python: {platform.python_version()} ({sys.executable})",
    ]
    if info.checkout is not None:
        lines.append(
            f"Running from the source checkout in {info.checkout}, so the version is from its pyproject.toml"
            " and the commit from Git."
        )
    return lines


def main(short: bool = False) -> int:
    info = build_info.read()
    print(build_info.short(info) if short else "\n".join(describe(info)))
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils version", description=summary(__doc__))
    parser.add_argument(
        "--short", action="store_true", help="print only the version and short commit, as `repoutils --version` does"
    )
    return main(parser.parse_args(argv).short)
