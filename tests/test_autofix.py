import io
import subprocess
import urllib.error
from email.message import Message
from typing import Any

import pytest

from repoutils import github, workflow_summaries
from repoutils.commands import autofix

API = github.API_URL
REPO = f"{API}/repos/o/r"
FIX_RUN = f"{REPO}/actions/runs/7"
PUSH_RUN = f"{REPO}/actions/runs/8"
PUSH_RUNS = f"{REPO}/actions/workflows/fix-push.yml/runs?per_page=100&page=1"
FIX_PAGE = "https://github.com/o/r/actions/runs/7"
PUSH_PAGE = "https://github.com/o/r/actions/runs/8"


def _pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "state": "open",
        "head": {"ref": "feature", "sha": "aaa", "repo": {"full_name": "o/r"}},
        "base": {"repo": {"default_branch": "main"}},
    }
    pr.update(overrides)
    return pr


def _done(run_id: int, conclusion: str = "success", actor: str = "someone") -> dict[str, Any]:
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "triggering_actor": {"login": actor},
    }


class FakeGitHub:
    """Answers requests from a table, giving each poll the next answer in turn."""

    def __init__(
        self,
        *,
        prs: list[dict[str, Any]],
        fix_runs: list[dict[str, Any]],
        push_lists: list[list[dict[str, Any]]],
        push_runs: list[dict[str, Any]],
    ) -> None:
        self.answers: dict[str, list[Any]] = {
            f"{REPO}/pulls/5": prs,
            FIX_RUN: fix_runs,
            PUSH_RUNS: [{"workflow_runs": runs} for runs in push_lists],
            PUSH_RUN: push_runs,
            REPO: [{"default_branch": "main"}],
        }
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        self.calls.append((method, url, json_body))
        assert token == "tok"
        if method == "POST":
            run_id = 7 if url.endswith("/fix.yml/dispatches") else 8
            return {"workflow_run_id": run_id}
        answers = self.answers[url]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def posts(self) -> list[tuple[str, object]]:
        return [(url.removeprefix(REPO), body) for method, url, body in self.calls if method == "POST"]


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Sleeping advances a fake clock instead, so the grace and timeouts run instantly."""
    clock = [1000.0]
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        clock[0] += seconds

    for module in (autofix, workflow_summaries):
        monkeypatch.setattr(module, "sleep", nap)
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    return naps


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    monkeypatch.setattr(github, "request", fake.request)


def _run() -> int:
    return autofix.autofix("o/r", 5, "tok", interval=10, timeout=600)


def _push_run(fix_run_id: int = 7) -> dict[str, Any]:
    return {"id": 8, "display_title": autofix.push_run_name(fix_run_id)}


def test_waits_for_the_push_run_github_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(
        prs=[_pr(), _pr(head={"ref": "feature", "sha": "bbb", "repo": {"full_name": "o/r"}})],
        fix_runs=[{"status": "in_progress"}, _done(7)],
        # The push run for another fix run doesn't count.
        push_lists=[[], [_push_run(6)], [_push_run(6), _push_run()]],
        push_runs=[{"status": "queued"}, _done(8, actor="github-actions[bot]")],
    )
    _install(monkeypatch, fake)
    assert _run() == 0
    captured = capsys.readouterr()
    assert captured.out == "bbb\n"
    assert captured.err == (
        f"Started fix.yml on feature: {FIX_PAGE}\n"
        f"Waiting for fix-push.yml: {PUSH_PAGE}\n"
        "PR Fixes pushed its fixes to feature, which is now at bbb\n"
    )
    assert fake.posts() == [
        (
            "/actions/workflows/fix.yml/dispatches",
            {"ref": "feature", "return_run_details": True, "inputs": {"head-sha": "aaa", "pr-number": "5"}},
        )
    ]
    assert slept == [10, 10, 10, 10]


def test_nothing_to_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8)])
    _install(monkeypatch, fake)
    assert _run() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("PR Fixes found nothing to fix on feature at aaa\n")
    assert slept == []


def test_starts_the_push_run_for_a_fix_run_a_github_token_started(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    fake = FakeGitHub(
        prs=[_pr()],
        fix_runs=[_done(7, actor="github-actions[bot]")],
        push_lists=[[]],
        push_runs=[_done(8)],
    )
    _install(monkeypatch, fake)
    assert _run() == 0
    # GitHub starts no push run for it, so this doesn't wait for one.
    assert PUSH_RUNS not in [url for _, url, _ in fake.calls]
    assert fake.posts()[1] == (
        "/actions/workflows/fix-push.yml/dispatches",
        {"ref": "main", "return_run_details": True, "inputs": {"run-id": "7"}},
    )
    assert slept == []


def test_starts_the_push_run_if_github_does_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[]], push_runs=[_done(8)])
    _install(monkeypatch, fake)
    assert _run() == 0
    assert sum(slept) == autofix.PUSH_GRACE
    assert "GitHub didn't start fix-push.yml for run 7, so starting it here\n" in capsys.readouterr().err
    assert fake.posts()[1][0] == "/actions/workflows/fix-push.yml/dispatches"


def test_failed_fix_run_waits_for_no_push(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7, "failure")], push_lists=[[]], push_runs=[])
    _install(monkeypatch, fake)
    assert _run() == 1
    assert capsys.readouterr().err.endswith(
        f"fix.yml run 7 finished with conclusion failure, so nothing was pushed: {FIX_PAGE}\n"
    )
    assert PUSH_RUNS not in [url for _, url, _ in fake.calls]


def test_failed_push_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8, "failure")])
    _install(monkeypatch, fake)
    assert _run() == 1
    assert capsys.readouterr().err.endswith(
        f"fix-push.yml run 8 finished with conclusion failure, so the fixes may not have been pushed: {PUSH_PAGE}\n"
    )


def test_find_push_run_reads_every_page_since_the_fix_run(monkeypatch: pytest.MonkeyPatch) -> None:
    since = f"{REPO}/actions/workflows/fix-push.yml/runs?per_page=100&created=%3E%3D2026-11-20T22%3A00%3A00Z"
    pages = {
        f"{since}&page=1": [{"id": n, "display_title": "other"} for n in range(autofix.PER_PAGE)],
        f"{since}&page=2": [_push_run()],
    }

    def request(method: str, url: str, token: str) -> Any:
        return {"workflow_runs": pages[url]}

    monkeypatch.setattr(github, "request", request)
    assert autofix.find_push_run("o/r", "fix-push.yml", 7, "tok", "2026-11-20T22:00:00Z") == _push_run()
    pages[f"{since}&page=2"] = []
    assert autofix.find_push_run("o/r", "fix-push.yml", 7, "tok", "2026-11-20T22:00:00Z") is None


def test_starts_no_push_run_after_the_timeout(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[]], push_runs=[])
    _install(monkeypatch, fake)
    with pytest.raises(TimeoutError, match="Gave up before starting fix-push.yml for run 7"):
        autofix.autofix("o/r", 5, "tok", interval=10, timeout=60)
    assert len(fake.posts()) == 1


def test_times_out(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    fake = FakeGitHub(
        prs=[_pr()], fix_runs=[{"status": "in_progress", "html_url": FIX_PAGE}], push_lists=[[]], push_runs=[]
    )
    _install(monkeypatch, fake)
    with pytest.raises(TimeoutError, match="Run 7 hadn't finished after 600 seconds"):
        _run()


@pytest.mark.parametrize(
    ("pr", "message"),
    [
        (_pr(state="closed"), "pull request #5 is not open"),
        (_pr(head={"ref": "feature", "sha": "aaa", "repo": {"full_name": "fork/r"}}), "is from a fork"),
        (_pr(head={"ref": "feature", "sha": "aaa", "repo": None}), "is from a fork"),
        (_pr(head={"ref": "main", "sha": "aaa", "repo": {"full_name": "o/r"}}), "branch is main"),
    ],
)
def test_refuses_a_pr_it_cannot_push_to(monkeypatch: pytest.MonkeyPatch, pr: dict[str, object], message: str) -> None:
    fake = FakeGitHub(prs=[pr], fix_runs=[], push_lists=[], push_runs=[])
    _install(monkeypatch, fake)
    with pytest.raises(ValueError, match=message):
        _run()
    assert fake.posts() == []


def test_command_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_autofix(repo: str, pr: int, token: str, **options: Any) -> int:
        calls.append((repo, pr, token, options))
        return 0

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(autofix, "autofix", fake_autofix)
    assert autofix.main(["5", "--repo", "o/r", "--timeout", "2", "--interval", "3", "--workflow", "f.yml"]) == 0
    assert calls == [
        ("o/r", 5, "tok", {"workflow": "f.yml", "push_workflow": "fix-push.yml", "interval": 3.0, "timeout": 120.0})
    ]


def test_command_line_reports_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(method: str, url: str, token: str, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b'{"message": "Not Found"}'))

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", refuse)
    assert autofix.main(["5", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == f'HTTP 404 from {REPO}/pulls/5:\n{{"message": "Not Found"}}\n'


def test_command_line_reports_a_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slept: list[float]
) -> None:
    fake = FakeGitHub(
        prs=[_pr()], fix_runs=[{"status": "in_progress", "html_url": FIX_PAGE}], push_lists=[[]], push_runs=[]
    )
    _install(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert autofix.main(["5", "--repo", "o/r", "--timeout", "1"]) == 1
    assert capsys.readouterr().err.endswith(f"Run 7 hadn't finished after 60 seconds: {FIX_PAGE}\n")


def test_command_line_reports_a_failed_gh(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in\n")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", fail)
    assert autofix.main(["5", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n\n"
