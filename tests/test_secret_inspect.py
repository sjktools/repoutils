import base64
import hashlib
import io
import json
import struct
import subprocess
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any, override

import pytest

from repoutils import github, secret_inspect
from repoutils.commands import secrets
from repoutils.secret_inspect import Finding, Response

NOW = datetime(2026, 11, 15, tzinfo=UTC)
CLASSIC = "ghp_" + "A1" * 18
FINE = "github_pat_" + "B2" * 30
APP = "ghs_" + "C3" * 18
CONFIG = """
[tool.repoutils.secrets]
actions = ["CLASSIC", "FINE"]
other = ["ORG_TOKEN"]
dependabot = ["DEPENDABOT_ONLY"]
environments.release = ["DEPLOY_KEY", "APP"]
"""


def _headers(**values: str) -> Message:
    headers = Message()
    for key, value in values.items():
        headers[key.replace("_", "-")] = value
    return headers


def _http_error(code: int, body: object = None) -> urllib.error.HTTPError:
    payload = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
    return urllib.error.HTTPError("https://api.github.com/user", code, "error", Message(), payload)


class FakeAPI:
    """Answers secret_inspect.get for each token, like the API would."""

    def __init__(self, answers: dict[tuple[str, str], Response | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, path: str, token: str) -> Response:
        self.calls.append((path, token))
        answer = self.answers[(path, token)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _user(login: str, **headers: str) -> Response:
    return Response(_headers(**headers), {"login": login})


def _openssh_key(key_type: bytes = b"ssh-ed25519", cipher: bytes = b"none") -> tuple[str, bytes]:
    """A made-up OpenSSH private key, and its public key blob."""

    def string(data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + data

    blob = string(key_type) + string(b"\x07" * 32)
    data = b"openssh-key-v1\0" + string(cipher) + string(b"none") + string(b"") + struct.pack(">I", 1) + string(blob)
    data += string(b"private part, never read")
    body = base64.b64encode(data).decode()
    lines = [body[i : i + 70] for i in range(0, len(body), 70)]
    return "\n".join(["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----", ""]), blob


def test_token_type() -> None:
    assert secret_inspect.token_type(CLASSIC) == "classic personal access token"
    assert secret_inspect.token_type(FINE) == "fine-grained personal access token"
    assert secret_inspect.token_type("gho_x") == "OAuth app token"
    assert secret_inspect.token_type("ghu_x") == "GitHub App user token"
    assert secret_inspect.token_type(APP) == "GitHub App installation token"
    assert secret_inspect.token_type("ghr_x") == "GitHub App refresh token"
    assert secret_inspect.token_type("sk-something") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-12-01 00:00:00 UTC", datetime(2026, 12, 1, tzinfo=UTC)),
        ("2026-12-01 10:00:00 +1000", datetime(2026, 12, 1, tzinfo=UTC)),
        ("next tuesday", None),
    ],
)
def test_parse_expiry(text: str, expected: datetime | None) -> None:
    assert secret_inspect.parse_expiry(text) == expected


def test_classic_token(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeAPI(
        {
            ("/user", CLASSIC): _user(
                "alice",
                X_OAuth_Scopes="repo, workflow",
                GitHub_Authentication_Token_Expiration="2027-01-14 00:00:00 UTC",
            )
        }
    )
    monkeypatch.setattr(secret_inspect, "get", api)
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert finding == Finding(
        "T",
        "classic personal access token",
        "alice",
        "2027-01-14 (in 60 days)",
        "scopes: repo, workflow",
        secret_inspect.token_hash(CLASSIC),
    )
    assert api.calls == [("/user", CLASSIC)]


def test_classic_token_without_expiry_or_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", CLASSIC): _user("alice", X_OAuth_Scopes="")}))
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert (finding.expires, finding.access, finding.problems) == ("never", "scopes: none", [])


@pytest.mark.parametrize(
    ("expiry", "expires", "problems"),
    [
        ("2026-11-28 00:00:00 UTC", "2026-11-28 (in 13 days)", ["expires in 13 day(s), on 2026-11-28"]),
        ("2026-11-29 00:00:00 UTC", "2026-11-29 (in 14 days)", []),
        ("2026-11-14 00:00:00 UTC", "2026-11-14 (expired)", ["expired"]),
    ],
)
def test_expiry(monkeypatch: pytest.MonkeyPatch, expiry: str, expires: str, problems: list[str]) -> None:
    api = FakeAPI({("/user", CLASSIC): _user("alice", GitHub_Authentication_Token_Expiration=expiry)})
    monkeypatch.setattr(secret_inspect, "get", api)
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert (finding.expires, finding.problems) == (expires, problems)


def test_fine_grained_token(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeAPI({("/user", FINE): _user("alice", GitHub_Authentication_Token_Expiration="2027-01-14 00:00:00 UTC")})
    monkeypatch.setattr(secret_inspect, "get", api)
    finding = secret_inspect.describe("T", FINE, NOW, 14)
    # No repositories: the API can't say which a fine-grained token was given.
    assert (finding.owner, finding.access) == ("alice", "")
    assert finding.expires == "2027-01-14 (in 60 days)"
    assert api.calls == [("/user", FINE)]


def test_token_hash() -> None:
    assert secret_inspect.token_hash(FINE) == hashlib.sha256(FINE.encode()).hexdigest()[:12]


def test_token_hash_ignores_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", CLASSIC): _user("alice")}))
    finding = secret_inspect.describe("T", f" {CLASSIC}\n", NOW, 14)
    assert finding.sha256 == secret_inspect.token_hash(CLASSIC)


def test_only_tokens_get_a_hash() -> None:
    assert secret_inspect.describe("T", "plain", NOW, 14).sha256 == ""
    assert secret_inspect.describe("T", _openssh_key()[0], NOW, 14).sha256 == ""


def test_installation_token(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"total_count": 1, "repositories": [{"full_name": "o/r", "owner": {"login": "o"}}]}
    path = "/installation/repositories?per_page=1"
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({(path, APP): Response(Message(), body)}))
    finding = secret_inspect.describe("T", APP, NOW, 14)
    assert (finding.owner, finding.access, finding.expires) == ("o", "1 repository", "an hour after it was made")
    assert finding.problems == ["installation tokens expire an hour after they're made, so a stored one stops working"]


def test_installation_token_in_jwt_format(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ghs_12345_eyJhbGciOi.eyJpc3Mi-Oi.c2lnbmF0dXJl_x"
    body = {"total_count": 0, "repositories": []}
    path = "/installation/repositories?per_page=1"
    headers = _headers(GitHub_Authentication_Token_Expiration="2026-11-15 01:00:00 UTC")
    api = FakeAPI({(path, token): Response(headers, body)})
    monkeypatch.setattr(secret_inspect, "get", api)
    finding = secret_inspect.describe("T", token, NOW, 0)
    assert (finding.expires, finding.problems) == ("2026-11-15 (in 0 days)", [])
    assert api.calls == [(path, token)]


@pytest.mark.parametrize("value", ["ghs_abc def", "ghs_abc\rX-Header: 1", "ghp_abc.def"])
def test_token_with_bad_characters_is_not_sent(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({}))
    finding = secret_inspect.describe("T", value, NOW, 14)
    assert finding.problems == ["has characters a GitHub token can't, so it wasn't tried"]


def test_oauth_token_without_expiry_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", "gho_abc"): _user("alice")}))
    assert secret_inspect.describe("T", "gho_abc", NOW, 14).expires == "not reported"


def test_installation_token_with_no_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"total_count": 0, "repositories": []}
    path = "/installation/repositories?per_page=1"
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({(path, APP): Response(Message(), body)}))
    finding = secret_inspect.describe("T", APP, NOW, 14)
    assert (finding.owner, finding.access) == ("", "no repositories")


def test_refresh_token_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({}))
    finding = secret_inspect.describe("T", "ghr_abc", NOW, 14)
    assert finding == Finding(
        "T",
        "GitHub App refresh token",
        access="a refresh token can't call the API",
        sha256=secret_inspect.token_hash("ghr_abc"),
    )


