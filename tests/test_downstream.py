import email.message
import io
import json
import re
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, NoReturn

import pytest

from repoutils import downstream_actions, downstream_pins, github, pypi_provenance
from repoutils.commands import downstream

API = f"{github.API_URL}/repos/d/down"
BRANCH = "downstream/pkg-0.4.0"
MARKER = "<!-- up-downstream-pkg -->"
UPSTREAM = '[project]\nname = "pkg"\n\n[tool.repoutils]\ncomment-prefix = "up"\n'
DOWNSTREAM = """\
[build-system]
requires = ["hatchling", "pkg==0.3.0"]

[project]
name = "down"

[dependency-groups]
pkg = ["pkg>=0.3.0"]
"""


ACTIONS_LOCK = Path(".github/workflows/actions.lock")
ACTIONS_LOCK_TEXT = (
    "workflows:\n    '.github/workflows/ci.yml':\n        - 'actions/checkout@v7.0.1'\n"
    "        - 'u/up/.github/actions/a@v0.3.0'\n        - 'u/up/.github/actions/b@v0.3.0'\n"
)
WORKFLOW = """\
jobs:
  bump:
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: u/up/.github/actions/a@v0.3.0
      - uses: u/up/.github/actions/a@v0.3.0
      - uses: u/up/.github/actions/b@v0.3.0
"""


def with_actions(root: Path, workflow: str = WORKFLOW) -> Path:
    """Commit a workflow that uses the upstream's actions, and an actions.lock, to the downstream root."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/ci.yml").write_text(workflow)
    (root / ACTIONS_LOCK).write_text(ACTIONS_LOCK_TEXT)
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", "actions")
    return root


def lock_text(version: str) -> str:
    return f'version = 1\n\n[[package]]\nname = "pkg"\nversion = "{version}"\n'


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("o/r", ["o/r"]), (" a-b/c.d\n\to2/R_e  ", ["a-b/c.d", "o2/R_e"])],
)
def test_list(text: str, expected: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert downstream.run(["list", "--repositories", text]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_list_reads_the_variable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DOWNSTREAM_REPOS", "o/r")
    assert downstream.run(["list"]) == 0
    assert capsys.readouterr().out == '["o/r"]\n'


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("  ", "names no repositories"),
        ("o", "has 'o', which isn't of the form owner/name"),
        ("o/r/x", "has 'o/r/x'"),
        ("-o/r", "has '-o/r'"),
        ("o_x/r", "has 'o_x/r'"),
        ("o--x/r", "has 'o--x/r'"),
        ("o/..", "has 'o/..'"),
        ("o/r O/R", "names O/R more than once"),
    ],
)
def test_list_refuses(text: str, error: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert downstream.run(["list", f"--repositories={text}"]) == 1
    assert error in capsys.readouterr().err


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The upstream's checkout, the current directory, with the downstream's
    checkout in down/, committed on main, with a bare repository as its origin."""
    root = tmp_path / "up"
    root.mkdir()
    (root / "pyproject.toml").write_text(UPSTREAM)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "u/up")
    # Git variables, such as a hook's, that would point Git at another repository.
    for name in (
        "GITHUB_RUN_ID",
        "GITHUB_SERVER_URL",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "GIT_AUTHOR_NAME": "a",
        "GIT_COMMITTER_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_EMAIL": "a@b",
    }.items():
        monkeypatch.setenv(name, value)
    down = root / "down"
    down.mkdir()
    (down / "pyproject.toml").write_text(DOWNSTREAM)
    (down / "uv.lock").write_text(lock_text("0.3.0"))
    origin = tmp_path / "down-origin.git"
    for args in (
        ["init", "--quiet", "--bare", str(origin)],
        ["init", "--quiet", "--initial-branch=main"],
        ["remote", "add", "origin", str(origin)],
        ["add", "--all"],
        ["commit", "--quiet", "--message", "start"],
    ):
        subprocess.run(["git", *args], cwd=down, check=True)
    return root


def pull(
    number: int, branch: str, *, body: str | None = MARKER, source: str | None = "d/down", sha: str = "a" * 40
) -> dict[str, Any]:
    """An open pull request as GitHub's API lists it."""
    return {
        "number": number,
        "html_url": f"https://github.com/d/down/pull/{number}",
        "body": body,
        "head": {"ref": branch, "sha": sha, "repo": {"full_name": source} if source else None},
        "base": {"ref": "main"},
        "user": {"login": "bot"},
    }


