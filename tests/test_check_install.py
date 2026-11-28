import json
import os
import shlex
import subprocess
import sys
import venv
from collections.abc import Callable
from pathlib import Path

import pytest

from repoutils import cli
from repoutils.commands import check_install

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
WHEEL = "my_pkg-1.2.0-py3-none-any.whl"
INDEX = "https://test.pypi.org/simple/"


def probe(
    version: str = "1.2.0",
    commit: str | None = COMMIT,
    dirty: bool | None = False,
    files: int = 1,
    tag: dict[str, object] | None = None,
) -> tuple[int, str]:
    """What PROBE prints for an install whose build info records commit and dirty, and the tag if given."""
    text = json.dumps({"commit": commit, "dirty": dirty} | (tag or {}))
    found = {f"my_pkg/build_info{n or ''}.json": text for n in range(files)}
    return 0, json.dumps({"version": version, "files": found, "commands": ["mp", "my-pkg"]})


class FakeRunner:
    """Answers each uv command line with the next result, recording the lines."""

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


def uv(source: str, *args: str, index: str | None = None) -> list[str]:
    return check_install.uv_run(source, args, index)


def test_wheel_passes_with_no_command(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe())
    assert check_install.main(Path("dist"), runner=runner) == 0
    assert capsys.readouterr().out == f"dist/{WHEEL} installs and records this checkout's commit\n"
    [args] = runner.calls
    assert args[:8] == ["uv", "run", "--quiet", "--isolated", "--no-project", "--no-cache", "--with", f"dist/{WHEEL}"]
    assert args[8:11] == ["python", "-I", "-c"]
    assert args[12:] == ["my-pkg"]


def test_wheel_with_a_command(project: Path) -> None:
    runner = FakeRunner(probe(), (0, "my-pkg 1.2.0 (0123456789ab)\n"), (0, "help"), (0, f"Commit: {COMMIT}\n"))
    assert (
        check_install.main(
            Path("dist"), command="mp", runs=["--help"], runs_with_commit=["version"], version_line=True, runner=runner
        )
        == 0
    )
    source = f"dist/{WHEEL}"
    assert runner.calls[1:] == [uv(source, "mp", "--version"), uv(source, "mp", "--help"), uv(source, "mp", "version")]


def test_wheel_with_uncommitted_changes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_install, "head_commit", lambda directory: (COMMIT, True))
    line = (0, "my-pkg 1.2.0 (0123456789ab with uncommitted changes)\n")
    assert check_install.main(Path("dist"), version_line=True, runner=FakeRunner(probe(dirty=True), line)) == 0
    assert check_install.main(Path("dist"), runner=FakeRunner(probe(dirty=False))) == 1


@pytest.mark.parametrize(
    ("tag", "suffix"),
    [
        ({"tag": "v1.1.0", "distance": 3}, ", 3 commits after v1.1.0"),
        ({"tag": "v1.2.0", "distance": 0}, ""),
        ({"tag": None, "distance": None}, ""),
        # A file from an older hook, without either key.
        ({}, ""),
    ],
)
def test_wheel_version_line_gives_the_distance_from_its_tag(project: Path, tag: dict[str, object], suffix: str) -> None:
    line = (0, f"my-pkg 1.2.0 (0123456789ab{suffix})\n")
    assert check_install.main(Path("dist"), version_line=True, runner=FakeRunner(probe(tag=tag), line)) == 0
    wrong = (0, "my-pkg 1.2.0 (0123456789ab, 9 commits after v0.1)\n")
    assert check_install.main(Path("dist"), version_line=True, runner=FakeRunner(probe(tag=tag), wrong)) == 1


def test_wheel_from_another_commit_fails(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(commit=OTHER, dirty=True))
    assert check_install.main(Path("dist"), runs=["--help"], runner=runner) == 1
    expected = (
        f"::error::dist/{WHEEL} was built from {OTHER} with uncommitted changes, but this checkout has {COMMIT}\n"
    )
    assert capsys.readouterr().out == expected
    # The command doesn't run once the build info is wrong.
    assert len(runner.calls) == 1


def test_wrong_version_line(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(), (0, "my-pkg 1.2.0\n"))
    assert check_install.main(Path("dist"), version_line=True, runner=runner) == 1
    assert "to print 'my-pkg 1.2.0 (0123456789ab)', got 'my-pkg 1.2.0'" in capsys.readouterr().out


