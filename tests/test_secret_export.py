import argparse
import base64
import json
import subprocess
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from repoutils import github, secret_export, secret_inspect
from repoutils.commands import secrets

NOW = datetime(2026, 11, 15, 2, 30, tzinfo=UTC)
CONFIG = """
[tool.repoutils.secrets]
actions = ["TOKEN", "DEPLOY_KEY"]
other = ["ORG_TOKEN"]
environments.release = ["RELEASE_TOKEN"]
"""
PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\n"
VALUES = {
    "TOKEN": 'ghp_quote"back\\slash',
    "DEPLOY_KEY": PEM,
    "RELEASE_TOKEN": "tab\there, del\x7f, bell\x07, ü",
    "ORG_TOKEN": "org",
    "STRAY": "stray",
    "github_token": "ghs_builtin",
}
ED25519 = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"\x01" * 36).decode()
ED25519_FP = secret_inspect.fingerprint(base64.b64decode(ED25519.split()[1]))
SK_KEY = "sk-ssh-ed25519@openssh.com " + base64.b64encode(b"security key").decode()
SK_FP = secret_inspect.fingerprint(b"security key")
KEYS = f"\nlonely\nnot a key\nssh-rsa !!!notbase64\n{SK_KEY}\n{ED25519} comment\n"


