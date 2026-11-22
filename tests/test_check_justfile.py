import json
import re
from pathlib import Path

import pytest

from repoutils import config
from repoutils.commands.check_justfile import Justfile, Settings, main, parse_dump, problems, read_justfile
from repoutils.justfile_standard import BY_NAME, FIX_CHECK_PAIRS, RECIPES

ROOT = Path(__file__).resolve().parent.parent
ALL_FEATURES = Settings(features=frozenset(config.FEATURES))


def dump(
    recipes: dict[str, list[str]], aliases: dict[str, str] | None = None, variables: tuple[str, ...] = ("repoutils",)
) -> str:
    """JSON shaped like the parts of `just --dump --dump-format json` the command reads."""
    return json.dumps(
        {
            "recipes": {
                name: {"name": name, "dependencies": [{"recipe": d, "arguments": []} for d in deps]}
                for name, deps in recipes.items()
            },
            "aliases": {name: {"name": name, "target": target} for name, target in (aliases or {}).items()},
            "assignments": {name: {"name": name} for name in variables},
        }
    )


def standard_recipes() -> dict[str, list[str]]:
    return {recipe.name: list(recipe.dependencies) for recipe in RECIPES}


def test_standard_justfile_passes() -> None:
    assert problems(parse_dump(dump(standard_recipes())), ALL_FEATURES) == ([], [])


def test_standard_names_are_consistent() -> None:
    assert len(BY_NAME) == len(RECIPES)
    for recipe in RECIPES:
        assert recipe.feature is None or recipe.feature in config.FEATURES
        assert set(recipe.dependencies) <= set(BY_NAME)
    for fixer, checker in FIX_CHECK_PAIRS:
        assert {fixer, checker} <= set(BY_NAME)


# A line that starts with a name, but isn't an assignment or a setting, is a recipe's header.
HEADER_RE = re.compile(r"[A-Za-z0-9_-]+")
ASSIGNMENT_RE = re.compile(r"[A-Za-z0-9_-]+\s*:=|(set|alias|eager|export|unexport|import|mod)\s")


def recipe_blocks(text: str) -> list[str]:
    """Each recipe of a justfile, with the comments and attributes right above it."""
    blocks = []
    for block in text.split("\n\n"):
        start = 0
        lines = block.strip("\n").split("\n")
        for index, line in enumerate(lines):
            if HEADER_RE.match(line) and not ASSIGNMENT_RE.match(line):
                end = index + 1
                while end < len(lines) and lines[end].startswith(" "):
                    end += 1
                blocks.append("\n".join(lines[start:end]) + "\n")
                start = end
    return blocks


def test_repoutils_justfile_has_the_standard_text() -> None:
    # Keeps the standard in step with repoutils' own justfile.
    blocks = recipe_blocks((ROOT / "justfile").read_text(encoding="utf-8"))
    settings = Settings.from_config(ROOT)
    for recipe in RECIPES:
        text = settings.text(recipe)
        if recipe.name == "smoke":
            # repoutils' own smoke adds options that run some of its commands.
            assert any(block.startswith(text.rstrip("\n") + " ") for block in blocks)
        else:
            assert text in blocks, recipe.name


def test_recipe_blocks() -> None:
    text = "x := 'a: b'\nset quiet\n\n# Doc\n[private]\na y='c=d': b\n    echo\n# Other\nb:\n    echo\n"
    assert recipe_blocks(text) == ["# Doc\n[private]\na y='c=d': b\n    echo\n", "# Other\nb:\n    echo\n"]


def test_missing_recipe_is_reported_with_its_text() -> None:
    recipes = standard_recipes()
    del recipes["prose-check"]
    recipes["check"].remove("prose-check")
    found, missing = problems(parse_dump(dump(recipes)), ALL_FEATURES)
    assert found == ["There's no `prose-check` recipe or alias.", "`check` doesn't reach `prose-check`."]
    assert missing == [BY_NAME["prose-check"]]


def test_alias_counts_as_the_recipe() -> None:
    recipes = standard_recipes()
    recipes["format-check"] = recipes.pop("fmt-check")
    recipes["check"] = ["format-check" if name == "fmt-check" else name for name in recipes["check"]]
    justfile = parse_dump(dump(recipes, aliases={"fmt-check": "format-check"}))
    assert problems(justfile, ALL_FEATURES) == ([], [])


def test_alias_to_a_module_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "justfile").write_text("mod tools\n\nalias default := tools::list\n\ncheck: tools::list\n")
    (tmp_path / "tools.just").write_text("list: other\n    echo\n\nother:\n    echo\n\nmod deeper\n")
    (tmp_path / "deeper.just").write_text("list:\n    echo\n")
    justfile = read_justfile("just")
    # The dump doesn't say which list the alias means, so it may be either.
    assert justfile.resolve("default") == {"tools::list", "tools::deeper::list"}
    assert justfile.reached("check") == {"tools::list", "tools::other"}
    assert justfile.reaches("check", "default")


