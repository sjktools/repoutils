set minimum-version := '1.58.0'

# Runs repoutils from this checkout's source with nothing installed (it needs
# only the standard library), for recipes CI runs with a write-capable token or
# on an untrusted Dependabot branch: `uv run` without --no-project would build
# and install the project, running its build backend, alongside the token.
repoutils := "PYTHONPATH=src uv run --no-project python -m repoutils"

# Dummy assignment, so every recipe run sets up the repo's local Git config
# (see `repoutils git-config-setup`). `eager` makes just evaluate it even though
# nothing uses it, whether or not `set lazy` is on (casey/just#3759).
eager _ := shell(repoutils + ' git-config-setup')

# List available recipes
default:
    @just --list

# Check that uv.lock is up to date with pyproject.toml
lock-check:
    uv lock --check

# Sync dependencies (including dev group)
sync:
    uv sync --group dev

# Run the test suite
test:
    uv run --group dev pytest -q

# Run the test suite with coverage, printing a report and writing coverage.xml
coverage *args:
    uv run --group dev pytest -q --cov --cov-report=term-missing --cov-report=xml {{ args }}

# Run coverage, then report coverage of the lines changed since base
patch-coverage base='origin/main': coverage (diff-cover base)

# Report coverage of the lines changed since base from an existing coverage.xml (extra args go to diff-cover)
diff-cover base='origin/main' *args:
    uv run --group dev diff-cover coverage.xml --compare-branch={{ base }} --branch-coverage {{ args }}

# The posting recipes run with a write-capable token in CI, so they use
# the repoutils variable above, installing nothing.

# Post a patch coverage report (default patch-coverage.md) to a PR as a comment, or update the existing one (--output <file> writes it there instead)
[positional-arguments]
patch-coverage-post *args:
    {{ repoutils }} post-patch-coverage "$@"

# Post the coverage in coverage.xml (and --patch-report) as commit statuses on a commit (argument: its SHA)
[positional-arguments]
coverage-status-post *args:
    {{ repoutils }} post-coverage-status "$@"

# Wait (up to 10 minutes) for a PR base commit's coverage/project status: arguments are owner/repo and the base SHA
[positional-arguments]
wait-for-base-coverage *args:
    {{ repoutils }} wait-for-base-coverage "$@"

# List files over the line limit as Markdown, or post that to a PR (--post <PR>)
[positional-arguments]
long-files *args:
    {{ repoutils }} long-files "$@"

# List the configured strings the lines added since a base have, or post that to a PR (--post <PR>)
[positional-arguments]
added-strings *args:
    {{ repoutils }} added-strings "$@"

# Build the sdist and wheel into dist/
build:
    uv build

# The publish jobs run this between attest-action and uv publish, which would
# otherwise upload a file without its attestation. It installs nothing, as
# they run it with id-token: write.

# Check that every distribution in dist/ (or the directory given) has a PEP 740 publish attestation
[positional-arguments]
check-attestations dist="dist":
    {{ repoutils }} check-attestations "$1"

# Install the wheel `just build` built into a fresh environment and check that it runs and records this checkout's commit
smoke:
    {{ repoutils }} check-install --wheel dist --version-line --run-with-commit version --run=--help --run check-whitespace

# Retries while the index catches up with a new upload. --version must also
# give a 12-hex commit after the version (and a dev build's bumped version
# counts as an uncommitted change).
# Check that a released version installs from PyPI (or the index given) and runs
[positional-arguments]
check-release version index="https://pypi.org/simple/":
    {{ repoutils }} check-install --version "$1" --index "$2" --version-line

# Lint with ruff, failing on any warning too, as ruff only warns about such
# things as an invalid noqa directive. --no-cache, as ruff gives those only for
# files it reparses. uv's --quiet keeps uv's own warnings out of the check.
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    stderr="$(mktemp)"
    trap 'rm -f "$stderr"' EXIT
    status=0
    uv run --quiet --group dev ruff check --no-cache . 2>"$stderr" || status=$?
    cat "$stderr" >&2
    if grep -q '^warning:' "$stderr"; then
        echo 'ruff gave a warning (see above)' >&2
        exit 1
    fi
    exit "$status"

