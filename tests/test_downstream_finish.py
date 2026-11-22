import io
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import downstream_finish, github
from repoutils.commands import downstream

API = f"{github.API_URL}/repos/d/down"
# The check runs and the commit statuses that one read of a commit's checks finds.
Poll = tuple[list[dict[str, Any]], list[dict[str, Any]]]
VERSION = "0.4.1.dev7"
BRANCH = f"downstream-test/pkg-{VERSION}"
MARKER = "<!-- up-downstream-test-pkg -->"
UPSTREAM = '[project]\nname = "pkg"\n\n[tool.repoutils]\ncomment-prefix = "up"\n'
RULES = [
    {"type": "pull_request", "parameters": {}},
    {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "test", "integration_id": 15368}, {"context": "lint"}]},
    },
    # Another ruleset can require the same check again.
    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}]}},
]


def run(
    name: str, status: str = "completed", conclusion: str | None = "success", app: int = 15368, run_id: int = 1
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"id": app},
        "html_url": f"https://github.com/d/down/runs/{name}",
    }


def status(context: str, state: str = "success") -> dict[str, Any]:
    return {"id": 2, "context": context, "state": state, "target_url": None}


def pull(number: int = 5, branch: str = BRANCH, body: str = MARKER + "\nBumps") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/d/down/pull/{number}",
        "head": {"ref": branch, "sha": "a" * 40, "repo": {"full_name": "d/down"}},
        "base": {"ref": "main"},
        "body": body,
    }


