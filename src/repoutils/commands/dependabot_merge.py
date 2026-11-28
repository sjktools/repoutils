"""Sync and squash-merge clean Dependabot pull requests, one at a time.

It handles them one at a time, as a ruleset that requires a branch to be
up to date leaves every other pull request behind after each merge, and
GitHub's own auto-merge neither syncs a branch nor waits for checks that
aren't required. It repeats these steps:

1. Wait until every check on the default branch's latest commit has
   finished, and stop if any of them fails.
2. Take the oldest open, non-draft pull request by --author into the default
   branch that it hasn't found unclean at its current head.
3. Skip it if it conflicts, or if a review thread is unresolved (outdated
   ones included) or a reviewer's latest review requests changes.
4. Sync it with the default branch, if it's behind.
5. Wait for all its checks, not only the required ones, and skip it if one
   fails, or they don't finish within --timeout.
6. Read its reviews again, as a bot may have reviewed the new head, and
   squash-merge it if nothing blocks it, giving GitHub the head whose checks
   passed, so that a push since isn't merged untested.

It exits 0 once no open pull requests by --author remain, and 1 if it stops
with some left, giving the reason for each. A pull request it skipped stays
skipped until its head changes, such as when Dependabot rebases it. Before it
gives up on a conflicting pull request that only --author has pushed to, it
waits --rebase-grace seconds for Dependabot to rebase it, as Dependabot does
by itself after a merge, and with --ask-rebase, asks it to. It keeps no state
outside GitHub, so it can be stopped at any point and run again.
Stdlib-only, like every repoutils module."""

import argparse
import math
import os
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic, sleep
from typing import Any, TextIO

from repoutils import dependabot_merge, github, pr_checks, workflow_summaries
from repoutils.dependabot_merge import Closed, Pull
from repoutils.docstrings import summary
from repoutils.pr_checks import HeadChanged

# The times it starts work on one pull request again after its head moves, before skipping it.
RESTARTS = 2


class Step(Enum):
    """What attempt() leads to, other than a skip or a merge."""

    # The default branch moved, so it waits for that branch's checks again.
    AGAIN = "again"
    # With --dry-run, it found a pull request that it would sync or merge.
    WOULD = "would"


@dataclass(frozen=True)
class Skip:
    """Why it skipped a pull request at head, and whether Dependabot may still rebase it."""

    head: str
    reason: str
    rebasable: bool = False


