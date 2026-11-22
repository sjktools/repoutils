"""The standard justfile recipes that ``repoutils check-justfile`` checks for.

Each recipe's text is the doc comment, attributes and body that the command
prints for a project to paste in when the recipe is missing. The command never
compares a recipe's text with the project's, as a project may have a reason to
change it. In the text, ``@default-branch@`` stands for the project's
``default-branch``. An aggregate recipe's text has no header line: the command
writes ``<name><parameters>: <dependencies>`` after it, with only the dependencies the
project's features and settings ask for.

A test checks that repoutils' own justfile has every recipe's text, so the
standard and that justfile stay in step.
"""

from dataclasses import dataclass

BRANCH = "@default-branch@"


@dataclass(frozen=True)
class Recipe:
    name: str
    text: str
    # The feature in [tool.repoutils] features that makes the recipe standard, or None if it always is.
    feature: str | None = None
    # The standard recipes it must reach, directly or through other recipes.
    dependencies: tuple[str, ...] = ()
    # Whether the text leaves out the header line, which is `<name><parameters>: <dependencies>`.
    aggregate: bool = False
    parameters: str = ""
    # How the header writes a dependency that it passes arguments, such as `(diff-cover base)`.
    calls: tuple[tuple[str, str], ...] = ()


def _recipe(name: str, text: str, feature: str | None = None) -> Recipe:
    return Recipe(name, text.removeprefix("\n"), feature)


def _aggregate(
    name: str, doc: str, dependencies: str, parameters: str = "", calls: tuple[tuple[str, str], ...] = ()
) -> Recipe:
    return Recipe(name, doc + "\n", None, tuple(dependencies.split()), True, parameters, calls)


