"""Check that every workflow or local composite action step that installs uv
or just pins the version the project requires: pyproject.toml's [tool.uv]
required-version minimum and the justfile's minimum-version. A Dockerfile's
ghcr.io/astral-sh/uv stage, if there is one, must use that uv too.

It also checks that the actions in .github/workflows/actions.lock from a
package's GitHub repository are at the commit that uv.lock's version of that
package was built from, as its wheel's build_info.json records. A package with
no build_info.json only needs its actions' refs to be v<version>, with a
warning."""

import argparse
import itertools
import re
import sys
from pathlib import Path

from repoutils import config, locked_actions
from repoutils.docstrings import summary

# (action, the `with:` input that pins its version)
UV = ("astral-sh/setup-uv", "version")
JUST = ("extractions/setup-just", "just-version")

_STEP_RE = re.compile(r"^(?P<indent>\s*)- ")
# A step's own `- ` marker, which YAML lets have more than one space.
_ITEM_RE = re.compile(r"^\s*-\s+")
_USES_RE = re.compile(r"^uses:\s*[\"']?(?P<action>[^@\s\"']+)@")
_WITH_RE = re.compile(r"^with:\s*(?:#.*)?$")
_JUST_MIN_RE = re.compile(r"^set minimum-version := '(?P<version>[^']+)'$", re.MULTILINE)
# The tag, without any @sha256: digest.
# A FROM line's flags (e.g. --platform=$BUILDPLATFORM) come before the image.
DOCKER_UV_RE = re.compile(
    r"^FROM\s+(?:--\S+\s+)*ghcr\.io/astral-sh/uv:(?P<version>[^\s@]+)", re.MULTILINE | re.IGNORECASE
)


def steps(text: str) -> list[list[str]]:
    """The outermost YAML sequence items in a workflow (so each job step, but
    not a list nested in one), each as its lines: an item runs from its `- `
    line up to the next line (ignoring blanks and comments) indented no deeper
    than that `- `."""
    lines = text.splitlines()
    return [lines[span] for span in step_spans(lines)]


def step_spans(lines: list[str]) -> list[slice]:
    """Where each of steps()' items sits in a workflow's lines."""
    result: list[slice] = []
    start = indent = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if start >= 0 and (not stripped or stripped.startswith("#") or len(line) - len(line.lstrip()) > indent):
            continue
        if start >= 0:
            result.append(slice(start, i))
        start = -1
        if match := _STEP_RE.match(line):
            start, indent = i, len(match["indent"])
    if start >= 0:
        result.append(slice(start, len(lines)))
    return result


def inputs(step: list[str]) -> list[str]:
    """The lines of a step's `with:` mapping that start one of its keys, so
    that a key in another of the step's mappings (`env:`), or a line of a
    multi-line value (`sparse-checkout: |`), isn't taken for an input."""
    return [step[i] for i in input_indexes(step)]


def input_indexes(step: list[str]) -> list[int]:
    """Where each of inputs()' lines sits in the step."""
    numbered = [(n, line) for n, line in enumerate(step) if line.strip() and not line.strip().startswith("#")]
    lines = [line for _, line in numbered]
    for i, (indent, key) in enumerate(_keys(lines)):
        if _WITH_RE.match(key):
            mapping = list(itertools.takewhile(lambda body: _depth(body[1]) > indent, numbered[i + 1 :]))
            return [n for n, key in mapping if _depth(key) == _depth(mapping[0][1])]
    return []


def _keys(lines: list[str]) -> list[tuple[int, str]]:
    """Each line of a step (blanks and comments already dropped) with its
    indent and, where it starts one of the step's own keys, that key's text, or
    "" where it doesn't. The step's keys sit just past its `- `, so a line
    deeper in (in a `run: |` script, say) isn't taken for one of them. A
    step always has its `- ` line, so lines is never empty."""
    item = _ITEM_RE.match(lines[0])
    key_depth = item.end() if item else _depth(lines[0])
    keys = [(key_depth, lines[0][key_depth:])]
    keys += [(_depth(line), line.strip() if _depth(line) == key_depth else "") for line in lines[1:]]
    return keys


def _uses(step: list[str]) -> str | None:
    """The action a step uses, or None if it runs a script instead."""
    lines = [line for line in step if line.strip() and not line.strip().startswith("#")]
    for _, key in _keys(lines):
        if match := _USES_RE.match(key):
            return match["action"]
    return None


def _depth(line: str) -> int:
    return len(line) - len(line.lstrip())


def _key_re(key: str) -> re.Pattern[str]:
    """Matches an input line, with the value in whichever of its groups
    double, single or plain matched. It takes the whole value, less its quotes
    or a trailing comment, so an unexpected one (`${{ env.UV }}`, `latest`,
    `"0.13.6 # x"`) is reported as written."""
    return re.compile(
        rf"""^\s*{re.escape(key)}:\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<plain>.*?))\s*(?:\s#.*)?$"""
    )


def _value_group(match: re.Match[str]) -> str:
    return next(name for name in ("double", "single", "plain") if match[name] is not None)


