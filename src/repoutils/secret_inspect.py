"""``repoutils secrets inspect``: what each secret a workflow hands it is, for
finding the matching token on GitHub's settings pages, without ever showing a
value or any part of one. Kept apart from the secrets command for length.

The workflow passes the secrets as ``${{ toJSON(secrets) }}`` in $SECRETS_JSON.
It inspects every secret the job gets, and warns about any that pyproject.toml
doesn't list for the job, as unexpected, without failing. A value that looks
like a GitHub token is used for an API request of its own, which gives its
owner and its expiry, and is reported with the start of its SHA-256 hash, so
that two secrets holding the same token can be told apart from two holding
different ones. An OpenSSH
private key gives its public key's fingerprint, as GitHub's deploy key page
shows it. Anything else is reported by name only.
Stdlib-only, like every repoutils module."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

from repoutils import github

# The environment variable the workflow passes the secrets in.
ENV_VAR = "SECRETS_JSON"
# The token GitHub gives every run, which toJSON(secrets) includes.
_BUILT_IN = "GITHUB_TOKEN"
# What each GitHub token prefix means, longest first.
TOKEN_TYPES = (
    ("github_pat_", "fine-grained personal access token"),
    ("ghp_", "classic personal access token"),
    ("gho_", "OAuth app token"),
    ("ghu_", "GitHub App user token"),
    ("ghs_", "GitHub App installation token"),
    ("ghr_", "GitHub App refresh token"),
)
# A whole token, which is all a header may carry: anything else isn't sent,
# as urllib's error for a bad header value would quote it.
# Installation tokens may be JWTs (ghs_APPID_JWT), with dots and hyphens.
_TOKEN_RE = re.compile(r"(?:gh[pour]_|github_pat_)[A-Za-z0-9_]+|ghs_[A-Za-z0-9_.-]+")
_PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
_OPENSSH_MAGIC = b"openssh-key-v1\0"
# The hex digits of a token's SHA-256 hash a report shows: enough to tell a
# repository's tokens apart. A GitHub token is far too random for its hash to
# lead back to it, but other secrets may not be, so only tokens get one.
HASH_DIGITS = 12
# The shortest token that gets a hash. GitHub's tokens are at least this long
# (a prefix and 36 random characters), and a shorter value with a token's
# prefix may be short enough to recover from its hash by trying every one.
MIN_HASHED_LENGTH = 40
EXPIRY_HEADER = "GitHub-Authentication-Token-Expiration"


@dataclass
class Finding:
    """What inspecting one secret found."""

    name: str
    what: str
    owner: str = ""
    expires: str = ""
    access: str = ""
    # The start of a GitHub token's SHA-256 hash.
    sha256: str = ""
    # Whether pyproject.toml doesn't list it for the job.
    unexpected: bool = False
    # What breaks a rule: a token GitHub rejects or that expires soon, or a malformed value.
    problems: list[str] = field(default_factory=list)
    # Why it couldn't be inspected, such as a failed request.
    error: str = ""


@dataclass(frozen=True)
class Response:
    headers: Message
    body: Any


def get(path: str, token: str) -> Response:
    """GET path from the API with token, following no redirect."""
    request = urllib.request.Request(f"{github.API_URL}{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    with github.no_redirect_opener.open(request, timeout=60) as response:
        payload = response.read()
        return Response(response.headers, json.loads(payload) if payload else None)


def token_type(value: str) -> str | None:
    """What kind of GitHub token value's prefix says it is, or None."""
    return next((what for prefix, what in TOKEN_TYPES if value.startswith(prefix)), None)


def parse_expiry(text: str) -> datetime | None:
    """The time an expiry header gives, such as ``2026-12-01 00:00:00 UTC``."""
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def token_hash(token: str) -> str:
    """The first HASH_DIGITS hex digits of token's SHA-256 hash."""
    return hashlib.sha256(token.encode()).hexdigest()[:HASH_DIGITS]


def _expiry(finding: Finding, token: str, headers: Message, now: datetime, within: int) -> None:
    header = headers.get(EXPIRY_HEADER)
    if header is None and token.startswith("ghs_"):
        finding.expires = "an hour after it was made"
        # Like a header giving an expiry later today, which only a threshold above 0 flags.
        if within > 0:
            finding.problems.append(
                "installation tokens expire an hour after they're made, so a stored one stops working"
            )
        return
    if header is None:
        # Only a personal access token's missing header means it doesn't expire.
        finding.expires = "never" if token.startswith(("ghp_", "github_pat_")) else "not reported"
        return
    expiry = parse_expiry(header)
    if expiry is None:
        # Fail closed: without the date, the expiry check can't tell whether it passes.
        finding.expires = header.strip()
        finding.error = f"can't read the expiry header {EXPIRY_HEADER}: {header.strip()!r}"
        return
    days = (expiry - now).days
    finding.expires = f"{expiry:%Y-%m-%d} (in {days} days)" if days >= 0 else f"{expiry:%Y-%m-%d} (expired)"
    if days < 0:
        finding.problems.append("expired")
    elif days < within:
        finding.problems.append(f"expires in {days} day(s), on {expiry:%Y-%m-%d}")


