import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from repoutils import cli
from repoutils.commands import check_install

COMMIT = "0123456789abcdef0123456789abcdef01234567"
WHEEL = "my_pkg-1.2.0-py3-none-any.whl"


class FakeRunner:
    """Answers each uvx command line with the next result, recording the lines."""

    def __init__(self, *results: tuple[int, str] | FileNotFoundError) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        result = self.results.pop(0)
        if isinstance(result, FileNotFoundError):
            raise result
        code, out = result
        return subprocess.CompletedProcess(args, code, out, "an error" if code else "")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-pkg"\nversion = "1.2.0"\n')
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / WHEEL).write_text("")
    (tmp_path / "dist" / "my_pkg-1.1.0-py3-none-any.whl").write_text("")
    monkeypatch.setattr(check_install, "head_commit", lambda directory: (COMMIT, False))
    return tmp_path


def no_sleep(seconds: float) -> None:
    pass


def test_wheel_passes(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((0, "my-pkg 1.2.0 (0123456789ab)\n"), (0, "help"))
    assert check_install.main(Path("dist"), runs=["--help"], runner=runner) == 0
    assert capsys.readouterr().out == f"dist/{WHEEL} installs and runs\n"
    source = f"dist/{WHEEL}"
    assert runner.calls == [
        ["uvx", "--isolated", "--no-cache", "--from", source, "my-pkg", "--version"],
        ["uvx", "--isolated", "--no-cache", "--from", source, "my-pkg", "--help"],
    ]


def test_wheel_with_uncommitted_changes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_install, "head_commit", lambda directory: (COMMIT, True))
    runner = FakeRunner((0, "my-pkg 1.2.0 (0123456789ab with uncommitted changes)\n"))
    assert check_install.main(Path("dist"), runner=runner) == 0
    runner = FakeRunner((0, "my-pkg 1.2.0 (0123456789ab)\n"))
    assert check_install.main(Path("dist"), runner=runner) == 1


def test_wheel_with_another_commit_fails(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((0, "my-pkg 1.2.0 (fedcba987654)\n"))
    assert check_install.main(Path("dist"), runs=["--help"], runner=runner) == 1
    assert "expected 'my-pkg 1.2.0 (0123456789ab)'" in capsys.readouterr().out
    # The --run commands don't run once --version is wrong.
    assert len(runner.calls) == 1


def test_wheel_file_with_its_own_version_and_a_command(project: Path) -> None:
    runner = FakeRunner((0, "my-pkg 1.1.0 (0123456789ab)\n"))
    assert check_install.main(Path("dist/my_pkg-1.1.0-py3-none-any.whl"), command="mp", runner=runner) == 0
    assert runner.calls[0][-2:] == ["mp", "--version"]


def test_wheel_without_a_static_version_needs_one_wheel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\ndynamic = ["version"]\n')
    assert check_install.main(Path("dist"), runner=FakeRunner()) == 1
    assert "expected one wheel matching dist/my_pkg-*-*.whl, found my_pkg-1.1.0" in capsys.readouterr().err


def test_a_run_that_fails(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((0, "my-pkg 1.2.0 (0123456789ab)\n"), (1, ""), (0, ""))
    assert check_install.main(Path("dist"), runs=["check 'a b'", "version"], runner=runner) == 1
    assert runner.calls[1][-2:] == ["check", "a b"]
    assert runner.calls[2][-1:] == ["version"]
    assert f"::error::`uvx --isolated --no-cache --from dist/{WHEEL} my-pkg check 'a b'` exited 1:\nan error" in (
        capsys.readouterr().out
    )


def test_wheel_that_fails_to_install(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main(Path("dist"), runner=FakeRunner((2, ""))) == 1
    assert "exited 2" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda root: None, "can't find the commit"),
        (lambda root: [wheel.unlink() for wheel in (root / "dist").iterdir()], "found none"),
    ],
)
def test_wheel_errors(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setup: Callable[[Path], None],
    error: str,
) -> None:
    if error.startswith("can't"):
        monkeypatch.setattr(check_install, "head_commit", lambda directory: None)
    setup(project)
    assert check_install.main(Path("dist"), runner=FakeRunner()) == 1
    assert error in capsys.readouterr().err


def test_wheel_path_that_is_missing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main(Path("nowhere"), runner=FakeRunner()) == 1
    assert "nowhere is neither a wheel nor a directory" in capsys.readouterr().err


def test_badly_named_wheel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "odd.whl").write_text("")
    assert check_install.main(Path("odd.whl"), runner=FakeRunner()) == 1
    assert "odd.whl is not named as a wheel is" in capsys.readouterr().err


