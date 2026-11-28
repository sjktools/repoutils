"""The reads and changes that `dependabot-merge` makes on one pull request:
whether it conflicts or has an unresolved review, whether it's behind its
base, syncing it with its base, and squash-merging it. Kept apart from the
command for length. Stdlib-only, like every repoutils module."""

import json
import urllib.error
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from repoutils import github
from repoutils.pr_checks import HeadChanged

# How often, and how many times, it reads a pull request whose mergeability GitHub hasn't worked out yet.
MERGEABLE_INTERVAL = 3.0
MERGEABLE_READS = 10

_PULL = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      state isDraft mergeable headRefOid title body url
      reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { url } } } pageInfo { hasNextPage } }
      latestReviews(first: 100) { nodes { state author { login } } }
    }
  }
}"""


class Closed(RuntimeError):
    """The pull request closed, or merged, while the command worked on it."""


class Refused(RuntimeError):
    """GitHub refused to merge the pull request."""


@dataclass(frozen=True)
class Pull:
    """A pull request, as one GraphQL query gives it."""

    number: int
    url: str
    title: str
    body: str
    head: str
    # OPEN, CLOSED or MERGED.
    state: str
    # MERGEABLE, CONFLICTING or UNKNOWN.
    mergeable: str
    # The URL of the first comment of each unresolved review thread, outdated ones included.
    unresolved: list[str]
    # Whether it has more review threads than the query reads.
    more_threads: bool
    # The reviewers whose latest review requests changes.
    changes_requested: list[str]

    def blocker(self) -> str | None:
        """Why its reviews keep it from merging, or None if nothing does."""
        if self.unresolved:
            count = len(self.unresolved)
            return f"{count} unresolved review thread{'s' if count > 1 else ''}, first {self.unresolved[0]}"
        if self.more_threads:
            return "over 100 review threads, more than it reads"
        if self.changes_requested:
            return f"changes requested by {', '.join(self.changes_requested)}"
        return None


def read(repo: str, token: str, number: int) -> Pull:
    """Pull request number in repo, read again while GitHub hasn't worked out
    whether it conflicts, which the first read starts it doing."""
    owner, name = repo.split("/", 1)
    variables = {"owner": owner, "name": name, "pr": number}
    node = github.graphql(_PULL, variables, token)["repository"]["pullRequest"]
    for _ in range(MERGEABLE_READS - 1):
        if node["mergeable"] != "UNKNOWN" or node["state"] != "OPEN":
            break
        sleep(MERGEABLE_INTERVAL)
        node = github.graphql(_PULL, variables, token)["repository"]["pullRequest"]
    threads = node["reviewThreads"]
    return Pull(
        number=number,
        url=node["url"],
        title=node["title"],
        body=node["body"] or "",
        head=node["headRefOid"],
        state=node["state"],
        mergeable=node["mergeable"],
        unresolved=[
            (t["comments"]["nodes"] or [{"url": node["url"]}])[0]["url"]
            for t in threads["nodes"]
            if not t["isResolved"]
        ],
        more_threads=threads["pageInfo"]["hasNextPage"],
        # A deleted account's review has a null author.
        changes_requested=[
            (r["author"] or {}).get("login") or "a deleted account"
            for r in node["latestReviews"]["nodes"]
            if r["state"] == "CHANGES_REQUESTED"
        ],
    )


def head(repo: str, token: str, number: int) -> str:
    """The head commit of pull request number, raising Closed if it's no longer open."""
    pull: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/pulls/{number}", token)
    if pull["state"] != "open":
        raise Closed(f"{pull['html_url']} is no longer open")
    sha: str = pull["head"]["sha"]
    return sha


def behind(repo: str, token: str, base: str, sha: str) -> int:
    """How many commits that commit base has, commit sha lacks. Read from the
    comparison rather than mergeStateStatus, which gives one state of several
    that can hold at once, such as both behind and blocked."""
    compared = github.request("GET", f"{github.API_URL}/repos/{repo}/compare/{base}...{sha}", token)
    count: int = compared["behind_by"]
    return count


def sync(repo: str, token: str, pull: Pull, *, interval: float, deadline: float) -> str:
    """Merge pull's base into its branch, as GitHub's Update branch button
    does, and return the new head once GitHub has made it. A merge rather
    than a rebase, so Dependabot's signed commits stay as they are, and the
    squash merge leaves the merge commit off the base. Raises HeadChanged if
    the head is no longer pull.head, so a push made since isn't merged
    untested, and TimeoutError if no new head appears by deadline."""
    try:
        github.request(
            "PUT",
            f"{github.API_URL}/repos/{repo}/pulls/{pull.number}/update-branch",
            token,
            json_body={"expected_head_sha": pull.head},
        )
    except urllib.error.HTTPError as e:
        # 422 if the head moved, or if the branch is already up to date, which a read again tells apart.
        if e.code != 422:
            raise
        e.close()
        raise HeadChanged(f"GitHub didn't sync {pull.url}, whose head may have moved from {pull.head}") from None
    while (new := head(repo, token, pull.number)) == pull.head:
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError(f"no new head appeared on {pull.url} after syncing it")
        sleep(min(interval, left))
    return new


def merge(repo: str, token: str, pull: Pull, sha: str, settings: dict[str, Any]) -> str:
    """Squash-merge pull, only if its head is still sha, the commit whose
    checks passed, and return the merge's commit. Gives the commit's title
    and message as the repository's settings (GET /repos/{repo}) say, as a
    merge through the API might not follow them. Raises HeadChanged if the
    head moved, and Refused, with GitHub's reason, if GitHub refuses the
    merge for another reason."""
    body: dict[str, Any] = {"merge_method": "squash", "sha": sha, "commit_title": f"{pull.title} (#{pull.number})"}
    message = settings.get("squash_merge_commit_message")
    if message == "BLANK":
        body["commit_message"] = ""
    elif message == "PR_BODY":
        body["commit_message"] = pull.body
    try:
        url = f"{github.API_URL}/repos/{repo}/pulls/{pull.number}/merge"
        merged = github.request("PUT", url, token, json_body=body)
    except urllib.error.HTTPError as e:
        if e.code not in (405, 409):
            raise
        with e:
            detail = e.read().decode(errors="replace")
        if e.code == 409:
            raise HeadChanged(f"the head of {pull.url} is no longer {sha}") from None
        try:
            detail = json.loads(detail)["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise Refused(f"GitHub refused to merge it: {detail}") from None
    commit: str = merged["sha"]
    return commit


def untouched(repo: str, token: str, number: int, author: str) -> bool:
    """Whether author made every commit on pull request number, so that
    Dependabot still rebases it by itself, and on request."""
    commits: list[dict[str, Any]] = github.request(
        "GET", f"{github.API_URL}/repos/{repo}/pulls/{number}/commits?per_page=100", token
    )
    # A commit by an email that no account has has a null author.
    return len(commits) < 100 and all((c["author"] or {}).get("login") == author for c in commits)


def ask_rebase(repo: str, token: str, number: int) -> None:
    """Ask Dependabot to rebase pull request number."""
    github.request(
        "POST",
        f"{github.API_URL}/repos/{repo}/issues/{number}/comments",
        token,
        json_body={"body": "@dependabot rebase"},
    )
