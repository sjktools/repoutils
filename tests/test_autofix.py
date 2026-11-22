import io
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import autofix_runs, github, workflow_summaries
from repoutils.commands import autofix

API = github.API_URL
REPO = f"{API}/repos/o/r"
FIX_RUN = f"{REPO}/actions/runs/7"
PUSH_RUN = f"{REPO}/actions/runs/8"
PUSH_RUNS = f"{REPO}/actions/workflows/fix-push.yml/runs?per_page=100&page=1"
PUSH_JOBS = f"{PUSH_RUN}/jobs?per_page=100"
REACTIONS = f"{REPO}/issues/comments/99/reactions"
FIX_PAGE = "https://github.com/o/r/actions/runs/7"
PUSH_PAGE = "https://github.com/o/r/actions/runs/8"


def _pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "state": "open",
        "head": {"ref": "feature", "sha": "aaa", "repo": {"id": 1}},
        "base": {"repo": {"id": 1, "default_branch": "main"}},
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
        report: str = "success",
    ) -> None:
        self.answers: dict[str, list[Any]] = {
            f"{REPO}/pulls/5": prs,
            FIX_RUN: fix_runs,
            PUSH_RUNS: [{"workflow_runs": runs} for runs in push_lists],
            PUSH_RUN: push_runs,
            REPO: [{"default_branch": "main"}],
            PUSH_JOBS: [
                {"jobs": [{"name": "push", "conclusion": "success"}, {"name": "report", "conclusion": report}]}
            ],
        }
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        self.calls.append((method, url, json_body))
        assert token == "tok"
        if method == "POST" and url == REACTIONS:
            return {"content": "rocket"}
        if method == "POST":
            run_id = 7 if url.endswith("/fix.yml/dispatches") else 8
            return {"workflow_run_id": run_id}
        answers = self.answers[url]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def posts(self) -> list[tuple[str, object]]:
        return [(url.removeprefix(REPO), body) for method, url, body in self.calls if method == "POST"]


@pytest.fixture(autouse=True)
def not_in_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside GitHub Actions, as CI runs these tests in it, which makes errors annotations."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


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
        prs=[_pr(), _pr(head={"ref": "feature", "sha": "bbb", "repo": {"id": 1}})],
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


@pytest.mark.parametrize(("report", "reported"), [("success", True), ("skipped", False)])
def test_failed_fix_run_still_goes_to_a_push_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    slept: list[float],
    report: str,
    reported: bool,
) -> None:
    fake = FakeGitHub(
        prs=[_pr()], fix_runs=[_done(7, "failure")], push_lists=[[_push_run()]], push_runs=[_done(8)], report=report
    )
    _install(monkeypatch, fake)
    progress = autofix.Progress()
    assert autofix.autofix("o/r", 5, "tok", interval=10, timeout=600, progress=progress) == 1
    assert capsys.readouterr().err == (
        f"Started fix.yml on feature: {FIX_PAGE}\n"
        f"fix.yml run 7 finished with conclusion failure, so it has nothing to push: {FIX_PAGE}\n"
        f"Waiting for fix-push.yml: {PUSH_PAGE}\n"
    )
    assert progress == autofix.Progress(fix_url=FIX_PAGE, push_url=PUSH_PAGE, reported=reported)


def test_starts_the_push_run_for_a_failed_fix_run_a_github_token_started(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    fake = FakeGitHub(
        prs=[_pr()], fix_runs=[_done(7, "failure", actor="github-actions[bot]")], push_lists=[[]], push_runs=[_done(8)]
    )
    _install(monkeypatch, fake)
    assert _run() == 1
    assert fake.posts()[1] == (
        "/actions/workflows/fix-push.yml/dispatches",
        {"ref": "main", "return_run_details": True, "inputs": {"run-id": "7"}},
    )


@pytest.mark.parametrize(("report", "reported"), [("success", True), ("failure", False)])
def test_failed_push_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    slept: list[float],
    report: str,
    reported: bool,
) -> None:
    fake = FakeGitHub(
        prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8, "failure")], report=report
    )
    _install(monkeypatch, fake)
    progress = autofix.Progress()
    assert autofix.autofix("o/r", 5, "tok", interval=10, timeout=600, progress=progress) == 1
    assert capsys.readouterr().err.endswith(
        f"fix-push.yml run 8 finished with conclusion failure, so the fixes may not have been pushed: {PUSH_PAGE}\n"
    )
    assert progress.reported is reported


@pytest.mark.parametrize(("report", "reported"), [("success", True), ("in_progress", False)])
def test_checks_the_report_job_when_waiting_for_the_push_run_fails(
    monkeypatch: pytest.MonkeyPatch, slept: list[float], report: str, reported: bool
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[], report=report)

    def request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if url == PUSH_RUN:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", Message(), io.BytesIO(b"{}"))
        return fake.request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", request)
    progress = autofix.Progress()
    with pytest.raises(urllib.error.HTTPError) as caught:
        autofix.autofix("o/r", 5, "tok", interval=10, timeout=600, progress=progress)
    caught.value.close()
    assert progress == autofix.Progress(fix_url=FIX_PAGE, push_url=PUSH_PAGE, reported=reported)


def test_reads_no_jobs_when_the_fixes_were_pushed(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8)])
    _install(monkeypatch, fake)
    assert _run() == 0
    assert PUSH_JOBS not in [url for _, url, _ in fake.calls]


