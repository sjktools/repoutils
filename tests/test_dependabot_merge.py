import io
import json
import urllib.error
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

import pytest

from repoutils import dependabot_merge, github, pr_checks
from repoutils.commands import dependabot_merge as command

API = f"{github.API_URL}/repos/o/r"
BOT = "dependabot[bot]"
RULES = [
    {"type": "pull_request", "parameters": {}},
    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "test"}]}},
]


def run(name: str, status: str = "completed", conclusion: str = "success", run_id: int = 1) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"id": pr_checks.ACTIONS_APP},
        "html_url": f"https://github.com/o/r/runs/{run_id}",
    }


# The checks that every commit gets, unless a test gives it others.
PASSED = [run("test"), run("lint", run_id=2), run("review", run_id=3)]


@dataclass
class Fake:
    """A pull request's state on GitHub."""

    number: int
    head: str
    title: str = "Bump x"
    author: str = BOT
    draft: bool = False
    state: str = "open"
    # The mergeable value of each GraphQL read, staying at the last.
    mergeable: list[str] = field(default_factory=lambda: ["MERGEABLE"])
    # The unresolved thread URLs of each GraphQL read, staying at the last.
    threads: list[list[str]] = field(default_factory=lambda: [[]])
    changes_requested: list[str] = field(default_factory=list[str])
    commit_authors: list[str] = field(default_factory=lambda: [BOT])
    committer: str = "web-flow"
    base: str = "main"
    more_reviews: bool = False
    # Heads that someone else pushes, one on each read of the pull request.
    pushes: list[str] = field(default_factory=list[str])
    reads: int = 0