class FakeGitHub:
    """The downstream's pull requests, rules and checks. Each read of the
    checks on a commit takes the next of its polls, and stays at the last."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        polls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        *,
        pulls: list[dict[str, Any]] | None = None,
        heads: list[str] | None = None,
        rules: list[dict[str, Any]] | None = None,
        state: str = "open",
        after: list[list[Poll]] | None = None,
        refuse: tuple[str, ...] = (),
    ) -> None:
        self.polls = polls
        # The polls after the first re-run, the second, and so on.
        self.after = after or []
        self.refuse = refuse
        # The re-run requests, and the rounds of them.
        self.reruns = 0
        self.rounds = 0
        self.in_round = False
        self.stage = {"check-runs": 0, "status": 0}
        self.pulls = [pull()] if pulls is None else pulls
        self.heads = heads or ["a" * 40]
        self.rules = RULES if rules is None else rules
        self.state = state
        self.reads = {"check-runs": 0, "status": 0, "pull": 0}
        self.changes: list[tuple[str, str, Any]] = []
        self.slept: list[float] = []
        self.now = 0.0
        monkeypatch.setattr(github, "request", self.request)
        monkeypatch.setattr(downstream_finish, "sleep", self.sleep)
        monkeypatch.setattr(downstream_finish, "monotonic", lambda: self.now)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def _poll(self, kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # After each round of re-runs, the reads go on from the polls of the next stage.
        self.in_round = False
        polls = self.polls if not self.after else ([self.polls] + self.after)[min(self.rounds, len(self.after))]
        if self.rounds != self.stage[kind]:
            self.stage[kind], self.reads[kind] = self.rounds, 0
        index = min(self.reads[kind], len(polls) - 1)
        self.reads[kind] += 1
        return polls[index]

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        assert token == "tok"
        path = url.removeprefix(API)
        if method != "GET":
            self.changes.append((method, path, json_body))
            if path in self.refuse:
                raise urllib.error.HTTPError(url, 403, "Resource not accessible", Message(), io.BytesIO(b"{}"))
            if path.endswith("/rerun-failed-jobs"):
                self.reruns += 1
                # The re-runs between two reads of the checks are one round.
                if not self.in_round:
                    self.rounds, self.in_round = self.rounds + 1, True
            return None
        if path.startswith("/actions/jobs/"):
            if path in self.refuse:
                raise urllib.error.URLError("Connection reset")
            return {"run_id": 100 + int(path.removeprefix("/actions/jobs/")) // 10}
        if path.startswith("/pulls?"):
            return self.pulls if "page=1" in path else []
        if path == "/pulls/5":
            head = self.heads[min(self.reads["pull"], len(self.heads) - 1)]
            self.reads["pull"] += 1
            return {"state": self.state, "head": {"sha": head}}
        if path.startswith("/rules/branches/main?"):
            return self.rules if "page=1" in path else []
        if "/check-runs?" in path:
            return {"check_runs": self._poll("check-runs")[0]}
        if "/status?" in path:
            return {"statuses": self._poll("status")[1]}
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(UPSTREAM)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "u/up")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    return tmp_path


def finish(*args: str) -> int:
    return downstream.run(["finish", "--repo", "d/down", "--version", VERSION, *args])


PASSED = ([run("test"), run("lint", conclusion="skipped"), run("docs", conclusion="neutral")], [status("cov")])


def test_closes_a_pull_request_whose_checks_pass(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        monkeypatch,
        [
            # Only some of the required checks have appeared.
            ([run("test")], []),
            # Every required check is there, but not every check has finished.
            ([run("test"), run("lint", status="in_progress")], [status("cov", "pending")]),
            PASSED,
        ],
    )
    assert finish("--interval", "5") == 0
    # And once more, to see that no other check appeared.
    assert fake.slept == [5, 5, 5]
    comment = "All 4 checks passed, so `repoutils downstream finish` in https://github.com/u/up/actions/runs/9 "
    assert fake.changes == [
        ("POST", "/issues/5/comments", {"body": comment + "closes this test."}),
        ("PATCH", "/pulls/5", {"state": "closed"}),
        ("DELETE", f"/git/refs/heads/{BRANCH}", None),
    ]
    out = capsys.readouterr().out
    assert "main's rulesets require test, lint\n" in out
    assert f"closed https://github.com/d/down/pull/5 and deleted its branch {BRANCH}" in out


def test_leaves_a_pull_request_open_if_a_check_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        monkeypatch,
        [([run("test", conclusion="failure"), run("lint")], [status("cov", "error")])],
    )
    assert finish() == 1
    assert fake.changes == []
    err = capsys.readouterr().err
    assert "Left https://github.com/d/down/pull/5 open, as these checks failed" in err
    assert "- test: failure https://github.com/d/down/runs/test\n- cov: error\n" in err


def test_reruns_failed_jobs_once(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two failed jobs of one workflow run (jobs 11 and 12 are in run 101), and one of another (run 102).
    failed = [run("test", conclusion="failure", run_id=11), run("lint", conclusion="failure", run_id=12)]
    first = ([*failed, run("e2e", conclusion="failure", run_id=21), run("docs", run_id=13)], [status("cov")])
    # Right after the re-run, GitHub still gives the old check runs, which mustn't count.
    rerun: Poll = ([run("test", run_id=31), run("lint", run_id=32), run("e2e", run_id=33), run("docs", run_id=13)], [])
    fake = FakeGitHub(monkeypatch, [first], after=[[first, first, (rerun[0], [status("cov")])]])
    assert finish("--interval", "5") == 0
    assert (fake.reruns, fake.rounds) == (2, 1)
    assert fake.changes[:2] == [
        ("POST", "/actions/runs/101/rerun-failed-jobs", None),
        ("POST", "/actions/runs/102/rerun-failed-jobs", None),
    ]
    assert fake.changes[2][:2] == ("POST", "/issues/5/comments")
    out = capsys.readouterr().out
    assert "Re-ran the failed jobs of https://github.com/d/down/actions/runs/101\n" in out
    assert f"closed https://github.com/d/down/pull/5 and deleted its branch {BRANCH}" in out


def test_leaves_it_open_if_a_rerun_fails_too(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first: Poll = ([run("test", conclusion="failure", run_id=11), run("lint")], [])
    second: Poll = ([run("test", conclusion="failure", run_id=14), run("lint")], [])
    fake = FakeGitHub(monkeypatch, [first], after=[[second]])
    assert finish() == 1
    # Only once.
    assert fake.changes == [("POST", "/actions/runs/101/rerun-failed-jobs", None)]
    assert "- test: failure" in capsys.readouterr().err


def test_reruns_as_many_times_as_retries(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fails: list[Poll] = [([run("test", conclusion="failure", run_id=n), run("lint")], []) for n in (11, 14, 15)]
    fake = FakeGitHub(monkeypatch, [fails[0]], after=[[fails[1]], [fails[2]], [PASSED]])
    assert finish("--retries", "2") == 1
    assert fake.reruns == fake.rounds == 2


def test_reruns_nothing_with_no_retries(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(monkeypatch, [([run("test", conclusion="failure", run_id=11), run("lint")], [])])
    assert finish("--retries", "0") == 1
    assert fake.changes == []


# A commit status, or another app's check run, can't be re-run.
OUTSIDE_ACTIONS: list[Poll] = [
    ([run("test", conclusion="failure", run_id=11), run("lint")], [status("cov", "failure")]),
    ([run("test", conclusion="failure", run_id=11), run("lint"), run("scan", conclusion="failure", app=7)], []),
]


@pytest.mark.parametrize("poll", OUTSIDE_ACTIONS)
def test_reruns_nothing_if_a_check_outside_actions_fails(
    poll: Poll, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(monkeypatch, [poll])
    assert finish() == 1
    assert fake.changes == []


def test_warns_if_github_refuses_the_rerun(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        monkeypatch,
        [([run("test", conclusion="failure", run_id=11), run("lint")], [])],
        refuse=("/actions/runs/101/rerun-failed-jobs",),
    )
    assert finish() == 1
    assert fake.reruns == 0
    err = capsys.readouterr().err
    assert "warning: couldn't re-run the failed jobs on d/down, which needs Actions write: HTTP Error 403" in err
    assert "Left https://github.com/d/down/pull/5 open" in err


def test_warns_if_the_job_lookup_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        monkeypatch, [([run("test", conclusion="failure", run_id=11), run("lint")], [])], refuse=("/actions/jobs/11",)
    )
    assert finish() == 1
    assert fake.changes == []
    assert "warning: couldn't re-run the failed jobs on d/down, which needs Actions write: <urlopen error" in (
        capsys.readouterr().err
    )


def test_times_out_waiting_for_a_rerun(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The re-run never replaces the failed check run.
    first: Poll = ([run("test", conclusion="failure", run_id=11), run("lint")], [])
    FakeGitHub(monkeypatch, [first])
    assert finish("--timeout", "60") == 1
    err = capsys.readouterr().err
    assert "Gave up after 60 seconds waiting for test on https://github.com/d/down/pull/5, so it stays open" in err


@pytest.mark.parametrize("value", ["-1", "10", "x"])
def test_refuses_bad_retries(value: str, upstream: Path) -> None:
    with pytest.raises(SystemExit):
        finish("--retries", value)


def test_waits_for_a_check_from_the_required_app(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A check run of the same name from another app isn't the required one.
    fake = FakeGitHub(monkeypatch, [([run("test", app=1), run("lint")], [])])
    assert finish("--timeout", "60", "--interval", "30") == 1
    assert fake.changes == []
    assert "waiting for test on https://github.com/d/down/pull/5, so it stays open" in capsys.readouterr().err


def test_a_status_counts_only_for_a_check_of_any_app(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The API doesn't say which app posted a status, so it isn't the required app's check.
    fake = FakeGitHub(monkeypatch, [([], [status("test"), status("lint")])])
    assert finish("--timeout", "60") == 1
    assert fake.changes == []
    assert "waiting for test on" in capsys.readouterr().err


def test_waits_for_a_check_that_appears_late(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every check had finished, but another workflow then added one.
    late = (PASSED[0] + [run("e2e", status="queued")], PASSED[1])
    fake = FakeGitHub(monkeypatch, [PASSED, late, (PASSED[0] + [run("e2e", conclusion="failure")], PASSED[1])])
    assert finish("--retries", "0") == 1
    assert fake.changes == []


def test_waits_for_any_check(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [([], [])], rules=[])
    assert finish("--timeout", "100", "--interval", "30") == 1
    assert fake.slept == [30, 30, 30, 10]
    err = capsys.readouterr().err
    assert "Gave up after 100 seconds waiting for any check to start on https://github.com/d/down/pull/5" in err


def test_waits_again_for_a_replaced_check_run(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A rerun of test replaced its run with another of the same name.
    # Both URLs are null, which GitHub allows, so only the IDs tell the two apart.
    first, rerun = (dict(run("test", run_id=n), html_url=None) for n in (1, 3))
    fake = FakeGitHub(monkeypatch, [([first, *PASSED[0][1:]], PASSED[1]), ([rerun, *PASSED[0][1:]], PASSED[1])])
    assert finish("--interval", "5") == 0
    assert fake.slept == [5, 5]
    assert fake.reads["check-runs"] == 3


def test_reads_the_checks_again_if_the_head_moves(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The checks passed on the first head, but someone pushed before it closed.
    fake = FakeGitHub(monkeypatch, [PASSED], heads=["a" * 40, "a" * 40, "b" * 40])
    assert finish() == 0
    assert fake.reads["check-runs"] == 4
    assert fake.reads["pull"] == 6


def test_finds_only_the_versions_test_pull_request(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = [
        # Another dev version's, and a release's.
        pull(3, "downstream-test/pkg-0.4.1.dev6"),
        pull(4, "downstream/pkg-0.4.0", "<!-- up-downstream-pkg -->"),
    ]
    fake = FakeGitHub(monkeypatch, [PASSED], pulls=pulls)
    assert finish() == 1
    assert fake.changes == []
    assert "d/down has no open test pull request that bumps pkg to 0.4.1.dev7" in capsys.readouterr().err


def test_refuses_a_release(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [PASSED], pulls=[pull(4, "downstream/pkg-0.4.0")])
    assert downstream.run(["finish", "--repo", "d/down", "--version", "0.4.0"]) == 1
    assert fake.changes == []
    assert "0.4.0 isn't a dev version" in capsys.readouterr().err


def test_fails_if_the_pull_request_closed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [PASSED], state="closed")
    assert finish() == 1
    assert fake.changes == []
    assert "https://github.com/d/down/pull/5 is no longer open" in capsys.readouterr().err


def test_reads_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "/rules/branches/a%2Fb?per_page=100&page=1": [RULES[2]] * 100,
        "/rules/branches/a%2Fb?per_page=100&page=2": [RULES[1]],
        "/commits/c/check-runs?per_page=100&page=1": {"check_runs": [run("lint")] * 100},
        "/commits/c/check-runs?per_page=100&page=2": {"check_runs": [run("test", status="queued")]},
        "/commits/c/status?per_page=100&page=1": {"statuses": [status("cov", "failure")]},
    }

    def request(method: str, url: str, token: str) -> Any:
        return pages[url.removeprefix(API)]

    monkeypatch.setattr(github, "request", request)
    required = downstream_finish.required_checks("d/down", "tok", "a/b")
    assert required == [downstream_finish.Required("lint", None), downstream_finish.Required("test", 15368)]
    found = downstream_finish.checks("d/down", "tok", "c")
    assert len(found) == 102
    assert found[100] == downstream_finish.Check(
        "test", 15368, "pending", "queued", "https://github.com/d/down/runs/test", 1
    )
    assert found[101] == downstream_finish.Check("cov", None, "failed", "failure", "", 2)


@pytest.mark.parametrize("value", ["0", "-1", "nan"])
def test_refuses_a_bad_interval(value: str, upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        finish("--interval", value)
    assert "must be a number more than 0" in capsys.readouterr().err