def test_rejected_token_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", CLASSIC): _http_error(401)}))
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert finding.problems == ["GitHub rejects it (HTTP 401): it has expired, or been revoked, or is wrong"]
    assert finding.error == ""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(403, {"message": "rate limited"}), "HTTP 403 from GET /user: rate limited"),
        (_http_error(500), "HTTP 500 from GET /user"),
        (urllib.error.URLError("no route"), "GET /user failed: URLError: <urlopen error no route>"),
    ],
)
def test_failed_request_is_an_error(monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", CLASSIC): error}))
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert (finding.error, finding.problems) == (expected, [])


def test_whitespace_around_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({("/user", CLASSIC): _user("alice")}))
    finding = secret_inspect.describe("T", f"{CLASSIC}\n", NOW, 14)
    assert finding.owner == "alice"
    assert finding.problems == ["has whitespace around the token, which breaks most uses of it"]


def test_malformed_token_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({}))
    finding = secret_inspect.describe("T", "ghp_abc def", NOW, 14)
    assert finding.problems == ["has characters a GitHub token can't, so it wasn't tried"]


def test_openssh_key_fingerprint() -> None:
    key, blob = _openssh_key()
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert secret_inspect.ssh_public_key(key) == ("ssh-ed25519", fingerprint, False)
    finding = secret_inspect.describe("K", key, NOW, 14)
    assert finding == Finding("K", "OpenSSH private key (ssh-ed25519)", access=fingerprint)