def test_no_uvx(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main(Path("dist"), runner=FakeRunner(FileNotFoundError("uvx"))) == 1
    assert "can't run uvx" in capsys.readouterr().err


def test_release_passes_after_retrying(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    slept: list[float] = []
    runner = FakeRunner((1, ""), (0, "my-pkg 1.2.0 (fedcba987654 with uncommitted changes)\n"), (0, ""))
    index = "https://test.pypi.org/simple/"
    assert check_install.main(version="1.2.0", index=index, runs=["--help"], runner=runner, sleep=slept.append) == 0
    assert slept == [0, 10]
    out, err = capsys.readouterr()
    assert out == f"my-pkg 1.2.0 installs from {index} and runs\n"
    assert "Retrying in 10 seconds." in err
    uvx = ["uvx", "--isolated", "--no-cache", "--default-index", index, "--from", "my-pkg==1.2.0", "my-pkg"]
    assert runner.calls == [[*uvx, "--version"], [*uvx, "--version"], [*uvx, "--help"]]


def test_release_gives_up(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((1, ""), (1, ""))
    assert check_install.main(version="1.2.0", runner=runner, delays=[0, 5], sleep=no_sleep) == 1
    assert "my-pkg==1.2.0 did not install from https://pypi.org/simple/ and run after 2 tries" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("line", ["my-pkg 1.2.0", "my-pkg 1.2.0 (0123456789ab", "my-pkg 1.2.0 (0123456789AB)"])
def test_release_without_a_commit_fails_at_once(line: str, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((0, line))
    assert check_install.main(version="1.2.0", runner=runner, sleep=no_sleep) == 1
    assert "<12-hex commit>" in capsys.readouterr().out
    assert len(runner.calls) == 1


def test_name_option_without_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner((0, "other 2.0 (0123456789ab)\n"))
    assert check_install.main(version="2.0", name="other", runner=runner, sleep=no_sleep) == 0


def test_no_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    assert check_install.main(version="2.0") == 2
    assert "give --name" in capsys.readouterr().err


def test_no_mode(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main() == 2
    assert "give --wheel or --version" in capsys.readouterr().err


def test_bad_project_table(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "pyproject.toml").write_text('project = "x"\n')
    assert cli.main(["check-install", "--version", "1"]) == 1
    assert "[project] must be a table" in capsys.readouterr().err


def test_index_needs_version(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        check_install.run(["--wheel", "dist", "--index", "https://example.invalid/"])
    assert exit_info.value.code == 2
    assert "--index needs --version" in capsys.readouterr().err


def test_run_passes_its_options(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_main(*args: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(check_install, "main", fake_main)
    argv = ["--version", "1", "--index", "https://example.invalid/", "--name", "n", "--command", "c"]
    assert check_install.run([*argv, "--run=--help", "--run", "x y"]) == 0
    assert check_install.run(["--wheel", "dist"]) == 0
    assert check_install.run(["--wheel", "dist", "--run-with-commit", "version"]) == 0
    assert calls == [
        (None, "1", "https://example.invalid/", "n", "c", ["--help", "x y"], []),
        (Path("dist"), None, check_install.DEFAULT_INDEX, None, None, [], []),
        (Path("dist"), None, check_install.DEFAULT_INDEX, None, None, [], ["version"]),
    ]


def test_wheel_name() -> None:
    assert check_install.wheel_name("My.Pkg--x") == "my_pkg_x"


def test_default_runner_captures_output() -> None:
    result = check_install._run([sys.executable, "-c", "print('hi')"])
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_run_with_commit_in_a_wheel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = (0, "my-pkg 1.2.0 (0123456789ab)\n")
    runner = FakeRunner(line, (0, f"Commit: {COMMIT}\n"))
    assert check_install.main(Path("dist"), runs_with_commit=["version"], runner=runner) == 0
    runner = FakeRunner(line, (0, f"Commit: {COMMIT[:-1]}0\n"))
    assert check_install.main(Path("dist"), runs_with_commit=["version"], runner=runner) == 1
    assert f"to print the whole commit {COMMIT}..." in capsys.readouterr().out


def test_run_with_commit_in_a_release(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = (0, "my-pkg 1.2.0 (fedcba987654)\n")
    other = "fedcba987654" + "1" * 28
    runner = FakeRunner(line, (0, f"Commit: {other}\n"))
    assert check_install.main(version="1.2.0", runs_with_commit=["version"], runner=runner, sleep=no_sleep) == 0
    runner = FakeRunner(line, (0, f"Commit: {COMMIT}\n"))
    assert check_install.main(version="1.2.0", runs_with_commit=["version"], runner=runner, sleep=no_sleep) == 1
    assert "to print the whole commit fedcba987654..." in capsys.readouterr().out


def test_unsplittable_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    assert check_install.main(Path("dist"), runs=["check 'a"], runner=runner) == 2
    assert "can't split a --run's arguments: No closing quotation" in capsys.readouterr().err
    assert runner.calls == []


def test_unnormalized_project_version_finds_the_one_wheel(project: Path) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\nversion = "1.2.0-rc1"\n')
    (project / "dist" / WHEEL).unlink()
    runner = FakeRunner((0, "my-pkg 1.1.0 (0123456789ab)\n"))
    assert check_install.main(Path("dist"), runner=runner) == 0
