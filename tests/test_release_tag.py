import os
import shlex
import subprocess
import time
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import cli, github
from repoutils.checkout import git_env
from repoutils.commands import release_tag
from repoutils.config import ConfigError

PYPROJECT = '[project]\nname = "demo"\nversion = "1.2.3"\n'


def git(directory: Path, *args: str) -> str:
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args]
    return subprocess.run(git, cwd=directory, check=True, capture_output=True, text=True, env=git_env()).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout of a project at version 1.2.3, cloned from tmp_path/remote.git, as the current directory."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(remote), str(checkout))
    (checkout / "pyproject.toml").write_text(PYPROJECT)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "Bump to 1.2.3")
    git(checkout, "push", "-q", "origin", "HEAD:refs/heads/main")
    monkeypatch.chdir(checkout)
    return checkout


@pytest.fixture
def key(tmp_path: Path) -> str:
    """A new Ed25519 private key, without its last newline, as a secret often is."""
    path = tmp_path / "keys" / "id"
    path.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)], check=True)
    return path.read_text().rstrip("\n")


def test_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    assert release_tag.version(tmp_path) == "1.2.3"


@pytest.mark.parametrize("pyproject", ['[project]\nname = "demo"\ndynamic = ["version"]\n', "project = 1\n"])
def test_version_must_be_a_string(tmp_path: Path, pyproject: str) -> None:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    with pytest.raises(ConfigError, match=r"\[project\] version must be a string"):
        release_tag.version(tmp_path)


def test_ssh_command_trusts_only_the_ed25519_key(tmp_path: Path) -> None:
    command = shlex.split(release_tag.ssh_command(tmp_path / "known hosts"))
    assert command == [
        "ssh",
        "-o",
        f"UserKnownHostsFile={tmp_path / 'known hosts'}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
    ]


def test_push_tags_and_pushes_once(checkout: Path, key: str, capsys: pytest.CaptureFixture[str]) -> None:
    remote = str(checkout.parent / "remote.git")
    assert release_tag.push(remote, key) == 0
    assert capsys.readouterr().out == "Created and pushed tag v1.2.3.\n"
    head = git(checkout, "rev-parse", "HEAD")
    assert git(checkout.parent / "remote.git", "rev-parse", "refs/tags/v1.2.3^{commit}") == head
    # Annotated, by the bot.
    assert git(checkout, "cat-file", "-t", "refs/tags/v1.2.3") == "tag"
    tag = git(checkout, "cat-file", "-p", "refs/tags/v1.2.3")
    assert "tagger github-actions[bot] <github-actions[bot]@users.noreply.github.com>" in tag
    assert tag.endswith("\n\nRelease v1.2.3")

    assert release_tag.push(remote, key) == 0
    assert capsys.readouterr().out == "Tag v1.2.3 already exists.\n"


def _wait_for_exit(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_push_goes_over_ssh_with_the_key_in_an_agent(
    checkout: Path, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stand-in for ssh, which records what it gets and refuses the push.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "ssh.txt"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f'exec >"{record}"\n'
        'printf "%s\\n" "$@"\n'
        'echo "agent=$SSH_AGENT_PID key=${RELEASE_DEPLOY_KEY:-none}"\n'
        "ssh-add -l\n"
        'sed -n p "$(printf "%s\\n" "$@" | sed -n "s/^UserKnownHostsFile=//p")"\n'
        "exit 1\n"
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RELEASE_DEPLOY_KEY", key)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    assert cli.main(["release-tag", "push"]) == 1
    assert (
        "git -c core.hooksPath=/dev/null push git@github.com:o/r.git refs/tags/v1.2.3 failed" in capsys.readouterr().err
    )
    lines = record.read_text().splitlines()
    assert lines[:9] == [
        "-o",
        lines[1],
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "git@github.com",
    ]
    assert lines[1].startswith("UserKnownHostsFile=")
    agent = next(line for line in lines if line.startswith("agent="))
    pid, secret = agent.removeprefix("agent=").split(" key=")
    # The key is in the agent, and not in ssh's environment.
    assert secret == "none"
    assert any(line.endswith("test (ED25519)") for line in lines)
    assert lines[-1] == release_tag.GITHUB_HOST_KEY
    # The agent is gone, and so is the known_hosts file.
    assert _wait_for_exit(int(pid))
    assert not Path(lines[1].removeprefix("UserKnownHostsFile=")).exists()
    # The failed push leaves no tag, so a retry here pushes it.
    assert git(checkout, "tag") == ""


def test_push_without_a_key(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RELEASE_DEPLOY_KEY", raising=False)
    assert cli.main(["release-tag", "--repository", "o/r", "push"]) == 1
    assert "$RELEASE_DEPLOY_KEY isn't set" in capsys.readouterr().err
    assert git(checkout, "tag") == ""


def test_needs_a_repository(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert cli.main(["release-tag", "release"]) == 1
    assert "give --repository, or set $GITHUB_REPOSITORY" in capsys.readouterr().err


def _not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", Message(), None)


class FakeAPI:
    def __init__(self, *, release: bool, tag: bool) -> None:
        self.release = release
        self.tag = tag
        self.posted: list[Any] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        assert token == "token"
        base = "https://api.github.com/repos/o/r"
        if method == "GET" and url == f"{base}/releases/tags/v1.2.3":
            if not self.release:
                raise _not_found(url)
            return {}
        if method == "GET" and url == f"{base}/git/ref/tags/v1.2.3":
            if not self.tag:
                raise _not_found(url)
            return {}
        assert (method, url) == ("POST", f"{base}/releases")
        self.posted.append(json_body)
        return {}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")


@pytest.mark.usefixtures("project")
def test_release_creates_the_release(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeAPI(release=False, tag=True)
    monkeypatch.setattr(github, "request", api)
    assert cli.main(["release-tag", "release"]) == 0
    assert api.posted == [{"tag_name": "v1.2.3", "name": "Release v1.2.3", "generate_release_notes": True}]
    assert capsys.readouterr().out == "Created GitHub release v1.2.3.\n"


@pytest.mark.usefixtures("project")
def test_release_that_exists(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeAPI(release=True, tag=True)
    monkeypatch.setattr(github, "request", api)
    assert cli.main(["release-tag", "release"]) == 0
    assert api.posted == []
    assert capsys.readouterr().out == "Release v1.2.3 already exists.\n"


@pytest.mark.usefixtures("project")
def test_release_needs_the_tag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeAPI(release=False, tag=False)
    monkeypatch.setattr(github, "request", api)
    assert cli.main(["release-tag", "release"]) == 1
    assert api.posted == []
    assert "tag v1.2.3 isn't in o/r" in capsys.readouterr().err


@pytest.mark.usefixtures("project")
def test_release_fails_on_another_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), None)

    monkeypatch.setattr(github, "request", forbidden)
    assert cli.main(["release-tag", "release"]) == 1
    assert "HTTP 403" in capsys.readouterr().err
