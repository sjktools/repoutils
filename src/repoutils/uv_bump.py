"""`dependabot-uv bump`'s file changes, kept apart from the command for length.

It moves every place that `check-pins` ties to `[tool.uv] required-version`'s
minimum from one uv version to another, then re-locks with the new uv. It
changes nothing unless check-pins passes first, and checks that check-pins
passes after, so it only ever moves a project from one consistent state to
another."""

import re
import subprocess
import tempfile
from pathlib import Path

from repoutils import config
from repoutils.commands import check_pins


def version_key(version: str) -> tuple[int, ...]:
    """A uv version (X.Y.Z) as numbers, for comparing."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise ValueError(f"uv {version} isn't a version of the form X.Y.Z")
    return tuple(int(part) for part in version.split("."))


def _pyproject(text: str, old: str, new: str) -> str:
    """text with [tool.uv] required-version's minimum set to new."""
    # A table's keys can come in any order, so this finds the key by name, and
    # uv_minimum() afterwards checks it was the one under [tool.uv].
    pattern = re.compile(
        rf"""^(?P<head>\s*required-version\s*=\s*["']>=\s*){re.escape(old)}(?=[\s,"'])""", re.MULTILINE
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(
            f'{config.PYPROJECT} has {len(matches)} `required-version = ">={old}..."` lines, not 1, so bump it by hand'
        )
    return pattern.sub(lambda m: m["head"] + new, text)


def _dockerfile(text: str, new: str) -> str:
    """text with each FROM ghcr.io/astral-sh/uv stage set to uv new. Each
    is uv old, as bump() runs this only once check-pins passes."""
    out: list[str] = []
    pos = 0
    for m in check_pins.DOCKER_UV_RE.finditer(text):
        if text[m.end() : m.end() + 1] == "@":
            # A new tag with the old digest would still pull the old image.
            raise RuntimeError("Dockerfile pins ghcr.io/astral-sh/uv by digest, so bump it by hand")
        out += [text[pos : m.start("version")], new]
        pos = m.end("version")
    return "".join([*out, text[pos:]])


def bump(root: Path, old: str, new: str) -> list[Path]:
    """Move the files check-pins checks from uv old to uv new, and return the
    paths it changed. It writes nothing unless it can change every one, and
    puts them all back if check-pins then fails."""
    found, _ = check_pins.problems(root)
    if found:
        raise RuntimeError("check-pins fails, so fix these first:\n" + "\n".join(found))
    pyproject = root / config.PYPROJECT
    texts = {pyproject: pyproject.read_text()}
    for path in check_pins.workflow_files(root):
        texts[path] = path.read_text()
    dockerfile = root / "Dockerfile"
    if dockerfile.is_file():
        texts[dockerfile] = dockerfile.read_text()

    changed: dict[Path, str] = {}
    for path, text in texts.items():
        if path == pyproject:
            new_text = _pyproject(text, old, new)
        elif path == dockerfile:
            new_text = _dockerfile(text, new)
        else:
            new_text, _ = check_pins.set_pins(text, *check_pins.UV, old, new)
        if new_text != text:
            changed[path] = new_text

    try:
        for path, text in changed.items():
            path.write_text(text)
        problems, _ = check_pins.problems(root)
        minimum = check_pins.uv_minimum(root)
        if problems or minimum != new:
            raise RuntimeError(
                f"After the bump, [tool.uv] required-version's minimum is {minimum}, and check-pins reports:\n"
                + "\n".join(problems or ["nothing"])
            )
    except BaseException:
        for path in changed:
            path.write_text(texts[path])
        raise
    return sorted(changed)


def lock(root: Path, version: str) -> bool:
    """Re-lock root's uv.lock, if it has one, with uv version, keeping every
    package's version, and say whether uv.lock changed. It runs the new uv
    through `uv tool run`, from a directory outside the project, as the uv on
    PATH may be older than the project now requires."""
    path = root / "uv.lock"
    if not path.is_file():
        return False
    before = path.read_bytes()
    with tempfile.TemporaryDirectory() as outside:
        subprocess.run(
            ["uv", "tool", "run", f"uv@{version}", "lock", "--directory", str(root.resolve())],
            cwd=outside,
            check=True,
        )
    return path.read_bytes() != before
