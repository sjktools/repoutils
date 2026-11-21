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
