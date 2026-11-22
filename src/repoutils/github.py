"""GitHub API helpers for the commands that post a PR comment and edit or hide
it on later runs, and the no-redirect opener wait-for-base-coverage uses.
Standard library only."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from email.message import Message
from pathlib import Path
from typing import IO, Any, override

API_URL = "https://api.github.com"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise every redirect as an HTTPError, for request() to follow itself.
    urllib's own handler follows GET redirects keeping every header, the
    token included, even to another host."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


# Public for other scripts that send a token (wait_for_base_coverage).
no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.netloc


def request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: object = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    """Send an API request and return its decoded JSON response (None if empty).
    A 301/302/307/308 redirect is followed with the same method and body,
    keeping the token only while the scheme, host and port are unchanged (so
    not on a downgrade to http)."""
    if json_body is not None:
        data = json.dumps(json_body).encode()
        content_type = "application/json"
    payload = request_bytes(method, url, token, data=data, content_type=content_type)
    return json.loads(payload) if payload else None


def request_bytes(
    method: str, url: str, token: str, *, data: bytes | None = None, content_type: str | None = None
) -> bytes:
    """Send an API request and return its raw response body, such as an
    artifact's zip, following redirects as request() does."""
    origin = _origin(url)
    for _ in range(5):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        if content_type:
            req.add_header("Content-Type", content_type)
        if _origin(url) == origin:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with no_redirect_opener.open(req, timeout=60) as response:
                payload: bytes = response.read()
            return payload
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if e.code not in (301, 302, 307, 308) or not location:
                raise
            e.close()
            url = urllib.parse.urljoin(url, location)
    raise RuntimeError(f"too many redirects for {method} {url}")


def gh_output(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """gh's output, run in cwd (default: the current directory), which picks the repository gh defaults to."""
    return subprocess.run(["gh", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def pr_head(repo: str, pr: int, token: str) -> str:
    """The PR's current head commit."""
    sha: str = request("GET", f"{API_URL}/repos/{repo}/pulls/{pr}", token)["head"]["sha"]
    return sha


def annotation(kind: str) -> str:
    """The prefix that makes GitHub Actions show a line as an annotation, such
    as ``::warning::``, or nothing outside Actions."""
    return f"::{kind}::" if os.environ.get("GITHUB_ACTIONS") == "true" else ""


def close_error(e: Exception) -> None:
    """Close an HTTPError's response, which an error that's handled and
    dropped would otherwise leave open."""
    if isinstance(e, urllib.error.HTTPError):
        e.close()


def react(repo: str, comment_id: int, token: str) -> None:
    """React to the comment with a rocket, to say the run it asked for has
    started. By then the run has started, so a failure only warns: a failed
    run would suggest commenting again."""
    try:
        request(
            "POST",
            f"{API_URL}/repos/{repo}/issues/comments/{comment_id}/reactions",
            token,
            json_body={"content": "rocket"},
        )
    # RuntimeError is request()'s error for too many redirects.
    except (OSError, RuntimeError) as e:
        close_error(e)
        print(f"{annotation('warning')}Couldn't react to comment {comment_id}: {e}", file=sys.stderr)


def guarded(error: str, action: Callable[[], int | None]) -> int:
    """Run action, returning its exit code (0 for None). If it raises an
    error from gh, the API or the network, or a ValueError or RuntimeError,
    print the error after the prefix error and return 1 instead."""
    try:
        return action() or 0
    except subprocess.CalledProcessError as e:
        print(f"{error}{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"{error}HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"{error}{e}", file=sys.stderr)
    return 1


def marked_comments(repo: str, pr: int, author: str | None, marker: str, token: str) -> list[dict[str, Any]]:
    """The comments author (or, if None, anyone) has posted on pr whose body starts with marker, oldest first."""
    comments = []
    page = 1
    while True:
        batch = request(
            "GET",
            f"{API_URL}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
            token,
        )
        # A deleted account's comment has a null user.
        comments += [
            c
            for c in batch
            if c["body"].startswith(marker) and (author is None or (c["user"] or {}).get("login") == author)
        ]
        if len(batch) < 100:
            return comments
        page += 1


def upsert_comment(repo: str, pr: int, author: str, marker: str, body: str, token: str, *, create: bool = True) -> bool:
    """Edit author's newest comment on pr starting with marker to have body,
    or post one if there's none and create is true. body should start with
    marker, so the next run finds it. Returns whether it edited or posted one."""
    previous = marked_comments(repo, pr, author, marker, token)
    comments_url = f"{API_URL}/repos/{repo}/issues/comments"
    if previous:
        # Edit in place, so the comment keeps its place in the conversation.
        # The newest, as a run with --hide-previous may have posted it.
        request("PATCH", f"{comments_url}/{previous[-1]['id']}", token, json_body={"body": body})
        # Keep only one, should a failed or raced run have left another.
        for comment in previous[:-1]:
            request("DELETE", f"{comments_url}/{comment['id']}", token)
    elif create:
        request("POST", f"{API_URL}/repos/{repo}/issues/{pr}/comments", token, json_body={"body": body})
    else:
        return False
    return True


def graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    """Run a GraphQL query or mutation and return its data, raising
    RuntimeError if GitHub reports errors (which it does with HTTP 200)."""
    result = request("POST", f"{API_URL}/graphql", token, json_body={"query": query, "variables": variables})
    if result.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(result['errors'])}")
    data: dict[str, Any] = result["data"]
    return data


_MY_COMMENTS = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      comments(first: 100, after: $cursor) {
        nodes { id databaseId isMinimized viewerDidAuthor body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_MINIMIZE = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId }
}"""


def own_comments(repo: str, pr: int, marker: str, token: str) -> Iterator[dict[str, Any]]:
    """The comments on pr that token's user posted whose body starts with
    marker, oldest first, as GraphQL nodes with id, databaseId and
    isMinimized. Matching the author means nobody else's comment is picked up
    by copying the marker."""
    owner, name = repo.split("/", 1)
    cursor = None
    while True:
        variables = {"owner": owner, "name": name, "pr": pr, "cursor": cursor}
        comments = graphql(_MY_COMMENTS, variables, token)["repository"]["pullRequest"]["comments"]
        for c in comments["nodes"]:
            if c["viewerDidAuthor"] and c["body"].startswith(marker):
                yield c
        if not comments["pageInfo"]["hasNextPage"]:
            return
        cursor = comments["pageInfo"]["endCursor"]


def hide_comments(repo: str, pr: int, marker: str, token: str, *, keep: str | None = None) -> int:
    """Hide as outdated each of own_comments that isn't hidden yet, up to the
    one whose node ID is keep, if given. Comments after keep are left alone,
    as another run may have posted them since, with a newer report. Returns
    how many it hid."""
    hidden = 0
    for c in own_comments(repo, pr, marker, token):
        if c["id"] == keep:
            break
        if not c["isMinimized"]:
            graphql(_MINIMIZE, {"id": c["id"]}, token)
            hidden += 1
    return hidden
