"""Check that the justfile has the standard recipes, and that check, fix, ci and patch-coverage reach the standard ones.

It reads the justfile in the current directory (or the one ``--justfile``
names) with ``just --dump --dump-format json``, which evaluates no assignment, not even ``shell()`` or
an ``eager`` one, so it runs nothing from the justfile. That output includes
the recipes of any ``import``. It checks that:

- every standard recipe exists, as a recipe or an alias.
- ``check``, ``fix``, ``ci`` and ``patch-coverage`` reach their standard
  dependencies, directly or through other recipes.
- if ``fix`` reaches a recipe that fixes something, such as ``fmt``,
  ``check`` reaches the recipe that checks it, such as ``fmt-check``.
- a ``repoutils`` variable exists, whatever its value.

It never compares a recipe's body or doc comment with the standard one, as a
project may have a reason to change them. For each missing recipe, it prints
the standard text, ready to paste.

Which recipes are standard depends on the features in ``[tool.repoutils]
features``: ``pypi-publication`` adds ``build``, ``check-attestations``,
``smoke`` and ``check-release`` (and ``ci``'s dependencies on ``build`` and
``smoke``), and ``secrets-export`` adds ``secrets-export-test``.
``[tool.repoutils.justfile]`` lists the standard recipes (``omit``) and
dependencies (``omit-dependencies``) the project leaves out on purpose.

With ``--exit-zero``, it reports what breaks the standard but exits 0. It
still exits 1 if it can't run ``just`` or read its output, or if a setting is
bad.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import config
from repoutils.docstrings import summary
from repoutils.justfile_standard import BRANCH, BY_NAME, FIX_CHECK_PAIRS, RECIPES, VARIABLE, Recipe

WHERE = "[tool.repoutils.justfile]"


@dataclass(frozen=True)
class Justfile:
    """What the command reads from ``just --dump``: each recipe's dependencies, the aliases and the variables."""

    recipes: dict[str, tuple[str, ...]]
    aliases: dict[str, str]
    variables: frozenset[str]

    def resolve(self, name: str) -> str | None:
        """The recipe that name is, or is an alias of, or None if there's neither."""
        if name in self.recipes:
            return name
        target = self.aliases.get(name)
        return target if target in self.recipes else None

    def reached(self, name: str) -> set[str]:
        """The recipes that the recipe name runs first or after, directly or through others."""
        seen: set[str] = set()
        start = self.resolve(name)
        todo = list(self.recipes[start]) if start else []
        while todo:
            recipe = self.resolve(todo.pop())
            if recipe is not None and recipe not in seen:
                seen.add(recipe)
                todo.extend(self.recipes[recipe])
        return seen

    def reaches(self, name: str, dependency: str) -> bool:
        target = self.resolve(dependency)
        return target is not None and target in self.reached(name)


def _table(dump: dict[str, Any], key: str) -> dict[str, Any]:
    value = dump.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key!r} isn't an object")
    return value


def parse_dump(text: str) -> Justfile:
    """The Justfile in the JSON of ``just --dump --dump-format json``, raising ValueError if it isn't that."""
    try:
        dump = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON: {error}") from None
    if not isinstance(dump, dict):
        raise ValueError("not a JSON object")
    recipes: dict[str, tuple[str, ...]] = {}
    for name, recipe in _table(dump, "recipes").items():
        dependencies = recipe.get("dependencies") if isinstance(recipe, dict) else None
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, dict) and isinstance(dependency.get("recipe"), str) for dependency in dependencies
        ):
            raise ValueError(f"recipe {name!r} has no list of dependencies")
        recipes[name] = tuple(dependency["recipe"] for dependency in dependencies)
    aliases: dict[str, str] = {}
    for name, alias in _table(dump, "aliases").items():
        target = alias.get("target") if isinstance(alias, dict) else None
        if not isinstance(target, str):
            raise ValueError(f"alias {name!r} has no target")
        aliases[name] = target
    return Justfile(recipes, aliases, frozenset(_table(dump, "assignments")))


