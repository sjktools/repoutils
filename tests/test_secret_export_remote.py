import io
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from repoutils import github, secret_export, secret_export_remote, workflow_summaries
from repoutils.commands import secrets
from tests.test_workflow_summaries import NAME_PAGES

API = github.API_URL
RUN = f"{API}/repos/o/r/actions/runs/7"
PAGE = "https://github.com/o/r/actions/runs/7"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, run: dict[str, Any], artifacts: list[dict[str, Any]], zips: dict[str, bytes]) -> None:
        self.run = run
        self.artifacts = artifacts
        self.zips = zips
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        self.calls.append((method, url))
        assert token == "tok"
        if method == "POST":
            return {"workflow_run_id": 7}
        if method == "DELETE":
            return None
        if url == f"{API}/repos/o/r":
            return {"default_branch": "main"}
        if url in NAME_PAGES:
            return NAME_PAGES[url]
        if url == RUN:
            return self.run
        if url.startswith(f"{RUN}/artifacts?"):
            return {"artifacts": self.artifacts if url.endswith("page=1") else []}
        raise AssertionError(url)

    def request_bytes(self, method: str, url: str, token: str) -> bytes:
        self.calls.append((method, url))
        return self.zips[url]


def _artifact(name: str, number: int) -> dict[str, Any]:
    return {"id": number, "name": name, "archive_download_url": f"{API}/zip/{name}"}


def _done(conclusion: str = "success") -> dict[str, Any]:
    return {"status": "completed", "conclusion": conclusion, "html_url": PAGE}


def _fake_age(decrypted: list[list[str]], fail_on: bytes | None = None) -> Any:
    """A stand-in for subprocess.run that "decrypts" by upper-casing, recording each command."""

    def run(args: list[str], *, input: bytes, stdout: int, check: bool) -> subprocess.CompletedProcess[bytes]:
        decrypted.append(args)
        if input == fail_on:
            return subprocess.CompletedProcess(args, 1, b"")
        return subprocess.CompletedProcess(args, 0, input.upper())

    return run


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    artifacts = [
        _artifact("step-summaries-release", 1),
        _artifact("secrets-repository", 2),
        _artifact("secrets-release", 3),
    ]
    zips = {
        f"{API}/zip/secrets-release": _zip({"secrets-release.toml.age": b"[environments.release]\n"}),
        f"{API}/zip/secrets-repository": _zip({"secrets-repository.toml.age": b"[actions]\n"}),
    }
    github_fake = FakeGitHub(_done(), artifacts, zips)
    monkeypatch.setattr(github, "request", github_fake.request)
    monkeypatch.setattr(github, "request_bytes", github_fake.request_bytes)
    monkeypatch.setattr(workflow_summaries, "sleep", lambda _: None)
    # The fake age stands in for any age but a missing one, so the tests needn't have age installed.
    monkeypatch.setattr(shutil, "which", lambda name: None if name.startswith("/no/") else name)  # pyright: ignore[reportUnknownLambdaType]
    return github_fake


def _remote(**overrides: Any) -> int:
    options: dict[str, Any] = {
        "workflow": "export-secrets.yml",
        "ref": None,
        "run_id": None,
        "interval": 5,
        "timeout": 600,
        "identities": [Path("/k/id_ed25519")],
    }
    options.update(overrides)
    return secret_export_remote.export_remote("o/r", "tok", **options)


def test_decrypts_prints_and_deletes(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_age(commands))
    assert _remote(identities=[Path("/k/id_ed25519"), Path("/k/id_rsa")]) == 0
    captured = capsys.readouterr()
    # Every secrets-* artifact, by name, as one TOML document.
    assert captured.out == "[ENVIRONMENTS.RELEASE]\n\n[ACTIONS]\n"
    assert commands == [["age", "--decrypt", "--identity", "/k/id_ed25519", "--identity", "/k/id_rsa"]] * 2
    assert ("POST", f"{API}/repos/o/r/actions/workflows/export-secrets.yml/dispatches") in fake.calls
    deleted = [url for method, url in fake.calls if method == "DELETE"]
    assert deleted == [f"{API}/repos/o/r/actions/artifacts/3", f"{API}/repos/o/r/actions/artifacts/2"]
    assert "Deleted secrets-release, secrets-repository from run 7" in captured.err
    assert f"Run 7 succeeded: {PAGE}" in captured.err


def test_keep_artifacts_and_save(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_age([]))
    fake.zips[f"{API}/zip/secrets-release"] = _zip({"../dir/secrets-release.toml.age": b"x"})
    assert _remote(run_id=7, save=tmp_path, keep_artifacts=True) == 0
    assert all(method == "GET" for method, _ in fake.calls)
    # Only the file's name, so the zip can't write outside the directory.
    assert (tmp_path / "secrets-release.toml.age").read_bytes() == b"x"
    assert not (tmp_path.parent / "dir").exists()
    assert (tmp_path / "secrets-repository.toml.age").read_bytes() == b"[actions]\n"
    assert f"Saved secrets-repository/secrets-repository.toml.age to {tmp_path / 'secrets-repository.toml.age'}" in (
        capsys.readouterr().err
    )


