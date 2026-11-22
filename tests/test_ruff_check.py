import sys
from pathlib import Path

import pytest

from repoutils import cli

# A fake ruff, which records its arguments, then writes $FAKE_RUFF_STDERR to stderr
# and exits $FAKE_RUFF_EXIT.
FAKE_RUFF = f"""#!{sys.executable}
import os, sys
with open(os.environ["FAKE_RUFF_ARGS"], "w") as f:
    f.write("\\n".join(sys.argv[1:]))
print("ruff's stdout")
sys.stderr.write(os.environ["FAKE_RUFF_STDERR"])
sys.exit(int(os.environ["FAKE_RUFF_EXIT"]))
"""


@pytest.fixture
def fake_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Puts a fake ruff first on PATH, and returns the file it records its arguments in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ruff = bin_dir / "ruff"
    ruff.write_text(FAKE_RUFF)
    ruff.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}")
    args = tmp_path / "args"
    monkeypatch.setenv("FAKE_RUFF_ARGS", str(args))
    return args


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], *, stderr: str = "", exit_code: int = 0) -> int:
    monkeypatch.setenv("FAKE_RUFF_STDERR", stderr)
    monkeypatch.setenv("FAKE_RUFF_EXIT", str(exit_code))
    return cli.main(["ruff-check", *argv])


def test_passes_arguments_in_order_after_no_cache(
    fake_ruff: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    argv = ["src", "--select", "E501", "--", "-odd"]
    assert _run(monkeypatch, argv) == 0
    assert fake_ruff.read_text().split("\n") == ["check", "--no-cache", *argv]
    out, err = capfd.readouterr()
    assert out == "ruff's stdout\n"
    assert err == ""


def test_fails_on_a_warning(
    fake_ruff: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    stderr = "All checks passed!\nwarning: Invalid `# noqa` directive on tests/x.py:3\n"
    assert _run(monkeypatch, ["."], stderr=stderr) == 1
    err = capfd.readouterr().err
    assert err == stderr + "repoutils ruff-check: ruff gave a warning (see above)\n"


def test_a_warning_inside_a_line_is_not_one(
    fake_ruff: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, [], stderr="note: warning: not at the start\n") == 0
    assert capfd.readouterr().err == "note: warning: not at the start\n"


def test_keeps_ruffs_exit_code(
    fake_ruff: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, [], stderr="warning: also this\n", exit_code=2) == 2
    assert capfd.readouterr().err == "warning: also this\n"


def test_help_is_its_own(fake_ruff: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ruff-check", "--help"])
    assert exit_info.value.code == 0
    assert "goes to `ruff check`" in capsys.readouterr().out
    assert not fake_ruff.exists()


def test_no_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli.main(["ruff-check"]) == 1
    assert capsys.readouterr().err.startswith("repoutils ruff-check: can't run ruff: ")