class FakeGitHub:
    """A repository's default branch, pull requests and checks. Each read of
    the checks on a commit takes the next of its polls, and stays at the last.
    A commit with no polls has PASSED."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pulls: list[Fake],
        *,
        polls: dict[str, list[list[dict[str, Any]]]] | None = None,
        rules: list[dict[str, Any]] | None = None,
        merge_errors: list[tuple[int, bytes]] | None = None,
        sync_errors: list[int] | None = None,
        sync_moves: bool = True,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.pulls = {p.number: p for p in pulls}
        self.polls = polls or {}
        self.check_reads: dict[str, int] = {}
        self.rules = RULES if rules is None else rules
        # The HTTP errors that the first merges, or syncs, get.
        self.merge_errors = merge_errors or []
        self.sync_errors = sync_errors or []
        self.sync_moves = sync_moves
        self.settings = {
            "default_branch": "main",
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "BLANK",
        } | (settings or {})
        self.main = "m1"
        self.parents = {"m1": "m0"}
        self.changes: list[tuple[str, str, Any]] = []
        self.now = 0.0
        self.slept: list[float] = []
        monkeypatch.setattr(github, "request", self.request)
        for module in (pr_checks, dependabot_merge, command):
            monkeypatch.setattr(module, "sleep", self.sleep)
            monkeypatch.setattr(module, "monotonic", lambda: self.now)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def _checks(self, sha: str) -> list[dict[str, Any]]:
        polls = self.polls.get(sha, [PASSED])
        index = self.check_reads.get(sha, 0)
        self.check_reads[sha] = index + 1
        return polls[min(index, len(polls) - 1)]

    def _graphql(self, variables: dict[str, Any]) -> dict[str, Any]:
        pull = self.pulls[variables["pr"]]
        index = pull.reads
        pull.reads += 1
        threads = pull.threads[min(index, len(pull.threads) - 1)]
        node = {
            "state": pull.state.upper(),
            "isDraft": pull.draft,
            "mergeable": pull.mergeable[min(index, len(pull.mergeable) - 1)],
            "headRefOid": pull.head,
            "baseRefName": pull.base,
            "title": pull.title,
            "body": "Bumps x.",
            "url": f"https://github.com/o/r/pull/{pull.number}",
            "reviewThreads": {
                "nodes": [{"isResolved": False, "comments": {"nodes": [{"url": u}]}} for u in threads]
                + [{"isResolved": True, "comments": {"nodes": []}}],
                "pageInfo": {"hasNextPage": False},
            },
            "latestReviews": {
                "nodes": [{"state": "CHANGES_REQUESTED", "author": {"login": a}} for a in pull.changes_requested]
                + [{"state": "APPROVED", "author": None}],
                "pageInfo": {"hasNextPage": pull.more_reviews},
            },
        }
        return {"data": {"repository": {"pullRequest": node}}}

    def _pull(self, number: int) -> dict[str, Any]:
        pull = self.pulls[number]
        if pull.pushes:
            pull.head = pull.pushes.pop(0)
        return {
            "number": number,
            "state": pull.state,
            "draft": pull.draft,
            "user": {"login": pull.author},
            "head": {"sha": pull.head},
            "html_url": f"https://github.com/o/r/pull/{number}",
        }

    def request(self, method: str, url: str, token: str, *, json_body: Any = None) -> Any:
        assert token == "tok"
        if url == f"{github.API_URL}/graphql":
            return self._graphql(json_body["variables"])
        path = url.removeprefix(API)
        if method != "GET":
            self.changes.append((method, path, json_body))
            return self._change(method, path, json_body)
        if path == "":
            return self.settings
        if path.startswith("/rules/branches/main?"):
            return self.rules if "page=1" in path else []
        if path.startswith("/commits/") and "/" not in path.removeprefix("/commits/"):
            sha = self.main if path == "/commits/main" else path.removeprefix("/commits/")
            parent = self.parents.get(sha)
            return {"sha": sha, "parents": [{"sha": parent}] if parent else []}
        if "/check-runs?" in path:
            return {"check_runs": self._checks(path.split("/")[2])}
        if "/status?" in path:
            return {"statuses": []}
        if path.startswith("/pulls?"):
            pulls = [self._pull(n) for n, p in sorted(self.pulls.items()) if p.state == "open"]
            return pulls if "page=1" in path else []
        if path.startswith("/compare/"):
            base, head = path.removeprefix("/compare/").split("...")
            # A synced head ends with the base it was synced with.
            return {"behind_by": 0 if head.endswith(f"+{base}") else 1}
        if path.endswith("/commits?per_page=100"):
            pull = self.pulls[int(path.split("/")[2])]
            return [
                {"author": {"login": a} if a else None, "committer": {"login": pull.committer}}
                for a in pull.commit_authors
            ]
        if path.startswith("/pulls/"):
            return self._pull(int(path.removeprefix("/pulls/")))
        raise AssertionError(f"unexpected {method} {url}")

    def _change(self, method: str, path: str, body: Any) -> Any:
        if path.endswith("/update-branch"):
            pull = self.pulls[int(path.split("/")[2])]
            assert body == {"expected_head_sha": pull.head}
            if self.sync_errors:
                raise urllib.error.HTTPError(path, self.sync_errors.pop(0), "No", Message(), io.BytesIO(b"{}"))
            if self.sync_moves:
                pull.head = f"{pull.head}+{self.main}"
            return {"message": "Updating pull request branch."}
        if path.endswith("/merge"):
            number = int(path.split("/")[2])
            if self.merge_errors:
                code, detail = self.merge_errors.pop(0)
                raise urllib.error.HTTPError(path, code, "No", Message(), io.BytesIO(detail))
            self.pulls[number].state = "closed"
            merged = f"m-{number}"
            self.parents[merged], self.main = self.main, merged
            return {"sha": merged, "merged": True}
        if path.endswith("/rerun-failed-jobs"):
            return None
        if path.endswith("/comments"):
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def merge(*args: str) -> int:
    return command.run(["--repo", "o/r", "--poll", "20", "--quiet-period", "60", *args])


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")


def merges(fake: FakeGitHub) -> list[tuple[str, str, Any]]:
    return [c for c in fake.changes if c[1].endswith(("/merge", "/update-branch"))]


def test_syncs_and_merges_each_pull_request_in_turn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A draft, and a pull request by someone else, are left alone.
    others = [Fake(3, "h3", draft=True), Fake(4, "h4", author="someone")]
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1"), Fake(2, "h2"), *others])
    assert merge() == 0
    assert merges(fake) == [
        ("PUT", "/pulls/1/update-branch", {"expected_head_sha": "h1"}),
        (
            "PUT",
            "/pulls/1/merge",
            {"merge_method": "squash", "sha": "h1+m1", "commit_title": "Bump x (#1)", "commit_message": ""},
        ),
        # Merging 1 left 2 behind the new commit on main.
        ("PUT", "/pulls/2/update-branch", {"expected_head_sha": "h2"}),
        (
            "PUT",
            "/pulls/2/merge",
            {"merge_method": "squash", "sha": "h2+m-1", "commit_title": "Bump x (#2)", "commit_message": ""},
        ),
    ]
    out = capsys.readouterr().out
    assert "Synced https://github.com/o/r/pull/1 with main, so its head is now h1+m1\n" in out
    assert "Merged https://github.com/o/r/pull/2 as m-2\n" in out
    assert out.endswith(f"No open pull requests by {BOT} are left, after merging 2 pull requests\n")


def test_merges_an_up_to_date_pull_request_without_syncing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1+m1")])
    assert merge() == 0
    assert [c[1] for c in merges(fake)] == ["/pulls/1/merge"]


def test_exits_0_with_no_pull_requests(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [])
    assert merge() == 0
    assert fake.changes == []
    assert capsys.readouterr().out.endswith(f"No open pull requests by {BOT} are left\n")


def test_stops_if_a_check_fails_on_the_default_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # It stops at the failure, without waiting for the pending check.
    failing = [run("test", conclusion="failure"), run("lint", "in_progress", run_id=2)]
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")], polls={"m1": [failing]})
    assert merge() == 1
    assert fake.changes == []
    assert fake.slept == []
    err = capsys.readouterr().err
    assert "Stopped, as these checks failed on main at m1:\n- test: failure https://github.com/o/r/runs/1\n" in err


def test_waits_for_the_default_branch_to_get_its_parents_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    polls = {"m1": [[run("test")], [run("test"), run("lint", "queued", run_id=2)], PASSED]}
    fake = FakeGitHub(monkeypatch, [], polls=polls)
    assert merge() == 0
    # Three reads to see every check finish, then the quiet period.
    assert fake.slept == [20, 20, 20, 20, 20]


def test_fails_if_the_default_branch_never_finishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeGitHub(monkeypatch, [], polls={"m1": [[run("test")]]})
    assert merge("--main-timeout", "100") == 1
    assert "gave up after 100 seconds waiting for lint, review on main at m1" in capsys.readouterr().err


def test_skips_a_pull_request_whose_checks_fail_and_merges_the_next(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    polls = {"h1+m1": [[run("test", conclusion="failure")]]}
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1"), Fake(2, "h2")], polls=polls)
    assert merge() == 1
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch", "/pulls/2/update-branch", "/pulls/2/merge"]
    out = capsys.readouterr().out
    assert "Skipped #1: checks failed: test\n" in out
    # After merging 2, it didn't try 1 again, as its head didn't change.
    assert out.endswith(
        f"Stopped with 1 pull request by {BOT} left:\n- https://github.com/o/r/pull/1: checks failed: test\n"
    )


def test_reruns_failed_jobs_with_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = [run("test", conclusion="failure", run_id=11), *PASSED[1:]]
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")], polls={"h1+m1": [failed]})

    def rerun(repo: str, token: str, checks: list[pr_checks.Check]) -> bool:
        assert [c.id for c in checks] == [11]
        fake.polls["h1+m1"] = [failed, [run("test", run_id=12), *PASSED[1:]]]
        fake.check_reads["h1+m1"] = 0
        return True

    monkeypatch.setattr(pr_checks, "rerun", rerun)
    assert merge("--retries", "1") == 0
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch", "/pulls/1/merge"]


@pytest.mark.parametrize(
    ("pull", "reason"),
    [
        (Fake(1, "h1", threads=[["https://github.com/o/r/pull/1#discussion_r1"]]), "1 unresolved review thread, "),
        (Fake(1, "h1", changes_requested=["a-review-bot[bot]"]), "changes requested by a-review-bot[bot]"),
        (Fake(1, "h1", mergeable=["UNKNOWN"]), "GitHub didn't work out whether it conflicts"),
        (Fake(1, "h1", mergeable=["CONFLICTING"], commit_authors=[BOT, "me"]), "it conflicts with main"),
    ],
)
def test_skips_an_unclean_pull_request_without_syncing_it(
    pull: Fake, reason: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [pull])
    assert merge() == 1
    assert fake.changes == []
    assert f"- https://github.com/o/r/pull/1: {reason}" in capsys.readouterr().out


def test_skips_a_pull_request_reviewed_while_its_checks_ran(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", threads=[[], ["https://x/1", "https://x/2"]])])
    assert merge() == 1
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch"]
    assert "Skipped #1: 2 unresolved review threads, first https://x/1\n" in capsys.readouterr().out


def test_waits_for_dependabot_to_rebase_a_conflicting_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1", mergeable=["CONFLICTING"])
    fake = FakeGitHub(monkeypatch, [pull])
    real = fake.sleep

    def rebase(seconds: float) -> None:
        # Dependabot rebases it while the command waits.
        if seconds == 180:
            pull.head, pull.mergeable, pull.reads = "h1r", ["MERGEABLE"], 0
        real(seconds)

    for module in (pr_checks, dependabot_merge, command):
        monkeypatch.setattr(module, "sleep", rebase)
    assert merge("--ask-rebase") == 0
    assert ("POST", "/issues/1/comments", {"body": "@dependabot rebase"}) in fake.changes
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch", "/pulls/1/merge"]
    assert "Waiting 180 seconds for Dependabot to rebase #1\n" in capsys.readouterr().out


def test_gives_up_on_a_conflicting_pull_request_after_the_grace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", mergeable=["CONFLICTING"])])
    assert merge("--rebase-grace", "30") == 1
    # It waited once at that head, and asked nothing.
    assert fake.changes == []
    assert fake.slept.count(30) == 1
    assert "- https://github.com/o/r/pull/1: it conflicts with main" in capsys.readouterr().out


def test_starts_again_when_the_head_moves(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pull = Fake(1, "h1")
    fake = FakeGitHub(monkeypatch, [pull])
    real = fake._pull
    pushed: list[bool] = []

    def push(number: int) -> dict[str, Any]:
        # Dependabot force-pushes once, while it waits for the synced head's checks.
        if pull.head == "h1+m1" and not pushed:
            pushed.append(True)
            pull.head = "h2"
        return real(number)

    monkeypatch.setattr(fake, "_pull", push)
    assert merge() == 0
    assert [c[2]["expected_head_sha"] for c in merges(fake) if c[1].endswith("update-branch")] == ["h1", "h2"]
    assert merges(fake)[-1][2]["sha"] == "h2+m1"
    assert "#1: h2 isn't synced with main at m1, so it starts again\n" in capsys.readouterr().out


def test_skips_a_pull_request_whose_checks_time_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")], polls={"h1+m1": [[run("test", "in_progress")]]})
    assert merge("--timeout", "100") == 1
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch"]
    out = capsys.readouterr().out
    assert "its checks didn't finish within 100 seconds: lint, review, test\n" in out


@pytest.mark.parametrize(
    ("detail", "reason"),
    [(json.dumps({"message": "Base moved"}).encode(), "Base moved"), (b"<html>", "<html>")],
)
def test_skips_a_pull_request_that_github_refuses_to_merge(
    detail: bytes, reason: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeGitHub(monkeypatch, [Fake(1, "h1")], merge_errors=[(405, detail)])
    assert merge() == 1
    assert f"- https://github.com/o/r/pull/1: GitHub refused to merge it: {reason}\n" in capsys.readouterr().out


def test_starts_again_if_github_says_the_head_moved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")], merge_errors=[(409, b"{}")], sync_errors=[422])
    assert merge() == 0
    assert [c[1] for c in merges(fake)] == [
        "/pulls/1/update-branch",
        "/pulls/1/update-branch",
        "/pulls/1/merge",
        "/pulls/1/merge",
    ]
    out = capsys.readouterr().out
    assert "#1: GitHub didn't sync https://github.com/o/r/pull/1, whose head may have moved from h1, so it" in out
    assert "#1: the head of https://github.com/o/r/pull/1 is no longer h1+m1, so it starts again\n" in out


def test_skips_a_pull_request_whose_head_keeps_moving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeGitHub(monkeypatch, [Fake(1, "h1")], sync_errors=[422, 422, 422])
    assert merge() == 1
    out = capsys.readouterr().out
    assert "may have moved from h1\n" in out
    assert "- https://github.com/o/r/pull/1: its head kept moving\n" in out


@pytest.mark.parametrize("on_merge", [False, True])
def test_fails_on_another_http_error(
    on_merge: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if on_merge:
        FakeGitHub(monkeypatch, [Fake(1, "h1")], merge_errors=[(500, b"{}")])
    else:
        FakeGitHub(monkeypatch, [Fake(1, "h1")], sync_errors=[500])
    assert merge() == 1
    assert "HTTP 500 from " in capsys.readouterr().err


def test_skips_a_pull_request_whose_sync_never_shows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeGitHub(monkeypatch, [Fake(1, "h1")], sync_moves=False)
    assert merge("--timeout", "50") == 1
    assert (
        "- https://github.com/o/r/pull/1: no new head appeared on https://github.com/o/r/pull/1 after syncing it"
        in (capsys.readouterr().out)
    )


def test_gives_the_body_as_the_message_if_the_repository_does(monkeypatch: pytest.MonkeyPatch) -> None:
    for message, body in (("PR_BODY", {"commit_message": "Bumps x."}), ("COMMIT_MESSAGES", {})):
        fake = FakeGitHub(monkeypatch, [Fake(1, "h1+m1")], settings={"squash_merge_commit_message": message})
        assert merge() == 0
        assert merges(fake)[0][2] == {"merge_method": "squash", "sha": "h1+m1", "commit_title": "Bump x (#1)"} | body


def test_skips_a_pull_request_that_closes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pull = Fake(1, "h1")
    fake = FakeGitHub(monkeypatch, [pull, Fake(2, "h2+m1")])
    real = fake._pull

    def close(number: int) -> dict[str, Any]:
        # Dependabot closes 1 as superseded while its checks run.
        if pull.head == "h1+m1":
            pull.state = "closed"
        return real(number)

    monkeypatch.setattr(fake, "_pull", close)
    assert merge() == 0
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch", "/pulls/2/merge"]
    assert "Skipped #1: https://github.com/o/r/pull/1 is no longer open\n" in capsys.readouterr().out


def test_starts_again_if_the_default_branch_moves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1")
    fake = FakeGitHub(monkeypatch, [pull])
    real = fake._checks

    def moved(sha: str) -> list[dict[str, Any]]:
        # Someone merges into main while the pull request's checks run.
        if sha == "h1+m1" and fake.main == "m1":
            fake.main, fake.parents["m2"] = "m2", "m1"
        return real(sha)

    monkeypatch.setattr(fake, "_checks", moved)
    assert merge() == 0
    assert merges(fake)[-1][2]["sha"] == "h1+m1+m2"
    out = capsys.readouterr().out
    assert "main moved while it waited, so it starts again\n" in out


def test_waits_for_the_default_branchs_new_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [])
    real = fake._checks

    def moved(sha: str) -> list[dict[str, Any]]:
        if sha == "m1":
            fake.main, fake.parents["m2"] = "m2", "m1"
        return real(sha)

    monkeypatch.setattr(fake, "_checks", moved)
    assert merge() == 0
    out = capsys.readouterr().out
    assert "main moved: the head moved from m1 to m2\nWaiting for the checks on main at m2\n" in out


def test_looks_again_at_a_pull_request_that_opened_while_it_worked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", threads=[["https://x/1"]])])
    real = fake._graphql

    def opened(variables: dict[str, Any]) -> dict[str, Any]:
        # Dependabot opens 2 while the command looks at 1.
        fake.pulls.setdefault(2, Fake(2, "h2+m1"))
        return real(variables)

    monkeypatch.setattr(fake, "_graphql", opened)
    assert merge() == 1
    assert [c[1] for c in merges(fake)] == ["/pulls/2/merge"]


def test_reads_at_most_100_review_threads(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")])
    real = fake._graphql

    def many(variables: dict[str, Any]) -> dict[str, Any]:
        data = real(variables)
        data["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]["hasNextPage"] = True
        return data

    monkeypatch.setattr(fake, "_graphql", many)
    assert merge() == 1
    assert "- https://github.com/o/r/pull/1: over 100 review threads, more than it reads\n" in capsys.readouterr().out


def test_dry_run_syncs_and_merges_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1"), Fake(2, "h2")])
    assert merge("--dry-run") == 0
    assert fake.changes == []
    assert "Would sync https://github.com/o/r/pull/1 with main" in capsys.readouterr().out


def test_dry_run_says_it_would_merge_an_up_to_date_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1+m1")])
    assert merge("--dry-run") == 0
    assert fake.changes == []
    assert "All 3 checks passed on https://github.com/o/r/pull/1 at h1+m1, so it would merge it" in (
        capsys.readouterr().out
    )


def test_refuses_a_merge_queue(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")], rules=[*RULES, {"type": "merge_queue"}])
    assert merge() == 1
    assert fake.changes == []
    assert "main requires a merge queue" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_refuses_a_bad_rebase_grace(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        merge("--rebase-grace", value)
    assert "--rebase-grace must be a number, 0 or more" in capsys.readouterr().err


def test_skips_a_pull_request_that_closed_before_it_looked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1")])
    real = fake._graphql

    def closed(variables: dict[str, Any]) -> dict[str, Any]:
        fake.pulls[1].state = "closed"
        return real(variables)

    monkeypatch.setattr(fake, "_graphql", closed)
    assert merge() == 0
    assert fake.changes == []
    assert "Skipped #1: https://github.com/o/r/pull/1 is no longer open\n" in capsys.readouterr().out


def test_starts_again_if_the_head_moved_by_the_last_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1")
    fake = FakeGitHub(monkeypatch, [pull])
    real = fake._graphql

    def pushed(variables: dict[str, Any]) -> dict[str, Any]:
        # Someone pushes just after its checks finish.
        if pull.reads == 1:
            pull.head = "h9"
        return real(variables)

    monkeypatch.setattr(fake, "_graphql", pushed)
    assert merge() == 0
    assert merges(fake)[-1][2]["sha"] == "h9+m1"
    assert "#1: the head moved from h1+m1 to h9, so it starts again\n" in capsys.readouterr().out


def test_waits_for_no_rebase_with_no_grace(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", mergeable=["CONFLICTING"])])
    assert merge("--rebase-grace", "0") == 1
    assert fake.changes == []
    assert "Dependabot to rebase" not in capsys.readouterr().out


def test_reads_every_page_of_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    # The only pull request by Dependabot is on the second page.
    others = [Fake(n, f"h{n}", author="someone") for n in range(1, 101)]
    fake = FakeGitHub(monkeypatch, [*others, Fake(101, "h101+m1")])
    real = fake.request

    def paged(method: str, url: str, token: str, *, json_body: Any = None) -> Any:
        if "/pulls?" in url:
            pulls = [fake._pull(n) for n, p in sorted(fake.pulls.items()) if p.state == "open"]
            return pulls[:100] if url.endswith("&page=1") else pulls[100:]
        return real(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", paged)
    assert merge() == 0
    assert [c[1] for c in merges(fake)] == ["/pulls/101/merge"]


def test_skips_a_pull_request_whose_base_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1")
    fake = FakeGitHub(monkeypatch, [pull])
    real = fake._graphql

    def retarget(variables: dict[str, Any]) -> dict[str, Any]:
        # Someone retargets it while its checks run.
        if pull.reads == 1:
            pull.base = "other"
        return real(variables)

    monkeypatch.setattr(fake, "_graphql", retarget)
    assert merge() == 1
    assert [c[1] for c in merges(fake)] == ["/pulls/1/update-branch"]
    assert "- https://github.com/o/r/pull/1: its base is now other, not main\n" in capsys.readouterr().out


def test_skips_a_pull_request_retargeted_before_it_looked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", base="other")])
    assert merge() == 1
    assert fake.changes == []
    assert "Skipped #1: its base is now other, not main\n" in capsys.readouterr().out


def test_skips_a_pull_request_that_closes_after_timing_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1")
    FakeGitHub(monkeypatch, [pull])

    def sync(*args: Any, **options: Any) -> str:
        # It closes just as the wait for the sync gives up.
        pull.state = "closed"
        raise TimeoutError("no new head appeared")

    monkeypatch.setattr(dependabot_merge, "sync", sync)
    # It skips it at the head it synced, and then finds it closed.
    assert merge() == 0
    assert "Skipped #1: no new head appeared\n" in capsys.readouterr().out


def test_skips_a_pull_request_with_over_100_reviewers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeGitHub(monkeypatch, [Fake(1, "h1", more_reviews=True)])
    assert merge() == 1
    assert "- https://github.com/o/r/pull/1: over 100 reviewers, more than it reads\n" in capsys.readouterr().out


def test_leaves_the_title_to_github_unless_the_repository_takes_the_pull_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1+m1")], settings={"squash_merge_commit_title": "COMMIT_OR_PR_TITLE"})
    assert merge() == 0
    assert merges(fake)[0][2] == {"merge_method": "squash", "sha": "h1+m1", "commit_message": ""}


def test_waits_for_no_rebase_of_a_pull_request_someone_else_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(monkeypatch, [Fake(1, "h1", mergeable=["CONFLICTING"], committer="someone")])
    assert merge() == 1
    assert 180 not in fake.slept


def test_refuses_to_ask_another_author_to_rebase(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        merge("--ask-rebase", "--author", "renovate[bot]")
    assert "--ask-rebase asks Dependabot, so it needs --author dependabot[bot]" in capsys.readouterr().err


def test_dry_run_reruns_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    failed = [run("test", conclusion="failure", run_id=11), *PASSED[1:]]
    FakeGitHub(monkeypatch, [Fake(1, "h1+m1")], polls={"h1+m1": [failed]})

    def rerun(repo: str, token: str, checks: list[pr_checks.Check]) -> bool:
        raise AssertionError("a dry run re-ran jobs")

    monkeypatch.setattr(pr_checks, "rerun", rerun)
    assert merge("--dry-run", "--retries", "1") == 1
    assert "Skipped #1: checks failed: test\n" in capsys.readouterr().out


def test_waits_for_no_pull_request_check_on_the_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ruleset requires pr-only, which runs on pull requests only.
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "pr-only"}]}}]
    FakeGitHub(monkeypatch, [], rules=rules)
    assert merge() == 0


def test_skips_a_pull_request_that_closes_while_its_head_keeps_moving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = Fake(1, "h1")
    FakeGitHub(monkeypatch, [pull])
    calls: list[int] = []

    def sync(*args: Any, **options: Any) -> str:
        calls.append(1)
        if len(calls) > command.RESTARTS:
            pull.state = "closed"
        raise pr_checks.HeadChanged("moved")

    monkeypatch.setattr(dependabot_merge, "sync", sync)
    assert merge() == 0
    assert "Skipped #1: https://github.com/o/r/pull/1 is no longer open\n" in capsys.readouterr().out