def test_save_without_a_key_only_saves(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def no_age(*args: object, **kwargs: object) -> None:
        raise AssertionError("age ran")

    monkeypatch.setattr(subprocess, "run", no_age)
    assert _remote(identities=[], save=tmp_path) == 0
    assert capsys.readouterr().out == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "secrets-release.toml.age",
        "secrets-repository.toml.age",
    ]


def test_no_key_and_no_save_starts_nothing(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="found no SSH private key"):
        _remote(identities=[])
    assert fake.calls == []


def test_failed_decryption_prints_no_secrets_and_keeps_the_artifacts(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_age([], fail_on=b"[actions]\n"))
    with pytest.raises(RuntimeError, match=r"age failed to decrypt \(exit 1\)"):
        _remote()
    assert capsys.readouterr().out == ""
    assert all(method != "DELETE" for method, _ in fake.calls)


def test_missing_age_starts_nothing(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="can't run /no/age: install age, or give its path with --age"):
        _remote(age="/no/age")
    assert fake.calls == []
    with pytest.raises(ValueError, match="can't run /no/age"):
        secret_export_remote.decrypt("/no/age", [Path("/k/id_ed25519")], b"")


def test_save_rejects_two_files_with_one_name(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.zips[f"{API}/zip/secrets-repository"] = _zip({"other/secrets-release.toml.age": b"y"})
    with pytest.raises(ValueError, match="would both be saved as .*secrets-release.toml.age"):
        _remote(identities=[], save=tmp_path)
    assert (tmp_path / "secrets-release.toml.age").read_bytes() == b"[environments.release]\n"
    assert all(method != "DELETE" for method, _ in fake.calls)


def test_save_creates_the_directory(fake: FakeGitHub, tmp_path: Path) -> None:
    assert _remote(identities=[], save=tmp_path / "new" / "dir") == 0
    assert (tmp_path / "new" / "dir" / "secrets-release.toml.age").exists()


def test_failed_run_still_prints_what_it_kept(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_age([]))
    fake.run = _done("failure")
    assert _remote() == 1
    captured = capsys.readouterr()
    assert "[ACTIONS]" in captured.out
    assert f"Run 7 finished with conclusion failure: {PAGE}" in captured.err


def test_run_without_artifacts_fails(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.artifacts = [_artifact("step-summaries-repository", 1)]
    assert _remote() == 1
    assert f"Run 7 kept no encrypted secrets (secrets-* artifacts), so nothing was exported: {PAGE}" in (
        capsys.readouterr().err
    )
    assert all(method != "DELETE" for method, _ in fake.calls)


def test_default_identities(tmp_path: Path) -> None:
    assert secret_export_remote.default_identities(tmp_path) == []
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("rsa")
    (tmp_path / ".ssh" / "id_ecdsa").write_text("age can't use it")
    assert secret_export_remote.default_identities(tmp_path) == [tmp_path / ".ssh" / "id_rsa"]
    (tmp_path / ".ssh" / "id_ed25519").write_text("ed25519")
    assert secret_export_remote.default_identities(tmp_path) == [
        tmp_path / ".ssh" / "id_ed25519",
        tmp_path / ".ssh" / "id_rsa",
    ]


def test_command_line_finds_keys_in_home(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_age(commands))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("key")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    argv = ["export-remote", "--repo", "o/r", "--ref", "dev", "--timeout", "1", "--age", "/bin/age"]
    assert secrets.main(argv) == 0
    assert commands[0] == ["/bin/age", "--decrypt", "--identity", str(tmp_path / ".ssh" / "id_ed25519")]
    # Starting a run lists the secret names set at each level (test_command_line_reports_errors
    # shows it lists none when it can't decrypt).
    assert {url for _, url in fake.calls if url in NAME_PAGES} == set(NAME_PAGES)
    assert "[ACTIONS]" in capsys.readouterr().out


def test_command_line_identity_replaces_the_defaults(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_age(commands))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("key")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert secrets.main(["export-remote", "--repo", "o/r", "--run-id", "7", "--identity", "/k/age.txt"]) == 0
    assert commands[0] == ["age", "--decrypt", "--identity", "/k/age.txt"]


def test_command_line_reports_errors(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert secrets.main(["export-remote", "--repo", "o/r"]) == 1
    assert "found no SSH private key" in capsys.readouterr().err
    assert fake.calls == []


@pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("ssh-keygen") is None, reason="needs age and ssh-keygen"
)
def test_round_trip_with_age(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    public = key.with_suffix(".pub").read_text().strip()
    encrypted = tmp_path / "secrets.toml.age"
    secret_export.encrypt("age", [public], b'[actions]\nA = "1"\n', encrypted, armor=True)
    assert secret_export_remote.decrypt("age", [key], encrypted.read_bytes()) == b'[actions]\nA = "1"\n'