RECIPES: tuple[Recipe, ...] = (
    _recipe(
        "default",
        """
# List available recipes
default:
    @just --list
""",
    ),
    _recipe(
        "lock-check",
        """
# Check that uv.lock is up to date with pyproject.toml
lock-check:
    uv lock --check
""",
    ),
    _recipe(
        "sync",
        """
# Sync dependencies (including dev group)
sync:
    uv sync --group dev
""",
    ),
    _recipe(
        "test",
        """
# Run the test suite
test:
    uv run --group dev pytest -q
""",
    ),
    _recipe(
        "coverage",
        """
# Run the test suite with coverage, printing a report and writing coverage.xml
coverage *args:
    uv run --group dev pytest -q --cov --cov-report=term-missing --cov-report=xml {{ args }}
""",
    ),
    _aggregate(
        "patch-coverage",
        "# Run coverage, then report coverage of the lines changed since base",
        "coverage diff-cover",
        f" base='origin/{BRANCH}'",
        (("diff-cover", "(diff-cover base)"),),
    ),
    _recipe(
        "diff-cover",
        f"""
# Report coverage of the lines changed since base from an existing coverage.xml (extra args go to diff-cover)
diff-cover base='origin/{BRANCH}' *args:
    uv run --group dev diff-cover coverage.xml --compare-branch={{{{ base }}}} --branch-coverage {{{{ args }}}}
""",
    ),
    _recipe(
        "patch-coverage-post",
        """
# Post a patch coverage report (default patch-coverage.md) to a PR as a comment, or update the existing one (--output <file> writes it there instead)
[positional-arguments]
patch-coverage-post *args:
    {{ repoutils }} post-patch-coverage "$@"
""",
    ),
    _recipe(
        "coverage-status-post",
        """
# Post the coverage in coverage.xml (and --patch-report) as commit statuses on a commit (argument: its SHA)
[positional-arguments]
coverage-status-post *args:
    {{ repoutils }} post-coverage-status "$@"
""",
    ),
    _recipe(
        "wait-for-base-coverage",
        """
# Wait (up to 10 minutes) for a PR base commit's coverage/project status: arguments are owner/repo and the base SHA
[positional-arguments]
wait-for-base-coverage *args:
    {{ repoutils }} wait-for-base-coverage "$@"
""",
    ),
    _recipe(
        "long-files",
        """
# List files over the line limit as Markdown, or post that to a PR (--post <PR>)
[positional-arguments]
long-files *args:
    {{ repoutils }} long-files "$@"
""",
    ),
    _recipe(
        "added-strings",
        """
# List the configured strings the lines added since a base have, or post that to a PR (--post <PR>)
[positional-arguments]
added-strings *args:
    {{ repoutils }} added-strings "$@"
""",
    ),
    _recipe(
        "build",
        """
# Build the sdist and wheel into dist/
build:
    uv build
""",
        "pypi-publication",
    ),
    _recipe(
        "check-attestations",
        """
# Check that every distribution in dist/ (or the directory given) has a PEP 740 publish attestation
[positional-arguments]
check-attestations dist="dist":
    {{ repoutils }} check-attestations "$1"
""",
        "pypi-publication",
    ),
    # repoutils' own smoke recipe adds options that run some of its commands.
    _recipe(
        "smoke",
        """
# Install the wheel `just build` built into a fresh environment and check that it runs and records this checkout's commit
smoke:
    {{ repoutils }} check-install --wheel dist --version-line
""",
        "pypi-publication",
    ),
    _recipe(
        "check-release",
        """
# Retries while the index catches up with a new upload. --version must also
# give a 12-hex commit after the version (and a dev build's bumped version
# counts as an uncommitted change).
# Check that a released version installs from PyPI (or the index given) and runs
[positional-arguments]
check-release version index="https://pypi.org/simple/":
    {{ repoutils }} check-install --version "$1" --index "$2" --version-line
""",
        "pypi-publication",
    ),
    _recipe(
        "lint",
        """
# Lint with ruff, failing on any warning too, as ruff only warns about such
# things as an invalid noqa directive (see `repoutils ruff-check --help`)
lint:
    uv run --group dev repoutils ruff-check .
""",
    ),
    _recipe(
        "lint-fix",
        """
# Lint with ruff, applying its autofixes
lint-fix:
    uv run --group dev ruff check --fix .
""",
    ),
    _recipe(
        "fmt",
        """
# Format code with ruff
fmt:
    uv run --group dev ruff format .
""",
    ),
    _recipe(
        "fmt-check",
        """
# Check code is formatted, without changing it
fmt-check:
    uv run --group dev ruff format --check .
""",
    ),
    _recipe(
        "just-fmt",
        """
# Format justfile using just's built-in formatter
just-fmt:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt
""",
    ),
    _recipe(
        "just-fmt-check",
        """
# Check that the justfile is formatted according to just's built-in formatter
just-fmt-check:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt --check
""",
    ),
    _recipe(
        "mypy",
        """
# Type-check src and tests with mypy (strict; see pyproject.toml's [tool.mypy])
mypy:
    uv run --group dev mypy
""",
    ),
    _recipe(
        "pyright",
        """
# Type-check src and tests with pyright (strict; see pyproject.toml's [tool.pyright])
pyright:
    uv run --group dev pyright
""",
    ),
    _recipe(
        "lint-workflows",
        """
# Lint the GitHub Actions workflows with jactionlint
lint-workflows:
    uv run --group dev jactionlint
""",
    ),
    _recipe(
        "whitespace-check",
        """
# Check Git-tracked text files for trailing whitespace and bad newlines
whitespace-check:
    uv run repoutils check-whitespace
""",
    ),
    _recipe(
        "sentence-per-line-check",
        """
# Check AGENTS.md and README.md stay one sentence per line
sentence-per-line-check:
    uv run repoutils check-sentence-per-line
""",
    ),
    _recipe(
        "prose-check",
        """
# Check AGENTS.md and README.md prose for long, clause-heavy sentences, or too many short ones
prose-check:
    uv run repoutils check-prose
""",
    ),
    _recipe(
        "check-pins",
        """
# Check that every workflow and local action pins the uv version pyproject.toml requires and the just version the justfile requires
check-pins:
    uv run repoutils check-pins
""",
    ),
    _recipe(
        "check-checkout-credentials",
        """
# Check that every actions/checkout step sets persist-credentials, so none keeps its token by default
check-checkout-credentials:
    uv run repoutils check-checkout-credentials
""",
    ),
    _recipe(
        "secrets-workflows-check",
        """
# Check that the workflows use exactly the secrets pyproject.toml lists
secrets-workflows-check:
    uv run repoutils secrets check-workflows
""",
    ),
    _recipe(
        "variables-workflows-check",
        """
# Check that the workflows use exactly the variables pyproject.toml lists
variables-workflows-check:
    uv run repoutils variables check-workflows
""",
    ),
    _recipe(
        "check-justfile",
        """
# Check that the justfile has the standard recipes, and that check, fix, ci and patch-coverage reach the standard ones
check-justfile:
    uv run repoutils check-justfile --just {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }}
""",
    ),
    _recipe(
        "secrets-export-test",
        """
# Check `secrets export` round-trips made-up secrets through age (on the PATH), also encrypting to the GitHub key given as USER/SHA256:FINGERPRINT
secrets-export-test $SECRETS_EXPORT_KEY='':
    {{ repoutils }} secrets export --self-test ${SECRETS_EXPORT_KEY:+--github-key "$SECRETS_EXPORT_KEY"}
""",
        "secrets-export",
    ),
    _recipe(
        "lock-platforms-check",
        """
# Check that uv.lock only has wheels for supported OS/CPU combinations
lock-platforms-check:
    uv run repoutils check-lock-platforms
""",
    ),
    _recipe(
        "requirements-check",
        """
# Check that dependency minimums in pyproject.toml match uv.lock
requirements-check:
    {{ repoutils }} requirements-min-versions check
""",
    ),
    _recipe(
        "requirements-fix",
        """
# Rewrite dependency minimums in pyproject.toml to the versions in uv.lock
requirements-fix:
    {{ repoutils }} requirements-min-versions fix
""",
    ),
    _recipe(
        "dependabot-relock",
        """
# Re-lock a Dependabot branch's uv.lock with this uv, from the base ref's uv.lock
dependabot-relock base:
    {{ repoutils }} dependabot-relock {{ base }}
""",
    ),
    _aggregate(
        "check",
        "# Run all checks, without fixing anything or running tests",
        "lint fmt-check just-fmt-check mypy pyright lint-workflows actions-lock-check whitespace-check"
        " sentence-per-line-check prose-check check-pins check-checkout-credentials secrets-workflows-check"
        " variables-workflows-check check-justfile lock-platforms-check requirements-check",
    ),
    _recipe(
        "actions-lock-check",
        """
# Check .github/workflows/actions.lock pins every action the workflows use, transitive ones included, to a commit that still verifies upstream (needs gh, logged in, with the github/gh-actions-lock extension)
actions-lock-check:
    gh actions-lock --verify
""",
    ),
    _recipe(
        "actions-lock",
        """
# Pin every action the workflows use in .github/workflows/actions.lock (needs gh, logged in, with the github/gh-actions-lock extension)
actions-lock:
    gh actions-lock
""",
    ),
    _aggregate(
        "fix", "# Reformat code, autofix lint issues and update actions.lock", "fmt just-fmt lint-fix actions-lock"
    ),
    _aggregate(
        "ci",
        "# Run everything CI's build, test and coverage jobs run (except the PR-only patch coverage),"
        " on this machine's Python and OS",
        "lock-check sync check coverage build smoke",
    ),
)

BY_NAME = {recipe.name: recipe for recipe in RECIPES}

# If `fix` reaches the first recipe of a pair, `check` must reach the second,
# so that whatever `just fix` fixes, `just check` checks.
FIX_CHECK_PAIRS = (
    ("fmt", "fmt-check"),
    ("just-fmt", "just-fmt-check"),
    ("lint-fix", "lint"),
    ("actions-lock", "actions-lock-check"),
    ("requirements-fix", "requirements-check"),
)

# The variable that the standard recipes run repoutils with.
VARIABLE = "repoutils"