def read_justfile(just: str, path: str | None = None) -> Justfile:
    """The justfile at path, or else the one ``just`` finds from the current directory, as ``just --dump`` gives it,
    raising RuntimeError if that fails."""
    command = [just, *(["--justfile", path] if path else []), "--dump", "--dump-format", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RuntimeError(f"can't run {just}: {error}") from None
    if result.returncode != 0:
        raise RuntimeError(f"`{' '.join(command)}` failed (exit {result.returncode}): {result.stderr.strip()}")
    try:
        return parse_dump(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"can't read the output of `{' '.join(command)}`: {error}") from None


@dataclass(frozen=True)
class Settings:
    features: frozenset[str] = frozenset()
    omit: frozenset[str] = frozenset()
    omit_dependencies: tuple[tuple[str, frozenset[str]], ...] = ()
    default_branch: str = "main"

    @classmethod
    def from_config(cls, root: Path = Path()) -> "Settings":
        table = config.section("justfile", root)
        omit = config.string_list(table, "omit", [], WHERE)
        unknown = sorted(set(omit) - set(BY_NAME))
        if unknown:
            raise config.ConfigError(f"{WHERE} omit has {', '.join(map(repr, unknown))}, which aren't standard recipes")
        value = table.get("omit-dependencies", {})
        if not isinstance(value, dict):
            raise config.ConfigError(f"{WHERE} omit-dependencies must be a table")
        omit_dependencies: list[tuple[str, frozenset[str]]] = []
        for name in value:
            where = f"{WHERE} omit-dependencies"
            recipe = BY_NAME.get(name)
            if recipe is None or not recipe.dependencies:
                raise config.ConfigError(f"{where} has {name!r}, which isn't a standard recipe with dependencies")
            dependencies = config.string_list(value, name, [], where)
            unknown = sorted(set(dependencies) - set(recipe.dependencies))
            if unknown:
                raise config.ConfigError(
                    f"{where} {name} has {', '.join(map(repr, unknown))}, which aren't standard dependencies of {name}"
                )
            omit_dependencies.append((name, frozenset(dependencies)))
        return cls(config.features(root), frozenset(omit), tuple(omit_dependencies), config.default_branch(root))

    def standard(self) -> list[Recipe]:
        """The standard recipes for the project's features, but for those it omits."""
        return [
            recipe
            for recipe in RECIPES
            if (recipe.feature is None or recipe.feature in self.features) and recipe.name not in self.omit
        ]

    def dependencies(self, recipe: Recipe) -> list[str]:
        """The standard dependencies that recipe must reach in this project."""
        names = {standard.name for standard in self.standard()}
        omitted = dict(self.omit_dependencies).get(recipe.name, frozenset())
        return [name for name in recipe.dependencies if name in names and name not in omitted]

    def text(self, recipe: Recipe) -> str:
        """recipe's standard text, for this project."""
        text = recipe.text.replace(BRANCH, self.default_branch)
        if recipe.aggregate:
            text += f"{recipe.name}: {' '.join(self.dependencies(recipe))}\n"
        return text


def problems(justfile: Justfile, settings: Settings) -> tuple[list[str], list[Recipe]]:
    """What in the justfile breaks the standard, and the standard recipes it lacks."""
    found: list[str] = []
    missing: list[Recipe] = []
    standard = settings.standard()
    for recipe in standard:
        if justfile.resolve(recipe.name) is None:
            found.append(f"There's no `{recipe.name}` recipe or alias.")
            missing.append(recipe)
    unreached: set[tuple[str, str]] = set()
    for recipe in standard:
        if justfile.resolve(recipe.name) is None:
            continue
        for dependency in settings.dependencies(recipe):
            if not justfile.reaches(recipe.name, dependency):
                found.append(f"`{recipe.name}` doesn't reach `{dependency}`.")
                unreached.add((recipe.name, dependency))
    if justfile.resolve("fix") is not None and justfile.resolve("check") is not None:
        for fixer, checker in FIX_CHECK_PAIRS:
            if (
                justfile.reaches("fix", fixer)
                and ("check", checker) not in unreached
                and not justfile.reaches("check", checker)
            ):
                found.append(f"`fix` reaches `{fixer}`, so `check` must reach `{checker}`.")
    if VARIABLE not in justfile.variables:
        found.append(f"There's no `{VARIABLE}` variable.")
    return found, missing


def main(just: str = "just", exit_zero: bool = False, justfile_path: str | None = None) -> int:
    settings = Settings.from_config()
    try:
        justfile = read_justfile(just, justfile_path)
    except RuntimeError as error:
        print(f"repoutils check-justfile: {error}", file=sys.stderr)
        return 1
    found, missing = problems(justfile, settings)
    if not found:
        print("The justfile has the standard recipes and dependencies.")
        return 0
    print("The justfile breaks the standard:")
    for problem in found:
        print(f"- {problem}")
    if missing:
        print("\nThe standard text of the missing recipes, to paste into the justfile:")
        for recipe in missing:
            print(f"\n{settings.text(recipe)}", end="")
    print(
        f"\nList a standard recipe the project leaves out on purpose in {WHERE} omit,"
        " and a standard dependency in omit-dependencies."
    )
    return 0 if exit_zero else 1


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils check-justfile", description=summary(__doc__))
    parser.add_argument("--just", default="just", help="the just to run (default: just, from the PATH)")
    parser.add_argument(
        "--justfile", help="the justfile to check (default: the one just finds from the current directory)"
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="report, but exit 0 even if the justfile breaks the standard (not if it can't be read)",
    )
    args = parser.parse_args(argv)
    return main(args.just, args.exit_zero, args.justfile)
