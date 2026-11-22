"""Tag a release of the project's version, and create its GitHub release.

It's for a release job, run on the default branch once a version bump merges.
The two subcommands are separate steps, and each does nothing if its work is
already done, so a rerun of a failed job finishes it:

- ``push`` tags the checked-out commit ``v<version>``, from ``pyproject.toml``'s
  ``[project] version``, unless that tag exists, and pushes the tag over SSH
  with the deploy key in ``$RELEASE_DEPLOY_KEY``. The key goes to an ssh-agent
  and never to a file, and the connection trusts only GitHub's published
  Ed25519 host key.
- ``release`` creates the GitHub release for that tag unless one exists, with
  the token in ``$GITHUB_TOKEN``.

The ``release-tag`` action runs both."""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

from repoutils import github
from repoutils.checkout import git_env
from repoutils.config import PYPROJECT, ConfigError, read_pyproject
from repoutils.docstrings import summary

PROG = "repoutils release-tag"
KEY_VARIABLE = "RELEASE_DEPLOY_KEY"
# GitHub's published SSH host key for github.com, from
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"


def version(root: Path = Path()) -> str:
    """``[project] version`` from ``root/pyproject.toml``."""
    path = root / PYPROJECT
    project = read_pyproject(path).get("project", {})
    value = project.get("version") if isinstance(project, dict) else None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: [project] version must be a string")
    return value


def _env() -> dict[str, str]:
    # Git, ssh and the agent get the key on stdin only.
    env = git_env()
    env.pop(KEY_VARIABLE, None)
    return env


def _run_captured(args: list[str], env: dict[str, str], stdin: str | None = None) -> None:
    # stderr is captured for github.guarded() to print if the command fails.
    subprocess.run(args, input=stdin, env=env, stderr=subprocess.PIPE, text=True, check=True)


def _git(*args: str, env: dict[str, str] | None = None) -> None:
    _run_captured(["git", *args], _env() if env is None else env)


def tag_exists(tag: str) -> bool:
    """Whether the checkout has the tag, which needs its tags fetched."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        env=_env(),
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _start_agent(env: dict[str, str]) -> dict[str, str]:
    """Start an ssh-agent, and return the SSH_AUTH_SOCK and SSH_AGENT_PID that it prints."""
    output = subprocess.run(["ssh-agent", "-s"], env=env, capture_output=True, text=True, check=True).stdout
    return dict(re.findall(r"\b(SSH_AUTH_SOCK|SSH_AGENT_PID)=([^;\n]+);", output))


def ssh_command(known_hosts: Path) -> str:
    """The GIT_SSH_COMMAND that trusts only the Ed25519 host key in known_hosts, and none in the system's file."""
    return shlex.join(
        [
            "ssh",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "HostKeyAlgorithms=ssh-ed25519",
        ]
    )


def push_tag(tag: str, remote: str, key: str) -> None:
    """Push the tag to remote over SSH, with key held only by an ssh-agent."""
    env = _env()
    agent = _start_agent(env)
    env |= agent
    try:
        # As a here-string does, which ssh-add needs to read the key's last line.
        _run_captured(["ssh-add", "-q", "-"], env, key if key.endswith("\n") else key + "\n")
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory, "known_hosts")
            known_hosts.write_text(GITHUB_HOST_KEY + "\n")
            env["GIT_SSH_COMMAND"] = ssh_command(known_hosts)
            _git("-c", "core.hooksPath=/dev/null", "push", remote, f"refs/tags/{tag}", env=env)
    finally:
        subprocess.run(["ssh-agent", "-k"], env=env, stdout=subprocess.DEVNULL, check=False)


def push(remote: str, key: str, root: Path = Path()) -> int:
    """Tag the checked-out commit and push the tag, unless the tag exists."""
    tag = f"v{version(root)}"
    if tag_exists(tag):
        print(f"Tag {tag} already exists.")
        return 0
    _git("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "tag", "-a", tag, "-m", f"Release {tag}")
    try:
        push_tag(tag, remote, key)
    except BaseException:
        # So a retry in this checkout pushes the tag rather than finding it here.
        _git("tag", "--delete", tag)
        raise
    print(f"Created and pushed tag {tag}.")
    return 0


def _exists(url: str, token: str) -> bool:
    try:
        github.request("GET", url, token)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        e.close()
        return False
    return True


def release(repo: str, token: str, root: Path = Path()) -> int:
    """Create the GitHub release for the version's tag, unless it exists."""
    tag = f"v{version(root)}"
    quoted = urllib.parse.quote(tag, safe="")
    if _exists(f"{github.API_URL}/repos/{repo}/releases/tags/{quoted}", token):
        print(f"Release {tag} already exists.")
        return 0
    # Creating a release for a missing tag would create the tag, so check it
    # exists first, as `gh release create --verify-tag` does.
    if not _exists(f"{github.API_URL}/repos/{repo}/git/ref/tags/{quoted}", token):
        raise ValueError(f"tag {tag} isn't in {repo}, so there's no commit to release; run `{PROG} push` first")
    github.request(
        "POST",
        f"{github.API_URL}/repos/{repo}/releases",
        token,
        json_body={"tag_name": tag, "name": f"Release {tag}", "generate_release_notes": True},
    )
    print(f"Created GitHub release {tag}.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=summary(__doc__))
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="the repository, as owner/name (default: $GITHUB_REPOSITORY)",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{push,release}")
    subparsers.add_parser(
        "push",
        help="tag the checked-out commit v<version> and push the tag, unless it exists",
        description="Tag the checked-out commit v<version>, from pyproject.toml's [project] version, unless the "
        "checkout has that tag, and push the tag to github.com over SSH with the deploy key in "
        f"${KEY_VARIABLE}. Check out with the tags fetched.",
    )
    subparsers.add_parser(
        "release",
        help="create the GitHub release for tag v<version>, unless it exists",
        description="Create the GitHub release for tag v<version>, with generated notes, unless it exists. "
        "Fails if the tag isn't on GitHub. Uses $GITHUB_TOKEN, defaulting to `gh auth token`, "
        "which needs the Contents write permission.",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if not args.repository:
        raise ValueError("give --repository, or set $GITHUB_REPOSITORY")
    if args.mode == "push":
        key = os.environ.get(KEY_VARIABLE)
        if not key:
            raise ValueError(f"${KEY_VARIABLE} isn't set")
        return push(f"git@github.com:{args.repository}.git", key)
    token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
    return release(args.repository, token)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    return github.guarded(f"{github.annotation('error')}{PROG}: ", lambda: _run(args))


run = main
