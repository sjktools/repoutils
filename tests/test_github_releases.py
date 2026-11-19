import email.message
import io
import subprocess
import urllib.error
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, NoReturn, override

import pytest

from repoutils import config, github, pull_request, release_pins
from repoutils.commands import github_releases

API = github.API_URL
RELEASES = f"{API}/repos/up/stream/releases"
PULLS = f"{API}/repos/o/r/pulls"
NOW = datetime(2026, 11, 19, tzinfo=UTC)
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
PYPROJECT = """\
[tool.repoutils.github-releases.tool]
repo = "up/stream"
file = "pins/tool.toml"
assets = ["tool-*.vsix"]
"""


def _release(
    tag: str,
    published: str | None,
    *,
    digest: str | None = A,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "published_at": published,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/up/stream/releases/tag/{tag}",
        "assets": [
            {"name": f"tool-{tag}.vsix", "digest": digest},
            {"name": "notes.txt", "digest": B},
        ],
    }


class _FixedNow(datetime):
    """datetime, with now() fixed at NOW, for the tests that go through run()."""

    @override
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "_FixedNow":
        return cls(NOW.year, NOW.month, NOW.day, tzinfo=UTC)


class FakeGitHub:
    """Answers the releases listing a page at a time, and the pull request listing and creation."""

    def __init__(self, releases: list[dict[str, Any]], open_pulls: list[dict[str, Any]] | None = None) -> None:
        self.releases = releases
        self.open_pulls = open_pulls or []
        self.created: list[Any] = []
        self.urls: list[str] = []

    def request(self, method: str, url: str, token: str, *, json_body: object = None) -> Any:
        assert token == "tok"
        self.urls.append(url)
        if url.startswith(f"{PULLS}?state=open&head="):
            return self.open_pulls
        if (method, url) == ("POST", PULLS):
            self.created.append(json_body)
            return {"html_url": "https://github.com/o/r/pull/5"}
        base, _, query = url.partition("?per_page=")
        assert (method, base) == ("GET", RELEASES)
        size, page = (int(part) for part in query.split("&page="))
        return self.releases[(page - 1) * size : page * size]


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> FakeGitHub:
    monkeypatch.setattr(github, "request", fake.request)
    return fake


def _pin(root: Path, tag: str) -> None:
    pins = release_pins.Pins("up/stream")
    release_pins.add(pins, tag, {f"tool-{tag}.vsix": A})
    (root / "pins").mkdir(exist_ok=True)
    (root / "pins/tool.toml").write_text(release_pins.render(pins))


# 2.0 is the newest release, but only 1.9 is past the 7-day cooldown.
RELEASES_LIST = [
    _release("2.1-rc", "2026-11-17T00:00:00Z", prerelease=True),
    _release("2.2", None, draft=True),
    _release("2.0", "2026-11-15T00:00:00Z"),
    _release("1.9", "2026-11-10T00:00:00Z"),
    _release("1.8", "2026-10-01T00:00:00Z"),
]


def test_upstreams(cwd: Path) -> None:
    (cwd / "pyproject.toml").write_text(PYPROJECT + "cooldown-days = 0\n")
    assert github_releases.upstreams() == [
        github_releases.Upstream("tool", "up/stream", Path("pins/tool.toml"), ["tool-*.vsix"], 0)
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[tool.repoutils.github-releases]\nx = 1\n", r"\[tool.repoutils.github-releases.x\] must be a table"),
        ('[tool.repoutils.github-releases."a b"]\n', "the name must be letters"),
        ("[tool.repoutils.github-releases.x]\nrepos = 1\n", "unknown setting\\(s\\): repos"),
        ('[tool.repoutils.github-releases.x]\nrepo = "up"\n', "repo must be the repository's owner/name"),
        ('[tool.repoutils.github-releases.x]\nrepo = "o/r"\nfile = ""\n', "file must be the path"),
        ('[tool.repoutils.github-releases.x]\nrepo = "o/r"\nfile = "f"\nassets = []\n', "at least one glob"),
        (
            '[tool.repoutils.github-releases.x]\nrepo = "o/r"\nfile = "f"\nassets = ["*"]\ncooldown-days = -1\n',
            "cooldown-days must be a whole number",
        ),
        (
            '[tool.repoutils.github-releases.x]\nrepo = "o/r"\nfile = "../f"\nassets = ["*"]\n',
            "file must be inside the project, not ../f",
        ),
        (
            PYPROJECT + PYPROJECT.replace(".tool]", ".other]"),
            r"Two \[tool.repoutils.github-releases\] tables have the same file",
        ),
    ],
)
def test_upstreams_refuses(cwd: Path, text: str, error: str) -> None:
    (cwd / "pyproject.toml").write_text(text)
    with pytest.raises(config.ConfigError, match=error):
        github_releases.upstreams()


