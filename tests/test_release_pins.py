import tomllib
from pathlib import Path

import pytest

from repoutils import release_pins

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def test_missing_file_pins_nothing(tmp_path: Path) -> None:
    assert release_pins.read(tmp_path / "pins.toml", "o/r") == release_pins.Pins("o/r")


def test_round_trip(tmp_path: Path) -> None:
    pins = release_pins.Pins("o/r")
    release_pins.add(pins, "1.0.0", {"z.vsix": A, "a.vsix": B})
    release_pins.add(pins, "v1.1", {"a.vsix": A})
    text = release_pins.render(pins)
    assert text == (
        f"{release_pins.HEADER}\n"
        'repo = "o/r"\n'
        'current = "v1.1"\n'
        "\n"
        '[releases."v1.1"]\n'
        f'"a.vsix" = "{A}"\n'
        "\n"
        '[releases."1.0.0"]\n'
        f'"a.vsix" = "{B}"\n'
        f'"z.vsix" = "{A}"\n'
    )
    path = tmp_path / "pins.toml"
    path.write_text(text)
    assert release_pins.read(path, "o/r") == pins
    assert tomllib.loads(text)["releases"]["v1.1"] == {"a.vsix": A}


def test_add_moves_a_tag_already_pinned_to_the_front() -> None:
    pins = release_pins.Pins("o/r")
    release_pins.add(pins, "1", {"a": A})
    release_pins.add(pins, "2", {"a": B})
    release_pins.add(pins, "1", {"a": A})
    assert list(pins.releases) == ["1", "2"]
    assert pins.current == "1"


def test_render_needs_a_current_release() -> None:
    with pytest.raises(ValueError, match="needs a current release"):
        release_pins.render(release_pins.Pins("o/r"))


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("repo = ", "pins.toml: "),
        (b"\xff", "not valid UTF-8"),
        ('repo = "o/r"\nextra = 1\n', "unknown key\\(s\\): extra"),
        ('repo = "x/y"\n', "pins the releases of 'x/y', not 'o/r'"),
        ('repo = "o/r"\nreleases = 1\n', "releases must be a table"),
        ('repo = "o/r"\ncurrent = "1"\n[releases.1]\n', "releases.1 must be a table of asset names"),
        ('repo = "o/r"\ncurrent = "1"\n[releases.1]\na = "md5:x"\n', "digest of a in releases.1 must be sha256:"),
        (f'repo = "o/r"\ncurrent = "2"\n[releases.1]\na = "{A}"\n', "current must be the tag of one of"),
        (f'repo = "o/r"\n[releases.1]\na = "{A}"\n', "current must say which"),
    ],
)
def test_read_refuses(tmp_path: Path, text: str | bytes, error: str) -> None:
    path = tmp_path / "pins.toml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    with pytest.raises(ValueError, match=error):
        release_pins.read(path, "o/r")