@dataclass
class Merger:
    repo: str
    token: str
    author: str
    timeout: float
    main_timeout: float
    poll: float
    quiet: float
    retries: int
    ask_rebase: bool
    rebase_grace: float
    dry_run: bool
    # The repository, from GET /repos/{repo}.
    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    required: list[pr_checks.Required] = field(default_factory=list[pr_checks.Required])
    skipped: dict[int, Skip] = field(default_factory=dict[int, Skip])
    merged: list[int] = field(default_factory=list[int])

    @property
    def branch(self) -> str:
        name: str = self.settings["default_branch"]
        return name

    def _commit(self, ref: str) -> dict[str, Any]:
        url = f"{github.API_URL}/repos/{self.repo}/commits/{urllib.parse.quote(ref, safe='')}"
        commit: dict[str, Any] = github.request("GET", url, self.token)
        return commit

    def _names(self, sha: str) -> set[str]:
        return {c.name for c in pr_checks.checks(self.repo, self.token, sha)}

    def _settle(
        self,
        sha: str,
        head: Callable[[], str],
        required: list[pr_checks.Required],
        expected: set[str],
        deadline: float,
        **options: Any,
    ) -> pr_checks.Outcome:
        return pr_checks.settle(
            self.repo,
            self.token,
            sha,
            head,
            required=required,
            expected=expected,
            interval=self.poll,
            quiet=self.quiet,
            deadline=deadline,
            **options,
        )

    def wait_main(self) -> str | None:
        """The default branch's latest commit once its checks pass, or None
        after printing the ones that failed. If the branch moves while it
        waits, it waits for the new commit instead."""
        deadline = monotonic() + self.main_timeout
        while True:
            sha: str = self._commit(self.branch)["sha"]
            print(f"Waiting for the checks on {self.branch} at {sha}")
            try:
                # Not the rulesets' required checks, which are for pull requests, and some of which may run on
                # pull requests only. Nor the parent's check names, as runs started by hand or on a schedule, or
                # workflows with path filters, can give the parent checks that the commit never gets. And it may get
                # no checks at all, if they run on pull requests only.
                outcome = self._settle(
                    sha, lambda: str(self._commit(self.branch)["sha"]), [], set(), deadline, allow_none=True
                )
            except HeadChanged as e:
                print(f"{self.branch} moved: {e}")
                continue
            if outcome.failed:
                print(f"Stopped, as these checks failed on {self.branch} at {sha}:", file=sys.stderr)
                _print_checks(outcome.failed)
                return None
            if outcome.timed_out:
                raise TimeoutError(
                    f"gave up after {self.main_timeout:g} seconds waiting for "
                    f"{', '.join(outcome.waiting)} on {self.branch} at {sha}"
                )
            if outcome.checks:
                print(f"All {len(outcome.checks)} checks passed on {self.branch} at {sha}")
            else:
                print(f"No checks ran on {self.branch} at {sha}")
            return sha

    def open_pulls(self) -> list[dict[str, Any]]:
        """The open, non-draft pull requests by author into the default branch, oldest first."""
        base = urllib.parse.quote(self.branch, safe="")
        url = f"{github.API_URL}/repos/{self.repo}/pulls?state=open&base={base}&sort=created&direction=asc"
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            batch: list[dict[str, Any]] = github.request("GET", f"{url}&per_page=100&page={page}", self.token)
            # A deleted account's pull request has a null user.
            found += [p for p in batch if (p["user"] or {}).get("login") == self.author and not p["draft"]]
            if len(batch) < 100:
                return found
            page += 1

    def _wait_pull(self, pull: Pull, sha: str, expected: set[str], deadline: float) -> pr_checks.Outcome:
        """The checks on pull at sha once they've finished, re-running failed
        GitHub Actions jobs up to retries times. It stops at the first
        failure once it has no re-runs left. With --dry-run, it re-runs nothing."""
        retries = 0 if self.dry_run else self.retries

        def head() -> str:
            return dependabot_merge.head(self.repo, self.token, pull.number)

        outcome = self._settle(sha, head, self.required, expected, deadline, stop_on_failure=retries == 0)
        while (
            outcome.failed
            and not outcome.timed_out
            and retries
            and pr_checks.rerun(self.repo, self.token, outcome.failed)
        ):
            retries -= 1
            replaced = frozenset(c.id for c in outcome.failed)
            outcome = self._settle(
                sha, head, self.required, expected, deadline, stop_on_failure=retries == 0, replaced=replaced
            )
        return outcome

    def attempt(self, number: int, base: str) -> Skip | Step | str:
        """Work on pull request number with the default branch at base. Return
        why it's unclean, the commit it merged as, Step.AGAIN if the default
        branch moved, or with --dry-run, Step.WOULD once it would sync or merge."""
        pull = dependabot_merge.read(self.repo, self.token, number)
        print(f"Looking at {pull.url} ({pull.title}) at {pull.head}")
        if pull.state != "OPEN":
            raise Closed(f"{pull.url} is no longer open")
        if pull.mergeable == "CONFLICTING":
            rebasable = dependabot_merge.untouched(self.repo, self.token, number, self.author)
            return Skip(pull.head, "it conflicts with " + self.branch, rebasable)
        if pull.mergeable != "MERGEABLE":
            return Skip(pull.head, "GitHub didn't work out whether it conflicts")
        if pull.base != self.branch:
            return Skip(pull.head, f"its base is now {pull.base}, not {self.branch}")
        if blocker := pull.blocker():
            return Skip(pull.head, blocker)
        deadline = monotonic() + self.timeout
        sha = pull.head
        if dependabot_merge.behind(self.repo, self.token, base, sha):
            if self.dry_run:
                print(f"Would sync {pull.url} with {self.branch}, wait for its checks, and merge it if they pass")
                return Step.WOULD
            expected = self._names(sha)
            try:
                sha = dependabot_merge.sync(self.repo, self.token, pull, interval=self.poll, deadline=deadline)
            except TimeoutError as e:
                # At the head it synced, so that a sync's head that appears late is one to look at.
                return Skip(pull.head, str(e))
            print(f"Synced {pull.url} with {self.branch}, so its head is now {sha}")
        else:
            expected = set()
        print(f"Waiting for the checks on {pull.url} at {sha}")
        outcome = self._wait_pull(pull, sha, expected, deadline)
        if outcome.failed:
            print(f"These checks failed on {pull.url} at {sha}:")
            _print_checks(outcome.failed, sys.stdout)
            return Skip(sha, "checks failed: " + ", ".join(c.name for c in outcome.failed))
        if outcome.timed_out:
            return Skip(sha, f"its checks didn't finish within {self.timeout:g} seconds: {', '.join(outcome.waiting)}")
        pull = dependabot_merge.read(self.repo, self.token, number)
        if pull.head != sha:
            raise HeadChanged(f"the head moved from {sha} to {pull.head}")
        if pull.base != self.branch:
            return Skip(sha, f"its base is now {pull.base}, not {self.branch}")
        if blocker := pull.blocker():
            return Skip(sha, blocker)
        if self._commit(self.branch)["sha"] != base:
            print(f"{self.branch} moved while it waited, so it starts again")
            return Step.AGAIN
        # A push while it synced, such as Dependabot's rebase, would otherwise pass for the sync's merge.
        if dependabot_merge.behind(self.repo, self.token, base, sha):
            raise HeadChanged(f"{sha} isn't synced with {self.branch} at {base}")
        if self.dry_run:
            print(f"All {len(outcome.checks)} checks passed on {pull.url} at {sha}, so it would merge it")
            return Step.WOULD
        try:
            commit = dependabot_merge.merge(self.repo, self.token, pull, sha, self.settings)
        except dependabot_merge.Refused as e:
            return Skip(sha, str(e))
        print(f"Merged {pull.url} as {commit}")
        return commit

    def handle(self, number: int, base: str) -> Skip | Step | str:
        """attempt(), started again up to RESTARTS times if the head moves."""
        for restart in range(RESTARTS + 1):
            try:
                return self.attempt(number, base)
            except Closed as e:
                return Skip("", str(e))
            except HeadChanged as e:
                print(f"#{number}: {e}" + (", so it starts again" if restart < RESTARTS else ""))
        return self._skip(number, "its head kept moving")

    def _skip(self, number: int, reason: str) -> Skip:
        """Skip pull request number at its current head, for reason, or as closed if it closed."""
        try:
            return Skip(dependabot_merge.head(self.repo, self.token, number), reason)
        except Closed as e:
            return Skip("", str(e))

    def _grace(self, pulls: list[dict[str, Any]], graced: set[tuple[int, str]]) -> bool:
        """Wait for Dependabot to rebase the conflicting pull requests in pulls
        that it still rebases, if there are any it hasn't waited for at their
        current head, asking it to with --ask-rebase. Returns whether it waited."""
        waiting = [
            p["number"]
            for p in pulls
            if (skip := self.skipped[p["number"]]).rebasable and (p["number"], skip.head) not in graced
        ]
        if not waiting or self.dry_run:
            return False
        for number in waiting:
            graced.add((number, self.skipped[number].head))
            if self.ask_rebase:
                dependabot_merge.ask_rebase(self.repo, self.token, number)
                print(f"Asked Dependabot to rebase #{number}")
        if self.rebase_grace <= 0 and not self.ask_rebase:
            return False
        listed = ", ".join(f"#{n}" for n in waiting)
        print(f"Waiting {self.rebase_grace:g} seconds for Dependabot to rebase {listed}")
        sleep(self.rebase_grace)
        return True

    def run(self) -> int:
        self.settings = github.request("GET", f"{github.API_URL}/repos/{self.repo}", self.token)
        rules = pr_checks.branch_rules(self.repo, self.token, self.branch)
        if any(r.get("type") == "merge_queue" for r in rules):
            raise RuntimeError(f"{self.branch} requires a merge queue, which dependabot-merge doesn't use")
        self.required = pr_checks.required(rules)
        graced: set[tuple[int, str]] = set()
        while True:
            if (base := self.wait_main()) is None:
                return 1
            pulls = self.open_pulls()
            if not pulls:
                merged = f", after merging {_count(len(self.merged), 'pull request')}" if self.merged else ""
                print(f"No open pull requests by {self.author} are left{merged}")
                return 0
            for p in pulls:
                number = p["number"]
                if (skip := self.skipped.get(number)) is not None and skip.head == p["head"]["sha"]:
                    continue
                outcome = self.handle(number, base)
                if not isinstance(outcome, Skip):
                    break
                print(f"Skipped #{number}: {outcome.reason}")
                self.skipped[number] = outcome
            else:
                pulls = self.open_pulls()
                # A pull request that opened, or moved, since it listed them is one to look at, and with
                # none left, it exits 0 once it's back at the top.
                if not pulls or any(
                    (s := self.skipped.get(p["number"])) is None or s.head != p["head"]["sha"] for p in pulls
                ):
                    continue
                if self._grace(pulls, graced):
                    continue
                print(f"Stopped with {_count(len(pulls), 'pull request')} by {self.author} left:")
                for p in pulls:
                    print(f"- {p['html_url']}: {self.skipped[p['number']].reason}")
                return 1
            if outcome is Step.WOULD:
                return 0
            if isinstance(outcome, str):
                self.merged.append(number)


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _print_checks(failed: list[pr_checks.Check], file: TextIO | None = None) -> None:
    """Print each check in failed on a line of its own, to file (default: stderr)."""
    for check in failed:
        print(f"- {check.name}: {check.detail}" + (f" {check.url}" if check.url else ""), file=file or sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoutils dependabot-merge",
        description=summary(__doc__),
        epilog=(
            "It uses $GITHUB_TOKEN, defaulting to `gh auth token`, which needs Contents and Pull requests write, "
            "and Workflows write to sync or merge a pull request that changes a file in .github/workflows. "
            "gh's token has that only with the workflow scope, which `gh auth refresh --scopes workflow` adds. "
            "A GITHUB_TOKEN in Actions can't have it, and a merge made with one starts no workflow on the default "
            "branch. With --retries, it also needs Actions write. A token that can bypass the rulesets may merge "
            "despite unmet rules, so the command's own checks are the gate."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--author", default=dependabot_merge.DEPENDABOT, help="the pull requests' author (default: %(default)s)"
    )
    seconds = workflow_summaries.positive
    parser.add_argument(
        "--timeout",
        type=seconds,
        default=1800,
        help="seconds to wait for a pull request to sync and its checks to finish, before skipping it "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--main-timeout",
        type=seconds,
        default=3600,
        help="seconds to wait for the checks on the default branch, before failing (default: %(default)s)",
    )
    parser.add_argument(
        "--poll", type=seconds, default=20, help="seconds between reads of the checks (default: %(default)s)"
    )
    parser.add_argument(
        "--quiet-period",
        type=seconds,
        default=60,
        help="seconds in which no new check may appear before the checks count as finished (default: %(default)s)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        choices=range(10),
        default=0,
        metavar="N",
        help="times to re-run a pull request's failed jobs if only GitHub Actions jobs failed, from 0 to 9 "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--rebase-grace",
        type=float,
        default=180,
        help="seconds to wait for Dependabot to rebase a conflicting pull request that only --author has "
        "pushed to, before giving up on it, or 0 not to wait (default: %(default)s)",
    )
    parser.add_argument(
        "--ask-rebase",
        action="store_true",
        help="comment `@dependabot rebase` on each such pull request before waiting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sync and merge nothing: stop at the first pull request it would sync or merge, and exit 0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not (0 <= args.rebase_grace < math.inf):
        _parser().error("--rebase-grace must be a number, 0 or more")
    if args.ask_rebase and args.author != dependabot_merge.DEPENDABOT:
        _parser().error(f"--ask-rebase asks Dependabot, so it needs --author {dependabot_merge.DEPENDABOT}")

    def go() -> int:
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        return Merger(
            repo=repo,
            token=token,
            author=args.author,
            timeout=args.timeout,
            main_timeout=args.main_timeout,
            poll=args.poll,
            quiet=args.quiet_period,
            retries=args.retries,
            ask_rebase=args.ask_rebase,
            rebase_grace=args.rebase_grace,
            dry_run=args.dry_run,
        ).run()

    return github.guarded("", go)


run = main