def _error(error: urllib.error.HTTPError, path: str) -> str:
    with error:
        detail = error.read().decode(errors="replace")
    try:
        message = json.loads(detail).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    return f"HTTP {error.code} from GET {path}" + (f": {message}" if message else "")


def _inspect_token(finding: Finding, token: str, now: datetime, within: int) -> None:
    if token.startswith("ghr_"):
        finding.access = "a refresh token can't call the API"
        return
    path = ""
    try:
        if token.startswith("ghs_"):
            # An installation token's repositories are the only ones the API
            # reports truly. A fine-grained token's aren't reported at all:
            # /user/repos lists every public repository, which any token can read.
            path = "/installation/repositories?per_page=1"
            response = get(path, token)
            repos = response.body["repositories"]
            finding.owner = repos[0]["owner"]["login"] if repos else ""
            total = response.body["total_count"]
            finding.access = {0: "no repositories", 1: "1 repository"}.get(total, f"{total} repositories")
        else:
            path = "/user"
            response = get(path, token)
            finding.owner = response.body["login"]
            scopes = response.headers.get("X-OAuth-Scopes")
            if scopes is not None:
                finding.access = f"scopes: {scopes.strip() or 'none'}"
        _expiry(finding, token, response.headers, now, within)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            e.close()
            finding.problems.append("GitHub rejects it (HTTP 401): it has expired, or been revoked, or is wrong")
        else:
            finding.error = _error(e, path)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        # Never the token: neither the URL nor any of these messages carries it.
        finding.error = f"GET {path} failed: {type(e).__name__}: {e}"


def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    (length,) = struct.unpack_from(">I", data, offset)
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("truncated")
    return data[offset + 4 : end], end


def fingerprint(blob: bytes) -> str:
    """An SSH public key blob's SHA256 fingerprint, as ``ssh-keygen -l`` and GitHub show it."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def ssh_public_key(value: str) -> tuple[str, str, bool] | None:
    """The key type, the public key's SHA256 fingerprint and whether a
    passphrase protects it, for an OpenSSH private key; None for anything else.
    Only the key's public, unencrypted header is read."""
    match = _PEM_RE.search(value)
    if not match or match.group(1) != "OPENSSH PRIVATE KEY":
        return None
    try:
        data = base64.b64decode("".join(match.group(2).split()), validate=True)
        if not data.startswith(_OPENSSH_MAGIC):
            return None
        cipher, offset = _read_string(data, len(_OPENSSH_MAGIC))
        _, offset = _read_string(data, offset)  # kdfname
        _, offset = _read_string(data, offset)  # kdfoptions
        # The number of keys. Only the first is read: a deploy key's file holds one.
        offset += 4
        blob, _ = _read_string(data, offset)
        key_type, _ = _read_string(blob, 0)
    except (binascii.Error, ValueError, struct.error):
        return None
    return key_type.decode(errors="replace"), fingerprint(blob), cipher != b"none"


def describe(name: str, value: str, now: datetime, within: int) -> Finding:
    """What the secret called name, holding value, is."""
    stripped = value.strip()
    kind = token_type(stripped)
    if kind is not None:
        finding = Finding(name, kind)
        if stripped != value:
            finding.problems.append("has whitespace around the token, which breaks most uses of it")
        if _TOKEN_RE.fullmatch(stripped):
            # Only a well-formed, full-length token: anything else may not be random enough to hash.
            if len(stripped) >= MIN_HASHED_LENGTH:
                finding.sha256 = token_hash(stripped)
            _inspect_token(finding, stripped, now, within)
        else:
            finding.problems.append("has characters a GitHub token can't, so it wasn't tried")
        return finding
    key = ssh_public_key(value)
    if key is not None:
        key_type, fingerprint, encrypted = key
        finding = Finding(name, f"OpenSSH private key ({key_type})", access=fingerprint)
        if encrypted:
            finding.access += ", passphrase-protected"
        return finding
    pem = _PEM_RE.search(value)
    if pem is not None:
        return Finding(name, f"PEM {pem.group(1).lower()}")
    if not value:
        return Finding(name, "empty")
    return Finding(name, "not a GitHub token or SSH key")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ") or " "