def commit(
    message: str,
    *,
    parents: int = 1,
    committer: str | None = "bot",
    git_committer: str = "bot",
    committed: str = "2026-11-21T00:00:00Z",
) -> dict[str, Any]:
    """A commit as GitHub's API lists a pull request's, made by bot unless
    git_committer says otherwise, and committer, its GitHub account."""
    return {
        "commit": {
            "message": message,
            "author": {"name": "bot", "email": "bot@example.com", "date": "2026-11-21T00:00:00Z"},
            "committer": {"name": git_committer, "email": f"{git_committer}@example.com", "date": committed},
        },
        "parents": [{"sha": "p"}] * parents,
        "committer": {"login": committer} if committer else None,
    }


BUMP_COMMIT = commit(f"Bump pkg to 0.3.5\n\nBumps pkg from 0.3.0 to 0.3.5.\n\n{downstream.TRAILER}")
UPDATE_MERGE = commit(
    "Merge branch 'main' into downstream/pkg-0.3.5", parents=2, committer="web-flow", git_committer="GitHub"
)
# The Update branch button's rebase makes GitHub the committer.
REBASED = commit(BUMP_COMMIT["commit"]["message"], committer="web-flow", git_committer="GitHub")
OLD = "downstream/pkg-0.3.5"


def _page(items: list[Any], url: str) -> list[Any]:
    page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
    return items[(page - 1) * 100 : page * 100]


