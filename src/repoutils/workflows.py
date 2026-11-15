"""Reading GitHub Actions workflows for the secrets and variables they use, and
each job's environment, without a YAML parser. Stdlib-only, like every repoutils module."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

WORKFLOWS = Path(".github") / "workflows"
# The contexts that hold secrets and variables.
SECRETS = "secrets"
VARS = "vars"
# A workflow's use of a secret or variable: secrets.NAME, secrets['NAME'] or
# secrets["NAME"], and vars likewise.
_USE_RES = {
    context: re.compile(rf"\b{context}\s*(?:\.|\[\s*['\"])\s*([A-Za-z_][A-Za-z0-9_]*)") for context in (SECRETS, VARS)
}
# The token GitHub gives every workflow run, which is no one's to set.
_BUILT_IN = "GITHUB_TOKEN"
# A workflow's top-level jobs key, and a key (such as a job's id) that starts a block.
_JOBS_RE = re.compile(r"jobs\s*:\s*")
_BLOCK_KEY_RE = re.compile(r"(['\"]?)([A-Za-z_][A-Za-z0-9_-]*)\1\s*:(?:\s+(.*))?")
# A value's leading anchors (&name) and tags (!tag), which don't make it a flow mapping.
_ANCHORS_AND_TAGS_RE = re.compile(r"^(?:[&!]\S*\s*)+")
# A job's environment key, with its value if it's on the same line.
_ENVIRONMENT_RE = re.compile(r"environment\s*:\s*(.*)")
# A job's environment that only an expression gives, so it can't be known.
UNKNOWN = "${{ }}"
# The keys of a job that GitHub evaluates before the job reaches a runner and
# enters its environment, so an environment's variables are empty in them
# (actions/runner#2472).
JOB_LEVEL_KEYS = ("if", "runs-on", "environment", "name")


def without_comment(line: str) -> str:
    """line up to its YAML or shell comment: a ``#`` that starts the line or
    follows whitespace, outside quotes. An apostrophe in plain text opens a
    quote, so a comment after one is kept, which only errs towards a use."""
    escaped = False
    quote = ""
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _split(text: str, separator: str) -> list[str]:
    """text split at each separator outside quotes, as YAML quotes a scalar."""
    parts = [""]
    escaped = False
    quote = ""
    for char in text:
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == separator:
            parts.append("")
            continue
        parts[-1] += char
    return parts


def _mapping_name(entries: list[str]) -> str:
    """The name in an environment mapping's entries (each ``key: value``), or UNKNOWN."""
    for entry in entries:
        key, *value = _split(entry, ":")
        if value and _unquote(key) == "name":
            return _scalar(":".join(value))
    return UNKNOWN


def _scalar(value: str) -> str:
    """The name a scalar value gives, past any anchor or tag, or UNKNOWN if it
    isn't one this reads: an alias, a block scalar, a collection or an expression."""
    value = _ANCHORS_AND_TAGS_RE.sub("", value.strip(), count=1)
    if not value or value[0] in "*|>{[":
        return UNKNOWN
    name = _unquote(value)
    return UNKNOWN if "${{" in name else name


def workflow_lines(root: Path) -> list[tuple[str, list[str]]]:
    """Each workflow in ``root/.github/workflows``, by file name, as its lines without comments."""
    return [
        (path.name, [without_comment(line) for line in path.read_text(encoding="utf-8").splitlines()])
        for path in sorted((root / WORKFLOWS).glob("*.y*ml"))
    ]


def uses(lines: list[str], context: str = SECRETS) -> set[str]:
    """The names lines use from context (SECRETS or VARS), in upper case, other than GITHUB_TOKEN."""
    return {match.upper() for match in _USE_RES[context].findall("\n".join(lines))} - {_BUILT_IN}


def workflow_uses(root: Path, context: str) -> dict[str, list[str]]:
    """The names the workflows in ``root/.github/workflows`` use from context
    (SECRETS or VARS), other than GITHUB_TOKEN: each name in upper case, with
    the names of the files that use it. Comments don't count."""
    used: dict[str, list[str]] = {}
    for file, lines in workflow_lines(root):
        for name in sorted(uses(lines, context)):
            used.setdefault(name, []).append(file)
    return used


