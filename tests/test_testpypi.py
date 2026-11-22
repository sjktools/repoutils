import io
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import testpypi

API = github.API_URL
REPO = f"{API}/repos/o/r"
DISPATCHES = f"{REPO}/actions/workflows/publish-pypi.yml/dispatches"
REACTIONS = f"{REPO}/issues/comments/99/reactions"
COMMENTS = f"{REPO}/issues/5/comments"
RUN_PAGE = "https://github.com/o/r/actions/runs/7"


def _pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "state": "open",
        "head": {"ref": "feature", "repo": {"id": 1}},
        "base": {"repo": {"id": 1}},
    }
    pr.update(overrides)
    return pr


class FakeGitHub:
    """Answers the requests testpypi makes, raising a 403 for each URL in failing."""

    def __init__(self, pr: dict[str, Any], *, failing: frozenset[str] = frozenset()) -> None:
        self.pr = pr
        self.failing = failing
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        self.calls.append((method, url, json_body))
        assert token == "tok"
        if url in self.failing:
            raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"{}"))
        if url == f"{REPO}/pulls/5":
            return self.pr
        if url.endswith("/dispatches"):
            return {"workflow_run_id": 7}
        return {}


@pytest.fixture(autouse=True)
def not_in_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside GitHub Actions, as CI runs these tests in it, which makes errors annotations."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def _fake(monkeypatch: pytest.MonkeyPatch, pr: dict[str, Any], **options: Any) -> FakeGitHub:
    fake = FakeGitHub(pr, **options)
    monkeypatch.setattr(github, "request", fake.request)
    return fake


def test_starts_the_workflow_on_the_branch(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _fake(monkeypatch, _pr())
    assert testpypi.testpypi("o/r", 5, "tok") == 0
    assert fake.calls == [
        ("GET", f"{REPO}/pulls/5", None),
        ("POST", DISPATCHES, {"ref": "feature", "return_run_details": True}),
    ]
    out = capsys.readouterr()
    assert out.out == f"{RUN_PAGE}\n"
    assert out.err == f"Started publish-pypi.yml on feature: {RUN_PAGE}\n"


def test_another_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch, _pr())
    assert testpypi.testpypi("o/r", 5, "tok", workflow="p.yml") == 0
    assert fake.calls[1][1] == f"{REPO}/actions/workflows/p.yml/dispatches"


def test_appends_to_the_step_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake(monkeypatch, _pr())
    summary = tmp_path / "summary.md"
    summary.write_text("before\n")
    assert testpypi.testpypi("o/r", 5, "tok", step_summary=summary) == 0
    assert summary.read_text() == (
        f"before\nStarted a TestPyPI dev release of `feature` (PR #5).\n\nPublish run: {RUN_PAGE}\n"
    )


def test_reacts_and_replies_to_the_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch, _pr())
    assert testpypi.testpypi("o/r", 5, "tok", comment_id=99) == 0
    assert fake.calls[2:] == [
        ("POST", REACTIONS, {"content": "rocket"}),
        ("POST", COMMENTS, {"body": f"Started a TestPyPI dev release of `feature`: {RUN_PAGE}"}),
    ]


@pytest.mark.parametrize(("actions", "prefix"), [("true", "::warning::"), (None, "")])
def test_a_failed_reaction_or_reply_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], actions: str | None, prefix: str
) -> None:
    fake = _fake(monkeypatch, _pr(), failing=frozenset({REACTIONS, COMMENTS}))
    if actions is not None:
        monkeypatch.setenv("GITHUB_ACTIONS", actions)
    assert testpypi.testpypi("o/r", 5, "tok", comment_id=99) == 0
    assert [url for _, url, _ in fake.calls[2:]] == [REACTIONS, COMMENTS]
    err = capsys.readouterr().err
    assert f"{prefix}Couldn't react to comment 99: HTTP Error 403: Forbidden\n" in err
    assert f"{prefix}Couldn't reply on pull request #5: HTTP Error 403: Forbidden\n" in err


def test_too_many_redirects_on_the_reaction_and_reply_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(_pr())

    def request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if url in (REACTIONS, COMMENTS):
            raise RuntimeError(f"too many redirects for {method} {url}")
        return fake.request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", request)
    assert testpypi.testpypi("o/r", 5, "tok", comment_id=99) == 0
    err = capsys.readouterr().err
    assert f"Couldn't react to comment 99: too many redirects for POST {REACTIONS}\n" in err
    assert f"Couldn't reply on pull request #5: too many redirects for POST {COMMENTS}\n" in err


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"state": "closed"}, "Pull request #5 is closed, so it gets no TestPyPI release."),
        ({"head": {"ref": "feature", "repo": {"id": 2}}}, "Pull request #5 is from a fork, whose branch isn't in o/r."),
        # A deleted fork.
        ({"head": {"ref": "feature", "repo": None}}, "Pull request #5 is from a fork, whose branch isn't in o/r."),
    ],
)
def test_refuses_a_pr_it_cannot_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], overrides: dict[str, object], message: str
) -> None:
    fake = _fake(monkeypatch, _pr(**overrides))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert testpypi.main(["5", "--repo", "o/r"]) == 1
    assert [method for method, _, _ in fake.calls] == ["GET"]
    assert capsys.readouterr().err == f"{message}\n"


def test_a_failed_dispatch_fails_as_an_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake(monkeypatch, _pr(), failing=frozenset({DISPATCHES}))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert testpypi.main(["5"]) == 1
    assert capsys.readouterr().err == f"::error::HTTP 403 from {DISPATCHES}:\n{{}}\n"


def test_command_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_testpypi(repo: str, pr: int, token: str, **options: Any) -> int:
        calls.append((repo, pr, token, options))
        return 0

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(testpypi, "testpypi", fake_testpypi)
    assert testpypi.main(["5", "--repo", "o/r", "--workflow", "p.yml", "--comment-id", "99"]) == 0
    assert calls == [("o/r", 5, "tok", {"workflow": "p.yml", "comment_id": 99, "step_summary": summary})]