class Fakes:
    """Stands in for PyPI's provenance, uv lock, gh actions-lock and GitHub's pull requests."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pulls: list[dict[str, Any]] | None = None,
        commits: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.checked: list[tuple[str, str, str]] = []
        self.locked: list[tuple[Path, str, str]] = []
        # The roots gh actions-lock ran in, and the roots it verified.
        self.actions_locked: list[Path] = []
        self.verified: list[Path] = []
        self.pulls = pulls or []
        self.commits = commits or {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        # Bodies that someone edited after the pull requests were listed.
        self.bodies: dict[int, str] = {}
        # The requests that change something, in order.
        self.changes: list[tuple[str, str, Any]] = []
        monkeypatch.setattr(pypi_provenance, "check", self.check)
        monkeypatch.setattr(downstream_pins, "lock", self.lock)
        monkeypatch.setattr(downstream_actions, "lock", self.actions_lock)
        monkeypatch.setattr(downstream_actions, "verify", self.verify)
        monkeypatch.setattr(github, "request", self.request)

    @property
    def created(self) -> list[Any]:
        return [body for method, url, body in self.changes if (method, url) == ("POST", "/pulls")]

    def check(self, package: str, version: str, repository: str) -> list[str]:
        self.checked.append((package, version, repository))
        return ["pkg-0.4.0.tar.gz"]

    def lock(self, root: Path, package: str, version: str) -> bool:
        self.locked.append((root, package, version))
        (root / "uv.lock").write_text(lock_text(version))
        return True

    def actions_lock(self, root: Path) -> bool:
        self.actions_locked.append(root)
        (root / ACTIONS_LOCK).write_text(ACTIONS_LOCK_TEXT.replace("v0.3.0", "v0.4.0"))
        return True

    def verify(self, root: Path) -> None:
        self.verified.append(root)

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        assert token == "tok"
        path = url.removeprefix(API).split("?")[0]
        if method == "GET":
            if url == f"{API}/pulls?state=open&head=d%3Adownstream%2Fpkg-0.4.0":
                return [p for p in self.pulls if p["head"]["ref"] == BRANCH]
            if path == "/pulls":
                return _page(self.pulls, url)
            if match := re.fullmatch(r"/pulls/(\d+)", path):
                listed = next(p for p in self.pulls if p["number"] == int(match[1]))
                return {**listed, "body": self.bodies.get(listed["number"], listed["body"])}
            if match := re.fullmatch(r"/pulls/(\d+)/commits", path):
                return _page(self.commits[int(match[1])], url)
            match = re.fullmatch(r"/issues/(\d+)/comments", path)
            assert match, url
            return _page(self.comments.get(int(match[1]), []), url)
        self.changes.append((method, path, json_body))
        if (method, path) == ("POST", "/pulls"):
            return {"html_url": "https://github.com/d/down/pull/5"}
        return None


def test_bump(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert capsys.readouterr().out == (
        "PyPI's provenance names u/up as the publisher of pkg-0.4.0.tar.gz\n"
        "Bumped pkg from 0.3.0 to 0.4.0 in pyproject.toml, uv.lock\n"
    )
    assert fakes.checked == [("pkg", "0.4.0", "u/up")]
    assert fakes.locked == [(Path("down"), "pkg", "0.4.0")]
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM.replace("0.3.0", "0.4.0")


def test_bump_without_a_lock(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (upstream / "down/uv.lock").unlink()
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        fakes.locked.append((root, package, version))
        return False

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert fakes.locked == [(Path("down"), "pkg", "0.4.0")]
    assert not (upstream / "down/uv.lock").exists()


def test_bump_nothing_to_do(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "down/pyproject.toml").write_text(DOWNSTREAM.replace("0.3.0", "0.4"))
    fakes = Fakes(monkeypatch)
    # An old lock file is ignored, as there's nothing to bump.
    (upstream / "old.lock").write_text(lock_text("0.3.0"))
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down", "--lock-file", "old.lock"]
    assert downstream.run(args) == 0
    assert "already pins pkg 0.4.0, so there's nothing to bump" in capsys.readouterr().out
    assert fakes.checked == []


def test_bump_pr(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fakes = Fakes(monkeypatch)
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    (upstream / "new.lock").write_text(lock_text("0.4.0"))
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down", "--lock-file", "new.lock"]
    assert downstream.run(args) == 0
    assert capsys.readouterr().out.endswith(
        "Bumped pkg from 0.3.0 to 0.4.0 in pyproject.toml, uv.lock\nOpened https://github.com/d/down/pull/5\n"
    )
    # It copies the lock file rather than re-lock.
    assert fakes.locked == []
    down = upstream / "down"
    assert fakes.created == [
        {
            "title": "Bump pkg to 0.4.0",
            "head": BRANCH,
            "base": "main",
            "body": "<!-- up-downstream-pkg -->\n"
            "Bumps [pkg](https://pypi.org/project/pkg/0.4.0/) from 0.3.0 to 0.4.0, "
            "which [u/up](https://github.com/u/up) released.\n\n"
            "- `build-system.requires`: `pkg==0.3.0` to `pkg==0.4.0`\n"
            "- `dependency-groups.pkg`: `pkg>=0.3.0` to `pkg>=0.4.0`\n\n"
            "PyPI's provenance names u/up as the trusted publisher of each of the release's files.\n\n"
            "Opened by `repoutils downstream bump --pr` in https://github.com/u/up/actions/runs/77.",
        }
    ]
    assert git(down, "log", "-1", "--format=%B", f"origin/{BRANCH}") == (
        "Bump pkg to 0.4.0\n\nBumps pkg from 0.3.0 to 0.4.0.\n\n" + downstream.TRAILER
    )
    assert git(down, "log", "-1", "--format=%(trailers:key=Bumped-By,valueonly)", f"origin/{BRANCH}") == (
        "repoutils downstream bump"
    )
    assert git(down, "diff", "--name-only", "main", f"origin/{BRANCH}").splitlines() == ["pyproject.toml", "uv.lock"]
    assert git(down, "show", f"origin/{BRANCH}:uv.lock") + "\n" == lock_text("0.4.0")
    assert git(down, "branch", "--show-current") == "main"
    assert not git(down, "status", "--porcelain")


# The second has lost its marker, so only its branch shows it's the same bump.
@pytest.mark.parametrize("body", [MARKER, "edited"])
def test_bump_pr_already_open(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    Fakes(monkeypatch, [pull(4, BRANCH, body=body)])
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 0
    assert capsys.readouterr().out.endswith(
        "Pull request https://github.com/d/down/pull/4 already bumps pkg to 0.4.0\n"
    )
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM


def test_bump_pr_refuses_other_changes(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        (root / "build.log").write_text("x")
        return fakes.lock(root, package, version)

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 1
    assert "The bump changed build.log, pyproject.toml, uv.lock, not just pyproject.toml, uv.lock" in (
        capsys.readouterr().err
    )
    assert fakes.created == []
    # pyproject.toml and uv.lock are put back.
    assert git(upstream / "down", "status", "--porcelain") == "?? build.log"


def test_bump_refuses_a_pyproject_the_lock_changed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        with (root / "pyproject.toml").open("a") as f:
            f.write("# built\n")
        return fakes.lock(root, package, version)

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == "down/pyproject.toml changed while uv lock ran, so the bump stops here\n"
    assert not git(upstream / "down", "status", "--porcelain")


def test_bump_puts_files_back_if_locking_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> NoReturn:
        (root / "uv.lock").write_text("half")
        raise subprocess.CalledProcessError(2, ["uv", "lock"])

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == "uv lock failed\n"
    assert not git(upstream / "down", "status", "--porcelain")


@pytest.mark.parametrize(
    ("lock", "error"),
    [
        (lock_text("0.3.0"), "new.lock locks pkg at 0.3.0, not 0.4.0"),
        (None, "There's no down/uv.lock for new.lock to replace"),
    ],
)
def test_bump_refuses_lock_file(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], lock: str | None, error: str
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_text(lock_text("0.3.0"))
    if lock is None:
        (upstream / "down/uv.lock").unlink()
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert error in capsys.readouterr().err


def test_bump_refuses_a_lock_file_that_isnt_utf8(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_bytes(b"\xff")
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert capsys.readouterr().err == "new.lock isn't valid UTF-8\n"


def test_bump_refuses_a_lock_file_without_the_package(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    (upstream / "new.lock").write_text("version = 1\n")
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--lock-file", "new.lock"]) == 1
    assert "new.lock doesn't lock pkg, which the project depends on" in capsys.readouterr().err


def test_bump_pr_needs_repo(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr"])
    assert "bump --pr needs --repo" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "project = 1\n", "[project]\nname = ''\n"])
def test_bump_needs_a_package_name(upstream: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
    (upstream / "pyproject.toml").write_text(text)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert "[project] name is missing, so pass --package" in capsys.readouterr().err


def test_bump_takes_package_and_asks_gh(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "pyproject.toml").unlink()
    monkeypatch.delenv("GITHUB_REPOSITORY")
    monkeypatch.delenv("GITHUB_TOKEN")
    fakes = Fakes(monkeypatch, [pull(4, BRANCH)])
    asked: list[tuple[str, ...]] = []

    def gh_output(*args: str) -> str:
        asked.append(args)
        return "tok" if args[0] == "auth" else "g/h"

    monkeypatch.setattr(github, "gh_output", gh_output)
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--package", "pkg", "--pr", "--repo", "d/down"]
    assert downstream.run(args) == 0
    assert fakes.checked == [("pkg", "0.4.0", "g/h")]
    assert [command[0] for command in asked] == ["repo", "auth"]
    # The default comment-prefix, as there's no pyproject.toml here.
    assert "already bumps pkg" in capsys.readouterr().out


def test_bump_http_error(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    Fakes(monkeypatch)

    def request(method: str, url: str, token: str) -> NoReturn:
        raise urllib.error.HTTPError(url, 403, "no", email.message.Message(), io.BytesIO(b"denied"))

    monkeypatch.setattr(github, "request", request)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]) == 1
    assert capsys.readouterr().err == f"HTTP 403 from {API}/pulls?state=open&per_page=100&page=1:\ndenied\n"


@pytest.mark.parametrize(("version", "error"), [("v1", "isn't a release version"), ("0.2", "never lowers a pin")])
def test_bump_refuses_version(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str, error: str
) -> None:
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", version, "--directory", "down"]) == 1
    assert error in capsys.readouterr().err
    assert fakes.checked == []


def test_bump_provenance_failure_changes_nothing(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)

    def check(package: str, version: str, repository: str) -> NoReturn:
        raise RuntimeError("bad provenance")

    monkeypatch.setattr(pypi_provenance, "check", check)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == "bad provenance\n"
    assert fakes.locked == []
    assert not git(upstream / "down", "status", "--porcelain")


def push_old(upstream: Path) -> str:
    """Push an earlier bump's branch, OLD, to the downstream's origin, and return its commit."""
    down = upstream / "down"
    git(down, "push", "--quiet", "origin", f"main:refs/heads/{OLD}")
    return git(down, "rev-parse", "main")


def remote_branches(upstream: Path) -> list[str]:
    return git(upstream / "down", "ls-remote", "--heads", "origin").split()[1::2]


PR_ARGS = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]


def test_bump_pr_supersedes_an_untouched_pr(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = push_old(upstream)
    # An Update branch merge still counts as untouched.
    fakes = Fakes(monkeypatch, [pull(3, OLD, sha=sha)], {3: [BUMP_COMMIT, UPDATE_MERGE, BUMP_COMMIT]})
    assert downstream.run(PR_ARGS) == 0
    assert capsys.readouterr().out.endswith(
        "Opened https://github.com/d/down/pull/5\n"
        "Closed https://github.com/d/down/pull/3, which #5 supersedes, and deleted its branch\n"
    )
    assert [(method, path) for method, path, _ in fakes.changes] == [
        ("POST", "/pulls"),
        ("PATCH", "/pulls/3"),
        ("POST", "/issues/3/comments"),
    ]
    assert fakes.changes[1][2] == {"state": "closed"}
    assert fakes.changes[2][2] == {"body": "<!-- up-downstream-pkg-0.4.0 -->\nSuperseded by #5."}
    assert "stays open" not in fakes.created[0]["body"]
    assert remote_branches(upstream) == [f"refs/heads/{BRANCH}"]


@pytest.mark.parametrize(
    "commits",
    [
        [BUMP_COMMIT, commit("Fix the tests")],
        # Amended, or rebased, by a person, who becomes its committer, or rebased by Update branch.
        [commit(BUMP_COMMIT["commit"]["message"], git_committer="simon")],
        # Amended by the identity that made it.
        [commit(BUMP_COMMIT["commit"]["message"], committed="2026-11-21T01:00:00Z")],
        [REBASED],
        # The trailer must be in the message's last paragraph.
        [commit(f"Bump\n\n{downstream.TRAILER}\n\nmore")],
        # A merge made by hand, or of another branch.
        [BUMP_COMMIT, commit(UPDATE_MERGE["commit"]["message"], parents=2)],
        [BUMP_COMMIT, commit("Merge branch 'other' into downstream/pkg-0.3.5", parents=2, committer="web-flow")],
        [BUMP_COMMIT, commit(UPDATE_MERGE["commit"]["message"], parents=2, committer=None)],
        # GitHub lists no more than 250 commits.
        [BUMP_COMMIT] * 250,
        [],
    ],
)
def test_bump_pr_warns_on_a_pr_with_other_commits(
    upstream: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    commits: list[dict[str, object]],
) -> None:
    push_old(upstream)
    fakes = Fakes(monkeypatch, [pull(3, OLD)], {3: commits})
    assert downstream.run(PR_ARGS) == 0
    assert capsys.readouterr().out.endswith(
        "Left https://github.com/d/down/pull/3 open, as it has other commits, and commented that #5 bumps 0.4.0\n"
    )
    assert [(method, path) for method, path, _ in fakes.changes] == [("POST", "/pulls"), ("POST", "/issues/3/comments")]
    assert fakes.changes[1][2] == {
        "body": "<!-- up-downstream-pkg-0.4.0 -->\n#5 bumps pkg to 0.4.0. "
        "This pull request has commits that `repoutils downstream bump` didn't make, so it stays open, "
        "but its changes probably need moving across to #5."
    }
    assert (
        "\n\n#3 bumps pkg to 0.3.5, and stays open, as it has commits that `repoutils downstream bump` didn't make. "
        "Its changes probably need moving across.\n\nOpened by "
    ) in fakes.created[0]["body"]
    assert f"refs/heads/{OLD}" in remote_branches(upstream)


def test_bump_pr_pages_through_commits(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = push_old(upstream)
    fakes = Fakes(monkeypatch, [pull(3, OLD, sha=sha)], {3: [BUMP_COMMIT] * 100 + [commit("Fix")]})
    assert downstream.run(PR_ARGS) == 0
    assert [path for _, path, _ in fakes.changes] == ["/pulls", "/issues/3/comments"]


def test_bump_pr_ignores_other_prs(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = push_old(upstream)
    others = [
        pull(10, OLD, body="no marker"),
        pull(11, OLD, body=None),
        pull(12, OLD, source="fork/down"),
        pull(13, OLD, source=None),
        pull(14, "downstream/pkg-extra-0.3"),
        pull(15, "other/pkg-0.3.5"),
        # The one it closes.
        pull(3, OLD, sha=sha),
    ]
    # They're on the second page.
    fakes = Fakes(monkeypatch, [pull(n, f"x{n}", body="") for n in range(100)] + others, {3: [BUMP_COMMIT]})
    assert downstream.run(PR_ARGS) == 0
    assert [path for _, path, _ in fakes.changes] == ["/pulls", "/pulls/3", "/issues/3/comments"]


@pytest.mark.parametrize("version", ["0.4.1", "0.5"])
def test_bump_pr_leaves_a_newer_pr(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    fakes = Fakes(monkeypatch, [pull(3, OLD), pull(6, f"downstream/pkg-{version}")], {3: [BUMP_COMMIT]})
    assert downstream.run(PR_ARGS) == 0
    assert capsys.readouterr().out.endswith(
        f"Pull request https://github.com/d/down/pull/6 already bumps pkg to {version}, so there's nothing to do\n"
    )
    assert fakes.changes == []
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM


LINK_4 = (
    "#4 bumps pkg to 0.3.6, and stays open, as it has commits that `repoutils downstream bump` didn't make. "
    "Its changes probably need moving across."
)


# Whether the run that opened #5 had already seen #4's other commits, and linked it.
@pytest.mark.parametrize("linked", [False, True])
def test_bump_pr_rerun_finishes_earlier_prs(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], linked: bool
) -> None:
    sha = push_old(upstream)
    # The run that opened #5 commented on #3 and #4, then failed.
    fakes = Fakes(
        monkeypatch,
        [
            pull(3, OLD, sha=sha),
            pull(4, "downstream/pkg-0.3.6"),
            pull(5, "downstream/pkg-0.4"),
        ],
        {3: [BUMP_COMMIT], 4: [commit("Fix")]},
    )
    # Edited since it was listed, and linking keeps the edit.
    fakes.bodies[5] = f"{MARKER}\n\n{LINK_4}\n" if linked else f"{MARKER}\n\nEdited."
    done = {"user": {"login": "bot"}, "body": "<!-- up-downstream-pkg-0.4.0 -->\nSuperseded by #5."}
    # A deleted account's comment has a null user.
    fakes.comments = {
        3: [{"user": None, "body": "hi"}, {"user": {"login": "someone"}, "body": done["body"]}],
        4: [done],
    }
    assert downstream.run(PR_ARGS) == 0
    out = capsys.readouterr().out
    assert "Pull request https://github.com/d/down/pull/5 already bumps pkg to 0.4.0\n" in out
    assert "Closed https://github.com/d/down/pull/3" in out
    assert "Left https://github.com/d/down/pull/4 open" in out
    # The comments are there already, but #5's body may not link #4 yet.
    closing = [("PATCH", "/pulls/3", {"state": "closed"})]
    linking = [] if linked else [("PATCH", "/pulls/5", {"body": f"{MARKER}\n\nEdited.\n\n{LINK_4}"})]
    assert fakes.changes == linking + closing
    assert remote_branches(upstream) == []
    assert (upstream / "down/pyproject.toml").read_text() == DOWNSTREAM


def test_bump_pr_keeps_a_branch_that_moved_on(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    push_old(upstream)
    # Listed at another commit than the branch is at now, and a second one to show the next is still handled.
    fakes = Fakes(monkeypatch, [pull(3, OLD), pull(4, "downstream/pkg-0.3.6")], {3: [BUMP_COMMIT], 4: [commit("Fix")]})
    assert downstream.run(PR_ARGS) == 1
    captured = capsys.readouterr()
    assert (
        f"Handling https://github.com/d/down/pull/3 failed: Deleting the branch {OLD} failed, so it's still open:\n"
    ) in captured.err
    assert "Left https://github.com/d/down/pull/4 open" in captured.out
    # Left open, for a rerun to judge again.
    assert [path for _, path, _ in fakes.changes] == ["/pulls", "/issues/4/comments"]
    assert f"refs/heads/{OLD}" in remote_branches(upstream)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            urllib.error.HTTPError(f"{API}/issues/3/comments", 403, "no", email.message.Message(), io.BytesIO(b"no")),
            f"HTTP 403 from {API}/issues/3/comments:\nno",
        ),
        (json.JSONDecodeError("Expecting value", "<", 0), "Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_bump_pr_reports_a_failed_request_on_an_earlier_pr(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, message: str
) -> None:
    # The second is still handled.
    fakes = Fakes(
        monkeypatch, [pull(3, OLD), pull(4, "downstream/pkg-0.3.6")], {3: [commit("Fix")], 4: [commit("Fix")]}
    )
    request = fakes.request

    def failing(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if url.endswith("/issues/3/comments"):
            raise error
        return request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", failing)
    assert downstream.run(PR_ARGS) == 1
    assert capsys.readouterr().err == f"Handling https://github.com/d/down/pull/3 failed: {message}\n"
    assert [path for _, path, _ in fakes.changes] == ["/pulls", "/issues/4/comments"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            urllib.error.HTTPError(f"{API}/pulls/5", 403, "no", email.message.Message(), io.BytesIO(b"no")),
            "HTTP 403:\nno",
        ),
        (json.JSONDecodeError("Expecting value", "<", 0), "Expecting value: line 1 column 1 (char 0)"),
        (RuntimeError("too many redirects"), "too many redirects"),
    ],
)
def test_bump_pr_rerun_still_handles_earlier_prs_if_linking_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, message: str
) -> None:
    sha = push_old(upstream)
    fakes = Fakes(
        monkeypatch,
        [pull(3, OLD, sha=sha), pull(4, "downstream/pkg-0.3.6"), pull(5, BRANCH)],
        {3: [BUMP_COMMIT], 4: [commit("Fix")]},
    )
    request = fakes.request

    def failing(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if (method, url) == ("PATCH", f"{API}/pulls/5"):
            raise error
        return request(method, url, token, json_body=json_body)

    monkeypatch.setattr(github, "request", failing)
    assert downstream.run(PR_ARGS) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        f"Linking the pull requests it leaves open from https://github.com/d/down/pull/5 failed: {message}\n"
    )
    assert "Closed https://github.com/d/down/pull/3" in captured.out
    assert "Left https://github.com/d/down/pull/4 open" in captured.out


MOVED = WORKFLOW.replace("@v0.3.0", "@v0.4.0")


def test_bump_moves_the_upstreams_actions(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = with_actions(upstream / "down")
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert capsys.readouterr().out.endswith(
        "Bumped pkg from 0.3.0 to 0.4.0 in pyproject.toml, uv.lock, .github/workflows/ci.yml, "
        ".github/workflows/actions.lock\n"
    )
    assert (down / ".github/workflows/ci.yml").read_text() == MOVED
    assert fakes.actions_locked == [Path("down")]
    assert fakes.verified == []


def test_bump_only_the_actions(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "down/pyproject.toml").write_text(DOWNSTREAM.replace("0.3.0", "0.4.0"))
    (upstream / "down/uv.lock").write_text(lock_text("0.4.0"))
    down = with_actions(upstream / "down", WORKFLOW.replace("a@v0.3.0", "a@v0.4.0"))
    fakes = Fakes(monkeypatch)
    (upstream / "new.lock").write_text(lock_text("0.4.0"))
    (upstream / "actions.lock").write_text("regenerated\n")
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]
    args += ["--lock-file", "new.lock", "--actions-lock-file", "actions.lock"]
    assert downstream.run(args) == 0
    assert capsys.readouterr().out.endswith(
        "Bumped pkg's actions from v0.3.0 to v0.4.0 in .github/workflows/ci.yml, .github/workflows/actions.lock\n"
        "Opened https://github.com/d/down/pull/5\n"
    )
    assert fakes.actions_locked == []
    assert fakes.verified == [Path("down")]
    assert fakes.created[0]["body"].startswith(
        "<!-- up-downstream-pkg -->\n"
        "Bumps the actions of [pkg](https://pypi.org/project/pkg/0.4.0/) from v0.3.0 to v0.4.0, "
        "which [u/up](https://github.com/u/up) released.\n\n"
        "- `.github/workflows/ci.yml`: `u/up/.github/actions/b@v0.3.0` to `@v0.4.0`\n\n"
    )
    assert git(down, "log", "-1", "--format=%B", f"origin/{BRANCH}").startswith(
        "Bump pkg to 0.4.0\n\nBumps pkg's actions from v0.3.0 to v0.4.0.\n\n"
    )
    assert git(down, "diff", "--name-only", "main", f"origin/{BRANCH}").splitlines() == [
        ".github/workflows/actions.lock",
        ".github/workflows/ci.yml",
    ]
    assert git(down, "show", f"origin/{BRANCH}:.github/workflows/actions.lock") == "regenerated"
    assert git(down, "show", f"origin/{BRANCH}:.github/workflows/ci.yml") + "\n" == MOVED


def test_bump_pr_takes_the_actions_lock(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    down = with_actions(upstream / "down")
    fakes = Fakes(monkeypatch)
    (upstream / "new.lock").write_text(lock_text("0.4.0"))
    (upstream / "actions.lock").write_text("regenerated\n")
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--pr", "--repo", "d/down"]
    args += ["--lock-file", "new.lock", "--actions-lock-file", "actions.lock"]
    assert downstream.run(args) == 0
    body = fakes.created[0]["body"]
    # Each action once, though two steps use a.
    assert (
        "- `dependency-groups.pkg`: `pkg>=0.3.0` to `pkg>=0.4.0`\n"
        "- `.github/workflows/ci.yml`: `u/up/.github/actions/a@v0.3.0` to `@v0.4.0`\n"
        "- `.github/workflows/ci.yml`: `u/up/.github/actions/b@v0.3.0` to `@v0.4.0`\n\n"
    ) in body
    assert git(down, "diff", "--name-only", "main", f"origin/{BRANCH}").splitlines() == [
        ".github/workflows/actions.lock",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "uv.lock",
    ]


def test_bump_nothing_to_do_with_actions(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (upstream / "down/pyproject.toml").write_text(DOWNSTREAM.replace("0.3.0", "0.4.0"))
    with_actions(upstream / "down", MOVED)
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 0
    assert capsys.readouterr().out == (
        "down/pyproject.toml already pins pkg 0.4.0, and uses u/up's actions at v0.4.0, so there's nothing to bump\n"
    )
    assert fakes.checked == []


def test_bump_refuses_an_actions_lock_file_without_an_actions_lock(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = with_actions(upstream / "down")
    (down / ACTIONS_LOCK).unlink()
    Fakes(monkeypatch)
    (upstream / "actions.lock").write_text("regenerated\n")
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--actions-lock-file", "actions.lock"]
    assert downstream.run(args) == 1
    assert capsys.readouterr().err == "There's no down/.github/workflows/actions.lock for actions.lock to replace\n"


def test_bump_refuses_a_workflow_the_lock_changed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = with_actions(upstream / "down")
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        with (root / ".github/workflows/ci.yml").open("a") as f:
            f.write("# built\n")
        return fakes.lock(root, package, version)

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == (
        "down/.github/workflows/ci.yml changed while uv lock ran, so the bump stops here\n"
    )
    assert not git(down, "status", "--porcelain")


def test_bump_refuses_an_actions_lock_the_lock_changed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The actions are already at the release's tag, so only the pins move.
    down = with_actions(upstream / "down", MOVED)
    fakes = Fakes(monkeypatch)

    def lock(root: Path, package: str, version: str) -> bool:
        with (root / ACTIONS_LOCK).open("a") as f:
            f.write("# built\n")
        return fakes.lock(root, package, version)

    monkeypatch.setattr(downstream_pins, "lock", lock)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert capsys.readouterr().err == (
        "down/.github/workflows/actions.lock changed while uv lock ran, so the bump stops here\n"
    )
    assert not git(down, "status", "--porcelain")


def test_bump_puts_files_back_if_verifying_fails(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = with_actions(upstream / "down")
    Fakes(monkeypatch)

    def verify(root: Path) -> NoReturn:
        raise subprocess.CalledProcessError(1, ["gh", "actions-lock", "--verify"], stderr="stale\n")

    monkeypatch.setattr(downstream_actions, "verify", verify)
    (upstream / "actions.lock").write_text("tampered\n")
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--actions-lock-file", "actions.lock"]
    assert downstream.run(args) == 1
    assert capsys.readouterr().err == "gh actions-lock --verify failed:\nstale\n\n"
    assert not git(down, "status", "--porcelain")


def test_bump_refuses_an_action_at_a_branch(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_actions(upstream / "down", WORKFLOW.replace("b@v0.3.0", "b@main"))
    fakes = Fakes(monkeypatch)
    assert downstream.run(["bump", "--version", "0.4.0", "--directory", "down"]) == 1
    assert "ci.yml:7 uses u/up/.github/actions/b@main, which bump can't move" in capsys.readouterr().err
    assert fakes.checked == []


def test_bump_refuses_an_actions_lock_file_that_moves_another_action(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = with_actions(upstream / "down")
    (down / ACTIONS_LOCK).write_text("dependencies:\n    'v/w@main':\n        commit: 'sha1-new'\n")
    git(down, "commit", "--quiet", "--all", "--message", "lock")
    fakes = Fakes(monkeypatch)
    (upstream / "actions.lock").write_text("dependencies:\n    'v/w@main':\n        commit: 'sha1-old'\n")
    args = ["bump", "--version", "0.4.0", "--directory", "down", "--actions-lock-file", "actions.lock"]
    assert downstream.run(args) == 1
    assert "actions.lock locks v/w@main at another commit than .github/workflows/actions.lock does" in (
        capsys.readouterr().err
    )
    assert fakes.verified == []
    assert not git(down, "status", "--porcelain")


@pytest.mark.parametrize("given", [True, False])
# The workflow that bump moves, and one it doesn't.
@pytest.mark.parametrize("edited", ["ci.yml", "other.yml"])
def test_bump_refuses_a_workflow_gh_actions_lock_changed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], given: bool, edited: str
) -> None:
    down = with_actions(upstream / "down")
    (down / ".github/workflows/other.yml").write_text("jobs: {}\n")
    git(down, "add", "--all")
    git(down, "commit", "--quiet", "--message", "other")
    fakes = Fakes(monkeypatch)

    def edit(root: Path) -> None:
        with (root / ".github/workflows" / edited).open("a") as f:
            f.write("# edited\n")

    def actions_lock(root: Path) -> bool:
        edit(root)
        return fakes.actions_lock(root)

    monkeypatch.setattr(downstream_actions, "lock", actions_lock)
    monkeypatch.setattr(downstream_actions, "verify", edit)
    (upstream / "actions.lock").write_text(ACTIONS_LOCK_TEXT)
    args = ["bump", "--version", "0.4.0", "--directory", "down"]
    args += ["--actions-lock-file", "actions.lock"] if given else []
    assert downstream.run(args) == 1
    command = "gh actions-lock --verify" if given else "gh actions-lock"
    assert (
        capsys.readouterr().err
        == f"down/.github/workflows/{edited} changed while {command} ran, so the bump stops here\n"
    )
    assert not git(down, "status", "--porcelain")
