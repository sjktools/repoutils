import textwrap
from pathlib import Path

import pytest

from repoutils.commands.check_doubled_spaces import collapse, doubled_spaces, main, run
from repoutils.config import ConfigError

DOCUMENT = textwrap.dedent("""\
    # Heading  with  spaces

    So  run the tests, and  that is all.
    Wrapped prose `a  b` keeps code spans, and ``x  `y` `` too.
    A code span ` runs over
    a  line ` and so is skipped.

        Indented  code block.

    - A  list item.
    1.  A padded list marker isn't a doubled space.
       Its  continuation line.

    | Table  | cell  |
    | ---    | ---   |

    > Quoted  terminal  output.

    <div>HTML  block.</div>

    [link]:  https://example.com

    ```text
    fenced  code
    ```

    A comment <!-- with  spaces --> is skipped.
    Two spaces after a sentence.  Next one.
    Emphasis *ends.*  Next one.
    """)


def test_doubled_spaces_reports_only_prose() -> None:
    assert doubled_spaces(DOCUMENT) == {
        3: [(2, 4), (22, 24)],
        10: [(3, 5)],
        12: [(6, 8)],
        28: [(28, 30)],
        29: [(16, 18)],
    }


def test_doubled_spaces_can_allow_two_spaces_after_a_sentence() -> None:
    found = doubled_spaces(DOCUMENT, allow_after_sentence=True)
    assert sorted(found) == [3, 10, 12]


def test_a_comment_keeps_the_columns_after_it() -> None:
    assert doubled_spaces("Text <!-- a --> and  more.\n") == {1: [(19, 21)]}


def test_a_comment_over_several_lines_is_skipped() -> None:
    assert doubled_spaces("Text <!-- a\nb  c\n--> and  more.\n") == {3: [(7, 9)]}


def test_collapse_keeps_everything_else() -> None:
    fixed, count = collapse("So  run   it.\r\n```\na  b\n```\nLast  line")
    assert (fixed, count) == ("So run it.\r\n```\na  b\n```\nLast line", 3)


def test_collapse_with_nothing_to_collapse() -> None:
    assert collapse("Fine.\n") == ("Fine.\n", 0)


def _write(tmp_path: Path, files: dict[str, str | bytes]) -> None:
    for name, contents in files.items():
        (tmp_path / name).write_bytes(contents.encode() if isinstance(contents, str) else contents)


def test_main_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"AGENTS.md": "One `a  b` sentence.\n"})
    assert main() == 0
    assert capsys.readouterr().out == "AGENTS.md: no doubled spaces in the prose.\n"


def test_main_with_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main() == 0
    assert capsys.readouterr().out == "No files: no doubled spaces in the prose.\n"


def test_main_reports_each_line_and_a_file_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"AGENTS.md": b"Caf\xe9.\n", "README.md": "Fine.\n  So  run  it.\n"})
    assert main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "Doubled spaces check failed:",
        "- AGENTS.md: cannot read (not valid UTF-8)",
        "- README.md:2: So  run  it.",
        "Run `repoutils check-doubled-spaces --fix` to collapse each doubled space to one.",
    ]


def test_main_fix_collapses_and_keeps_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"AGENTS.md": "So  run  it.\r\nFine.\r\n", "README.md": "Fine.\n"})
    assert main(fix=True) == 0
    assert (tmp_path / "AGENTS.md").read_bytes() == b"So run it.\r\nFine.\r\n"
    assert capsys.readouterr().out == "AGENTS.md: collapsed 2 doubled spaces.\n"
    assert main(fix=True) == 0
    assert capsys.readouterr().out == "AGENTS.md, README.md: no doubled spaces in the prose.\n"


def test_main_fix_counts_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"README.md": "So  run it.\n"})
    assert main(fix=True) == 0
    assert capsys.readouterr().out == "README.md: collapsed 1 doubled space.\n"


def test_main_fix_still_fails_on_a_file_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"AGENTS.md": b"Caf\xe9.\n", "README.md": "So  run it.\n"})
    assert main(fix=True) == 1
    assert (tmp_path / "README.md").read_text() == "So run it.\n"
    assert capsys.readouterr().out.splitlines() == [
        "README.md: collapsed 1 doubled space.",
        "Doubled spaces fix failed:",
        "- AGENTS.md: cannot read (not valid UTF-8)",
    ]


def test_main_fix_reports_a_file_it_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"README.md": "So  run it.\n"})

    def refuse(*args: object, **kwargs: object) -> object:
        if "w" in args[1:2]:
            raise PermissionError(13, "Permission denied")
        return open_(*args, **kwargs)  # type: ignore[call-overload]

    open_ = open
    monkeypatch.setattr("builtins.open", refuse)
    assert main(fix=True) == 1
    monkeypatch.undo()
    assert capsys.readouterr().out.splitlines() == [
        "Doubled spaces fix failed:",
        "- README.md: cannot write (Permission denied)",
    ]


def test_run_reads_config_and_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "pyproject.toml": "[tool.repoutils.doubled-spaces]\nfiles = ['docs.md']\nallow-after-sentence = true\n",
            "docs.md": "One.  Two.\n",
            "README.md": "So  run it.\n",
        },
    )
    assert run([]) == 0
    assert run(["--no-allow-after-sentence"]) == 1
    assert run(["README.md"]) == 1
    assert "README.md:1: So  run it." in capsys.readouterr().out
    assert run(["--fix", "README.md"]) == 0
    assert (tmp_path / "README.md").read_text() == "So run it.\n"


def test_run_rejects_a_bad_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.doubled-spaces]\nallow-after-sentence = 'yes'\n")
    with pytest.raises(ConfigError, match=r"allow-after-sentence must be true or false"):
        run([])


def test_run_rejects_a_named_path_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        run(["missing.md"])
    assert error.value.code == 2
    assert "missing.md is not a file" in capsys.readouterr().err


def test_main_reports_a_path_it_cannot_open(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path)]) == 1
    assert f"- {tmp_path}: cannot read (Is a directory)" in capsys.readouterr().out