def summary_markdown(title: str, findings: list[Finding]) -> str:
    lines = [
        f"## {title}",
        "",
        "| Secret | What it is | Owner | Expires | Access | SHA-256 starts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for f in findings:
        name = f"`{f.name}` (unexpected)" if f.unexpected else f"`{f.name}`"
        cells = (f.what, f.owner, f.expires, f.access, f"`{f.sha256}`" if f.sha256 else "")
        lines.append(f"| {name} | {' | '.join(_cell(cell) for cell in cells)} |")
    problems = [f"- `{f.name}`: {problem}" for f in findings for problem in f.problems]
    errors = [f"- `{f.name}`: {f.error}" for f in findings if f.error]
    unexpected = [f"`{f.name}`" for f in findings if f.unexpected]
    if unexpected:
        lines += [
            "",
            "> [!WARNING]",
            f"> pyproject.toml doesn't list these secrets for this job: {', '.join(unexpected)}.",
            "> Delete each one from where it is set, or list it in pyproject.toml.",
        ]
    if problems:
        lines += ["", "### Problems", "", *problems]
    if errors:
        lines += ["", "### Errors", "", *errors]
    return "".join(f"{line}\n" for line in lines) + "\n"


def _line(f: Finding) -> str:
    parts = [f.what]
    if f.owner:
        parts.append(f"owner {f.owner}")
    if f.expires:
        parts.append(f"expires {f.expires}")
    if f.access:
        parts.append(f.access)
    if f.sha256:
        parts.append(f"SHA-256 starts {f.sha256}")
    return f"{f.name}: {', '.join(parts)}"


def read_secrets(text: str) -> dict[str, str]:
    """The secrets in toJSON(secrets)'s output, by upper-cased name."""
    try:
        secrets = json.loads(text)
    except ValueError:
        # Not the error itself, whose message could quote the text.
        raise ValueError(f"${ENV_VAR} isn't JSON: set it to ${{{{ toJSON(secrets) }}}}") from None
    if not isinstance(secrets, dict) or not all(isinstance(v, str) for v in secrets.values()):
        raise ValueError(f"${ENV_VAR} must be a JSON object of strings, as ${{{{ toJSON(secrets) }}}} gives")
    return {name.upper(): value for name, value in secrets.items()}


# The warning for an unexpected secret, which isn't a problem: the job still passes.
UNEXPECTED = "warning: unexpected, as pyproject.toml doesn't list it for this job"


def inspect(
    title: str,
    wanted: list[str],
    expected: Iterable[str],
    secrets_json: str,
    *,
    within: int,
    exit_zero: bool,
    summary: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Report on each secret in wanted, and on every other secret the job has
    in secrets_json that isn't in expected, which is unexpected. expected is
    every name pyproject.toml lists that the job can get, inspected here or not."""
    now = now or datetime.now(UTC)
    values = read_secrets(secrets_json)
    findings = [
        describe(name, values[name.upper()], now, within) if name.upper() in values else Finding(name, "not set here")
        for name in wanted
    ]
    known = {name.upper() for name in [*wanted, *expected]} | {_BUILT_IN}
    for name in sorted(name for name in values if name not in known):
        finding = describe(name, values[name], now, within)
        finding.unexpected = True
        findings.append(finding)
    for finding in findings:
        print(_line(finding))
    for finding in findings:
        if finding.unexpected:
            print(f"{finding.name}: {UNEXPECTED}")
        for problem in finding.problems:
            print(f"{finding.name}: {problem}")
        if finding.error:
            print(f"{finding.name}: {finding.error}", file=sys.stderr)
    if summary is not None:
        with summary.open("a") as file:
            file.write(summary_markdown(title, findings))
    # An unexpected secret's problems and errors are reported, but never fail:
    # the job only answers for the secrets pyproject.toml lists for it.
    listed = [finding for finding in findings if not finding.unexpected]
    problems = sum(len(finding.problems) for finding in listed)
    errors = sum(1 for finding in listed if finding.error)
    if problems:
        print(f"{problems} problem(s) with the {len(listed)} secret(s) pyproject.toml lists for this job.")
    if errors:
        print(f"{errors} secret(s) couldn't be inspected.", file=sys.stderr)
        return 1
    if problems:
        return 0 if exit_zero else 1
    if len(listed) < len(findings):
        print(
            f"Inspected {len(findings)} secret(s), with no problems in the {len(listed)} "
            "pyproject.toml lists for this job."
        )
    else:
        print(f"Inspected {len(findings)} secret(s), with no problems.")
    return 0


def _days(text: str) -> int:
    days = int(text)
    if days < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return days


# argparse has no public name for the type add_subparsers() returns.
def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    """Add the inspect subcommand and its options."""
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="report what each secret a workflow passes in $SECRETS_JSON is, never its value",
        description="Report what each secret pyproject.toml lists is, from the values a workflow passes "
        f"as ${{{{ toJSON(secrets) }}}} in ${ENV_VAR}: each GitHub token's type, owner and expiry, "
        "from a request made with it, and the start of its SHA-256 hash, and each SSH key's fingerprint. "
        "Also reports every secret the job gets that pyproject.toml doesn't list for it, as unexpected. "
        "Never shows a value. Fails if GitHub rejects a token or it expires soon.",
    )
    inspect_parser.add_argument(
        "--environment",
        help="inspect the secrets pyproject.toml lists for this environment (default: the actions and other lists)",
    )
    inspect_parser.add_argument(
        "--expires-within",
        type=_days,
        default=14,
        metavar="DAYS",
        help="fail if a token expires in fewer than this many days (default: 14)",
    )
    inspect_parser.add_argument(
        "--summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        help="append a Markdown report to this file (default: $GITHUB_STEP_SUMMARY)",
    )
    inspect_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if a token is rejected or expires soon "
        "(still exit 1 if one can't be inspected, unlike `|| true`)",
    )