def test_version_line_that_fails(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main(Path("dist"), version_line=True, runner=FakeRunner(probe(), (2, ""))) == 1
    assert "--version` exited 2:\nan error" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("result", "error"),
    [
        ((0, "not json"), f"can't read what the probe of dist/{WHEEL} printed: 'not json'"),
        (probe(files=0), "expected one build_info.json in my-pkg from dist/"),
        (probe(files=2), "found my_pkg/build_info.json, my_pkg/build_info1.json (see --build-info)"),
        (probe(commit=None, dirty=None), "records no commit, as its build had no Git checkout"),
        (probe(commit=None, dirty=False), "records no commit"),
        (probe(dirty=None), "records no commit"),
        (probe(commit=COMMIT[:12]), "records no commit"),
        ((0, json.dumps({"version": "1", "files": {"a.json": "[]"}, "commands": []})), "a.json in dist/"),
        ((0, json.dumps({"version": "1", "files": {}})), "can't read what the probe"),
    ],
)
def test_bad_build_info(project: Path, capsys: pytest.CaptureFixture[str], result: tuple[int, str], error: str) -> None:
    assert check_install.main(Path("dist"), runner=FakeRunner(result)) == 1
    assert error in capsys.readouterr().out


def test_build_info_path(project: Path) -> None:
    runner = FakeRunner(probe())
    assert check_install.main(Path("dist"), build_info_path="my_pkg/build_info.json", runner=runner) == 0
    assert runner.calls[0][-2:] == ["my-pkg", "my_pkg/build_info.json"]


def test_wheel_file_given(project: Path) -> None:
    runner = FakeRunner(probe("1.1.0"))
    assert check_install.main(Path("dist/my_pkg-1.1.0-py3-none-any.whl"), runner=runner) == 0
    assert "dist/my_pkg-1.1.0-py3-none-any.whl" in runner.calls[0]


def test_wheel_without_a_static_version_needs_one_wheel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\ndynamic = ["version"]\n')
    assert check_install.main(Path("dist"), runner=FakeRunner()) == 1
    assert "expected one wheel matching dist/my_pkg-*-*.whl, found my_pkg-1.1.0" in capsys.readouterr().err


def test_unnormalized_project_version_finds_its_wheel(project: Path) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\nversion = "1.2.0-RC1"\n')
    (project / "dist" / "my_pkg-1.2.0rc1-py3-none-any.whl").write_text("")
    runner = FakeRunner(probe("1.2.0rc1"))
    assert check_install.main(Path("dist"), runner=runner) == 0
    assert "dist/my_pkg-1.2.0rc1-py3-none-any.whl" in runner.calls[0]


def test_project_version_that_isnt_pep_440_needs_one_wheel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\nversion = "latest"\n')
    assert check_install.main(Path("dist"), runner=FakeRunner()) == 1
    assert "expected one wheel matching dist/my_pkg-*-*.whl" in capsys.readouterr().err


def test_another_name_ignores_the_project_version(project: Path) -> None:
    (project / "dist" / "other-2.0-py3-none-any.whl").write_text("")
    runner = FakeRunner(probe("2.0"))
    assert check_install.main(Path("dist"), name="other", runner=runner) == 0
    assert "dist/other-2.0-py3-none-any.whl" in runner.calls[0]
    # The project's own name, however spelt, still uses its version.
    assert check_install.main(Path("dist"), name="My_Pkg", runner=FakeRunner(probe())) == 0


@pytest.mark.parametrize(
    ("version", "normalized"),
    [
        ("1.0", "1.0"),
        ("v01.02", "1.2"),
        ("1.1alpha1", "1.1a1"),
        ("1.0-beta.2", "1.0b2"),
        ("1.0c", "1.0rc0"),
        ("1.0preview3", "1.0rc3"),
        ("1.0-1", "1.0.post1"),
        ("1.0_rev", "1.0.post0"),
        ("1.0.DEV", "1.0.dev0"),
        ("0!1.0", "1.0"),
        ("2!1.0+Ubuntu-1", "2!1.0+ubuntu.1"),
        ("1.0+ABC.01", "1.0+abc.1"),
        ("latest", None),
    ],
)
def test_normalize_version(version: str, normalized: str | None) -> None:
    assert check_install.normalize_version(version) == normalized


def test_shown_hides_credentials() -> None:
    assert check_install.shown("https://user:secret@example.com/simple/") == "https://***@example.com/simple/"
    assert check_install.shown(INDEX) == INDEX
    assert check_install._shown_args(["--default-index", "HTTPS://u:secret@example.com/"]) == (
        "--default-index 'https://***@example.com/'"
    )