def test_reacts_to_the_comment_once_the_fix_run_starts(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8)])
    _install(monkeypatch, fake)
    assert autofix.autofix("o/r", 5, "tok", interval=10, timeout=600, comment_id=99) == 0
    assert [url for url, _ in fake.posts()] == [
        "/actions/workflows/fix.yml/dispatches",
        "/issues/comments/99/reactions",
    ]
    assert fake.posts()[1][1] == {"content": "rocket"}


@pytest.mark.parametrize(("actions", "prefix"), [("true", "::warning::"), (None, "")])
def test_a_failed_reaction_only_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    slept: list[float],
    actions: str | None,
    prefix: str,
) -> None:
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7)], push_lists=[[_push_run()]], push_runs=[_done(8)])

    def request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if url == REACTIONS:
            raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"{}"))
        return fake.request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", request)
    if actions is not None:
        monkeypatch.setenv("GITHUB_ACTIONS", actions)
    assert autofix.autofix("o/r", 5, "tok", interval=10, timeout=600, comment_id=99) == 0
    assert f"{prefix}Couldn't react to comment 99: HTTP Error 403: Forbidden\n" in capsys.readouterr().err


def test_a_reaction_that_cannot_connect_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(github, "request", request)
    autofix.react("o/r", 99, "tok")
    assert capsys.readouterr().err == "Couldn't react to comment 99: <urlopen error no route to host>\n"


def test_report() -> None:
    run = "https://github.com/o/r/actions/runs/1"
    assert autofix.report(autofix.Progress(), run) == f"/autofix [couldn't start PR Fixes]({run}).\n"
    assert autofix.report(autofix.Progress(fix_url=FIX_PAGE), run) == (
        f"/autofix started [PR Fixes]({FIX_PAGE}), but [couldn't start pushing its fixes]({run}).\n"
    )
    assert autofix.report(autofix.Progress(fix_url=FIX_PAGE, push_url=PUSH_PAGE), run) == (
        f"/autofix started [PR Fixes]({FIX_PAGE}), "
        f"but [PR Fixes Push]({PUSH_PAGE}) didn't report whether it pushed the fixes.\n"
    )
    assert autofix.report(autofix.Progress(fix_url=FIX_PAGE, push_url=PUSH_PAGE, reported=True), run) is None


def test_find_push_run_reads_every_page_since_the_fix_run(monkeypatch: pytest.MonkeyPatch) -> None:
    since = f"{REPO}/actions/workflows/fix-push.yml/runs?per_page=100&created=%3E%3D2026-11-20T22%3A00%3A00Z"
    pages = {
        f"{since}&page=1": [{"id": n, "display_title": "other"} for n in range(autofix_runs.PER_PAGE)],
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
        (_pr(state="closed"), "Pull request #5 is not open"),
        (_pr(head={"ref": "feature", "sha": "aaa", "repo": {"id": 2}}), "is from a fork"),
        (_pr(head={"ref": "feature", "sha": "aaa", "repo": None}), "is from a fork"),
        (_pr(head={"ref": "main", "sha": "aaa", "repo": {"id": 1}}), "branch is main"),
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
        (
            "o/r",
            5,
            "tok",
            {
                "workflow": "f.yml",
                "push_workflow": "fix-push.yml",
                "interval": 3.0,
                "timeout": 120.0,
                "comment_id": None,
                "progress": autofix.Progress(),
            },
        )
    ]


def _actions_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    return "https://github.com/o/r/actions/runs/1"


def test_command_line_writes_a_report_when_the_push_run_did_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, slept: list[float]
) -> None:
    run = _actions_env(monkeypatch)
    fake = FakeGitHub(
        prs=[_pr()],
        fix_runs=[_done(7)],
        push_lists=[[_push_run()]],
        push_runs=[_done(8, "cancelled")],
        report="skipped",
    )
    _install(monkeypatch, fake)
    report = tmp_path / "report.md"
    assert autofix.main(["5", "--comment-id", "99", "--report", str(report)]) == 1
    assert report.read_text() == autofix.report(autofix.Progress(fix_url=FIX_PAGE, push_url=PUSH_PAGE), run)
    assert REACTIONS in [url for _, url, _ in fake.calls]


def test_command_line_writes_no_report_when_the_push_run_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, slept: list[float]
) -> None:
    _actions_env(monkeypatch)
    fake = FakeGitHub(prs=[_pr()], fix_runs=[_done(7, "failure")], push_lists=[[_push_run()]], push_runs=[_done(8)])
    _install(monkeypatch, fake)
    report = tmp_path / "report.md"
    assert autofix.main(["5", "--report", str(report)]) == 1
    assert not report.exists()
    assert capsys.readouterr().err.endswith(
        "The push run reported on the pull request, so there's nothing more to report.\n"
    )


def test_command_line_reports_a_pr_it_cannot_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run = _actions_env(monkeypatch)
    fake = FakeGitHub(prs=[_pr(state="closed")], fix_runs=[], push_lists=[], push_runs=[])
    _install(monkeypatch, fake)
    report = tmp_path / "report.md"
    assert autofix.main(["5", "--report", str(report)]) == 1
    assert report.read_text() == f"/autofix [couldn't start PR Fixes]({run}).\n"
    assert capsys.readouterr().err == "::error::Pull request #5 is not open.\n"


def test_command_line_report_needs_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    fake = FakeGitHub(prs=[_pr(state="closed")], fix_runs=[], push_lists=[], push_runs=[])
    _install(monkeypatch, fake)
    report = tmp_path / "report.md"
    assert autofix.main(["5", "--repo", "o/r", "--report", str(report)]) == 1
    assert not report.exists()
    assert capsys.readouterr().err.endswith("$GITHUB_SERVER_URL isn't set: run this in a GitHub Actions job.\n")


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