def test_passphrase_protected_key() -> None:
    key, _ = _openssh_key(cipher=b"aes256-ctr")
    finding = secret_inspect.describe("K", key, NOW, 14)
    assert finding.access.endswith(", passphrase-protected")


def test_fingerprint_matches_ssh_keygen(tmp_path: Path) -> None:
    try:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    except FileNotFoundError:
        pytest.skip("no ssh-keygen")
    listed = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k")], capture_output=True, text=True, check=True)
    key = secret_inspect.ssh_public_key((tmp_path / "k").read_text())
    assert key is not None
    assert key[1] == listed.stdout.split()[1]


@pytest.mark.parametrize(
    ("value", "what"),
    [
        ("-----BEGIN OPENSSH PRIVATE KEY-----\n!!!\n-----END OPENSSH PRIVATE KEY-----", "PEM openssh private key"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----", "PEM openssh private key"),
        ("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----", "PEM rsa private key"),
        ("", "empty"),
        ("sk-something", "not a GitHub token or SSH key"),
    ],
)
def test_other_values(value: str, what: str) -> None:
    assert secret_inspect.describe("V", value, NOW, 14) == Finding("V", what)


def test_truncated_openssh_key() -> None:
    data = b"openssh-key-v1\0" + struct.pack(">I", 100)
    pem = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{base64.b64encode(data).decode()}\n-----END OPENSSH PRIVATE KEY-----"
    assert secret_inspect.ssh_public_key(pem) is None


@pytest.mark.parametrize("text", ["not json", "[1]", '{"A": 1}'])
def test_read_secrets_errors(text: str) -> None:
    with pytest.raises(ValueError, match=r"\$SECRETS_JSON"):
        secret_inspect.read_secrets(text)


def _inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str], argv: list[str], api: FakeAPI
) -> int:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRETS_JSON", json.dumps(values))
    monkeypatch.setattr(secret_inspect, "get", api)
    monkeypatch.setattr(secret_inspect, "datetime", _FixedNow)
    return secrets.main(["inspect", "--summary", str(tmp_path / "summary.md"), *argv])


class _FixedNow(datetime):
    @classmethod
    @override
    def now(cls, tz: Any = None) -> "_FixedNow":
        return cls(2026, 11, 15, tzinfo=UTC)