def test_index_credentials_are_not_printed(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    index = "https://user:secret@example.com/simple/"
    assert check_install.main(version="1.2.0", index=index, runner=FakeRunner((1, "")), delays=[0]) == 1
    assert check_install.main(version="1.2.0", index=index, runner=FakeRunner(probe())) == 0
    out, err = capsys.readouterr()
    assert "secret" not in out + err
    assert "https://***@example.com/simple/" in out


def test_a_run_that_fails(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(), (1, ""), (0, ""))
    assert check_install.main(Path("dist"), runs=["check 'a b'", "version"], runner=runner) == 1
    assert runner.calls[1][-3:] == ["my-pkg", "check", "a b"]
    assert runner.calls[2][-2:] == ["my-pkg", "version"]
    assert f"--with dist/{WHEEL} my-pkg check 'a b'` exited 1:\nan error" in capsys.readouterr().out


def test_run_with_commit_that_gives_another(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(), (0, f"Commit: {OTHER}\n"))
    assert check_install.main(Path("dist"), runs_with_commit=["version"], runner=runner) == 1
    assert f"to print the commit {COMMIT}" in capsys.readouterr().out


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


def test_no_uv(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main(Path("dist"), runner=FakeRunner(FileNotFoundError("uv"))) == 1
    assert "can't run uv" in capsys.readouterr().err


def test_release_passes_after_retrying(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    slept: list[float] = []
    line = (0, "my-pkg 1.2.0 (fedcba987654 with uncommitted changes)\n")
    runner = FakeRunner((1, ""), probe(commit=OTHER, dirty=True), line, (0, ""))
    assert (
        check_install.main(
            version="1.2.0", index=INDEX, runs=["--help"], version_line=True, runner=runner, sleep=slept.append
        )
        == 0
    )
    assert slept == [0, 10]
    out, err = capsys.readouterr()
    assert out == f"my-pkg 1.2.0 installs from {INDEX} and records its commit\n"
    assert "Retrying in 10 seconds." in err
    source = "my-pkg==1.2.0"
    assert runner.calls[0] == runner.calls[1]
    assert runner.calls[0][6:10] == ["--default-index", INDEX, "--with", source]
    assert runner.calls[2:] == [
        uv(source, "my-pkg", "--version", index=INDEX),
        uv(source, "my-pkg", "--help", index=INDEX),
    ]


def test_release_gives_up(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner((1, ""), (1, ""))
    assert check_install.main(version="1.2.0", runner=runner, delays=[0, 5], sleep=no_sleep) == 1
    assert "my-pkg==1.2.0 did not install from https://pypi.org/simple/ after 2 tries" in capsys.readouterr().out


def test_release_retries_a_command_that_fails_to_install(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The probe installs the version, but the index doesn't yet serve it to --version's own install.
    slept: list[float] = []
    line = (0, "my-pkg 1.2.0 (0123456789ab)\n")
    runner = FakeRunner(probe(), (1, ""), (0, ""), probe(), line, (0, ""))
    assert (
        check_install.main(
            version="1.2.0", index=INDEX, runs=["--help"], version_line=True, runner=runner, sleep=slept.append
        )
        == 0
    )
    assert slept == [0, 10]
    out, err = capsys.readouterr()
    assert out == f"my-pkg 1.2.0 installs from {INDEX} and records its commit\n"
    assert "--version` exited 1:\nan error\nRetrying in 10 seconds." in err
    source = "my-pkg==1.2.0"
    assert runner.calls[1] == uv(source, "my-pkg", "--version", index=INDEX)
    assert runner.calls[3] == runner.calls[0]
    assert runner.calls[4:] == [
        uv(source, "my-pkg", "--version", index=INDEX),
        uv(source, "my-pkg", "--help", index=INDEX),
    ]


def test_release_with_a_run_that_keeps_failing_gives_up(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(), (1, ""), probe(), (1, ""))
    assert check_install.main(version="1.2.0", runs=["--help"], runner=runner, delays=[0, 5], sleep=no_sleep) == 1
    out, err = capsys.readouterr()
    args = uv("my-pkg==1.2.0", "my-pkg", "--help", index=check_install.DEFAULT_INDEX)
    assert out == f"::error::`{shlex.join(args)}` exited 1:\nan error\n"
    assert err.count("Retrying in 5 seconds.") == 1


def test_release_with_a_wrong_version_line_fails_at_once(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(), (0, "my-pkg 1.2.0\n"))
    assert check_install.main(version="1.2.0", version_line=True, runner=runner, sleep=no_sleep) == 1
    assert "to print 'my-pkg 1.2.0 (0123456789ab)', got 'my-pkg 1.2.0'" in capsys.readouterr().out
    assert len(runner.calls) == 2


def test_release_with_a_wrong_version_line_and_a_failed_run_fails_at_once(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(probe(), (0, "my-pkg 1.2.0\n"), (1, ""))
    assert check_install.main(version="1.2.0", version_line=True, runs=["--help"], runner=runner, sleep=no_sleep) == 1
    out, err = capsys.readouterr()
    assert "to print 'my-pkg 1.2.0 (0123456789ab)', got 'my-pkg 1.2.0'" in out
    assert "--help` exited 1" in out
    assert "Retrying" not in err
    assert len(runner.calls) == 3


def test_release_without_build_info_fails_at_once(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe(files=0))
    assert check_install.main(version="1.2.0", runner=runner, sleep=no_sleep) == 1
    assert "found none" in capsys.readouterr().out
    assert len(runner.calls) == 1


def test_name_option_without_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner(probe("2.0"))
    assert check_install.main(version="2.0", name="other", runner=runner, sleep=no_sleep) == 0
    assert runner.calls[0][-1] == "other"


def test_no_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    assert check_install.main(version="2.0") == 2
    assert "give --name" in capsys.readouterr().err


def test_no_mode(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_install.main() == 2
    assert "give --wheel or --version" in capsys.readouterr().err


def test_unsplittable_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    assert check_install.main(Path("dist"), runs=["check 'a"], runner=runner) == 2
    assert "can't split a --run's arguments: No closing quotation" in capsys.readouterr().err
    assert runner.calls == []


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
    argv += ["--run=--help", "--run", "x y", "--version-line", "--build-info", "n/b.json"]
    assert check_install.run(argv) == 0
    assert check_install.run(["--wheel", "dist", "--run-with-commit", "version"]) == 0
    assert calls == [
        (None, "1", "https://example.invalid/", "n", "c", ["--help", "x y"], [], True, "n/b.json"),
        (Path("dist"), None, check_install.DEFAULT_INDEX, None, None, [], ["version"], False, None),
    ]


def test_wheel_name() -> None:
    assert check_install.wheel_name("My.Pkg--x") == "my_pkg_x"


def test_default_runner_drops_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "src")
    result = check_install._run([sys.executable, "-c", "import os; print(os.environ.get('PYTHONPATH'))"])
    assert (result.returncode, result.stdout) == (0, "None\n")
    assert os.environ["PYTHONPATH"] == "src"


def test_probe_reads_an_installed_distribution(tmp_path: Path) -> None:
    # PROBE itself, against a distribution put by hand in a virtual environment.
    venv.create(tmp_path / "venv", with_pip=False)
    python = tmp_path / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    where = "import sysconfig; print(sysconfig.get_path('purelib')); print(sysconfig.get_path('scripts'))"
    site_path, scripts_path = subprocess.run(
        [python, "-I", "-c", where], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    site, scripts = Path(site_path), Path(scripts_path)
    (scripts / "my-script").write_text("")
    package = site / "my_pkg"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "data-not-a-command").write_text("")
    (package / "build_info.json").write_text(json.dumps({"commit": COMMIT, "dirty": False}))
    (package / "other.json").write_text("{}")
    info = site / "my_pkg-1.2.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: my-pkg\nVersion: 1.2.0\n")
    (info / "build_info.json").write_text("{}")
    record = ["my_pkg/build_info.json", "my_pkg/other.json", "my_pkg/bin/data-not-a-command"]
    record += ["my_pkg-1.2.0.dist-info/build_info.json", Path(os.path.relpath(scripts / "my-script", site)).as_posix()]
    (info / "RECORD").write_text("".join(f"{path},,\n" for path in record))
    (info / "entry_points.txt").write_text("[console_scripts]\nmy-pkg = my_pkg:main\n[other]\nx = y\n")

    def run(*args: str) -> check_install.Installed | None:
        command = [str(python), "-I", "-c", check_install.PROBE, *args]
        out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
        return check_install.read_probe("my-pkg", "here", out)[0]

    installed = check_install.Installed("1.2.0", COMMIT, False, ("my-pkg", "my-script"))
    assert run("my-pkg") == installed
    assert run("my-pkg", "my_pkg/build_info.json") == installed
    assert run("my-pkg", "my_pkg/other.json") is None


def test_stale_wheel_of_another_version_is_not_taken(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "dist" / WHEEL).unlink()
    assert check_install.main(Path("dist"), runner=FakeRunner()) == 1
    assert "expected one wheel matching dist/my_pkg-1.2.0-*.whl, found none" in capsys.readouterr().err


def test_command_the_package_doesnt_install(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(probe())
    assert check_install.main(Path("dist"), command="elsewhere", version_line=True, runner=runner) == 1
    assert "installs no command 'elsewhere' (it installs: mp, my-pkg)" in capsys.readouterr().out
    # It doesn't run a command of that name from elsewhere on PATH.
    assert len(runner.calls) == 1
    # Without --version-line or a run, the command isn't needed.
    assert check_install.main(Path("dist"), command="elsewhere", runner=FakeRunner(probe())) == 0
