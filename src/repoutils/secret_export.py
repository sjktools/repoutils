"""``repoutils secrets export``: a backup of the secrets a workflow hands it,
as TOML encrypted with age (https://age-encryption.org), for the workflow to
upload as an artifact. Kept apart from the secrets command for length.

The workflow passes the secrets as ``${{ toJSON(secrets) }}`` in $SECRETS_JSON,
as for ``secrets inspect``. The TOML goes to age on its standard input, so the
plaintext is never written to disk; only the encrypted file is. Each recipient
is an age recipient or SSH public key given as it is, or a key of a GitHub
user's that a pinned fingerprint picks from ``https://github.com/<user>.keys``.
Given the names set at each level in $SECRET_NAMES, as for ``secrets inspect``,
an environment's job fails if the environment doesn't set a secret it lists,
rather than export the repository's or the organization's value as the
environment's.
Stdlib-only, like every repoutils module: age is run as a program."""

import argparse
import base64
import binascii
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from repoutils import config, github, secret_inspect

# The SSH key types age can encrypt to.
AGE_SSH_TYPES = ("ssh-ed25519", "ssh-rsa")
# A GitHub username, then a SHA256 fingerprint as ssh-keygen -l prints it.
_GITHUB_KEY_RE = re.compile(r"(?P<user>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<fingerprint>SHA256:[A-Za-z0-9+/]{43})=?")
KEYS_URL = "https://github.com/{user}.keys"


def github_key(text: str) -> tuple[str, str]:
    """The user and fingerprint ``USER/SHA256:...`` gives, for argparse."""
    match = _GITHUB_KEY_RE.fullmatch(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"{text!r} isn't USER/SHA256:FINGERPRINT, as `ssh-keygen -l` prints it")
    return match["user"], match["fingerprint"]


def fetch_keys(user: str) -> str:
    """The SSH public keys GitHub lists for user, one per line."""
    url = KEYS_URL.format(user=urllib.parse.quote(user))
    with github.no_redirect_opener.open(urllib.request.Request(url), timeout=60) as response:
        text: str = response.read().decode()
        return text


def pick_key(user: str, fingerprint: str, keys: str) -> str:
    """The key in keys (as ``<user>.keys`` gives them) whose fingerprint is
    fingerprint, as ``TYPE BASE64``: failing if none is, or age can't use it."""
    for line in keys.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except binascii.Error:
            continue
        if secret_inspect.fingerprint(blob) != fingerprint:
            continue
        if parts[0] not in AGE_SSH_TYPES:
            raise ValueError(
                f"{user}'s key {fingerprint} is {parts[0]}, which age can't encrypt to: "
                f"use an {' or '.join(AGE_SSH_TYPES)} key, or an age recipient with --recipient"
            )
        return f"{parts[0]} {parts[1]}"
    raise ValueError(f"none of the keys at {KEYS_URL.format(user=user)} has the fingerprint {fingerprint}")


def export_toml(table: str, values: dict[str, str], header: Iterable[str]) -> str:
    """values as table, after header's lines as comments."""
    lines = [*(f"# {line}" for line in header), table]
    lines += [f"{config.toml_key(name)} = {config.toml_string(value)}" for name, value in values.items()]
    return "".join(f"{line}\n" for line in lines)