def test_inspect_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAPI(
        {
            ("/user", CLASSIC): _user("a|b", X_OAuth_Scopes="repo"),
            ("/user", FINE): _user("alice"),
        }
    )
    values = {"github_token": "x", "CLASSIC": CLASSIC, "FINE": FINE, "ORG_TOKEN": "o"}
    assert _inspect(tmp_path, monkeypatch, values, [], api) == 0
    out = capsys.readouterr().out
    classic, fine = secret_inspect.token_hash(CLASSIC), secret_inspect.token_hash(FINE)
    assert out == (
        f"CLASSIC: classic personal access token, owner a|b, expires never, scopes: repo, SHA-256 starts {classic}\n"
        f"FINE: fine-grained personal access token, owner alice, expires never, SHA-256 starts {fine}\n"
        "ORG_TOKEN: not a GitHub token or SSH key\n"
        "Inspected 3 secret(s), with no problems.\n"
    )
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith(
        "## Repository secrets\n\n| Secret | What it is | Owner | Expires | Access | SHA-256 starts |\n"
    )
    assert f"| `CLASSIC` | classic personal access token | a\\|b | never | scopes: repo | `{classic}` |\n" in summary
    assert "| `ORG_TOKEN` | not a GitHub token or SSH key |   |   |   |   |\n" in summary
    assert "### Problems" not in summary
    for value in (CLASSIC, FINE):
        assert value not in out
        assert value not in summary


def test_inspect_unexpected_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The repository's job doesn't expect an environment's secrets, or one pyproject.toml lists nowhere.
    api = FakeAPI({("/user", CLASSIC): _user("alice"), ("/user", FINE): _user("bob")})
    values = {"CLASSIC": CLASSIC, "FINE": FINE, "STRAY": "s", "AUTOFIX": FINE, "DEPLOY_KEY": "k"}
    assert _inspect(tmp_path, monkeypatch, values, [], api) == 1
    out = capsys.readouterr().out
    assert "STRAY: not a GitHub token or SSH key\n" in out
    # An unexpected token is inspected like any other, so it can be matched to the declared one.
    fine = secret_inspect.token_hash(FINE)
    assert f"AUTOFIX: fine-grained personal access token, owner bob, expires never, SHA-256 starts {fine}\n" in out
    for name in ("AUTOFIX", "DEPLOY_KEY", "STRAY"):
        assert f"{name}: {secret_inspect.UNEXPECTED}\n" in out
    assert "3 problem(s) with the 6 secret(s) inspected.\n" in out
    summary = (tmp_path / "summary.md").read_text()
    assert "| `STRAY` (unexpected) | not a GitHub token or SSH key |" in summary
    assert f"- `STRAY`: {secret_inspect.UNEXPECTED}\n" in summary
    assert _inspect(tmp_path, monkeypatch, values, ["--exit-zero"], api) == 0


def test_inspect_environment_expects_repository_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An environment's job gets the repository's and organization's secrets too, and only inspects its own.
    values = {"CLASSIC": CLASSIC, "ORG_TOKEN": "o", "DEPLOY_KEY": "k", "APP": "a"}
    assert _inspect(tmp_path, monkeypatch, values, ["--environment", "release"], FakeAPI({})) == 0
    assert capsys.readouterr().out == (
        "DEPLOY_KEY: not a GitHub token or SSH key\n"
        "APP: not a GitHub token or SSH key\n"
        "Inspected 2 secret(s), with no problems.\n"
    )


def test_inspect_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key, _ = _openssh_key()
    path = "/installation/repositories?per_page=1"
    api = FakeAPI({(path, APP): _http_error(401)})
    values = {"CLASSIC": CLASSIC, "DEPLOY_KEY": key, "APP": APP}
    assert _inspect(tmp_path, monkeypatch, values, ["--environment", "RELEASE"], api) == 1
    out = capsys.readouterr().out
    assert out.startswith("DEPLOY_KEY: OpenSSH private key (ssh-ed25519), SHA256:")
    assert "APP: GitHub rejects it (HTTP 401)" in out
    assert "1 problem(s) with the 2 secret(s) inspected.\n" in out
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith("## Environment RELEASE's secrets\n")
    assert "### Problems\n\n- `APP`: GitHub rejects it" in summary
    assert api.calls == [(path, APP)]


def test_inspect_exit_zero_forgives_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeAPI({("/user", CLASSIC): _http_error(401), ("/user", FINE): _http_error(401)})
    values = {"CLASSIC": CLASSIC, "FINE": FINE}
    assert _inspect(tmp_path, monkeypatch, values, ["--exit-zero"], api) == 0


