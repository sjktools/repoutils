import email.message
import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from repoutils import pypi_provenance

RELEASE = "https://pypi.org/pypi/pkg/1.0/json"
WHEEL = "https://pypi.org/integrity/pkg/1.0/pkg-1.0-py3-none-any.whl/provenance"
SDIST = "https://pypi.org/integrity/pkg/1.0/pkg-1.0.tar.gz/provenance"
GOOD = {"kind": "GitHub", "repository": "Owner/Repo", "workflow": "publish.yml"}


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(), io.BytesIO(b""))


def bundles(*publishers: dict[str, str]) -> dict[str, Any]:
    return {"version": 1, "attestation_bundles": [{"publisher": p, "attestations": []} for p in publishers]}


Answers = dict[str, list[object]]


@pytest.fixture
def pypi(monkeypatch: pytest.MonkeyPatch) -> Callable[[Answers], list[str]]:
    """Answers each URL with the next of its answers, raising an int as that HTTP error."""

    def install(answers: Answers) -> list[str]:
        asked: list[str] = []

        def get_json(url: str) -> Any:
            asked.append(url)
            answer = answers[url].pop(0) if len(answers[url]) > 1 else answers[url][0]
            if isinstance(answer, int):
                raise http_error(url, answer)
            return answer

        monkeypatch.setattr(pypi_provenance, "get_json", get_json)
        monkeypatch.setattr(pypi_provenance, "DELAY", 0.0)
        return asked

    return install


def release(*names: str) -> dict[str, Any]:
    return {"urls": [{"filename": name} for name in names]}


def test_check(pypi: Callable[[Answers], list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    asked = pypi(
        {
            RELEASE: [404, release("pkg-1.0-py3-none-any.whl", "pkg-1.0.tar.gz")],
            WHEEL: [bundles(GOOD)],
            SDIST: [bundles(GOOD, GOOD)],
        }
    )
    assert pypi_provenance.check("pkg", "1.0", "owner/repo") == ["pkg-1.0-py3-none-any.whl", "pkg-1.0.tar.gz"]
    assert asked == [RELEASE, RELEASE, WHEEL, SDIST]
    assert "PyPI has no pkg 1.0 yet, so asking again in 0s" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ({RELEASE: [404]}, r"PyPI has no release 1.0 of pkg \(https://pypi.org/pypi/pkg/1.0/json\)"),
        ({RELEASE: [500]}, "PyPI answered HTTP 500 for https://pypi.org/pypi/pkg/1.0/json"),
        ({RELEASE: [release()]}, "PyPI's release 1.0 of pkg has no files"),
        ({RELEASE: [release("pkg-1.0.tar.gz")], SDIST: [404]}, "PyPI has no provenance for pkg-1.0.tar.gz"),
        ({RELEASE: [release("pkg-1.0.tar.gz")], SDIST: [503]}, f"PyPI answered HTTP 503 for {SDIST}"),
        ({RELEASE: [release("pkg-1.0.tar.gz")], SDIST: [bundles()]}, "has no attestation bundles"),
        (
            {RELEASE: [release("pkg-1.0.tar.gz")], SDIST: [bundles(GOOD, {**GOOD, "repository": "evil/repo"})]},
            "names GitHub 'evil/repo' as its publisher, not GitHub repository owner/repo",
        ),
        (
            {RELEASE: [release("pkg-1.0.tar.gz")], SDIST: [bundles({"kind": "GitLab", "repository": "owner/repo"})]},
            "names GitLab 'owner/repo' as its publisher",
        ),
    ],
)
def test_check_refuses(pypi: Callable[[Answers], list[str]], answers: Answers, error: str) -> None:
    asked = pypi(answers)
    with pytest.raises(RuntimeError, match=error):
        pypi_provenance.check("pkg", "1.0", "owner/repo")
    if answers[RELEASE] == [404]:
        assert asked == [RELEASE] * pypi_provenance.ATTEMPTS


def test_get_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: Any, timeout: float) -> io.BytesIO:
        assert (request.full_url, request.get_header("Accept"), timeout) == (RELEASE, "application/json", 60)
        return io.BytesIO(json.dumps({"urls": []}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert pypi_provenance.get_json(RELEASE) == {"urls": []}


def test_quotes_names(pypi: Callable[[Answers], list[str]]) -> None:
    url = "https://pypi.org/pypi/a%2Fb/1%3F/json"
    pypi({url: [release()]})
    with pytest.raises(RuntimeError, match="has no files"):
        pypi_provenance.release_files("a/b", "1?")