# Lint with ruff, applying its autofixes
lint-fix:
    uv run --group dev ruff check --fix .

# Format code with ruff
fmt:
    uv run --group dev ruff format .

# Check code is formatted, without changing it
fmt-check:
    uv run --group dev ruff format --check .

# Format justfile using just's built-in formatter
just-fmt:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt

# Check that the justfile is formatted according to just's built-in formatter
just-fmt-check:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt --check

# Type-check src and tests with mypy (strict; see pyproject.toml's [tool.mypy])
mypy:
    uv run --group dev mypy

# Type-check src and tests with pyright (strict; see pyproject.toml's [tool.pyright])
pyright:
    uv run --group dev pyright

# Lint the GitHub Actions workflows with jactionlint
lint-workflows:
    uv run --group dev jactionlint

# repoutils' own checks, run from this checkout, so CI dogfoods them.

# Check Git-tracked text files for trailing whitespace and bad newlines
whitespace-check:
    uv run repoutils check-whitespace

# Check AGENTS.md and README.md stay one sentence per line
sentence-per-line-check:
    uv run repoutils check-sentence-per-line

# Check AGENTS.md and README.md prose for long, clause-heavy sentences, or too many short ones
prose-check:
    uv run repoutils check-prose

# Check that every workflow and local action pins the uv version pyproject.toml requires and the just version the justfile requires
check-pins:
    uv run repoutils check-pins

# Check that every actions/checkout step sets persist-credentials, so none keeps its token by default
check-checkout-credentials:
    uv run repoutils check-checkout-credentials

# Check that the workflows use exactly the secrets pyproject.toml lists
secrets-workflows-check:
    uv run repoutils secrets check-workflows

# Check that the workflows use exactly the variables pyproject.toml lists
variables-workflows-check:
    uv run repoutils variables check-workflows

# Check `secrets export` round-trips made-up secrets through age (on the PATH), also encrypting to the GitHub key given as USER/SHA256:FINGERPRINT
secrets-export-test $SECRETS_EXPORT_KEY='':
    {{ repoutils }} secrets export --self-test ${SECRETS_EXPORT_KEY:+--github-key "$SECRETS_EXPORT_KEY"}

# Check that uv.lock only has wheels for supported OS/CPU combinations
lock-platforms-check:
    uv run repoutils check-lock-platforms

# Check that dependency minimums in pyproject.toml match uv.lock
requirements-check:
    {{ repoutils }} requirements-min-versions check

# Rewrite dependency minimums in pyproject.toml to the versions in uv.lock
requirements-fix:
    {{ repoutils }} requirements-min-versions fix

# Re-lock a Dependabot branch's uv.lock with this uv, from the base ref's uv.lock
dependabot-relock base:
    {{ repoutils }} dependabot-relock {{ base }}

# Run all checks, without fixing anything or running tests
check: lint fmt-check just-fmt-check mypy pyright lint-workflows actions-lock-check whitespace-check sentence-per-line-check prose-check check-pins check-checkout-credentials secrets-workflows-check variables-workflows-check lock-platforms-check requirements-check

# Check .github/workflows/actions.lock pins every action the workflows use, transitive ones included, to a commit that still verifies upstream (needs gh, logged in, with the github/gh-actions-lock extension)
actions-lock-check:
    gh actions-lock --verify

# Pin every action the workflows use in .github/workflows/actions.lock (needs gh, logged in, with the github/gh-actions-lock extension)
actions-lock:
    gh actions-lock

# Formatting first fixes the long lines it can, so lint-fix stops only on
# findings that need a hand edit.

# Reformat code, autofix lint issues and update actions.lock
fix: fmt just-fmt lint-fix actions-lock

# Run everything CI's build, test and coverage jobs run (except the PR-only patch coverage), on this machine's Python and OS
ci: lock-check sync check coverage build smoke
