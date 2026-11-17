# repoutils

`repoutils` is a package of repository checks and CI helper commands for Python projects that use `uv`, `just` and GitHub Actions.
Run a command as `repoutils <command>`.

These checks and helper commands previously lived in the `scripts/` directories of several personal projects, such as [exolot](https://github.com/skissane/exolot).
This package collects them in one place so that each project can depend on one package instead of maintaining separate copies.

`repoutils` has no runtime dependencies and uses only the Python standard library (Python 3.12 or later).
It should keep zero dependencies because projects run it in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches.
Those CI jobs should not install any other packages.

## Installing

Put `repoutils` in a separate dependency group in `pyproject.toml`, and include that group in `dev`:

```toml
[dependency-groups]
repoutils = ["repoutils>=0.1.0"]
dev = [{ include-group = "repoutils" }, "pytest>=9"]
```

Run a command with `uv run repoutils <command>`.
In a CI job with a write-capable token, run `uv run --only-group repoutils repoutils <command>` instead.
That command installs only `repoutils`, verifies its hashes against `uv.lock`, and does not install the project or other dependencies.

## Commands

Run `repoutils --help` to list all commands, and run `repoutils <command> --help` to show the options for one command.

| Command | What it does |
| --- | --- |
| `check-whitespace` | Fails on trailing whitespace, a leading blank line, or a missing or extra final newline in any Git-tracked text file. |
| `check-sentence-per-line` | Fails if a Markdown file has two sentences on one line. It reads the files named on the command line, or else the configured files (by default `AGENTS.md` and `README.md`). |
| `check-prose` | Fails if the prose in a configured Markdown file has long sentences, many joined clauses, or too many short sentences. |
| `check-blame-ignore-revs` | Fails unless `.git-blame-ignore-revs` lists only full commit hashes that are ancestors of a target commit. |
| `check-screenshots-dir` | Fails unless `.gitignore` is the only tracked file in the screenshots directory and ignores all other files in that directory. |
| `git-config-setup` | Sets the local `blame.ignoreRevsFile` Git setting (idempotent, so a justfile can run it before every recipe). |
| `long-files` | Lists the files over a line limit as Markdown (with changed files first), or posts that list to a pull request. |
| `check-pins` | Fails unless every `setup-uv` and `setup-just` workflow step pins the versions required by `pyproject.toml` and the justfile. |
| `check-checkout-credentials` | Fails on any `actions/checkout` step that does not set `persist-credentials`. |
| `claude-route` | Reads a comment from standard input and prints `review`, `other` or `none` to route between the Claude workflows. |
| `requirements-min-versions` | Checks or updates the `>=` minimum versions in `pyproject.toml` to match the versions in `uv.lock`. |
| `dependabot-relock` | Re-locks `uv.lock` on a Dependabot branch with the current `uv` version, using the exact package versions from Dependabot. |
| `check-lock-platforms` | Fails if `uv.lock` contains wheels for platforms that the project does not allow. |
| `post-comment` | Posts a Markdown file to a pull request, and edits the previous comment (posting nothing with `--update-only` when none exists) or hides it when you pass `--hide-previous`. |
| `added-strings` | Lists the added lines in a pull request that match strings configured in `pyproject.toml` (such as `# noqa:`), or posts the list to the pull request. |
| `post-patch-coverage` | Formats the Markdown patch coverage report from `diff-cover`, adds the project coverage and the change from the base commit, posts the report to a pull request, and warns when coverage drops or falls below a limit. |
| `post-coverage-status` | Posts the project and patch coverage of a commit as GitHub commit statuses for information only. |
| `wait-for-base-coverage` | Waits until the base commit of a pull request has a `coverage/project` commit status so that `post-patch-coverage` can compare coverage. |
| `post-screenshots` | Uploads PNG files from a directory and posts them to a pull request in a comment. |
| `recreate-pr` | Recreates a pull request as a draft from the same branch and closes the original pull request. |
| `github-settings` | Runs `check`, `ensure` or `dump` across all GitHub settings commands below. `check` and `ensure` skip commands not configured in `pyproject.toml`, and `dump` runs all commands. |
| `labels` | Checks or creates the issue labels defined in `pyproject.toml`, or prints the labels that exist in the repository. |
| `repo-settings` | Checks or updates the repository settings defined in `pyproject.toml`, or prints the current settings of the repository. |
| `rulesets` | Checks, creates or updates the branch and tag rulesets defined in `pyproject.toml`, or prints the current rulesets of the repository. |
| `environments` | Checks, creates or updates the deployment environments defined in `pyproject.toml`, or prints the current environments of the repository. |
| `actions-settings` | Checks or updates the GitHub Actions settings defined in `pyproject.toml`, or prints the current settings of the repository. |
| `secrets` | Checks that the repository has the Actions, Dependabot and environment secrets listed in `pyproject.toml`, or prints the secret names in the repository. Also checks that workflows use only those secrets, inspects token secrets in a workflow, or exports secret values encrypted with `age`. |
| `variables` | Checks that the repository has the Actions and environment variables listed in `pyproject.toml` with non-empty values, or prints the variable names in the repository. Also checks that workflows use only those variables. |
| `deploy-keys` | Checks or creates the deploy keys declared in `pyproject.toml` and the secrets that store their private keys, or prints the deploy keys in the repository. |
| `security-settings` | Checks or updates the security settings defined in `pyproject.toml` (such as secret protection), or prints the current security settings of the repository. |
| `check-attestations` | Fails unless every distribution file in `dist/` has a PEP 740 publish attestation. |

These commands require a GitHub token:

- any command that posts to a pull request, and `post-coverage-status`
- the `github-settings`, `labels`, `repo-settings`, `actions-settings`, `rulesets`, `environments`, `secrets`, `variables`, `deploy-keys` and `security-settings` commands

These commands read the token from `$GITHUB_TOKEN`, or run `gh auth token` when `$GITHUB_TOKEN` is not set.
`wait-for-base-coverage` sends `$GITHUB_TOKEN` when it is set, and does not run `gh auth token`.
Of the `secrets` and `variables` subcommands, only `check` and `dump` use the token.

## Configuration

Put all configuration settings in `pyproject.toml`.
Every setting is optional:

```toml
[tool.repoutils]
# Prefix of the hidden markers used to find this project's PR comments,
# e.g. <!-- repoutils-patch-coverage -->.
comment-prefix = "repoutils"
# The branch to compare with when there's no PR base.
default-branch = "main"

# The names of the commit statuses `repoutils post-coverage-status` posts, and
# when `repoutils post-patch-coverage` warns (false for never); see below.
[tool.repoutils.coverage]
project-status = "coverage/project"
patch-status = "coverage/patch"
warn-project-drop = 2
warn-patch-below = 1

[tool.repoutils.sentence-per-line]
files = ["AGENTS.md", "README.md"]

# The limits `repoutils check-prose` holds these files to; see below.
[tool.repoutils.prose]
files = ["AGENTS.md", "README.md"]
max-words-per-sentence = 18

[tool.repoutils.screenshots]
dir = "screenshots"

[tool.repoutils.lock-platforms]
# "os" allows any arch, and "os:arch" allows one arch ("*" for any).
# "linux" matches manylinux and musllinux.
supported = ["any", "linux:x86_64", "linux:aarch64", "macosx"]
tolerated = ["ios", "pyemscripten"]

[tool.repoutils.long-files]
include = ["*.py"]
exclude = ["*/migrations/*"]
limit = 500
limits = { "tests/*" = 700 }
policy = "Split a long file only where that makes the code better."

# The strings `repoutils added-strings` points out in the lines a PR adds; see below.
[tool.repoutils.added-strings]
exclude = ["pyproject.toml", "*.md"]

[[tool.repoutils.added-strings.strings]]
string = "TODO"

[[tool.repoutils.added-strings.strings]]
regex = '^\s*if TYPE_CHECKING\b'
name = "if TYPE_CHECKING:"

[[tool.repoutils.added-strings.strings]]
regex = "# type: ?ignore"
message = "please fix the type error instead, if you can"
include = ["*.py"]

# The labels that `repoutils labels` checks or creates; color and description are optional.
[[tool.repoutils.labels]]
name = "bug"
color = "d73a4a"
description = "Something isn't working"

[[tool.repoutils.labels]]
name = "dependencies"

# The repository settings that `repoutils repo-settings` checks or changes (see below).
[tool.repoutils.repo-settings]
description = "Repository tooling for Python projects"
homepage = "https://pypi.org/project/repoutils/"
issues = true
wiki = false
pull-request-creation = "collaborators"
squash-merge = { allow = true, title = "pr-title", message = "pr-body" }
merge-commit.allow = false
rebase-merge.allow = false
delete-branch-on-merge = true

# The Actions settings that `repoutils actions-settings` checks or changes (see below).
[tool.repoutils.actions-settings]
allowed-actions = "all"
require-sha-pinning = true
default-token-permissions = "read"
oidc.immutable-subject = true

# A ruleset `repoutils rulesets` checks or creates, giving only what isn't a default; see below.
[tool.repoutils.rulesets.main]
include = ["~DEFAULT_BRANCH"]
bypass = [{ role = "admin", mode = "pull-request" }]

[tool.repoutils.rulesets.main.rules]
deletion = true
non-fast-forward = true
pull-request = { allowed-merge-methods = ["squash"] }
required-status-checks = { required-status-checks = [{ context = "test", app = "github-actions" }] }

# The names of the secrets `repoutils secrets` checks the repository has; see below.
[tool.repoutils.secrets]
actions = []
dependabot = ["DEPENDABOT_FIX_TOKEN"]
environments.release = ["RELEASE_TOKEN"]
other = ["ORG_DEPLOY_KEY"]

# The names (never the values) of the variables `repoutils variables` checks the repository has; see below.
[tool.repoutils.variables]
actions = ["DEPLOY_URL", "NEXT_RELEASE_URL"]
unused = ["NEXT_RELEASE_URL"]

# The security settings that `repoutils security-settings` checks or changes; see below.
[tool.repoutils.security-settings]
dependabot-alerts = true
secret-protection = true
push-protection = true
immutable-releases = true

# The deployment environments `repoutils environments` checks or creates; see below.
[tool.repoutils.environments.pypi]
allowed-tags = ["v*"]

[tool.repoutils.environments.testpypi]

# A deploy key `repoutils deploy-keys` checks or creates, and the secret that holds its private half; see below.
[tool.repoutils.deploy-keys.release]
write = true
secret = { environment = "release", name = "RELEASE_DEPLOY_KEY" }
```

### Prose

`repoutils check-prose` measures the prose in each configured Markdown file.
It fails when the sentences in a file are too long or contain too many joined clauses.
Machine-generated text often uses that style, which is difficult to read.

The command also fails when a file has too many very short sentences.
Authors sometimes split sentences into fragments only to stay below the word limit, which is also difficult to read.
One limit therefore caps the percentage of short sentences in a file.
That limit excludes list items, because short sentences are normal in lists.
That limit also excludes sentences that end with a colon before a list or a code block.
Treat all limits as maximum values rather than targets, and do not edit sentences when the file already passes.

The command skips code blocks, headings, tables, block quotes, HTML comments, and lines that start with an HTML block tag.
A block quote continues until the next blank line, list item or heading.
The command counts an inline code span as one word and a URL as one word.
It ignores text that has no closing punctuation or has fewer than three words, such as a short list item.

The command splits sentences according to standard English punctuation.
A sentence ends at a full stop, question mark or exclamation mark when the next sentence starts with an uppercase letter, digit or inline code.
A colon at the end of a paragraph or list item also ends a sentence.
A newline after any of those punctuation marks always starts a new sentence, regardless of the first character on the next line.
In a wrapped paragraph, a sentence that starts mid-line with a lowercase word (such as `uv`) is counted as part of the previous sentence.

| Setting | Default | What it limits |
|---|---|---|
| `max-words-per-sentence` | 18 | Mean number of words per sentence. |
| `max-long-sentences` | 12 | Percentage of sentences with more than `long-sentence-words` words (default 30). |
| `max-short-sentences` | 25 | Percentage of sentences with fewer than `short-sentence-words` words (default 8), excluding list items and sentences ending with a colon. |
| `max-commas-per-sentence` | 1.3 | Mean number of commas per sentence. |
| `max-semicolons-per-sentence` | 0.05 | Mean number of semicolons per sentence. |
| `max-joints-per-sentence` | 0.6 | Mean number of clause joints per sentence. |

A clause joint is a semicolon, a colon, a dash, or a comma before a conjunction such as "and", "but", "because" or "which".
Set any limit to `false` to disable that limit.
A file with fewer than `min-sentences` sentences (default 15) is too short to measure and always passes.
The settings `min-sentences`, `long-sentence-words` and `short-sentence-words` must be integers.

The limits apply to the average rates across the whole file rather than to individual sentences.
A file can contain a few long sentences and still pass.
For each limit that a file exceeds, the command prints the five sentences that contribute the most to that rate.
The two limits for long sentences share one list of sentences under the first exceeded limit.
Pass `--exit-zero` to print the same output and exit with code `0`.
Pass `--stats` to print all rates for every file.
`--stats` also prints the sentence-length CV, which no setting limits: the standard deviation of sentence length divided by the mean sentence length.

When the command cannot read a file (for example, when the file is not valid UTF-8), it prints `<file>: cannot read (<reason>)`.
The command still reads the remaining files, and exits with code `1` even when you pass `--exit-zero`.
`repoutils check-sentence-per-line` handles unreadable files in the same way.

The default limits for long sentences come from a sample of 17 documents, both pre-LLM README files and recent machine-generated documents.
The default limit for short sentences comes from a smaller sample of rewrites of this README, so adjust the limits to fit your project.

### Added strings

`repoutils added-strings` searches the lines added in the working tree since the merge base of `--base` and `HEAD`.
It lists every added line that matches any configured pattern in `strings`.
Each entry in `strings` specifies either `string` (a literal substring) or `regex` (a Python regular expression).
The default `message` asks the author to verify that the matched text is necessary.
Unlike a literal substring, a regular expression can match code syntax without matching documentation or test strings that mention that syntax.
For example, `^\s*if TYPE_CHECKING\b` matches only the `if TYPE_CHECKING` statement, and `repoutils` uses a regular expression in its own `pyproject.toml` for `# noqa` comments.
For a `regex` entry, `name` sets the label shown in the report instead of the raw regular expression.

The `include` and `exclude` settings take glob patterns for file paths, where `*` matches across `/`.
The patterns in `[tool.repoutils.added-strings]` apply to all entries, and the patterns on a single entry narrow the search for that entry.
Exclude `pyproject.toml` and documentation files because those files often mention the configured strings.

The report is for information only, so the command exits with code `0` even when it finds matches.
The command exits non-zero when it cannot run the check, such as on an invalid setting, failed `git` command or failed GitHub request.
When you pass `--post <PR>`, the command posts a pull request comment only when the pull request adds a matching string.
If a later push removes all matches, the command updates the previous comment to state that none remain.

### Coverage status

`repoutils post-coverage-status <sha>` posts two commit statuses (`coverage/project` and `coverage/patch`) on the specified commit.
The command reads the project coverage from `coverage.xml` for the whole project.
It reads the patch coverage from the `diff-cover` JSON report (`--patch-report`) for the lines changed in the pull request or commit.

Run this command in CI on every default-branch commit and on the head commit of every pull request.
The default branch statuses then record the coverage history of the project.
Skip this step on pull requests from forks and Dependabot, because their tokens lack write permission.
Pass `--base` with the pull request base or parent commit to include the coverage change in the project status.
If the base commit has no `coverage/project` status, the status omits the coverage change.

Both statuses always use the `success` state because they record coverage without failing the commit.
Their description strings use a fixed format so that other tools can parse them:

```text
96.52% (2114/2172 lines, 610/650 branches), +0.12 points from 1a2b3c4
92.30% (12/13 lines)
```

The project coverage number is the total from `coverage.py`, which combines covered lines and covered branches.
The command truncates this number to two decimal places instead of rounding it, so `100.00%` appears only when coverage is complete.
The branch count appears only when `coverage.xml` includes branch coverage.
The change is the difference between the two displayed percentages.
The patch coverage number counts lines in the same way as `diff-cover`, and shows `no changed lines to cover` when the diff has no executable lines.

A token with permission to post commit statuses can post a status with any context name, including the name of a required check.
Therefore, run this command in a separate job that installs no other packages and receives the coverage reports as artifacts.
That job requires the `statuses: write` permission, which GitHub does not grant to pull requests from forks or from Dependabot.
Pass `--dry-run` to print the statuses without posting them to GitHub.

### Patch coverage comment

`repoutils post-patch-coverage <PR>` posts the Markdown report from `diff-cover` to the pull request, and updates that existing comment on subsequent runs.
When you pass `--coverage-report coverage.xml`, the comment also shows the project coverage.
When you also pass `--base <sha>`, the comment shows the `coverage/project` status of that base commit and the change from it.
The command never compares against an older commit, because an older commit would include changes from other merged pull requests.
When the base commit has no coverage status, the comment notes that fact and shows only the coverage of the pull request.

The comment includes a warning and prints a `::warning::` workflow command in two cases:

- Project coverage dropped from the base commit by more than `warn-project-drop` percentage points (default 2).
- Patch coverage is more than `warn-patch-below` points (default 1) below the base project coverage, or the pull request project coverage when the base has no status.

These conditions only produce warnings, so low coverage never causes the command to fail.
The command exits non-zero only when it cannot produce or post the report, such as on a bad file, bad setting or failed request.
Pass `--output <file>` to append the comment text to a file (such as `$GITHUB_STEP_SUMMARY`) instead of posting it.
Use `--output` on pull requests from forks and Dependabot, where the workflow token cannot post pull request comments.

The CI workflow on the base commit can still be in progress when the pull request finishes its coverage run.
Run `repoutils wait-for-base-coverage <owner/repo> <base>` before `post-patch-coverage`.
This command polls the `coverage/project` commit status on the base commit and exits as soon as that status exists.
While the status is absent, the command checks the `post-coverage-status` check run (`--check-name`) on that commit, exiting when that check run finishes.
The command also stops after 10 minutes (`--timeout`).
If the check run finishes or the wait times out without a status, the command prints a warning and exits with code `0`.
A broken base commit therefore does not fail the checks on the pull request.
This command requires the `checks: read` and `statuses: read` permissions.

### GitHub settings

`repoutils github-settings check` runs `check` for every GitHub settings command configured in `pyproject.toml`, following the order in the command table above.
It prints the names of the unconfigured commands that it skips, and fails if `pyproject.toml` configures none of them.
It runs all configured commands and fails if any command fails.
It forwards `--repo` and `--exit-zero` to every command, and forwards `--fail-on-unexpected` to the commands that support that option.

`repoutils github-settings ensure` runs `ensure` for each configured command in the same order, excluding `secrets` and `variables` because those commands have no `ensure` subcommand.
It stops immediately at the first command that fails.
Stopping on failure prevents `deploy-keys ensure` from storing a secret in an environment before `environments ensure` applies the protection rules for that environment.

`repoutils github-settings dump` runs `dump` for every settings command regardless of `pyproject.toml`, with a blank line between the outputs.
You can paste the output directly into `pyproject.toml`.

### Labels

`repoutils labels ensure` creates every label defined in `pyproject.toml` that does not exist in the repository, using a grey color when `pyproject.toml` omits `color`.
When an existing label has an empty description on GitHub, `ensure` sets the description from `pyproject.toml`.

By default, `ensure` reports other differences without modifying them:

- label names that differ in letter case
- differences in color
- differences in non-empty descriptions

Pass `--strict` to update those existing labels to match `pyproject.toml`.
`ensure` never modifies or deletes a label that `pyproject.toml` does not define.
If a request fails mid-run, earlier label changes remain in place, and you can run `ensure` again to finish the rest.

`repoutils labels check` does not modify the repository.
It fails in the exact cases where `ensure` (with the same `--strict` option) would create or update a label.
It also prints the differences that `ensure` leaves unchanged, without failing on them.

When you pass `--fail-on-unexpected`, `check` also lists any labels in the repository that `pyproject.toml` does not define (matching names case-insensitively) and fails if any exist.
Use `--fail-on-unexpected` when `pyproject.toml` must define every label in the repository.
`ensure` still ignores unexpected labels, so you must delete them on GitHub or add them to `pyproject.toml`.

When you pass `--exit-zero`, `check` prints the same report and exits with code `0`, unless an error (such as a bad token) stops it from comparing.
Using `--exit-zero` is safer than `|| true` in a shell script because `|| true` also hides runtime and authentication errors.

In GitHub Actions, `ensure` requires the `issues: write` permission, and `check` requires `issues: read` on a private repository.

`repoutils labels dump` prints every label in the repository as `[[tool.repoutils.labels]]` entries, including the name, color, and non-empty description of each label.
You can paste the output into `pyproject.toml` and edit the entries as needed.
If you paste the output without changes, `repoutils labels check --strict` passes.
`dump` does not read or modify `pyproject.toml`.

### Repository settings

`[tool.repoutils.repo-settings]` configures the settings on the General settings page of a repository, primarily in the Features and Pull Requests sections.
Every setting is optional, but `check` and `ensure` fail if the table contains no settings.
Any setting omitted from the table keeps its current value on GitHub.

| Setting | Values | On GitHub |
|---|---|---|
| `description`, `homepage` | string (`""` for empty) | Populates the repository About box. |
| `issues`, `projects`, `wiki`, `discussions`, `pull-requests` | `true` or `false` | Whether each repository feature is enabled. |
| `pull-request-creation` | `"all"` or `"collaborators"` | Who can create pull requests. |
| `merge-commit.allow`, `squash-merge.allow`, `rebase-merge.allow` | `true` or `false` | Which pull request merge methods are enabled. |
| `merge-commit.title` | `"merge-message"` or `"pr-title"` | Default title of a merge commit (`"merge-message"` produces `Merge pull request #123 from branch`). |
| `merge-commit.message` | `"pr-title"`, `"pr-body"` or `"blank"` | Default message body of a merge commit. |
| `squash-merge.title` | `"pr-title"` or `"commit-or-pr-title"` | Default title of a squash commit (`"commit-or-pr-title"` uses the commit title when the pull request has one commit). |
| `squash-merge.message` | `"pr-body"`, `"commit-messages"` or `"blank"` | Default message body of a squash commit. |
| `allow-auto-merge` | `true` or `false` | Allow auto-merge on pull requests. |
| `allow-update-branch` | `true` or `false` | Always suggest updating pull request branches. |
| `delete-branch-on-merge` | `true` or `false` | Automatically delete head branches after merge. |
| `require-web-commit-signoff` | `true` or `false` | Require contributors to sign off on web-based commits. |

The GitHub API accepts a commit message setting only when the request also includes the corresponding commit title setting.
When `pyproject.toml` sets only `squash-merge.message` (or `merge-commit.message`), `repoutils` sends that message together with the title currently set on GitHub.
GitHub rejects some combinations of title and message, and `ensure` reports that rejection as an HTTP 422 error.

`repo-settings` intentionally excludes destructive or structural settings.
It cannot rename, transfer or archive a repository, change its default branch or visibility, or make it a template.
It rejects any configuration table that contains those keys.

`repoutils repo-settings ensure` updates all differing settings in a single API request.
It then verifies the response and fails if GitHub did not apply every change.
GitHub silently ignores some requested changes instead of returning an error, such as when an organization policy forbids the change.

`repoutils repo-settings check` does not modify the repository, and fails in the exact cases where `ensure` would update a setting.
`--exit-zero` works in the same way as in `labels check`.

`ensure` requires a token with administration write access to the repository, such as a fine-grained personal access token with the Administration `write` permission.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have the Administration permission.

`repoutils repo-settings dump` prints every setting from the table above with its current value in the repository.
The output is a `[tool.repoutils.repo-settings]` table with a sub-table for each merge method.
Paste the output into `pyproject.toml` and remove the settings that you do not want to manage.
If you paste the output without changes, `repoutils repo-settings check` passes.
`dump` does not read or modify `pyproject.toml`.
If GitHub omits a setting from its response or returns an unsupported value, `dump` omits that setting and prints a warning to standard error.

### Actions settings

`[tool.repoutils.actions-settings]` configures the GitHub Actions settings page of the repository (Settings > Actions > General).
It follows the same model as `[tool.repoutils.repo-settings]`.
Every setting is optional, but `check` and `ensure` fail if the table contains no settings.
Any setting omitted from the table keeps its current value on GitHub.

| Setting | Values | On GitHub |
|---|---|---|
| `allowed-actions` | `"all"`, `"owner"`, `"selected"` or `"disabled"` | Determines whether all actions and reusable workflows can run, only those belonging to the repository owner, only those in `selected-actions`, or none (Actions is disabled). |
| `require-sha-pinning` | `true` or `false` | Require actions to be pinned to a full-length commit SHA. |
| `selected-actions.github-owned` | `true` or `false` | Allow actions created by GitHub. |
| `selected-actions.verified-creators` | `true` or `false` | Allow actions created by Marketplace verified creators. |
| `selected-actions.patterns` | list of strings | Patterns for other allowed actions and reusable workflows (such as `"astral-sh/*"`), in any order. |
| `default-token-permissions` | `"read"` or `"write"` | Controls whether `GITHUB_TOKEN` gets read-only or read-write permissions by default. |
| `can-approve-pull-requests` | `true` or `false` | Allow GitHub Actions to create and approve pull requests. |
| `fork-pr-approval` | `"first-time-contributors-new-to-github"`, `"first-time-contributors"` or `"all-external-contributors"` | Which external contributors require approval before workflows run on their fork pull requests. |
| `retention-days` | positive integer | Number of days that GitHub retains checks, workflow runs, commit statuses, artifacts and logs. |
| `cache-retention-days` | positive integer | Number of days that GitHub retains an unused Actions cache. |
| `cache-size-gb` | positive integer | Maximum total cache storage in gigabytes before GitHub evicts the oldest caches. |
| `oidc.use-default-subject` | `true` or `false` | Use the default template for the OpenID Connect (OIDC) token subject claim. |
| `oidc.include-claim-keys` | list of strings | Ordered list of claim keys for a custom OIDC subject template (requires `oidc.use-default-subject = false`). |
| `oidc.immutable-subject` | `true` or `false` | Use the immutable OIDC subject claim based on the numeric repository ID instead of the repository name. |

The `selected-actions` settings require `allowed-actions = "selected"`, because GitHub stores them only in that mode.

`repoutils actions-settings ensure` updates all differing settings, sending one API request for each endpoint that requires a change.
It fails if GitHub does not apply every requested change.
An organization policy can lock some settings, such as write permissions for `GITHUB_TOKEN`.
GitHub rejects changes to a locked setting with an HTTP 409 error, and `ensure` reports that error.
Some settings do not exist on all repositories, such as `fork-pr-approval` on a private repository.
GitHub returns HTTP 404 for those endpoints, so `check` and `ensure` fail when `pyproject.toml` configures them, and `dump` omits them.

`repoutils actions-settings check` does not modify the repository, and fails in the exact cases where `ensure` would update a setting.
`--exit-zero` works in the same way as in `labels check`.

`check` and `dump` require a token with read access to repository administration settings, plus read access to Actions when reading `oidc` settings.
For a fine-grained personal access token, grant Administration and Actions `read` for `check` and `dump`, and `write` for `ensure`.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have the Administration permission.

`repoutils actions-settings dump` prints every setting from the table above with its current value in the repository.
It omits `selected-actions` unless `allowed-actions` is `"selected"`, and omits `oidc.include-claim-keys` when the repository uses the default OIDC template.
Paste the output into `pyproject.toml` and remove the settings that you do not want to manage.
If you paste the output without changes, `repoutils actions-settings check` passes.
`dump` does not read or modify `pyproject.toml`.
If GitHub omits a setting from its response or returns an unsupported value, `dump` omits that setting and prints a warning to standard error.

### Rulesets

Each `[tool.repoutils.rulesets.<name>]` table defines one branch ruleset or tag ruleset in the repository (Settings > Rules > Rulesets).
The table specifies only the settings that differ from the defaults of a new ruleset.
Omitted rules are disabled, omitted `exclude` and `bypass` lists are empty, and other omitted settings use their defaults.
The table defines the complete ruleset, and `ensure` disables any rule that the table omits.

`repoutils rulesets` ignores push rulesets.
It also ignores rulesets inherited from an organization and legacy branch protection rules.

| Setting | Values | On GitHub |
|---|---|---|
| `target` | `"branch"` (default) or `"tag"` | Whether the ruleset applies to branches or tags. |
| `enforcement` | `"active"` (default), `"evaluate"` or `"disabled"` | Enforcement state of the ruleset. |
| `include`, `exclude` | branch or tag name patterns (such as `["main", "release/*"]` or `["v*"]`), `"~ALL"`, or `"~DEFAULT_BRANCH"` (for branch rulesets only) | Which branches or tags the ruleset targets. The `refs/heads/` or `refs/tags/` prefix is optional, and `include` is required. |
| `bypass` | list of actor tables (see below) | Which actors can bypass the ruleset. |
| `rules.<rule>` | `true`, or a table of rule parameters | Each rule enabled in the ruleset. |

Each table in `bypass` identifies one actor.
Specify the actor with `role` (`"admin"`, `"maintain"`, `"write"` or a custom role ID), `app` (a GitHub App slug), `team` (a team slug), `org-admin = true` or `deploy-keys = true`.
Each entry can also specify `mode` as `"always"` (default), `"pull-request"` or `"exempt"`.

For example, these two tag rulesets prevent anyone from updating or deleting a `v*` tag, and let only deploy keys create one.
They must be separate rulesets because a `bypass` list applies to all rules in its ruleset.

```toml
[tool.repoutils.rulesets.release-tags-immutable]
target = "tag"
include = ["v*"]

[tool.repoutils.rulesets.release-tags-immutable.rules]
update = true
deletion = true

[tool.repoutils.rulesets.release-tags-create]
target = "tag"
include = ["v*"]
bypass = [{ deploy-keys = true }]

[tool.repoutils.rulesets.release-tags-create.rules]
creation = true
```

Rule names and parameter names use the kebab-case forms of the names in the GitHub REST API and ruleset JSON exports.
For example, the API rule `non_fast_forward` is written as `rules.non-fast-forward`.
A tag ruleset can contain only the rules that GitHub supports for tags.
Those tag rules are `creation`, `update`, `deletion`, `required-linear-history`, `required-deployments`, `required-signatures`, `required-status-checks`, `non-fast-forward`, `tag-name-pattern` and the three commit metadata pattern rules.
The `tag-name-pattern` rule applies only to tag rulesets, and `check` rejects any rule placed in the wrong ruleset target.
Any omitted rule parameter uses the default value from the GitHub ruleset creation page, which is `false`, `0` or empty.
Two parameters have non-empty defaults: `pull-request.allowed-merge-methods` defaults to all three merge methods, and `merge-queue` defaults to the standard values on the GitHub page.

Above each rule or parameter whose GitHub UI label is known to `repoutils`, `dump` writes that label as a comment.
The following table lists the setting names that differ most from their labels in the GitHub UI:

| In `pyproject.toml` | On GitHub's ruleset page |
|---|---|
| `rules.deletion` | Restrict deletions |
| `rules.non-fast-forward` | Block force pushes |
| `required-status-checks.strict-required-status-checks-policy` | Require branches to be up to date before merging |
| `required-status-checks.do-not-enforce-on-create` | Do not require status checks on creation |
| `pull-request.dismiss-stale-reviews-on-push` | Dismiss stale pull request approvals when new commits are pushed |
| `pull-request.require-last-push-approval` | Require approval of the most recent reviewable push |
| `pull-request.required-review-thread-resolution` | Require conversation resolution before merging |
| `merge-queue.max-entries-to-build` | Build concurrency |
| `merge-queue.grouping-strategy` | Require all queue entries to pass required checks (`"ALLGREEN"`) or not (`"HEADGREEN"`) |

Configure each required status check as `{ context = "test", app = "github-actions" }`.
The `app` key is the slug of the GitHub App that must report the check status.
Omit `app` to accept the status check from any source.
`check` and `ensure` resolve the numeric app ID from the slug automatically, and reject a numeric app ID in `pyproject.toml`.

`repoutils rulesets ensure` creates every missing ruleset.
It replaces every ruleset that differs from `pyproject.toml`, sending all parameters for every configured rule.
It then reads the updated ruleset back and fails if GitHub did not apply a change.
It ignores rule parameters that `repoutils` does not recognize, so `ensure` can reset unknown parameters to their defaults.
It never deletes an existing ruleset.

`repoutils rulesets check` does not modify the repository, and fails in the exact cases where `ensure` would create or update a ruleset.
`--exit-zero` and `--fail-on-unexpected` work in the same way as in `labels check`.
For `rulesets check`, `--fail-on-unexpected` fails when the repository has branch or tag rulesets that `pyproject.toml` does not define.

The `check`, `ensure` and `dump` subcommands require a token with repository administration access, because GitHub shows the `bypass` list only to administrators.
For a fine-grained personal access token, grant Administration `write` for `ensure`, and grant Administration `read` for `check` and `dump`.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have the Administration permission.
When a `bypass` entry references a team, the token also requires the organization Members `read` permission to resolve the team slug.

`repoutils rulesets dump` prints every branch and tag ruleset in the repository as `[tool.repoutils.rulesets.<name>]` tables.
You can paste the output into `pyproject.toml`.
If you paste the output without changes, `repoutils rulesets check --fail-on-unexpected` passes, unless `dump` printed a warning about an unsupported value.
Pass `--from-file` to convert JSON ruleset files exported from the GitHub ruleset UI instead of querying the repository API.
Using `--from-file` lets you design a ruleset in the GitHub UI on any repository and convert the exported JSON into `pyproject.toml` format.
`dump` does not read or modify `pyproject.toml`.

The GitHub REST API provides no endpoint to look up a GitHub App slug from its numeric app ID.
Instead, `dump` collects app slugs from the check runs on the latest commit of the default branch and of each branch named in the rulesets.
When `dump` cannot find the slug for an app or team ID, it prints the numeric ID and writes a warning to standard error.
Replace that numeric ID with the corresponding slug before you run `check`.

### Environments

Each `[tool.repoutils.environments.<name>]` table defines one deployment environment in the repository, which a GitHub Actions job references with `environment:`.
An empty table verifies or creates the environment without checking or changing its settings.
Any setting omitted from the table keeps its current value on GitHub.
Environment names match case-insensitively, as on GitHub, and GitHub cannot rename an existing environment.

| Setting | Values | On GitHub |
|---|---|---|
| `deployment-branches` | `"all"`, `"protected"` or `"selected"` | Controls which branches and tags can deploy to this environment (all, protected only, or a custom selection). |
| `allowed-branches`, `allowed-tags` | lists of name patterns, such as `["v*"]` | Selected branch and tag patterns allowed to deploy. Each list defines the complete set of patterns, and setting either list implies `deployment-branches = "selected"`. |
| `wait-timer` | integer from 0 to 43200 (minutes) | Number of minutes that a job waits before deploying to the environment. |
| `reviewers` | list of up to six user logins (`"login"`) or organization teams (`"owner/team-slug"`) | Required reviewers, where one reviewer must approve each deployment. |
| `prevent-self-review` | `true` or `false` | Prevent the user who triggered the workflow run from approving the deployment. |
| `can-admins-bypass` | `true` or `false` | Allow repository administrators to bypass the environment protection rules. |

Wait timers and required reviewers require a public repository or a paid GitHub plan.
On an unsupported plan, GitHub rejects those settings and `ensure` reports the HTTP error.

`repoutils environments ensure` creates every missing environment and updates every setting that differs from `pyproject.toml`.
It adds and removes branch and tag patterns so that the environment matches `allowed-branches` and `allowed-tags`.
It then verifies the environment and fails if GitHub did not apply a change.
It never deletes an environment, and never modifies environment secrets, environment variables or custom deployment protection rules.

`repoutils environments check` does not modify the repository, and fails in the exact cases where `ensure` would create or update an environment.
`--exit-zero` and `--fail-on-unexpected` work in the same way as in `labels check`.

`ensure` requires a token with administration write access to the repository, such as a fine-grained personal access token with the Administration `write` permission.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have the Administration permission.

`repoutils environments dump` prints every environment in the repository along with all of its settings.
It includes `allowed-branches` and `allowed-tags` only when `deployment-branches` is `"selected"`.
Paste the output into `pyproject.toml` and remove the settings that you do not want to manage.
If you paste the output without changes, `repoutils environments check --fail-on-unexpected` passes.
`dump` does not read or modify `pyproject.toml`.

### Secrets

`[tool.repoutils.secrets]` lists the names of the secrets that the workflows in the repository use.
The `actions` key lists repository Actions secrets.
The `dependabot` key lists Dependabot secrets, which are available to workflows triggered by Dependabot.
Each `environments.<name>` key lists the secrets stored in the deployment environment `<name>`.
The command ignores any list that the table omits.
Secret names match case-insensitively, as on GitHub.
The `other` key lists secrets used by workflows that are not stored in the repository itself, such as organization secrets.
The `check` and `dump` subcommands ignore `other`, and only `check-workflows` reads `other`.

`repoutils secrets` reads only secret names from the GitHub API.
The GitHub API never returns secret values, so `repoutils secrets` has no `ensure` subcommand.
Set a missing secret with `gh secret set NAME` or on the repository settings page.
Pass `--app dependabot` for a Dependabot secret, or pass `--env ENVIRONMENT` for an environment secret.

`repoutils secrets check` fails if the repository lacks any secret or environment listed in the table.
It does not create missing environments, whereas `repoutils environments ensure` can create them.
`--exit-zero` and `--fail-on-unexpected` work in the same way as in `labels check`.
For `secrets check`, `--fail-on-unexpected` fails when a configured secret store contains a secret that its list does not name.
For example, `dependabot = []` requires the repository to have no Dependabot secrets.
When the table includes any `environments` key, `--fail-on-unexpected` covers the secrets of every environment in the repository, including environments not listed in the table.
Setting `environments = {}` requires every environment to have no secrets.

`check` and `dump` require a token that can list the secrets in the repository.
For a fine-grained personal access token, grant the Secrets `read` and Dependabot secrets `read` permissions.
Checking or dumping environment secrets also requires the Actions `read` and Environments `read` permissions.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have those permissions.

`repoutils secrets dump` prints the name of every secret in the repository as a `[tool.repoutils.secrets]` table.
The output includes `actions`, `dependabot`, and an `environments.<name>` list for each environment that holds at least one secret.
You can paste the output into `pyproject.toml`.
If you paste the output without changes, `repoutils secrets check --fail-on-unexpected` passes.
`dump` does not read or modify `pyproject.toml`.

`repoutils secrets check-workflows` requires no GitHub token, so you can run it alongside local lint checks.
It scans every workflow file in `.github/workflows` for secret references written as `secrets.NAME` or `secrets['NAME']`.
It fails if a workflow references a secret not listed in `[tool.repoutils.secrets]`.
It also fails if `[tool.repoutils.secrets]` lists a secret which no workflow references.
This check keeps `[tool.repoutils.secrets]` synchronized with `.github/workflows`.
A workflow job can access an environment secret only when the job specifies that environment in `environment:`.
Therefore, `check-workflows` also fails when a job references an environment-only secret without specifying one of the environments that hold that secret.
When a job sets `environment:` to an expression, `check-workflows` cannot determine the environment name statically and skips environment secret checks for that job.
The command ignores `GITHUB_TOKEN` (which GitHub provides automatically) and ignores text inside YAML comments.

`repoutils secrets inspect` runs inside a GitHub Actions workflow and reports metadata about each secret without printing secret values.
Use `inspect` to identify which token or key a secret contains and to detect tokens that will expire soon.
Pass the job secrets to the command in the environment variable `SECRETS_JSON: ${{ toJSON(secrets) }}`.
By default, `inspect` reports on the secrets in `actions` and `other`, or on the secrets for one environment when you pass `--environment NAME`.
`inspect` also reports on every other secret in `SECRETS_JSON` that `pyproject.toml` does not list for the job, except the built-in `GITHUB_TOKEN`, and warns that each one is unexpected.
An unexpected secret does not make `inspect` fail, even when GitHub rejects it or `inspect` cannot inspect it.
A job that deploys to an environment also receives the repository and organization secrets, so `inspect --environment NAME` expects the secrets in `actions` and `other` as well, but does not report on them.
A workflow job receives the secrets of an environment only when the job deploys to that environment, so inspect each environment in a separate job.
Workflows not triggered by Dependabot cannot read Dependabot secrets, so `inspect` skips `dependabot`.

When a secret value has the format of a GitHub token, `inspect` sends a read-only API request with that token.
It reports the token type (from the token prefix), the token owner, and the expiration date.
It also reports the scopes of a classic personal access token, or the number of repositories of a GitHub App installation token.
The GitHub API does not report which repositories a fine-grained token can access, so `inspect` does not report them.
`inspect` also reports the first 12 hexadecimal digits of the SHA-256 hash of each GitHub token, so you can see whether two secrets hold the same token.
A GitHub token is too random for anyone to recover it from its hash, but other secret values can be less random, so `inspect` does not hash them.
For the same reason, `inspect` does not hash a value shorter than 40 characters, even when it has a token prefix, as every GitHub token is at least that long.
An installation token expires after one hour, so `inspect` always reports an installation token stored in a secret as expiring.
When a secret value is an OpenSSH private key, `inspect` reports the public key fingerprint, which also appears on the GitHub deploy keys page.
For any other secret value, `inspect` reports only the secret name.
The command writes a Markdown summary table to `$GITHUB_STEP_SUMMARY` (or to `--summary FILE`), which has the same visibility as the workflow logs.

`inspect` fails in these cases, for a secret that `pyproject.toml` lists for the job:

- GitHub rejects a token.
- A token expires within 14 days (`--expires-within DAYS`).
- A token has surrounding whitespace.

Passing `--exit-zero` ignores those three findings, but the command still exits non-zero on a failed network request or an unparseable expiration date.

`repoutils secrets export` also runs inside a GitHub Actions workflow and writes an encrypted backup of the secret values using [age](https://age-encryption.org).
Pass the job secrets in `SECRETS_JSON` in the same way as for `inspect`.
The command exports the `actions` list, or exports `environments.NAME` when you pass `--environment NAME`, and fails if `SECRETS_JSON` lacks any listed secret.
It does not export `other` because those secrets belong to the organization rather than the repository.
It formats the secrets as a TOML table (`[actions]` or `[environments.NAME]`) and pipes the TOML text to the standard input of `age`.
The command never writes plaintext secrets to disk, and writes only the encrypted output file (`--output FILE`).

Each `--recipient` argument takes an `age` recipient string (`age1...`) or an SSH public key and passes it directly to `age`.
Pass `--github-key USER/SHA256:...` to encrypt to the SSH public key of the GitHub user `USER` that matches that `ssh-keygen -l` fingerprint.
The command downloads the user's public keys from `https://github.com/USER.keys` and fails if none of the keys matches the fingerprint.
`age` supports only `ssh-ed25519` and `ssh-rsa` SSH keys, and requires a plaintext private key file for decryption.
A key stored on a hardware security key or in a password manager agent cannot decrypt the backup.
Use a dedicated `age-keygen` key in that case.
Pass `--armor` to write ASCII-armored output instead of binary output (`age --decrypt` accepts both formats).
Pass `--age PATH` to specify the path to the `age` executable when `age` is not on `PATH`.
The command writes the exported secret names and the decryption command to `$GITHUB_STEP_SUMMARY` (or to `--summary FILE`).

The command writes the encrypted file only to the runner, so add a workflow step after `export` to upload that file as a workflow artifact.
Any signed-in GitHub user can download artifacts from a public repository, so set a short `retention-days` and delete the artifact after download.
Deleting old artifacts prevents a future compromise of the private key from decrypting earlier backups left on GitHub.

### Variables

`[tool.repoutils.variables]` lists the GitHub Actions variables that the workflows in the repository use, following the same structure as `[tool.repoutils.secrets]`.
The `actions` key lists repository variables, and each `environments.<name>` key lists variables for the deployment environment `<name>`.
The `other` key lists variables used by workflows that are not stored in the repository (such as organization variables), and only `check-workflows` reads `other`.
The configuration table stores only variable names and never stores variable values, so `repoutils variables` has no `ensure` subcommand.
Set a missing variable with `gh variable set NAME` (with `--env ENVIRONMENT` for an environment variable) or on the repository settings page.

`repoutils variables check` fails if the repository lacks any variable or environment listed in the table.
Unlike secret values, variable values are readable through the API, so `check` also fails when a variable value is empty or contains only whitespace.
`--exit-zero` and `--fail-on-unexpected` work in the same way as in `secrets check`.

`check` requires a token with the Variables `read` permission.
Checking environment variables also requires the Actions `read` and Environments `read` permissions.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have those permissions.

`repoutils variables dump` prints the name of every variable in the repository as a `[tool.repoutils.variables]` table.
It reads the variables from the API but omits their values from the output.

`repoutils variables check-workflows` requires no GitHub token, just like `secrets check-workflows`.
It scans `.github/workflows` for variable references written as `vars.NAME` or `vars['NAME']`.
It fails if a workflow references a variable that no configured list names, or if a configured list names a variable that no workflow references.
If you create a variable before any workflow uses it, list its name in `unused` in addition to its store list (`actions` or `environments.<name>`).
`check-workflows` then allows the unused variable, and fails once a workflow references that variable so that you remove it from `unused`.
The command also fails when a job references an environment-only variable without specifying one of the environments that hold that variable in `environment:`.
GitHub Actions evaluates a job's `if`, `runs-on`, `name`, and `environment` name expressions before loading the job environment ([actions/runner#2472](https://github.com/actions/runner/issues/2472)).
Variables scoped to a deployment environment always evaluate to empty strings in those fields.
Therefore, `check-workflows` also fails when any of those fields references an environment-only variable, even when the job deploys to that environment.

### Deploy keys

Each `[tool.repoutils.deploy-keys.<title>]` table declares one repository deploy key whose GitHub title is `<title>`.
Set `write = true` to grant write access to the key, or omit `write` to make the key read-only.
The `secret` key specifies the Actions secret that stores the private half of the deploy key, written as `{ name = "NAME" }` for a repository secret.
Add `environment = "ENVIRONMENT"` inside `secret` to store the private key in a deployment environment secret.
`pyproject.toml` does not store the public key material, so rotating a key requires no commit to the repository.
An empty `[tool.repoutils.deploy-keys]` table declares that the repository has no write-capable deploy keys.

`repoutils deploy-keys ensure` creates every missing deploy key that specifies `secret`.
It runs `ssh-keygen` to generate an Ed25519 key pair in a private temporary directory, and deletes that directory before exiting.
It uploads the public key to the repository and pipes the private key on standard input to `gh secret set`.
It passes its GitHub token to `gh` in the `GH_TOKEN` environment variable.
If `gh secret set` fails, `ensure` deletes the newly created deploy key from the repository.
Creating or rotating a key requires `ssh-keygen` and `gh`, whereas `check` and `dump` do not run either tool.

GitHub cannot change the read/write access of an existing deploy key.
If a key has the wrong access, the behavior of `ensure` depends on whether the key has a `secret`.
If it has a `secret`, `ensure` generates a new key pair, replaces the deploy key, and updates that `secret`.
If it lacks a `secret`, `ensure` re-adds the existing public key with the configured access.
Run `ensure --rotate <title>` to replace a deploy key and its secret, adding the new key before deleting the old key.
`ensure` refuses to create a missing key that omits `secret`, because the generated private key would not be saved anywhere.
It also fails when the target environment for `secret` does not exist, which `repoutils environments ensure` can create.
It never deletes a deploy key that `pyproject.toml` does not declare.

`repoutils deploy-keys check` does not modify the repository.
It fails in the following cases:

- a declared key is missing, has the wrong read/write access, or shares its title with another key in the repository
- the configured secret is missing
- the secret was updated before the deploy key was created – such a secret may not hold the private key of that deploy key

Because `ensure` cannot recover a lost private key to fix a missing or outdated secret, it instructs you to run `ensure --rotate <title>`.
`check` always fails on any undeclared write-capable deploy key, because a ruleset deploy-key bypass covers all write-capable deploy keys in the repository.
Pass `--fail-on-unexpected` to fail on undeclared read-only deploy keys as well.
`--exit-zero` works in the same way as in `labels check`.

`ensure` requires a token that can administer the repository, such as a fine-grained token with the Administration `write` and Secrets `write` permissions.
Storing a private key in an environment secret requires the Environments `write` and Actions `read` permissions instead of Secrets `write`.
`check` requires `read` access for the same permissions.

`repoutils deploy-keys dump` prints every deploy key in the repository as `[tool.repoutils.deploy-keys.<title>]` tables with their access settings.
The GitHub API does not link a deploy key to a secret, so add the `secret` setting to each dumped table manually.
If multiple deploy keys share the same title, `dump` prints that title once and writes a warning to standard error.
`dump` does not read or modify `pyproject.toml`.

### Security settings

`[tool.repoutils.security-settings]` configures the repository security settings on the Advanced Security settings page that the GitHub REST API supports, plus release immutability.
Every setting takes `true` or `false`.
Every setting is optional, but `check` and `ensure` fail if the table contains no settings.
Any setting omitted from the table keeps its current value on GitHub.

| Setting | On GitHub's settings pages |
|---|---|
| `private-vulnerability-reporting` | Private vulnerability reporting |
| `dependabot-alerts` | Dependabot alerts |
| `dependabot-security-updates` | Dependabot security updates |
| `codeql-default-setup` | CodeQL analysis, with the default setup (`true`) or not set up (`false`) |
| `secret-protection` | Secret Protection |
| `push-protection` | Push protection, under Secret Protection |
| `immutable-releases` | Enable release immutability, under Releases on the General page |

The Advanced Security page includes additional settings that the GitHub REST API cannot read or update independently, so `[tool.repoutils.security-settings]` rejects them.
Those unsupported settings are the dependency graph, automatic dependency submission, Dependabot malware alerts, grouped security updates, and Copilot Autofix.
Enabling `dependabot-alerts` automatically enables the dependency graph.
The `codeql-default-setup` setting only enables or disables CodeQL default setup, and leaves the analyzed languages and query suite to GitHub.
Enabling `immutable-releases` prevents anyone from modifying the assets of a published release or moving or deleting its Git tag.
Release immutability protects a tag only after release publication, so it does not replace a tag ruleset that restricts who can push release tags.

Some security settings depend on another setting.
`dependabot-security-updates` requires `dependabot-alerts`, and `push-protection` requires `secret-protection`.
The configuration table cannot enable a dependent setting while disabling the setting that it requires.
If the table enables a dependent setting and omits the required setting while that required setting is disabled on GitHub, `ensure` fails.
`ensure` disables settings before enabling settings, and applies changes in dependency order.

`repoutils security-settings ensure` updates every differing setting and then reads all settings back from GitHub.
It fails if GitHub did not apply every requested change.
GitHub can ignore or reject a change when the repository plan lacks the feature, such as CodeQL on a private repository without Code Security.
GitHub can also ignore a change when an organization policy or security configuration controls the setting, such as organization-wide immutable releases.
When enabling `codeql-default-setup`, GitHub configures CodeQL asynchronously, so `ensure` polls for up to two minutes until the setup completes.

`repoutils security-settings check` does not modify the repository, and fails in the exact cases where `ensure` would update a setting.
`--exit-zero` works in the same way as in `labels check`.

The `check`, `ensure` and `dump` subcommands require a token with repository administration access, because GitHub hides these settings from other tokens.
For a fine-grained personal access token, grant Administration `write` for `ensure`, and grant Administration `read` for `check` and `dump`.
The `GITHUB_TOKEN` provided by GitHub Actions cannot have the Administration permission.
When a token lacks permission to view a security setting, `check` fails instead of treating the setting as disabled.

`repoutils security-settings dump` prints every setting from the table above with its current repository value, under a comment with its GitHub UI label.
Paste the output into `pyproject.toml` and remove the settings that you do not want to manage.
If you paste the output without changes, `repoutils security-settings check` passes.
`dump` does not read or modify `pyproject.toml`.
If GitHub refuses to return a setting, `dump` omits that setting and prints a warning to standard error.

## Development

See [AGENTS.md](AGENTS.md).
