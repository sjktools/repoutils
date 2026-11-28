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
