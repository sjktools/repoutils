# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

**`CLAUDE.md` is a symlink to this file**, not a separate file.
Always edit `AGENTS.md` directly.

This file and `README.md` are written one sentence per line, not wrapped at a fixed width, and `just sentence-per-line-check` enforces it.

## What this is

repoutils is a command-line tool, `repoutils <command>`, that collects the checks and CI helpers several projects used to keep in their own `scripts/` directories.
It's published to PyPI, and projects depend on it as a dev dependency.

It must stay standard library only, with no runtime dependencies.
Projects install it alone in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches.
It supports Python 3.12 and later, so don't use newer syntax or `typing` features (e.g. `TypeIs`, or `except A, B:` without parentheses).

## Layout

- `src/repoutils/cli.py` is the entry point.
  `GROUPS` lists every command with its module and one-line summary, and `main()` dispatches to that module's `run(argv) -> int`.
- `src/repoutils/commands/<name>.py` is one module per command (`check-pins` is `check_pins.py`).
  Each parses its own arguments in `run()` with `argparse`, using `prog="repoutils <command>"`.
  Most also have a `main()` with plain parameters that the tests call directly.
- `src/repoutils/config.py` reads the project's `[tool.repoutils]` table from `pyproject.toml` in the current directory.
  Every setting has a default, and command-line options override it.
- `src/repoutils/ruleset_format.py` is the `rulesets` command's pyproject.toml format, and its conversion to and from GitHub's ruleset JSON.
  It's kept apart from the command for length.
  `src/repoutils/ruleset_labels.py` gives the labels GitHub's ruleset page uses, which `dump` writes as comments.
- `src/repoutils/github.py` holds the GitHub API helpers the posting commands share.
  They are a request function that never follows a redirect with the token to another host, paginated comment listing, `upsert_comment` and `hide_comments`.
- `src/repoutils/secret_inspect.py` is `secrets inspect`, which reports what each secret a workflow passes it is, kept apart from the command for length.
  `src/repoutils/secret_export.py` is `secrets export`, which encrypts those secrets' values with age for a backup.
- `src/repoutils/workflows.py` reads the workflows in `.github/workflows` for the secrets and variables they use and each job's environment, without a YAML parser.
  `secrets check-workflows` and `variables check-workflows` use it.
- `src/repoutils/commands/variables.py` shares its table format, and most of its workings, with `secrets.py`.
- `src/repoutils/markdown.py` holds the Markdown reading that `check-sentence-per-line` and `check-prose` share.
  It skips code fences and splits a document's prose into sentences.
- `src/repoutils/docstrings.py` gives the summary a command's `--help` shows, the first paragraph of its module docstring.
  It returns None under `python -OO`, which strips docstrings.
- `tests/test_<module>.py` tests each module, and `tests/test_cli.py` runs every command through `cli.main()`.

To add a command:

1. Add its module, with a `run()`.
2. Add it to `cli.GROUPS`.
3. Add its tests, and a case in `tests/test_cli.py` if it needs one.
4. Add it to the table in `README.md`.

A command that enforces a rule can take `--exit-zero`, which makes it report what breaks the rule but exit 0.
The option forgives a broken rule and nothing else.
If the command can't tell whether the rule holds, it still exits non-zero, as it does without the option.
That covers a file it can't read, a bad setting in `pyproject.toml`, and a failed request to GitHub.
Give a new `--exit-zero` a test that an error still fails with it.

