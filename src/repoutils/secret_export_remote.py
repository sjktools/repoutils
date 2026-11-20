"""``repoutils secrets export-remote``: run the workflow that exports the
repository's secrets, wait for it, download the age-encrypted files its jobs
keep as ``secrets-*`` artifacts, and decrypt them to standard output with the
SSH private key in ``~/.ssh/id_ed25519`` or ``~/.ssh/id_rsa``. It's the remote
counterpart of ``secrets export``, which only runs inside a workflow, as
``secrets inspect-remote`` is of ``secrets inspect``. Kept apart from the
secrets command for length.

Only age (run as a program) decrypts the files, so the plaintext is never
written to disk. ``--save DIR`` keeps the encrypted files as well, or instead
when there's no key to decrypt them with. Once every file is decrypted or
saved, it deletes the artifacts from the run, as the workflow's summary asks.
Stdlib-only, like every repoutils module."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from repoutils import github, workflow_summaries

DEFAULT_WORKFLOW = "export-secrets.yml"
# The prefix of the artifacts that hold export-secrets.yml's encrypted files.
ARTIFACT_PREFIX = "secrets-"
# The SSH private keys age can decrypt with, in the order they're tried.
DEFAULT_IDENTITIES = ("id_ed25519", "id_rsa")


def default_identities(home: Path) -> list[Path]:
    """The SSH private keys in home's .ssh directory that age can use."""
    return [path for name in DEFAULT_IDENTITIES if (path := home / ".ssh" / name).is_file()]


def decrypt(age: str, identities: list[Path], ciphertext: bytes) -> bytes:
    """Decrypt ciphertext with age and the identity files, returning the
    plaintext without writing it to disk. age's standard error is left alone,
    so it can ask for a key's passphrase."""
    args = [age, "--decrypt", *(arg for identity in identities for arg in ("--identity", str(identity)))]
    try:
        result = subprocess.run(args, input=ciphertext, stdout=subprocess.PIPE, check=False)
    except FileNotFoundError:
        raise ValueError(f"can't run {age}: install age, or give its path with --age") from None
    if result.returncode:
        raise RuntimeError(f"age failed to decrypt (exit {result.returncode})")
    return result.stdout


def delete_artifact(repo: str, artifact: dict[str, Any], token: str) -> None:
    github.request("DELETE", f"{github.API_URL}/repos/{repo}/actions/artifacts/{artifact['id']}", token)


def export_remote(
    repo: str,
    token: str,
    *,
    workflow: str,
    ref: str | None,
    run_id: int | None,
    interval: float,
    timeout: float,
    identities: list[Path],
    save: Path | None = None,
    age: str = "age",
    keep_artifacts: bool = False,
) -> int:
    """Run (or read) the workflow, and print the secrets its artifacts hold,
    decrypted with identities, saving the encrypted files in save if given.
    Exits 0 only if the run succeeded and kept some."""
    if not identities and save is None:
        raise ValueError(
            "found no SSH private key to decrypt with (~/.ssh/id_ed25519 or ~/.ssh/id_rsa): "
            "give one with --identity, or keep the encrypted files with --save DIR"
        )
    # Check what decrypting and saving need before starting a run they'd fail on.
    if identities and shutil.which(age) is None:
        raise ValueError(f"can't run {age}: install age, or give its path with --age")
    if save is not None:
        save.mkdir(parents=True, exist_ok=True)
        # mkdir() doesn't check an existing directory can be written to, but this does.
        with tempfile.TemporaryFile(dir=save):
            pass
    run_id, run = workflow_summaries.start(
        repo, token, workflow=workflow, ref=ref, run_id=run_id, interval=interval, timeout=timeout
    )
    found = workflow_summaries.artifacts(repo, run_id, token, ARTIFACT_PREFIX)
    plaintexts: list[bytes] = []
    saved: set[Path] = set()
    for artifact in found:
        for path, data in workflow_summaries.files(artifact, token):
            if save is not None:
                # Only the file's name, so a path in the zip can't point outside save.
                target = save / PurePosixPath(path).name
                if target in saved:
                    # Rather than overwrite one backup with another, and then delete both artifacts.
                    raise ValueError(f"two files in run {run_id}'s artifacts would both be saved as {target}")
                saved.add(target)
                target.write_bytes(data)
                print(f"Saved {artifact['name']}/{path} to {target}", file=sys.stderr)
            if identities:
                plaintexts.append(decrypt(age, identities, data))
    # Print only once every file has decrypted, so a failure prints no secrets.
    sys.stdout.write("\n".join(text.decode() for text in plaintexts))
    sys.stdout.flush()
    if not found:
        # Such as when every job was skipped: export-secrets.yml's jobs only run on main.
        print(
            f"Run {run_id} kept no encrypted secrets ({ARTIFACT_PREFIX}* artifacts), so nothing was exported: "
            f"{run.get('html_url')}",
            file=sys.stderr,
        )
        return 1
    if not keep_artifacts:
        for artifact in found:
            delete_artifact(repo, artifact, token)
        print(f"Deleted {', '.join(str(a['name']) for a in found)} from run {run_id}", file=sys.stderr)
    conclusion = run.get("conclusion")
    if conclusion != "success":
        print(f"Run {run_id} finished with conclusion {conclusion}: {run.get('html_url')}", file=sys.stderr)
        return 1
    print(f"Run {run_id} succeeded: {run.get('html_url')}", file=sys.stderr)
    return 0


# argparse has no public name for the type add_subparsers() returns.
def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    """Add the export-remote subcommand and its options."""
    parser = subparsers.add_parser(
        "export-remote",
        help="run the workflow that exports the secrets, and decrypt and print them",
        description=f"Dispatch the workflow that runs `secrets export` (default {DEFAULT_WORKFLOW}), "
        f"wait for it, download the encrypted files its jobs keep as {ARTIFACT_PREFIX}* artifacts, "
        "and print them decrypted with age, using ~/.ssh/id_ed25519 or ~/.ssh/id_rsa (whichever exist). "
        "Then delete the artifacts from the run. "
        "While GitHub holds the run for approval, print where to approve it and keep waiting. "
        "Fails unless the run succeeds and keeps some encrypted files. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`, "
        "which needs the Actions write permission to dispatch and to delete the artifacts, "
        "or only Actions read with --run-id and --keep-artifacts.",
    )
    workflow_summaries.add_run_options(parser, DEFAULT_WORKFLOW)
    parser.add_argument(
        "--identity",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help="decrypt with this SSH private key or age identity file (repeatable; "
        "default: ~/.ssh/id_ed25519 and ~/.ssh/id_rsa, whichever exist)",
    )
    parser.add_argument(
        "--save",
        type=Path,
        metavar="DIR",
        help="also write the encrypted files to this directory, replacing earlier ones of the same names "
        "(decrypting nothing if no key is found)",
    )
    parser.add_argument("--age", default="age", help="the age program to run (default: age)")
    parser.add_argument(
        "--keep-artifacts", action="store_true", help="don't delete the artifacts from the run afterwards"
    )
