from typing import Any

import pytest

from repoutils import autofix_runs, github
from repoutils.commands import autofix

API = github.API_URL
REPO = f"{API}/repos/o/r"
RUN = f"{REPO}/actions/runs/7"
FIX_WORKFLOW = f"{REPO}/actions/workflows/fix.yml"
PUSH_RUNS = f"{REPO}/actions/workflows/fix-push.yml/runs?per_page=100&page=1"
ARTIFACTS = f"{RUN}/artifacts?per_page=100&page=1"
OPEN_PRS = f"{REPO}/pulls?state=open&head=o%3Afeature&per_page=100&page=1"


def _run(**overrides: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "workflow_id": 11,
        "repository": {"id": 1, "full_name": "o/r"},
        "head_repository": {"id": 1, "full_name": "o/r"},
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "aaa",
        "html_url": "https://github.com/o/r/actions/runs/7",
        "head_branch": "feature",
        "display_title": "PR Fixes #5",
    }
    run.update(overrides)
    return run


def _pr(number: int = 5, **overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": number,
        "state": "open",
        "head": {"ref": "feature", "sha": "aaa", "repo": {"id": 1}},
        "base": {"ref": "main", "repo": {"id": 1, "default_branch": "main"}},
    }
    pr.update(overrides)
    return pr


class FakeGitHub:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = {REPO: {"default_branch": "main"}, FIX_WORKFLOW: {"id": 11}, **answers}
        self.urls: list[str] = []

    def request(self, method: str, url: str, token: str) -> Any:
        assert (method, token) == ("GET", "tok")
        self.urls.append(url)
        return self.answers[url]


@pytest.fixture
def outputs() -> list[tuple[str, str]]:
    return []


def _install(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> FakeGitHub:
    fake = FakeGitHub(answers)
    monkeypatch.setattr(github, "request", fake.request)
    return fake


def _resolve(outputs: list[tuple[str, str]], ref: str | None = "refs/heads/main") -> None:
    autofix_runs.resolve(
        "o/r", 7, "tok", ref=ref, this_run="9", output=lambda name, value: outputs.append((name, value))
    )


def _check_branch(
    outputs: list[tuple[str, str]],
    *,
    ref: str = "refs/heads/feature",
    head_sha: str | None = None,
    pr_number: int | None = 5,
) -> None:
    autofix_runs.check_branch(
        "o/r",
        "tok",
        ref=ref,
        sha="aaa",
        head_sha=head_sha,
        pr_number=pr_number,
        output=lambda name, value: outputs.append((name, value)),
    )


RUN_OUTPUTS = [
    ("run_id", "7"),
    ("pr_number", "5"),
    ("branch", "feature"),
    ("start_sha", "aaa"),
    ("conclusion", "success"),
    ("url", "https://github.com/o/r/actions/runs/7"),
]
PR_OUTPUTS = [("number", "5"), ("head_sha", "aaa"), ("base", "main")]


def test_resolve_finds_the_fixes(monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]) -> None:
    _install(
        monkeypatch,
        {RUN: _run(), f"{REPO}/pulls/5": _pr(), ARTIFACTS: {"artifacts": [{"name": "fixes", "expired": False}]}},
    )
    _resolve(outputs)
    assert outputs == [*RUN_OUTPUTS, *PR_OUTPUTS, ("fixes", "true"), ("already_pushed", "false")]


def test_resolve_finds_nothing_to_push(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, {RUN: _run(), f"{REPO}/pulls/5": _pr(), ARTIFACTS: {"artifacts": []}})
    _resolve(outputs)
    assert outputs[-2:] == [("fixes", "false"), ("already_pushed", "false")]
    assert capsys.readouterr().err == "PR Fixes found nothing to push to feature.\n"


def test_resolve_refuses_an_expired_bundle(monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]) -> None:
    _install(
        monkeypatch,
        {RUN: _run(), f"{REPO}/pulls/5": _pr(), ARTIFACTS: {"artifacts": [{"name": "fixes", "expired": True}]}},
    )
    with pytest.raises(RuntimeError, match="Artifact fixes of run 7 has expired"):
        _resolve(outputs)