def workflow_secrets(root: Path = Path()) -> dict[str, list[str]]:
    """The secrets the workflows in ``root/.github/workflows`` use (see workflow_uses)."""
    return workflow_uses(root, SECRETS)


def workflow_variables(root: Path = Path()) -> dict[str, list[str]]:
    """The variables the workflows in ``root/.github/workflows`` use (see workflow_uses)."""
    return workflow_uses(root, VARS)


@dataclass(frozen=True)
class Job:
    # The name of its workflow's file.
    file: str
    # Its id, its key under jobs.
    id: str
    # The environment it declares: None if it declares none, UNKNOWN if an expression gives it.
    environment: str | None
    # The secrets it uses, other than GITHUB_TOKEN, in upper case.
    secrets: frozenset[str]
    # The variables it uses, in upper case.
    variables: frozenset[str] = frozenset()
    # Those of its variables it uses in one of JOB_LEVEL_KEYS.
    job_level_variables: frozenset[str] = frozenset()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    """value as YAML reads a scalar: a single-quoted one doubles its apostrophes,
    and a double-quoted one has JSON's escapes (any other is UNKNOWN)."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except ValueError:
            return UNKNOWN
    return value


def _environment(body: list[str]) -> str | None:
    """The environment a job's body (the lines under its id) declares."""
    indent = min((_indent(line) for line in body if line.strip()), default=0)
    for i, line in enumerate(body):
        match = _ENVIRONMENT_RE.fullmatch(line.strip()) if _indent(line) == indent else None
        if not match:
            continue
        value = _ANCHORS_AND_TAGS_RE.sub("", match[1].strip(), count=1)
        if value.startswith("{"):
            # A flow mapping, which this reads only on one line.
            return _mapping_name(_split(value[1:-1], ",")) if value.endswith("}") else UNKNOWN
        if value:
            return _scalar(value)
        # A block mapping: its entries are the lines indented under environment,
        # at the first one's indent (deeper ones belong to an entry's value).
        nested = []
        for line in body[i + 1 :]:
            if line.strip() and _indent(line) <= indent:
                break
            if line.strip():
                nested.append(line)
        entry_indent = _indent(nested[0]) if nested else 0
        return _mapping_name([line.strip() for line in nested if _indent(line) == entry_indent])
    return None


def _job_level_lines(body: list[str]) -> list[str]:
    """The lines of a job's body (the lines under its id) that give one of
    JOB_LEVEL_KEYS: the key's own line, and the lines indented under it."""
    indent = min((_indent(line) for line in body if line.strip()), default=0)
    found: list[str] = []
    inside = False
    for line in body:
        if line.strip() and _indent(line) <= indent:
            match = _BLOCK_KEY_RE.fullmatch(line.strip())
            inside = match is not None and _indent(line) == indent and match[2] in JOB_LEVEL_KEYS
        if inside:
            found.append(line)
    return found


def jobs(file: str, lines: list[str]) -> list[Job]:
    """The jobs in a workflow's lines (without comments), read by indentation, as
    YAML's block style lays them out. A workflow written another way has none."""
    start = next((i for i, line in enumerate(lines) if _JOBS_RE.fullmatch(line.rstrip())), None)
    if start is None:
        return []
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not _indent(line):
            break
        block.append(line)
    job_indent = next((_indent(line) for line in block if line.strip()), 0)
    # Each job's id, its body, and whether it's written on one line (as a flow mapping).
    bodies: list[tuple[str, list[str], bool]] = []
    for line in block:
        if line.strip() and _indent(line) == job_indent:
            match = _BLOCK_KEY_RE.fullmatch(line.strip())
            if match:
                value = match[3] or ""
                flow = bool(_ANCHORS_AND_TAGS_RE.sub("", value, count=1).strip())
                bodies.append((match[2], [value], flow))
        elif bodies:
            bodies[-1][1].append(line)
    return [
        Job(
            file,
            job_id,
            UNKNOWN if flow else _environment(body[1:]),
            frozenset(uses(body, SECRETS)),
            frozenset(uses(body, VARS)),
            frozenset() if flow else frozenset(uses(_job_level_lines(body[1:]), VARS)),
        )
        for job_id, body, flow in bodies
    ]


def workflow_jobs(root: Path = Path()) -> list[Job]:
    """The jobs of every workflow in ``root/.github/workflows``."""
    return [job for file, lines in workflow_lines(root) for job in jobs(file, lines)]
