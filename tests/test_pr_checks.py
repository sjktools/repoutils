from typing import Any

import pytest

from repoutils import github, pr_checks


def run(name: str, status: str = "completed", run_id: int = 1) -> dict[str, Any]:
    return {"id": run_id, "name": name, "status": status, "conclusion": "success", "app": {"id": 1}}


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, polls: list[list[dict[str, Any]]]) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.polls = polls
        self.reads = 0
        monkeypatch.setattr(pr_checks, "sleep", self.sleep)
        monkeypatch.setattr(pr_checks, "monotonic", lambda: self.now)
        monkeypatch.setattr(github, "request", self.request)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def request(self, method: str, url: str, token: str) -> Any:
        if "/check-runs?" in url:
            self.reads += 1
            return {"check_runs": self.polls[min(self.reads - 1, len(self.polls) - 1)]}
        return {"statuses": []}


def settle(head: Any = lambda: "c", **options: Any) -> pr_checks.Outcome:
    defaults: dict[str, Any] = {"required": [], "expected": set(), "interval": 10, "quiet": 25, "deadline": 100}
    return pr_checks.settle("o/r", "tok", "c", head, **(defaults | options))


def test_waits_out_the_quiet_period_after_the_last_new_check(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch, [[run("a")], [run("a"), run("b", run_id=2)]])
    outcome = settle()
    assert [c.name for c in outcome.checks] == ["a", "b"]
    assert not outcome.timed_out
    # b appeared at 10, so it waits until 35, and no longer than the quiet period needs.
    assert clock.slept == [10, 10, 10, 5]


def test_says_it_waited_for_any_check_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, [[]])
    assert settle().waiting == ["any check to start"]


def test_says_it_waited_for_the_quiet_period(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, [[run("a")]])
    assert settle(quiet=500).waiting == ["500 seconds with no new check"]


def test_waits_for_required_and_expected_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, [[run("a", "queued")]])
    outcome = settle(required=[pr_checks.Required("r", None)], expected={"e", "a"})
    assert outcome.waiting == ["r", "e", "a"]


def test_raises_if_the_head_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, [[run("a")]])
    with pytest.raises(pr_checks.HeadChanged, match="the head moved from c to d"):
        settle(lambda: "d")


def test_waits_for_a_check_unless_none_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch, [[]])
    assert settle(allow_none=True).waiting == []
    # The quiet period only.
    assert clock.slept == [10, 10, 5]


def test_counts_the_quiet_period_from_when_github_says_a_check_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    # It finished 20 seconds before the first read.
    finished = run("a") | {"started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:01:00Z"}
    clock = Clock(monkeypatch, [[finished]])
    monkeypatch.setattr(pr_checks, "time", lambda: 1767225680.0)
    assert settle().waiting == []
    assert clock.slept == [5]


def test_a_check_finishing_restarts_the_quiet_period(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch, [[run("a", "in_progress")], [run("a", "in_progress")], [run("a")]])
    assert settle().waiting == []
    # It finished at 20, so it waits until 45.
    assert clock.slept == [10, 10, 10, 10, 5]


def test_reads_when_each_check_started_or_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    started = run("a", "in_progress") | {"started_at": "2026-01-01T00:00:00Z", "completed_at": None}
    finished = run("b") | {"started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:01:00+00:00"}

    def request(method: str, url: str, token: str) -> Any:
        if "/check-runs?" in url:
            return {"check_runs": [started, finished]}
        return {"statuses": [{"context": "c", "state": "success", "id": 3, "created_at": "2026-01-01T00:02:00Z"}]}

    monkeypatch.setattr(github, "request", request)
    assert [c.since for c in pr_checks.checks("o/r", "tok", "c")] == [1767225600.0, 1767225660.0, 1767225720.0]