def test_resolve_of_a_failed_run_only_finds_the_pr(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]
) -> None:
    # Moved on, but that's no matter, as there's nothing to push.
    head = {"ref": "feature", "sha": "bbb", "repo": {"id": 1}}
    _install(monkeypatch, {RUN: _run(conclusion="failure", display_title="PR Fixes"), OPEN_PRS: [_pr(head=head)]})
    _resolve(outputs)
    assert outputs == [
        ("run_id", "7"),
        ("pr_number", ""),
        ("branch", "feature"),
        ("start_sha", "aaa"),
        ("conclusion", "failure"),
        ("url", "https://github.com/o/r/actions/runs/7"),
        ("number", "5"),
        ("head_sha", "bbb"),
        ("base", "main"),
    ]


def test_resolve_finds_another_run_pushed(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    name = autofix_runs.push_run_name(7)
    _install(
        monkeypatch,
        {
            RUN: _run(),
            f"{REPO}/pulls/5": _pr(head={"ref": "feature", "sha": "bbb", "repo": {"id": 1}}),
            PUSH_RUNS: {
                "workflow_runs": [
                    # This run itself, a run for another fix run, one that didn't push, and one that did.
                    {"id": 9, "display_title": name},
                    {"id": 10, "display_title": autofix_runs.push_run_name(6)},
                    {"id": 11, "display_title": name},
                    {"id": 12, "display_title": name},
                ]
            },
            f"{REPO}/actions/runs/11/jobs?per_page=100": {
                "jobs": [{"name": "resolve", "conclusion": "success"}, {"name": "push", "conclusion": "failure"}]
            },
            f"{REPO}/actions/runs/12/jobs?per_page=100": {"jobs": [{"name": "push", "conclusion": "success"}]},
        },
    )
    _resolve(outputs)
    assert outputs[-2:] == [("fixes", "false"), ("already_pushed", "true")]
    assert capsys.readouterr().err == "Run 12 already pushed PR Fixes run 7's fixes to feature.\n"


def test_resolve_refuses_a_branch_that_moved_on(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]
) -> None:
    _install(
        monkeypatch,
        {
            RUN: _run(),
            f"{REPO}/pulls/5": _pr(head={"ref": "feature", "sha": "bbb", "repo": {"id": 1}}),
            PUSH_RUNS: {"workflow_runs": []},
        },
    )
    with pytest.raises(ValueError, match="Branch feature is at bbb, not aaa, where PR Fixes started"):
        _resolve(outputs)
    # The report job still finds the PR.
    assert outputs == [*RUN_OUTPUTS, ("number", "5"), ("head_sha", "bbb"), ("base", "main")]


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (_run(workflow_id=12), "Run 7 isn't a run of fix.yml"),
        (_run(head_repository={"id": 2, "full_name": "f/r"}), "Run 7 ran on f/r, not o/r"),
        (_run(head_repository=None), "Run 7 ran on None, not o/r"),
        (_run(event="push"), "Run 7 was started by push, not workflow_dispatch"),
        (_run(status="in_progress"), "Run 7 hasn't finished; it's in_progress"),
        (_run(head_branch="-x"), "-x isn't a valid branch name"),
        (_run(head_branch="a..b"), "a..b isn't a valid branch name"),
        (_run(head_branch="main"), "PR Fixes doesn't push to main"),
        (_run(display_title="PR Fixes #x"), "Run 7's name, PR Fixes #x, doesn't give a pull request"),
        (_run(display_title="PR Fixes #5 "), "doesn't give a pull request"),
    ],
)
def test_resolve_refuses_a_run(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]], run: dict[str, object], message: str
) -> None:
    _install(monkeypatch, {RUN: run})
    with pytest.raises(ValueError, match=message):
        _resolve(outputs)
    assert outputs == []


def test_resolve_runs_on_the_default_branch_only(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]
) -> None:
    fake = _install(monkeypatch, {})
    with pytest.raises(ValueError, match="Run this workflow on main, not on refs/heads/feature"):
        _resolve(outputs, ref="refs/heads/feature")
    assert fake.urls == [REPO]


@pytest.mark.parametrize(
    ("pr", "message"),
    [
        (_pr(state="closed"), "Pull request #5 is not open"),
        (_pr(head={"ref": "feature", "sha": "aaa", "repo": None}), "Pull request #5 is from a fork"),
        (_pr(head={"ref": "other", "sha": "aaa", "repo": {"id": 1}}), "is from branch other, not feature"),
    ],
)
def test_resolve_refuses_the_named_pr(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]], pr: dict[str, object], message: str
) -> None:
    _install(monkeypatch, {RUN: _run(), f"{REPO}/pulls/5": pr})
    with pytest.raises(ValueError, match=message):
        _resolve(outputs)
    assert outputs == RUN_OUTPUTS