def test_alias_whose_target_name_is_also_at_the_top() -> None:
    # alias fmt-check := tools::list, with a top-level list too: check reaches fmt-check
    # through tools::list, and lint-only reaches it through list.
    recipes = {"list": (), "tools::list": (), "check": ("tools::list",), "lint-only": ("list",), "other": ()}
    justfile = Justfile(recipes, {"fmt-check": "list"}, frozenset())
    assert justfile.resolve("fmt-check") == {"list", "tools::list"}
    assert justfile.reaches("check", "fmt-check")
    assert justfile.reaches("lint-only", "fmt-check")
    assert not justfile.reaches("other", "fmt-check")


def test_alias_to_nothing_is_missing() -> None:
    recipes = standard_recipes()
    del recipes["default"]
    justfile = parse_dump(dump(recipes, aliases={"default": "nothing"}))
    assert problems(justfile, ALL_FEATURES)[0] == ["There's no `default` recipe or alias."]


def test_dependencies_are_reached_through_other_recipes() -> None:
    recipes = standard_recipes()
    recipes["types"] = ["mypy", "pyright"]
    recipes["check"] = [name for name in recipes["check"] if name not in ("mypy", "pyright")] + ["types"]
    # A cycle doesn't stop the search.
    recipes["mypy"] = ["types"]
    assert problems(parse_dump(dump(recipes)), ALL_FEATURES) == ([], [])


def test_dependency_through_an_alias() -> None:
    justfile = Justfile({"a": ("x",), "b": ()}, {"x": "b"}, frozenset())
    assert justfile.reached("a") == {"b"}
    assert justfile.resolve("missing") == frozenset()
    assert justfile.reaches("a", "x")
    assert not justfile.reaches("missing", "b")


def test_omitted_recipes_and_dependencies() -> None:
    recipes = standard_recipes()
    del recipes["prose-check"]
    recipes["check"] = [name for name in recipes["check"] if name not in ("prose-check", "pyright")]
    settings = Settings(
        features=frozenset(config.FEATURES),
        omit=frozenset({"prose-check"}),
        omit_dependencies=(("check", frozenset({"pyright"})),),
    )
    assert problems(parse_dump(dump(recipes)), settings) == ([], [])


def test_features_decide_the_standard() -> None:
    recipes = standard_recipes()
    for name in ("build", "check-attestations", "smoke", "check-release", "secrets-export-test"):
        del recipes[name]
    recipes["ci"] = ["lock-check", "sync", "check", "coverage"]
    justfile = parse_dump(dump(recipes))
    assert problems(justfile, Settings()) == ([], [])
    found, missing = problems(justfile, Settings(features=frozenset({"pypi-publication"})))
    assert [recipe.name for recipe in missing] == ["build", "check-attestations", "smoke", "check-release"]
    assert "`ci` doesn't reach `smoke`." in found


def test_check_must_check_what_fix_fixes() -> None:
    recipes = standard_recipes()
    recipes["fix"].append("requirements-fix")
    recipes["check"].remove("requirements-check")
    # Omitting the dependency doesn't excuse it, as fix reaches requirements-fix.
    settings = Settings(
        features=frozenset(config.FEATURES), omit_dependencies=(("check", frozenset({"requirements-check"})),)
    )
    assert problems(parse_dump(dump(recipes)), settings)[0] == [
        "`fix` reaches `requirements-fix`, so `check` must reach `requirements-check`."
    ]
    # Without the omission, it's reported once.
    assert problems(parse_dump(dump(recipes)), ALL_FEATURES)[0] == ["`check` doesn't reach `requirements-check`."]


def test_missing_variable() -> None:
    justfile = parse_dump(dump(standard_recipes(), variables=("other",)))
    assert problems(justfile, ALL_FEATURES)[0] == ["There's no `repoutils` variable."]