A command that posts a PR comment finds its earlier comment by a hidden marker, `<!-- <comment-prefix>-<name> -->`.
`comment-prefix` comes from `[tool.repoutils]` (default `repoutils`), so projects that switch from their own scripts keep finding the comments those posted.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`.
Run `just ci` to run everything CI's test, build and coverage jobs run, or `just --list` for the recipes.
`just check` runs every check except `lock-check` (which `just ci` runs first) without fixing anything.
`just fix` reformats, applies lint autofixes and regenerates `actions.lock`.
Both need `gh`, logged in, with the gh-actions-lock extension.
By design, `just fix` fixes what `just check` finds, so whatever `just fix` fixes, `just check` must check.
When you add a fix to `just fix`, add the check that goes with it to `just check`.
`just test` runs the tests, and `just coverage` runs them with a coverage report and writes `coverage.xml`.
Before opening a PR, run `just patch-coverage` too, which reports the changed lines the tests miss, and cover them where a test is worth it.

The justfile runs repoutils' own checks (`whitespace-check`, `check-pins` and so on) from this checkout, so CI dogfoods them.
Recipes that CI runs with a write-capable token or on a Dependabot branch use the justfile's `repoutils` variable.
It runs `python -m repoutils` from `src/` with `uv run --no-project`, installing nothing (not even the project, whose build backend would otherwise run alongside the token).

`just prose-check` runs `check-prose`, which fails if the prose in this file or `README.md` is over a limit.
Its limits are ceilings and not targets, so reword the sentences it lists and leave the rest alone.
A file cut up into very short sentences fails too, and reads no better than one made of long ones.

When you reword documentation, keep every condition and reason it gives.
Dropping a "when" or "only" clause can make a true sentence false.
Use one verb for one action.
Don't use a subcommand's name, such as `ensure` or `check`, as an ordinary verb.
Name the command or setting wherever "it" or "that" could mean more than one thing.
Write exact values as code: exit codes, `true` and `false`, and permission names such as `read`.
Turn a sentence that lists several conditions into a bulleted list.
`README.md` is public, so it spells out "pull request", though this file can say "PR".

`just mypy` type-checks `src` and `tests` in strict mode, so all new code, tests included, needs full type annotations.
`just pyright`, part of `just check`, type-checks them again with pyright in strict mode, as it catches some mistakes that mypy and ruff miss.
Its checks for unknown types are off, as they mostly flag JSON values and code that mypy infers fully.
`[tool.pytest.ini_options]` sets `filterwarnings = ["error"]`, so a warning fails its test.
Fix its cause, or assert it with `pytest.warns` around the call that raises it.

CI pins its tools, and `just check-pins` fails if a pin differs.
Every `setup-uv` step sets `version` to the minimum in `pyproject.toml`'s `[tool.uv] required-version`, and every `setup-just` step sets `just-version` to the justfile's `minimum-version`.
`.github/workflows/actions.lock` pins every action the workflows use to a commit.
After changing any `uses:`, regenerate it with `just fix` or `just actions-lock`, which run `gh actions-lock` (the [github/gh-actions-lock](https://github.com/github/gh-actions-lock) extension), and never edit it by hand.
`just actions-lock-check`, part of `just check`, verifies it.
CI's `check-and-build` job and `fix.yml` install the extension with the `.github/actions/install-gh-actions-lock` action, which pins its release by version and SHA-256.

## CI and releases

`ci.yml` has these jobs:

- The tests, on Linux and macOS for each supported Python version.
- Once the tests pass, `just check`, the build and a smoke test of the wheel.
- Coverage, which doesn't wait for the tests.
- The coverage report (patch, project and the change from the PR's base) and the long files report, posted as PR comments on PRs from this repository that aren't Dependabot's.
- The project and patch coverage, posted as `coverage/project` and `coverage/patch` commit statuses on `main`'s commits and on PRs that get the comments.

Windows isn't supported for development, as `[tool.uv] environments` leaves it out of `uv.lock`, though repoutils itself is pure Python.
On Dependabot PRs, the `dependabot-fix` job re-locks with our uv and syncs the dependency minimums.
The `dependabot-push` job then pushes the result with the `DEPENDABOT_FIX_TOKEN` secret (a Dependabot secret with contents write), so the push triggers CI.
A job that runs a branch's code never references a token that can push to the repository, as code from an earlier step could read it from a later one.
It hands its commits, as a Git bundle artifact, to a separate push job that runs nothing from the branch (`/autofix` does the same).

Releases are cut from `main`.
The Bump Version workflow, run from `main`, opens a PR bumping `version` in `pyproject.toml` (with the `GH_ACTIONS_TOKEN` secret).
Once it merges, the `release` job in `ci.yml` pushes the tag `v<version>` over SSH with the `RELEASE_DEPLOY_KEY` deploy key, which triggers the Publish workflow.
It then creates the GitHub release with the job's own `GITHUB_TOKEN`.
`publish-pypi.yml` builds once, publishes to TestPyPI and checks the release installs from there.
Only then, once a maintainer approves the `pypi` environment deployment, does it publish the same files to PyPI, and check they install from PyPI.
It publishes with PEP 740 attestations, through trusted publishing (no API token).

`GH_ACTIONS_TOKEN` and `RELEASE_DEPLOY_KEY` live in the `release` environment, which only `main` can deploy to, so a workflow run from any other branch can't read them.
Two tag rulesets protect `v*` tags: nobody can move or delete one, and only a deploy key can create one.
That deploy key must be the repository's only one with write access, as the ruleset's bypass covers them all.

Two comments act on an open PR from this repository, when the comment is just the command.
The commenter's author association must be `OWNER` or `MEMBER`, so outside collaborators and contributors can't use them.
`/testpypi` publishes a dev build of the PR's branch to TestPyPI only.
`/autofix` runs `just fix` on the PR's branch and pushes the result (with the `AUTOFIX_TOKEN` secret).
It dispatches `fix.yml` on the PR's branch, which runs `just fix` with no secrets and uploads its commits as a Git bundle.
Then `fix-push.yml`, which a `workflow_run` trigger always runs from `main`, checks that the bundle only adds commits to the branch, and pushes them.
If either run fails, or PR Fixes can't start, the PR gets one comment saying so, which later runs edit, so a success replaces an earlier failure.
The fix runs on the branch rather than on `main` because code in a run can write to the Actions cache of the run's branch.
On `main`, that would reach the jobs that publish, tag or bump a release, which use no cache either, as a second line of defence.
`AUTOFIX_TOKEN` lives in the `autofix` environment, which only `main` can deploy to, as with `release`.

`claude.yml` and `claude-code-review.yml` run Claude Code when a comment starts with `@claude` (with the `CLAUDE_CODE_OAUTH_TOKEN` secret).
Their `route` jobs run `repoutils claude-route` from a sparse checkout of `src/repoutils/` with the runner's `python3`, installing nothing.
`[tool.repoutils.secrets]` in `pyproject.toml` lists the secrets these workflows use, which `repoutils secrets check` confirms the repository has.
Update it when a workflow starts or stops using one, as `just secrets-workflows-check` fails if the two differ.
A workflow that uses an Actions variable (`vars.NAME`) needs it listed in `[tool.repoutils.variables]` in the same way, which `just variables-workflows-check` checks.
`inspect-secrets.yml`, run by hand from `main`, reports what each of them is with `repoutils secrets inspect`, with a job for each environment that holds secrets.
`export-secrets.yml` is run the same way, and backs up their values with `repoutils secrets export`, encrypted to the GitHub key that the `SECRETS_EXPORT_KEY` variable names.
`ci.yml`'s `secrets-export` job checks on every PR that this still works, by round-tripping made-up secrets with `just secrets-export-test`.
Both install age with the `.github/actions/install-age` action, which pins its release by version and SHA-256, so update both together.

## Issue labels

`pyproject.toml`'s `[[tool.repoutils.labels]]` defines the labels, as exolot and taxtracker use them.
`repoutils labels check` reports any the repository lacks, and `repoutils labels ensure` creates them.

Issues are labelled `dimension:value`.
When you file an issue, give it one `type:` and one `priority:` label, a `size:` label unless it's a parent issue, and any `concern:` labels that apply.

`type:` (exactly one):

- `bug`: something behaves wrongly.
- `feature`: a new capability, such as a new command.
- `improvement`: makes something that already works better.
- `maintenance`: refactors, dead code, CI or dependencies, with no visible change.
- `docs`: documentation only.

`priority:` (exactly one), defined by what to do about it:

- `p0`: drop everything, as a release is broken, or a check passes or fails wrongly.
- `p1`: do next.
- `p2`: worth doing when convenient.
- `p3`: someday/maybe.

`size:` (exactly one, except on parent issues), by how many PRs it takes rather than hours:

- `s`: one small PR.
- `m`: one PR that needs some design.
- `l`: several PRs.

An `l` issue should become a parent issue with sub-issues.
Parent issues get no `size:` label, but their sub-issues do.

`concern:` (zero or more):

- `security`: a security risk or hardening.
- `breaking`: changes commands, options, config or exit codes that projects rely on.

`question` is for issues that ask something rather than report or request something.
`dependencies` marks Dependabot's PRs, which also get `type:maintenance` (`.github/dependabot.yml` sets both).
Close duplicates and things that won't be done with GitHub's "duplicate" and "not planned" close reasons, as there are no labels for them.
Use only these labels: applying a label that doesn't exist creates a new grey one, so a typo or a new value silently adds to the scheme.