def test_resolve_needs_one_open_pr_without_a_number(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]
) -> None:
    _install(monkeypatch, {RUN: _run(display_title="PR Fixes"), OPEN_PRS: [_pr(5), _pr(6)]})
    with pytest.raises(
        ValueError,
        match="Branch feature has 2 open pull requests in this repository; "
        "run 7's name doesn't say which one it was for",
    ):
        _resolve(outputs)


def test_find_pr_reads_every_page_and_skips_forks(monkeypatch: pytest.MonkeyPatch) -> None:
    fork = _pr(4, head={"ref": "feature", "sha": "fff", "repo": {"id": 2}})
    _install(
        monkeypatch,
        {OPEN_PRS: [fork] * autofix_runs.PER_PAGE, OPEN_PRS.removesuffix("1") + "2": [_pr(), fork]},
    )
    assert autofix_runs.find_pr("o/r", "feature", None, "tok", which="")["number"] == 5


def test_find_pr_needs_an_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {OPEN_PRS: [_pr(4, head={"ref": "feature", "sha": "fff", "repo": None})]})
    with pytest.raises(ValueError, match="Branch feature has no open pull request in this repository"):
        autofix_runs.find_pr("o/r", "feature", None, "tok", which="")


def test_check_branch(monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]) -> None:
    _install(
        monkeypatch, {f"{REPO}/pulls/5": _pr(base={"ref": "develop", "repo": {"id": 1, "default_branch": "main"}})}
    )
    _check_branch(outputs, head_sha="aaa")
    assert outputs == [("base_branch", "develop")]


def test_check_branch_without_a_number(monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]]) -> None:
    _install(monkeypatch, {OPEN_PRS: [_pr(), _pr(6)]})
    with pytest.raises(ValueError, match="run PR Fixes again with the number of the one to fix"):
        _check_branch(outputs, pr_number=None)
    assert outputs == []


@pytest.mark.parametrize(
    ("ref", "head_sha", "message"),
    [
        ("refs/heads/main", None, "Run this workflow on a pull request's branch .*, not on refs/heads/main"),
        ("refs/tags/v1", None, "not on refs/tags/v1"),
        ("refs/heads/feature", "bbb", "Branch feature is at aaa, not bbb as expected"),
    ],
)
def test_check_branch_refuses(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, str]], ref: str, head_sha: str | None, message: str
) -> None:
    _install(monkeypatch, {})
    with pytest.raises(ValueError, match=message):
        _check_branch(outputs, ref=ref, head_sha=head_sha)


def test_print_output(capsys: pytest.CaptureFixture[str]) -> None:
    autofix_runs.print_output("branch", "feature")
    assert capsys.readouterr().out == "branch=feature\n"
    with pytest.raises(ValueError, match="branch's value has a line break in it"):
        autofix_runs.print_output("branch", "a\nfixes=true")


def test_resolve_command_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_resolve(repo: str, run_id: int, token: str, **options: Any) -> None:
        calls.append((repo, run_id, token, options))

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setattr(autofix_runs, "resolve", fake_resolve)
    assert autofix.main(["resolve", "--run-id", "7", "--repo", "o/r", "--workflow", "f.yml"]) == 0
    assert calls == [
        (
            "o/r",
            7,
            "tok",
            {"ref": "refs/heads/main", "this_run": "9", "workflow": "f.yml", "push_workflow": "fix-push.yml"},
        )
    ]


@pytest.mark.parametrize("run_id", ["x", "+7", "٧", ""])
def test_resolve_command_line_needs_a_run_id(capsys: pytest.CaptureFixture[str], run_id: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        autofix.main(["resolve", "--run-id", run_id])
    assert exit_info.value.code == 2
    assert "isn't a number" in capsys.readouterr().err


def test_check_branch_command_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature")
    monkeypatch.setenv("GITHUB_SHA", "aaa")
    _install(monkeypatch, {f"{REPO}/pulls/5": _pr()})
    # A workflow passes an input it wasn't given as an empty string.
    assert autofix.main(["check-branch", "--repo", "o/r", "--pr-number", "5", "--head-sha", ""]) == 0
    assert capsys.readouterr().out == "base_branch=main\n"
    _install(monkeypatch, {OPEN_PRS: [_pr()]})
    assert autofix.main(["check-branch", "--repo", "o/r", "--pr-number", ""]) == 0
    assert capsys.readouterr().out == "base_branch=main\n"


def test_check_branch_command_line_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature")
    assert autofix.main(["check-branch", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "::error::$GITHUB_SHA isn't set: run this in a GitHub Actions job.\n"