def test_text() -> None:
    settings = Settings(default_branch="master")
    header = "patch-coverage base='origin/master': coverage (diff-cover base)\n"
    assert header in settings.text(BY_NAME["patch-coverage"])
    assert settings.text(BY_NAME["ci"]).endswith("\nci: lock-check sync check coverage\n")
    assert ALL_FEATURES.text(BY_NAME["ci"]).endswith("\nci: lock-check sync check coverage build smoke\n")
    assert Settings(omit=frozenset({"pyright"})).text(BY_NAME["check"]).count("pyright") == 0
    # An omitted dependency isn't in the header, even one that's passed arguments.
    omitted = Settings(omit=frozenset({"coverage"}), omit_dependencies=(("patch-coverage", frozenset({"diff-cover"})),))
    assert omitted.text(BY_NAME["patch-coverage"]).endswith("\npatch-coverage base='origin/main':\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "not JSON"),
        ("[]", "not a JSON object"),
        ('{"aliases": {}, "assignments": {}}', "'recipes' isn't an object"),
        ('{"recipes": {"a": {}}, "aliases": {}, "assignments": {}}', "recipe 'a' has no list of dependencies"),
        ('{"recipes": {"a": {"dependencies": [{}]}}}', "recipe 'a' has no list of dependencies"),
        ('{"recipes": {}, "aliases": {"a": 1}, "assignments": {}}', "alias 'a' has no target"),
        ('{"recipes": {}, "aliases": {}, "assignments": []}', "'assignments' isn't an object"),
        ('{"recipes": {}, "modules": {"m": 1}}', "module 'm' isn't an object"),
        ('{"recipes": {}, "modules": {"m": {"recipes": {"a": {}}}}}', "recipe 'm::a' has no list of dependencies"),
    ],
)
def test_parse_dump_rejects(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_dump(text)


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        ("[tool.repoutils]\nfeatures = ['pypi']\n", "[tool.repoutils] features has unknown 'pypi'"),
        ("[tool.repoutils]\nfeatures = 'pypi-publication'\n", "[tool.repoutils] features must be a list of strings"),
        ("[tool.repoutils.justfile]\nomit = ['nope']\n", "omit has 'nope', which aren't standard recipes"),
        ("[tool.repoutils.justfile]\nomit-dependencies = 1\n", "omit-dependencies must be a table"),
        (
            "[tool.repoutils.justfile]\nomit-dependencies = { lint = ['x'] }\n",
            "has 'lint', which isn't a standard recipe with dependencies",
        ),
        (
            "[tool.repoutils.justfile]\nomit-dependencies = { check = ['build'] }\n",
            "check has 'build', which aren't standard dependencies of check",
        ),
        (
            "[tool.repoutils.justfile]\nomit-dependencies = { check = 'pyright' }\n",
            "omit-dependencies check must be a list of strings",
        ),
    ],
)
def test_bad_settings(tmp_path: Path, config_text: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    with pytest.raises(config.ConfigError, match=re.escape(message)):
        Settings.from_config(tmp_path)


def test_settings_from_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.repoutils]\nfeatures = ['secrets-export']\ndefault-branch = 'master'\n"
        "[tool.repoutils.justfile]\nomit = ['prose-check']\nomit-dependencies = { check = ['pyright'] }\n"
    )
    assert Settings.from_config(tmp_path) == Settings(
        frozenset({"secrets-export"}),
        frozenset({"prose-check"}),
        (("check", frozenset({"pyright"})),),
        "master",
    )


def fake_just(tmp_path: Path, script: str) -> str:
    path = tmp_path / "just"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("exit_zero", [False, True])
def test_main_reports_and_prints_missing_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exit_zero: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    recipes = standard_recipes()
    del recipes["sync"]
    (tmp_path / "dump.json").write_text(dump(recipes))
    just = fake_just(tmp_path, f"cat {tmp_path / 'dump.json'}")
    assert main(just, exit_zero) == (0 if exit_zero else 1)
    out = capsys.readouterr().out
    assert "- There's no `sync` recipe or alias.\n" in out
    assert "\n# Sync dependencies (including dev group)\nsync:\n    uv sync --group dev\n" in out
    assert "omit-dependencies" in out


@pytest.mark.parametrize("exit_zero", [False, True])
@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("echo oops >&2; exit 3", "failed (exit 3): oops"),
        ("echo '{'", "can't read the output of `"),
        (None, "can't run "),
    ],
)
def test_main_fails_when_just_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str | None,
    message: str,
    exit_zero: bool,
) -> None:
    # --exit-zero doesn't forgive a justfile the command couldn't read.
    monkeypatch.chdir(tmp_path)
    just = str(tmp_path / "missing") if script is None else fake_just(tmp_path, script)
    assert main(just, exit_zero) == 1
    assert message in capsys.readouterr().err


def test_dump_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # just --dump evaluates no assignment, eager and backtick ones included.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "justfile").write_text(
        "repoutils := 'x'\neager _ := shell('touch eager')\nlater := `touch backtick`\n\n"
        "# Check\ncheck: fmt-check\n\nfmt-check:\n    touch body\n\nalias c := check\n"
    )
    assert main() == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["justfile"]


def test_main_prints_no_text_when_no_recipe_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dump.json").write_text(dump(standard_recipes(), variables=()))
    assert main(fake_just(tmp_path, f"cat {tmp_path / 'dump.json'}")) == 1
    out = capsys.readouterr().out
    assert "- There's no `repoutils` variable.\n" in out
    assert "standard text" not in out


def test_main_checks_the_justfile_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only the justfile given has a default recipe, and the current directory has no justfile.
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "my.just").write_text("repoutils := 'x'\n\ndefault:\n    echo\n")
    monkeypatch.chdir(tmp_path)
    assert main(justfile_path=str(tmp_path / "other" / "my.just"), exit_zero=True) == 0
    out = capsys.readouterr().out
    assert "There's no `default`" not in out
    assert "There's no `sync` recipe or alias." in out