def test_check_up_to_date(cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _pin(cwd, "1.9")
    assert github_releases.main("check", [], now=NOW) == 0
    assert capsys.readouterr().out == "tool: pins/tool.toml has 1.9 in use, which is up to date\n"


def test_check_keeps_a_newer_release_pinned_by_hand(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _pin(cwd, "2.1-rc")
    assert github_releases.main("check", ["tool"], now=NOW) == 0
    assert "has 2.1-rc in use, which is up to date" in capsys.readouterr().out


@pytest.mark.parametrize(("exit_zero", "status"), [(False, 1), (True, 0)])
def test_check_newer(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exit_zero: bool, status: int
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _pin(cwd, "1.8")
    assert github_releases.main("check", [], exit_zero=exit_zero, now=NOW) == status
    assert capsys.readouterr().err == (
        "tool: pins/tool.toml has 1.8 in use, but there's a newer release: "
        "up/stream 1.9, released on 2026-11-10: https://github.com/up/stream/releases/tag/1.9\n"
    )


def test_check_with_nothing_pinned(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    assert github_releases.main("check", [], now=NOW) == 1
    assert capsys.readouterr().err.startswith("tool: pins/tool.toml pins no release yet, but there's a newer release")


def test_check_with_no_release_past_its_cooldown(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST[:3]))
    assert github_releases.main("check", [], now=NOW) == 0
    assert capsys.readouterr().out == (
        "tool: pins/tool.toml pins no release yet, and up/stream has no release past its 7-day cooldown\n"
    )


def test_check_reads_every_page(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The listing is in order of creation, so a release published late can be on a later page.
    many = [_release(f"0.{n}", "2026-01-01T00:00:00Z") for n in range(github_releases.PER_PAGE)]
    fake = _install(monkeypatch, FakeGitHub([*many, _release("1.9", "2026-11-10T00:00:00Z")]))
    _pin(cwd, "0.1")
    assert github_releases.main("check", [], now=NOW) == 1
    assert f"{RELEASES}?per_page=100&page=2" in fake.urls


def test_check_exit_zero_still_fails_on_an_error(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _pin(cwd, "0.5")
    assert github_releases.run(["check", "--exit-zero"]) == 1
    assert capsys.readouterr().err == (
        "pins/tool.toml has release 0.5 in use, but up/stream has no published release with that tag\n"
    )


def test_unknown_name(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert github_releases.run(["check", "tool", "nope"]) == 1
    assert capsys.readouterr().err == "pyproject.toml has no [tool.repoutils.github-releases] table for: nope\n"


def test_no_upstreams(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cwd / "pyproject.toml").write_text("")
    assert github_releases.run(["bump"]) == 1
    assert capsys.readouterr().err == "pyproject.toml has no [tool.repoutils.github-releases.<name>] tables\n"


def test_http_error(cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(method: str, url: str, token: str) -> NoReturn:
        raise urllib.error.HTTPError(url, 404, "error", email.message.Message(), io.BytesIO(b"Not Found"))

    monkeypatch.setattr(github, "request", fail)
    assert github_releases.run(["check"]) == 1
    assert capsys.readouterr().err.startswith(f"HTTP 404 from {RELEASES}?per_page=100&page=1:\nNot Found")


def test_bump(cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _pin(cwd, "1.8")
    assert github_releases.main("bump", [], now=NOW) == 0
    assert capsys.readouterr().out == "tool: bumped pins/tool.toml from 1.8 to 1.9\n"
    pins = release_pins.read(cwd / "pins/tool.toml", "up/stream")
    assert pins.current == "1.9"
    assert pins.releases == {"1.9": {"tool-1.9.vsix": A}, "1.8": {"tool-1.8.vsix": A}}
    # A second bump finds nothing newer.
    assert github_releases.main("bump", [], now=NOW) == 0
    assert capsys.readouterr().out == "tool: pins/tool.toml is up to date, so there's nothing to bump\n"


def test_bump_creates_the_pins_file(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    assert github_releases.run(["bump", "tool"]) == 0
    assert capsys.readouterr().out == "tool: bumped pins/tool.toml to 1.9\n"
    assert release_pins.read(cwd / "pins/tool.toml", "up/stream").current == "1.9"


@pytest.mark.parametrize(
    ("release", "error"),
    [
        (_release("1.9", "2026-11-10T00:00:00Z", digest=None), "GitHub gives no sha256 digest for asset tool-1.9.vsix"),
        ({**_release("1.9", "2026-11-10T00:00:00Z"), "assets": []}, "has no asset matching tool-*.vsix"),
    ],
)
def test_bump_refuses(
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    release: dict[str, object],
    error: str,
) -> None:
    _install(monkeypatch, FakeGitHub([release]))
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    assert github_releases.run(["bump"]) == 1
    assert error in capsys.readouterr().err
    assert not (cwd / "pins").exists()


def _git(root: Path, *args: str) -> str:
    return pull_request.git(root, *args)


@pytest.fixture
def project(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """cwd, pinning 1.8, committed on main, with a bare repository as its origin."""
    _pin(cwd, "1.8")
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "a")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "a@b")
    origin = cwd.parent / f"{cwd.name}-origin.git"
    for args in (
        ["init", "--quiet", "--bare", str(origin)],
        ["init", "--quiet", "--initial-branch=main"],
        ["remote", "add", "origin", str(origin)],
        ["add", "--all"],
        ["commit", "--quiet", "--message", "start"],
    ):
        subprocess.run(["git", *args], cwd=cwd, check=True)
    return cwd


def test_bump_pr(project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    assert github_releases.main("bump", [], repo="o/r", pr=True, now=NOW) == 0
    assert capsys.readouterr().out == (
        "tool: bumped pins/tool.toml from 1.8 to 1.9\ntool: opened https://github.com/o/r/pull/5\n"
    )
    source = "Bumps tool from 1.8 to 1.9: up/stream 1.9, released on 2026-11-10: https://github.com/up/stream/releases/tag/1.9"
    assert fake.created == [
        {
            "title": "Bump tool to 1.9",
            "head": "github-releases/tool/1.9",
            "base": "main",
            "body": f"{source}\n\nPins 1 asset(s) in `pins/tool.toml`.\n\n"
            "Opened by `repoutils github-releases bump --pr`.",
        }
    ]
    assert (
        _git(project, "log", "-1", "--format=%B", "origin/github-releases/tool/1.9") == f"Bump tool to 1.9\n\n{source}"
    )
    assert _git(project, "diff", "--name-only", "main", "origin/github-releases/tool/1.9") == "pins/tool.toml"
    # Back on main, unchanged, ready for the next project's bump.
    assert _git(project, "branch", "--show-current") == "main"
    assert not _git(project, "status", "--porcelain")


def test_bump_pr_adds_a_new_pins_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    _git(project, "rm", "--quiet", "-r", "pins")
    _git(project, "commit", "--quiet", "--message", "no pins")
    assert github_releases.main("bump", [], repo="o/r", pr=True, now=NOW) == 0
    assert _git(project, "diff", "--name-status", "main", "origin/github-releases/tool/1.9") == "A\tpins/tool.toml"


def test_bump_pr_already_open(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeGitHub(RELEASES_LIST, [{"html_url": "https://github.com/o/r/pull/4"}]))
    assert github_releases.main("bump", [], repo="o/r", pr=True, now=NOW) == 0
    assert capsys.readouterr().out == "tool: pull request https://github.com/o/r/pull/4 already bumps it to 1.9\n"
    assert f"{PULLS}?state=open&head=o%3Agithub-releases%2Ftool%2F1.9" in fake.urls
    assert not _git(project, "status", "--porcelain")


def test_bump_pr_refuses_changes(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    (project / "new.txt").write_text("x")
    assert github_releases.run(["bump", "--pr", "--repo", "o/r"]) == 1
    assert "The working tree has changes" in capsys.readouterr().err
    assert release_pins.read(project / "pins/tool.toml", "up/stream").current == "1.8"


def test_bump_pr_asks_gh_for_the_repo(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    monkeypatch.setattr(github, "gh_output", lambda *args: "o/r")
    assert github_releases.main("bump", [], pr=True, now=NOW) == 0
    assert len(fake.created) == 1


def test_bump_pr_git_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    _git(project, "remote", "remove", "origin")
    assert github_releases.run(["bump", "--pr", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err.startswith(
        "git ls-remote --heads origin refs/heads/github-releases/tool/1.9 failed:\n"
    )


def test_bump_pr_stages_a_file_name_literally(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "pyproject.toml").write_text(PYPROJECT.replace("pins/tool.toml", "pins/[t]ool.toml"))
    (project / "pins/[t]ool.toml").write_text((project / "pins/tool.toml").read_text())
    _git(project, "add", "--all")
    _git(project, "commit", "--quiet", "--message", "glob name")
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    assert github_releases.main("bump", [], repo="o/r", pr=True, now=NOW) == 0
    assert _git(project, "diff", "--name-only", "main", "origin/github-releases/tool/1.9") == "pins/[t]ool.toml"


def test_bump_pr_push_failure_switches_back(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    # The branch check passes, but the push is refused.
    (project.parent / f"{project.name}-origin.git" / "hooks" / "pre-receive").write_text("#!/bin/sh\nexit 1\n")
    (project.parent / f"{project.name}-origin.git" / "hooks" / "pre-receive").chmod(0o755)
    assert github_releases.run(["bump", "--pr", "--repo", "o/r"]) == 1
    assert "git push --set-upstream origin github-releases/tool/1.9 failed" in capsys.readouterr().err
    assert _git(project, "branch", "--show-current") == "main"


def test_bump_pr_network_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeGitHub(RELEASES_LIST))
    request = fake.request

    def unreachable(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        if method == "POST":
            raise urllib.error.URLError("no route to host")
        return request(method, url, token)

    monkeypatch.setattr(github, "request", unreachable)
    monkeypatch.setattr(github_releases, "datetime", _FixedNow)
    assert github_releases.run(["bump", "--pr", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == (
        "Pushed github-releases/tool/1.9, but opening its pull request failed (<urlopen error no route to host>)\n"
        "Open it at https://github.com/o/r/compare/main...github-releases/tool/1.9\n"
    )