def pins(text: str, action: str, key: str) -> list[str | None]:
    """The value of input key in each step of a workflow that uses action, or
    None where that step doesn't set it."""
    key_re = _key_re(key)
    found: list[str | None] = []
    for step in steps(text):
        if _uses(step) == action:
            values = [m[_value_group(m)] for line in inputs(step) if (m := key_re.match(line))]
            found.append(values[0] if values else None)
    return found


def set_pins(text: str, action: str, key: str, old: str, new: str) -> tuple[str, int]:
    """text with input key set to new, keeping its quotes and any comment, in
    each step that uses action where key is old, and how many steps changed."""
    key_re = _key_re(key)
    bare, lines = text.splitlines(), text.splitlines(keepends=True)
    changed = 0
    for span in step_spans(bare):
        if _uses(bare[span]) != action:
            continue
        for i in input_indexes(bare[span]):
            if m := key_re.match(bare[span.start + i]):
                if m[_value_group(m)] == old:
                    # The line's ending follows bare's text, so spans in it hold in lines too.
                    start, end = m.span(_value_group(m))
                    line = lines[span.start + i]
                    lines[span.start + i] = line[:start] + new + line[end:]
                    changed += 1
                break
    return "".join(lines), changed


def workflow_files(root: Path) -> list[Path]:
    """The workflows, and the local composite actions they use, whose steps the
    checks read: .github/workflows/*.y*ml and .github/actions/**/action.y*ml."""
    github = root / ".github"
    return sorted([*(github / "workflows").glob("*.y*ml"), *(github / "actions").glob("**/action.y*ml")])


def uv_minimum(root: Path) -> str:
    """The minimum in ``root/pyproject.toml``'s ``[tool.uv] required-version``."""
    path = root / config.PYPROJECT
    tool = config.read_pyproject(path).get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    required = uv.get("required-version") if isinstance(uv, dict) else None
    # The lower bound of e.g. ">=0.13.6, <0.14".
    minimum = required.removeprefix(">=").split(",", 1)[0].strip() if isinstance(required, str) else ""
    if not isinstance(required, str) or not required.startswith(">=") or not minimum:
        found = "is missing" if required is None else f"{required!r} isn't of the form >=X.Y.Z"
        raise config.ConfigError(f"{path}: [tool.uv] required-version {found}")
    return minimum


def minimums(root: Path) -> dict[tuple[str, str], str]:
    uv = uv_minimum(root)
    match = _JUST_MIN_RE.search((root / "justfile").read_text())
    if match is None:
        raise ValueError("justfile has no `set minimum-version := '...'`")
    return {UV: uv, JUST: match["version"]}


def dockerfile_problems(root: Path, uv: str) -> list[str]:
    """Each Dockerfile uv stage whose tag isn't uv. A Dockerfile with no such
    stage is a problem too, as it more likely means DOCKER_UV_RE no longer
    recognizes it; a project with no Dockerfile has nothing to check."""
    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        return []
    tags = [m["version"] for m in DOCKER_UV_RE.finditer(dockerfile.read_text())]
    if not tags:
        return ["Dockerfile has no FROM ghcr.io/astral-sh/uv:<version> stage"]
    return [f"Dockerfile: FROM ghcr.io/astral-sh/uv:{tag}, not {uv}" for tag in tags if tag != uv]


def problems(root: Path) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Each pin that's missing or differs from the minimum, and how many steps
    use each action. No step using one is a problem too, as it more likely
    means steps() or _USES_RE no longer recognize them than that CI stopped
    installing uv or just."""
    want = minimums(root)
    found: list[str] = []
    counts = dict.fromkeys(want, 0)
    for path in workflow_files(root):
        text = path.read_text()
        for (action, key), version in want.items():
            for pin in pins(text, action, key):
                counts[action, key] += 1
                if pin != version:
                    setting = f"{key}: {pin}" if pin else f"no {key}"
                    found.append(f"{path.relative_to(root).as_posix()}: a {action} step has {setting}, not {version}")
    found += [f"no workflow or action has a {action} step" for (action, _), count in counts.items() if not count]
    found += dockerfile_problems(root, want[UV])
    return found, counts


def main(root: Path = Path(), path: list[str] | None = None) -> int:
    """Run the checks in root, looking up uv.lock's packages in path, or
    else sys.path."""
    found, counts = problems(root)
    try:
        actions = locked_actions.check(root, path)
    except locked_actions.LockError as e:
        actions, found = locked_actions.Result(), [*found, str(e)]
    for warning in actions.warnings:
        print(warning, file=sys.stderr)
    found += actions.problems
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    want = minimums(root)
    uv, just = f"uv {want[UV]} in {counts[UV]} steps", f"just {want[JUST]} in {counts[JUST]} steps"
    dockerfile = f", and the Dockerfile uv {want[UV]}" if (root / "Dockerfile").is_file() else ""
    print(f"workflows and actions pin {uv} and {just}{dockerfile}")
    for line in actions.matched:
        print(line)
    return 0


def run(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="repoutils check-pins", description=summary(__doc__)).parse_args(argv)
    return main()