def test_inspect_error_fails_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAPI({("/user", CLASSIC): _http_error(502), ("/user", FINE): _http_error(401)})
    values = {"CLASSIC": CLASSIC, "FINE": FINE}
    assert _inspect(tmp_path, monkeypatch, values, ["--exit-zero"], api) == 1
    err = capsys.readouterr().err
    assert err == "CLASSIC: HTTP 502 from GET /user\n1 secret(s) couldn't be inspected.\n"
    assert "### Errors\n\n- `CLASSIC`: HTTP 502 from GET /user\n" in (tmp_path / "summary.md").read_text()


def test_inspect_expires_within(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expiry = {"GitHub_Authentication_Token_Expiration": "2026-12-15 00:00:00 UTC"}
    api = FakeAPI(
        {
            ("/user", CLASSIC): _user("alice", **expiry),
            ("/user", FINE): _user("alice", **expiry),
        }
    )
    values = {"CLASSIC": CLASSIC, "FINE": FINE}
    assert _inspect(tmp_path, monkeypatch, values, [], api) == 0
    assert _inspect(tmp_path, monkeypatch, values, ["--expires-within", "31"], api) == 1


@pytest.mark.parametrize(
    ("config_text", "argv", "env", "error"),
    [
        (CONFIG, ["--environment", "pypi"], "{}", "lists no secrets for environment pypi"),
        ('[tool.repoutils.secrets]\ndependabot = ["A"]\n', [], "{}", "lists no actions or other secrets"),
        (CONFIG, [], None, "$SECRETS_JSON isn't set"),
        (CONFIG, [], "nope", "$SECRETS_JSON isn't JSON"),
    ],
)
def test_inspect_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_text: str,
    argv: list[str],
    env: str | None,
    error: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    monkeypatch.chdir(tmp_path)
    if env is None:
        monkeypatch.delenv("SECRETS_JSON", raising=False)
    else:
        monkeypatch.setenv("SECRETS_JSON", env)
    assert secrets.main(["inspect", *argv]) == 1
    assert error in capsys.readouterr().err


def test_inspect_rejects_negative_days(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        secrets.main(["inspect", "--expires-within", "-1"])
    assert "must be 0 or more" in capsys.readouterr().err


def test_get(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        headers = _headers(X_OAuth_Scopes="repo")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"login": "alice"}'

    requests: list[Any] = []

    def fake_open(request: Any, timeout: float) -> FakeResponse:
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(github.no_redirect_opener, "open", fake_open)
    response = secret_inspect.get("/user", CLASSIC)
    assert response.body == {"login": "alice"}
    assert response.headers["X-OAuth-Scopes"] == "repo"
    assert requests[0].full_url == "https://api.github.com/user"
    assert requests[0].get_header("Authorization") == f"Bearer {CLASSIC}"


def test_inspect_without_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert secret_inspect.inspect("T", ["A"], ["A"], '{"A": "plain"}', within=14, exit_zero=False) == 0
    assert capsys.readouterr().out == "A: not a GitHub token or SSH key\nInspected 1 secret(s), with no problems.\n"


def test_installation_token_without_expiry_header_and_no_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"total_count": 0, "repositories": []}
    path = "/installation/repositories?per_page=1"
    monkeypatch.setattr(secret_inspect, "get", FakeAPI({(path, APP): Response(Message(), body)}))
    finding = secret_inspect.describe("T", APP, NOW, 0)
    assert (finding.expires, finding.problems) == ("an hour after it was made", [])


def test_unreadable_expiry_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeAPI({("/user", CLASSIC): _user("alice", GitHub_Authentication_Token_Expiration="soon")})
    monkeypatch.setattr(secret_inspect, "get", api)
    finding = secret_inspect.describe("T", CLASSIC, NOW, 14)
    assert (finding.expires, finding.problems) == ("soon", [])
    assert finding.error == "can't read the expiry header GitHub-Authentication-Token-Expiration: 'soon'"