def encrypt(age: str, recipients: list[str], plaintext: bytes, output: Path, *, armor: bool = False) -> None:
    """Encrypt plaintext with age to each recipient, into output, passing it on
    age's standard input. age writes to a new temporary file beside output, which
    replaces output only once age succeeds, so a failure leaves any earlier file
    as it was, and two exports to the same output never share a temporary file.
    With armor, age writes PEM-style text rather than binary."""
    args = [age, "--encrypt", *(["--armor"] if armor else [])]
    args += [arg for recipient in recipients for arg in ("--recipient", recipient)]
    fd, name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".partial")
    os.close(fd)
    partial = Path(name)
    try:
        try:
            result = subprocess.run([*args, "--output", name], input=plaintext, capture_output=True, check=False)
        except FileNotFoundError:
            raise ValueError(f"can't run {age}: install age, or give its path with --age") from None
        if result.returncode:
            raise RuntimeError(f"age failed (exit {result.returncode}):\n{result.stderr.decode(errors='replace')}")
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def _run_url() -> str | None:
    parts = [os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    if not all(parts):
        return None
    server, repo, run_id = parts
    return f"{server}/{repo}/actions/runs/{run_id}"


def summary_markdown(
    title: str, names: list[str], recipients: list[str], unlisted: list[str], output: Path, note: str = ""
) -> str:
    lines = [f"## {title}", ""]
    if note:
        lines += [f"> [!NOTE]\n> {note}", ""]
    lines += [f"Exported {len(names)} secret(s), encrypted with age to:", ""]
    lines += [f"- {recipient}" for recipient in recipients]
    lines += ["", "Secrets: " + ", ".join(f"`{name}`" for name in names) + "."]
    if unlisted:
        lines += ["", "Not listed in pyproject.toml, so not exported: " + ", ".join(f"`{n}`" for n in unlisted) + "."]
    lines += [
        "",
        "Download the artifact, delete it from the run, and unzip it.",
        "Then decrypt it with the private key's file (an SSH key, such as `~/.ssh/id_ed25519`, or an age identity):",
        "",
        "```sh",
        f"age --decrypt --identity ~/.ssh/id_ed25519 --output secrets.toml {output.name}",
        "```",
    ]
    return "".join(f"{line}\n" for line in lines) + "\n"


def export(
    wanted: list[str],
    listed: Iterable[str],
    environment: str | None,
    *,
    recipients: list[str],
    github_keys: list[tuple[str, str]],
    output: Path,
    age: str = "age",
    armor: bool = False,
    summary: Path | None = None,
    secrets_json: str | None = None,
    now: datetime | None = None,
    levels: secret_inspect.Levels | None = None,
) -> int:
    """Encrypt the secrets in wanted, which the job has in secrets_json (default
    $SECRETS_JSON), into output. listed is every name pyproject.toml lists,
    so that the rest can be named. With levels, an environment's job only
    exports secrets the environment itself sets."""
    if not recipients and not github_keys:
        raise ValueError("give at least one --recipient or --github-key to encrypt to")
    if secrets_json is None:
        secrets_json = os.environ.get(secret_inspect.ENV_VAR)
    if secrets_json is None:
        raise ValueError(f"${secret_inspect.ENV_VAR} isn't set: set it to ${{{{ toJSON(secrets) }}}} in the workflow")
    values = secret_inspect.read_secrets(secrets_json)
    missing = [name for name in wanted if name.upper() not in values]
    if missing:
        raise ValueError(f"the job doesn't have these secrets, so nothing was exported: {', '.join(missing)}")
    here = levels.environment(environment) if levels is not None and environment is not None else None
    if levels is not None and here is not None:
        inherited = [f"{name} ({levels.source(name)})" for name in wanted if name.upper() not in here]
        if inherited:
            raise ValueError(
                f"environment {environment} doesn't set these secrets, and the job only inherits them, "
                f"so nothing was exported: {', '.join(inherited)}"
            )
    shown = list(recipients)
    recipients = list(recipients)
    for user, fingerprint in github_keys:
        recipients.append(pick_key(user, fingerprint, fetch_keys(user)))
        shown.append(f"{user}'s SSH key {fingerprint}")
    if environment is None:
        table, title = "[actions]", "Repository secrets"
    else:
        table, title = f"[environments.{config.toml_key(environment)}]", f"Environment {environment}'s secrets"
    now = now or datetime.now(UTC)
    header = [f"{title}, exported by `repoutils secrets export` at {now:%Y-%m-%dT%H:%M:%SZ}."]
    run_url = _run_url()
    if run_url:
        header.append(f"From {run_url}")
    plaintext = export_toml(table, {name: values[name.upper()] for name in wanted}, header)
    encrypt(age, recipients, plaintext.encode(), output, armor=armor)
    known = {name.upper() for name in listed} | {"GITHUB_TOKEN"}
    # An inherited secret is the repository's job's to name.
    unlisted = sorted(name for name in values if name not in known and (here is None or name in here))
    print(f"Exported {len(wanted)} secret(s) to {output}, encrypted to {', '.join(shown)}: {', '.join(wanted)}")
    if unlisted:
        print(f"Not listed in pyproject.toml, so not exported: {', '.join(unlisted)}", file=sys.stderr)
    note = secret_inspect.unknown_levels("secrets export-remote") if environment is not None and levels is None else ""
    if note:
        print(note, file=sys.stderr)
    if summary is not None:
        with summary.open("a") as file:
            file.write(summary_markdown(title, wanted, shown, unlisted, output, note))
    return 0


# argparse has no public name for the type add_subparsers() returns.
def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    """Add the export subcommand and its options."""
    parser = subparsers.add_parser(
        "export",
        help="encrypt the secrets a workflow passes in $SECRETS_JSON with age, for a backup",
        description="Write the secrets pyproject.toml lists, from the values a workflow passes "
        f"as ${{{{ toJSON(secrets) }}}} in ${secret_inspect.ENV_VAR}, as TOML encrypted with age. "
        "The plaintext goes to age on its standard input, and is never written to disk.",
    )
    parser.add_argument(
        "--environment",
        help="export the secrets pyproject.toml lists for this environment (default: the actions list)",
    )
    parser.add_argument(
        "--recipient",
        action="append",
        default=[],
        help="an age recipient (age1...) or SSH public key to encrypt to (repeatable)",
    )
    parser.add_argument(
        "--github-key",
        action="append",
        type=github_key,
        default=[],
        metavar="USER/SHA256:FINGERPRINT",
        help="encrypt to the key of USER's on GitHub with this fingerprint, "
        f"fetched from {KEYS_URL.format(user='USER')} (repeatable)",
    )
    parser.add_argument("--output", type=Path, required=True, help="the encrypted file to write")
    parser.add_argument("--age", default="age", help="the age program to run (default: age)")
    parser.add_argument(
        "--armor", action="store_true", help="write the encrypted file as ASCII text rather than binary"
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        help="append a Markdown report, of names only, to this file (default: $GITHUB_STEP_SUMMARY)",
    )
