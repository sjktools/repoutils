"""
Run `ruff check` without its cache, and fail if ruff gives any warning.

Ruff only warns about some mistakes, such as an invalid noqa directive, and can't make
them errors (astral-sh/ruff#8157). It gives those warnings only for the files it parses,
so `--no-cache` makes it parse every file.

The command passes its arguments on to `ruff check`, and runs the `ruff` on `PATH`, such
as the one in the project's dev group under `uv run`. It exits with ruff's exit code, or
`1` if ruff exits `0` but writes a line starting `warning:` to standard error.
"""

import argparse
import subprocess
import sys

from repoutils.docstrings import summary

WARNING = "warning:"


def main(ruff_args: list[str]) -> int:
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-cache", *ruff_args], stderr=subprocess.PIPE, text=True, check=False
        )
    except FileNotFoundError as error:
        print(f"repoutils ruff-check: can't run ruff: {error}", file=sys.stderr)
        return 1
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        return result.returncode
    if any(line.startswith(WARNING) for line in result.stderr.splitlines()):
        print("repoutils ruff-check: ruff gave a warning (see above)", file=sys.stderr)
        return 1
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils ruff-check",
        allow_abbrev=False,
        usage="%(prog)s [-h] [RUFF_ARGS ...]",
        description=summary(__doc__),
        epilog="Every argument but -h and --help goes to `ruff check`, in order.",
    )
    _, ruff_args = parser.parse_known_args(argv)
    return main(ruff_args)