class FakeAge:
    """Stands in for subprocess.run of age: records the call, and writes output."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.args: list[str] = []
        self.input = b""

    def __call__(self, args: list[str], *, input: bytes, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.args, self.input = args, input
        Path(args[args.index("--output") + 1]).write_bytes(b"age-encryption.org/v1 ...")
        return subprocess.CompletedProcess(args, self.returncode, b"", b"age: error: bad recipient\n")

    def plaintext(self) -> dict[str, Any]:
        return tomllib.loads(self.input.decode())

    def recipients(self) -> list[str]:
        return [self.args[i + 1] for i, arg in enumerate(self.args) if arg == "--recipient"]


@pytest.fixture
def age(monkeypatch: pytest.MonkeyPatch) -> FakeAge:
    fake = FakeAge()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str] | None = VALUES) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    if values is None:
        monkeypatch.delenv("SECRETS_JSON", raising=False)
    else:
        monkeypatch.setenv("SECRETS_JSON", json.dumps(values))
    for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)


def test_github_key() -> None:
    assert secret_export.github_key(f"alice/{ED25519_FP}") == ("alice", ED25519_FP)
    # ssh-keygen leaves the padding off, but it may be pasted with it.
    assert secret_export.github_key(f"bob-2/{ED25519_FP}=") == ("bob-2", ED25519_FP)
    for bad in (ED25519_FP, f"alice/{ED25519_FP[:-1]}", f"../x/{ED25519_FP}", "alice/MD5:aa:bb"):
        with pytest.raises(argparse.ArgumentTypeError, match="isn't USER/SHA256:FINGERPRINT"):
            secret_export.github_key(bad)


def test_pick_key() -> None:
    assert secret_export.pick_key("alice", ED25519_FP, KEYS) == ED25519
    with pytest.raises(ValueError, match=r"none of the keys at https://github.com/alice.keys has the fingerprint"):
        secret_export.pick_key("alice", "SHA256:" + "A" * 43, KEYS)
    with pytest.raises(ValueError, match="is sk-ssh-ed25519@openssh.com, which age can't encrypt to"):
        secret_export.pick_key("alice", SK_FP, KEYS)


def test_fetch_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return KEYS.encode()

    def fake_open(request: urllib.request.Request, timeout: float) -> FakeResponse:
        requested.append(request.full_url)
        # No token goes with it: the keys are public.
        assert not request.has_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(github.no_redirect_opener, "open", fake_open)
    assert secret_export.fetch_keys("alice") == KEYS
    assert requested == ["https://github.com/alice.keys"]


def test_export_toml_round_trips() -> None:
    values = {"TOKEN": VALUES["TOKEN"], "DEPLOY_KEY": PEM, "odd.name": VALUES["RELEASE_TOKEN"]}
    text = secret_export.export_toml("[environments.release]", values, ["header", "two"])
    assert text.startswith("# header\n# two\n[environments.release]\n")
    assert tomllib.loads(text) == {"environments": {"release": values}}


def test_encrypt(tmp_path: Path, age: FakeAge) -> None:
    output = tmp_path / "out.age"
    secret_export.encrypt("/bin/age", ["age1abc", "ssh-ed25519 AAAA"], b"plain", output)
    assert age.args == [
        "/bin/age",
        "--encrypt",
        "--recipient",
        "age1abc",
        "--recipient",
        "ssh-ed25519 AAAA",
        "--output",
        str(tmp_path / ".out.age.partial"),
    ]
    assert age.input == b"plain"
    assert output.read_bytes().startswith(b"age-encryption.org")
    assert list(tmp_path.iterdir()) == [output]


def test_encrypt_failure_keeps_an_earlier_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeAge(returncode=1))
    output = tmp_path / "out.age"
    output.write_bytes(b"an earlier backup")
    with pytest.raises(RuntimeError, match="age failed \\(exit 1\\):\nage: error: bad recipient"):
        secret_export.encrypt("age", ["age1abc"], b"plain", output)
    assert output.read_bytes() == b"an earlier backup"
    assert list(tmp_path.iterdir()) == [output]


def test_encrypt_without_age(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="can't run .*missing-age: install age"):
        secret_export.encrypt(str(tmp_path / "missing-age"), ["age1abc"], b"plain", tmp_path / "out.age")


def test_export_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], age: FakeAge
) -> None:
    _project(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(secret_export, "fetch_keys", lambda user: KEYS)
    summary = tmp_path / "summary.md"
    argv = ["export", "--github-key", f"alice/{ED25519_FP}", "--recipient", "age1abc"]
    assert secrets.main([*argv, "--output", "out.age", "--summary", str(summary)]) == 0
    # Only the actions list: other secrets aren't the repository's to restore.
    assert age.plaintext() == {"actions": {"TOKEN": VALUES["TOKEN"], "DEPLOY_KEY": PEM}}
    assert "From https://github.com/o/r/actions/runs/42" in age.input.decode()
    assert age.recipients() == ["age1abc", ED25519]
    out, err = capsys.readouterr()
    assert f"Exported 2 secret(s) to out.age, encrypted to age1abc, alice's SSH key {ED25519_FP}" in out
    assert "Not listed in pyproject.toml, so not exported: STRAY" in err
    report = summary.read_text()
    assert report.startswith("## Repository secrets\n")
    assert f"- alice's SSH key {ED25519_FP}\n" in report
    assert "Secrets: `TOKEN`, `DEPLOY_KEY`." in report
    assert "so not exported: `STRAY`." in report
    assert "--output secrets.toml out.age" in report
    # No value is ever shown.
    for value in VALUES.values():
        assert value not in out + err + report


def test_export_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: FakeAge) -> None:
    _project(tmp_path, monkeypatch)
    assert secrets.main(["export", "--environment", "Release", "--recipient", "age1abc", "--output", "r.age"]) == 0
    assert age.plaintext() == {"environments": {"Release": {"RELEASE_TOKEN": VALUES["RELEASE_TOKEN"]}}}
    assert "Environment Release's secrets, exported by `repoutils secrets export`" in age.input.decode()


def test_export_header(age: FakeAge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    secret_export.export(
        ["A"],
        ["A"],
        None,
        recipients=["age1abc"],
        github_keys=[],
        output=tmp_path / "o",
        summary=tmp_path / "summary.md",
        secrets_json='{"A": "1"}',
        now=NOW,
    )
    assert "Not listed" not in (tmp_path / "summary.md").read_text()
    assert age.input.decode() == (
        '# Repository secrets, exported by `repoutils secrets export` at 2026-11-15T02:30:00Z.\n[actions]\nA = "1"\n'
    )


@pytest.mark.parametrize(
    ("argv", "values", "error"),
    [
        ([], VALUES, "give at least one --recipient or --github-key"),
        (["--recipient", "age1abc"], None, "$SECRETS_JSON isn't set"),
        (
            ["--recipient", "age1abc"],
            {"TOKEN": "t"},
            "the job doesn't have these secrets, so nothing was exported: DEPLOY_KEY",
        ),
        (["--recipient", "age1abc", "--environment", "pypi"], VALUES, "lists no secrets for environment pypi"),
    ],
)
def test_export_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    age: FakeAge,
    argv: list[str],
    values: dict[str, str] | None,
    error: str,
) -> None:
    _project(tmp_path, monkeypatch, values)
    assert secrets.main(["export", *argv, "--output", "out.age"]) == 1
    assert error in capsys.readouterr().err
    assert not age.args
    assert not (tmp_path / "out.age").exists()


def test_export_needs_an_actions_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoutils.secrets]\nother = ["A"]\n')
    monkeypatch.chdir(tmp_path)
    assert secrets.main(["export", "--recipient", "age1abc", "--output", "out.age"]) == 1
    assert "lists no actions secrets to export" in capsys.readouterr().err


def test_export_age_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "run", FakeAge(returncode=1))
    assert secrets.main(["export", "--recipient", "nope", "--output", "out.age"]) == 1
    assert "age failed (exit 1)" in capsys.readouterr().err
    assert not (tmp_path / "out.age").exists()
