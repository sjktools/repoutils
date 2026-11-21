import subprocess
from pathlib import Path
from typing import Any

import pytest

from repoutils import downstream_actions
from repoutils.downstream_actions import Use

WORKFLOW = """\
jobs:
  reuse:
    uses: U/Up/.github/workflows/w.yml@v0.3.0
  bump:
    steps:
      # uses: u/up/.github/actions/x@v0.1.0
      - uses: actions/checkout@v7.0.1
      - uses: "u/up/.github/actions/x@v0.3.0"  # the bump
      -   uses: 'u/up@v0.3.5'
      - run: echo uses u/up@v0.1.0
      - run: |
          cat <<'EOF'
          uses: u/up@v0.1.0

          - uses: u/up@main
          EOF
        shell: bash
      - uses: u/upper/.github/actions/x@v0.1.0
      - name: >-
          A folded name, with uses: u/up@v0.1.0 in it
        uses: u/up/.github/actions/z@v0.3.0
"""
ACTION = "runs:\n  using: composite\n  steps:\n    - uses: u/up/.github/actions/y@v0.3.0\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/actions/z").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text(WORKFLOW)
    (tmp_path / ".github/actions/z/action.yml").write_text(ACTION)
    return tmp_path


def test_uses(root: Path) -> None:
    assert downstream_actions.uses(root, "u/up") == [
        Use(".github/actions/z/action.yml", 4, "u/up/.github/actions/y", "v0.3.0"),
        Use(".github/workflows/ci.yml", 3, "U/Up/.github/workflows/w.yml", "v0.3.0"),
        Use(".github/workflows/ci.yml", 8, "u/up/.github/actions/x", "v0.3.0"),
        Use(".github/workflows/ci.yml", 9, "u/up", "v0.3.5"),
        Use(".github/workflows/ci.yml", 21, "u/up/.github/actions/z", "v0.3.0"),
    ]


def test_stale_leaves_the_current_tag(root: Path) -> None:
    (root / ".github/workflows/ci.yml").write_text(WORKFLOW.replace("v0.3.0", "v0.3.5"))
    assert downstream_actions.stale(root, "u/up", "0.3.5") == [
        Use(".github/actions/z/action.yml", 4, "u/up/.github/actions/y", "v0.3.0")
    ]


@pytest.mark.parametrize(
    ("ref", "error"),
    [
        ("main", "uses u/up/.github/actions/y@main, which bump can't move, as main isn't a release tag"),
        ("v1.x", "as v1.x isn't a release tag of the form vX.Y.Z"),
        ("a" * 40, "isn't a release tag"),
        ("v1", "as v1 isn't a release tag"),
        ("v0.3", "as v0.3 isn't a release tag"),
        ("v0.5.0", "uses u/up/.github/actions/y@v0.5.0, newer than v0.4.0, and bump never lowers a ref"),
    ],
)
def test_stale_refuses(root: Path, ref: str, error: str) -> None:
    (root / ".github/actions/z/action.yml").write_text(ACTION.replace("v0.3.0", ref))
    with pytest.raises(RuntimeError, match=r"action\.yml:4 uses ") as raised:
        downstream_actions.stale(root, "u/up", "0.4.0")
    assert error in str(raised.value)


def test_move(root: Path) -> None:
    path = root / ".github/workflows/ci.yml"
    path.write_bytes(WORKFLOW.replace("\n", "\r\n").encode())
    moving = downstream_actions.stale(root, "u/up", "0.4.0")
    assert downstream_actions.move(root, "0.4.0", moving) == [
        ".github/actions/z/action.yml",
        ".github/workflows/ci.yml",
    ]
    expected = (
        WORKFLOW.replace("w.yml@v0.3.0", "w.yml@v0.4.0")
        .replace('x@v0.3.0"', 'x@v0.4.0"')
        .replace("'u/up@v0.3.5'", "'u/up@v0.4.0'")
        .replace("z@v0.3.0", "z@v0.4.0")
    )
    assert path.read_bytes() == expected.replace("\n", "\r\n").encode()
    assert (root / ".github/actions/z/action.yml").read_text() == ACTION.replace("v0.3.0", "v0.4.0")
    assert downstream_actions.stale(root, "u/up", "0.4.0") == []


def test_move_refuses_a_changed_line(root: Path) -> None:
    moving = downstream_actions.stale(root, "u/up", "0.4.0")
    (root / ".github/workflows/ci.yml").write_text("jobs: {}\n")
    with pytest.raises(RuntimeError, match=r"ci\.yml:3 changed while bump ran"):
        downstream_actions.move(root, "0.4.0", moving)
    # Nothing is written, not even the file it read first.
    assert (root / ".github/actions/z/action.yml").read_text() == ACTION


def test_lock_without_an_actions_lock(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("gh ran")

    monkeypatch.setattr(subprocess, "run", run)
    assert downstream_actions.lock(root) is False


@pytest.mark.parametrize("changes", [True, False])
def test_lock(root: Path, monkeypatch: pytest.MonkeyPatch, changes: bool) -> None:
    path = root / ".github/workflows/actions.lock"
    path.write_text("old\n")
    ran: list[tuple[list[str], Path]] = []

    def run(command: list[str], *, cwd: Path, **kwargs: Any) -> None:
        ran.append((command, cwd))
        if changes:
            path.write_text("new\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert downstream_actions.lock(root) is changes
    assert ran == [(["gh", "actions-lock"], root)]


def test_verify(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[list[str], Path, bool]] = []

    def run(command: list[str], *, cwd: Path, check: bool, **kwargs: Any) -> None:
        ran.append((command, cwd, check))

    monkeypatch.setattr(subprocess, "run", run)
    downstream_actions.verify(root)
    assert ran == [(["gh", "actions-lock", "--verify"], root, True)]


LOCK = """\
# This file is machine-generated by `gh actions-lock`.
version: 'v0.0.3'
workflows:
    '.github/workflows/ci.yml':
        - 'u/up/.github/actions/x@v0.3.0'
        - 'v/w@main'
dependencies:
    'u/up/.github/actions/x@v0.3.0':
        ref: 'v0.3.0'
        commit: 'sha1-aaa'
        uses:
            - 'actions/checkout@v7.0.1'
    'actions/checkout@v7.0.1':
        ref: 'v7.0.1'
        commit: 'sha1-bbb'
    'v/w@main':
        ref: 'main'
        commit: 'sha1-ccc'
"""


def test_locked_commits() -> None:
    assert downstream_actions.locked_commits(LOCK) == {
        "u/up/.github/actions/x@v0.3.0": "sha1-aaa",
        "actions/checkout@v7.0.1": "sha1-bbb",
        "v/w@main": "sha1-ccc",
    }


def test_check_kept_allows_new_entries() -> None:
    new = LOCK.replace("x@v0.3.0", "x@v0.4.0").replace("sha1-aaa", "sha1-ddd")
    downstream_actions.check_kept(LOCK.encode(), new.encode(), "new.lock")


def test_check_kept_refuses_a_moved_commit() -> None:
    new = LOCK.replace("sha1-ccc", "sha1-old")
    with pytest.raises(RuntimeError, match=r"new\.lock locks v/w@main at another commit than"):
        downstream_actions.check_kept(LOCK.encode(), new.encode(), "new.lock")


def test_check_kept_refuses_bytes_that_arent_utf8() -> None:
    with pytest.raises(RuntimeError, match=r"new\.lock isn't valid UTF-8"):
        downstream_actions.check_kept(LOCK.encode(), b"\xff", "new.lock")
