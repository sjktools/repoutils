"""Check that a repository has the Actions and environment variables
pyproject.toml names, with a value, or print the names it has.

``[tool.repoutils.variables]`` lists the names: ``actions`` for the
repository's Actions variables, and ``environments.<name>`` for each
deployment environment's. A list the table leaves out isn't checked. Names
match regardless of case, as on GitHub. ``pyproject.toml`` only ever holds
names, never values, so there's no ``ensure``: variables are set with
``gh variable set`` (``--env NAME`` for an environment's) or on the
repository's settings page.

``check`` fails if the repository lacks a variable the table names, or an
environment it names, or if a variable's value is empty (only whitespace).
With ``--fail-on-unexpected`` it also fails on variables of a listed kind that
the table doesn't name (an empty list then means there must be none). If the
table has ``environments``, that covers every environment's variables, so
``environments = {}`` means no environment may have any.

``dump`` prints the names of every variable the repository has as the table,
never their values, to paste into pyproject.toml.

``check-workflows`` needs no token: it fails if a workflow in
``.github/workflows`` uses a variable (``vars.NAME``) the table doesn't name,
or the table names one that no workflow uses. It also fails if a job uses a
variable that only environments hold without declaring one of them as its
``environment``, or uses one in a key GitHub evaluates before the job enters
its environment (``if``, ``runs-on``, ``environment`` or ``name``), where it's
always empty. ``other`` lists the variables workflows use that the
repository doesn't hold (such as an organization's), which ``check`` doesn't
look for. ``unused`` lists variables, from the other lists, that no workflow
uses yet on purpose (such as one set up ahead of the workflow that will read
it): ``check-workflows`` doesn't report them as unused, and fails if a
workflow does use one, so the mark can't outlive its reason.

This shares its table's format, and most of its workings, with
``repoutils secrets``. Stdlib-only, like every repoutils command."""

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterable
from pathlib import Path

from repoutils import config, github, workflows
from repoutils.commands import secrets

SECTION = "variables"
TABLE = f"[tool.repoutils.{SECTION}]"
# The key in TABLE for the repository's own variables.
ACTIONS = "actions"
# The key in TABLE for the variables, listed elsewhere in it, that no workflow uses on purpose.
UNUSED = "unused"
# Variables per page of the list (GitHub's most, lower than for secrets).
PER_PAGE = 30


def load_with_unused(root: Path = Path()) -> tuple[secrets.Listed, list[str]]:
    """What ``[tool.repoutils.variables]`` in ``root/pyproject.toml`` lists, and
    its ``unused`` list apart; raising config.ConfigError (a ValueError) if
    it's malformed, or ``unused`` names a variable no other list does."""
    listed = secrets.load_listed(SECTION, [ACTIONS, secrets.OTHER, UNUSED], "variable", root)
    unused = listed.lists.pop(UNUSED, [])
    elsewhere = {name.upper() for name in listed.all_names()}
    for name in unused:
        if name.upper() not in elsewhere:
            raise config.ConfigError(
                f"{TABLE} {UNUSED} {name!r} isn't listed under {ACTIONS}, environments or {secrets.OTHER}"
            )
    return listed, unused


def load(root: Path = Path()) -> secrets.Listed:
    """What ``[tool.repoutils.variables]`` in ``root/pyproject.toml`` lists,
    without ``unused``; raising config.ConfigError (a ValueError) if it's malformed."""
    return load_with_unused(root)[0]


def checks_anything(listed: secrets.Listed) -> bool:
    """Whether listed has anything that ``check`` looks for (``other`` alone isn't)."""
    return ACTIONS in listed.lists or listed.environments is not None


def job_level_uses(listed: secrets.Listed, jobs: Iterable[workflows.Job]) -> list[str]:
    """A line for each variable that only environments hold that a job uses in
    one of workflows.JOB_LEVEL_KEYS, where it's empty even if the job deploys
    to one of those environments."""
    repository = {name.upper() for names in listed.lists.values() for name in names}
    environment_only = {name.upper() for names in (listed.environments or {}).values() for name in names} - repository
    return [
        f"{name}: used by job {job.id} in {job.file} in its {', '.join(workflows.JOB_LEVEL_KEYS)}, "
        "where an environment's variables are empty"
        for job in jobs
        for name in sorted(job.job_level_variables & environment_only)
    ]


def check_workflows(root: Path = Path()) -> int:
    loaded, marked = load_with_unused(root)
    listed: dict[str, str] = {}
    for name in loaded.all_names():
        listed.setdefault(name.upper(), name)
    used = workflows.workflow_variables(root)
    unused_on_purpose = {name.upper() for name in marked}
    unlisted = sorted(set(used) - set(listed))
    unused = sorted(listed[name] for name in set(listed) - set(used) - unused_on_purpose)
    used_after_all = [name for name in marked if name.upper() in used]
    jobs = workflows.workflow_jobs(root)
    job_level = job_level_uses(loaded, jobs)
    # A job-level use is reported as such, whatever environment the job declares.
    undeclared = secrets.undeclared_environments(loaded, jobs, lambda job: job.variables - job.job_level_variables)
    for name in unlisted:
        print(f"{name}: used by {', '.join(used[name])}, but not listed in pyproject.toml")
    for name in unused:
        print(f"{name}: listed in pyproject.toml, but no workflow uses it")
    for name in used_after_all:
        print(f"{name}: listed as {UNUSED} in pyproject.toml, but used by {', '.join(used[name.upper()])}")
    for line in [*undeclared, *job_level]:
        print(line)
    if unlisted:
        print(
            f"{len(unlisted)} variable(s) the workflows use aren't listed in pyproject.toml: add each to {TABLE}, "
            f"under {ACTIONS} or environments.<name>, or under {secrets.OTHER} if the repository doesn't hold it."
        )
    if unused:
        print(
            f"{len(unused)} variable(s) pyproject.toml lists aren't used by any workflow: remove them from {TABLE}, "
            f"or list them under {UNUSED} too if that's on purpose."
        )
    if used_after_all:
        print(
            f"{len(used_after_all)} variable(s) pyproject.toml lists as {UNUSED} are used by a workflow: "
            f"remove them from {UNUSED} in {TABLE}."
        )
    if undeclared:
        print(
            f"{len(undeclared)} use(s) of an environment's variable are in a job that doesn't deploy to it, "
            "so the variable is empty there: give the job that environment."
        )
    if job_level:
        print(
            f"{len(job_level)} use(s) of an environment's variable are in a job's "
            f"{', '.join(workflows.JOB_LEVEL_KEYS)}, which GitHub evaluates before the job enters its environment: "
            f"make it a repository variable (under {ACTIONS}) or move the use into a step."
        )
    if unlisted or unused or used_after_all or undeclared or job_level:
        return 1
    print(f"The workflows use exactly the {len(listed)} variable(s) pyproject.toml lists.")
    return 0


def _variables(url: str, token: str) -> dict[str, str]:
    """The value of every variable the list at url gives, by name."""
    values: dict[str, str] = {}
    page = 1
    while True:
        batch = github.request("GET", f"{url}?per_page={PER_PAGE}&page={page}", token)["variables"]
        values.update((variable["name"], variable["value"]) for variable in batch)
        if len(batch) < PER_PAGE:
            return values
        page += 1


def repo_variables(repo: str, token: str) -> dict[str, str]:
    """The value of every Actions variable the repository has, by name."""
    return _variables(f"{github.API_URL}/repos/{repo}/actions/variables", token)


def environment_variables(repo: str, token: str, environment: str) -> dict[str, str]:
    """The value of every variable the repository's environment has, by name."""
    quoted = urllib.parse.quote(environment, safe="")
    return _variables(f"{github.API_URL}/repos/{repo}/environments/{quoted}/variables", token)


def dump(repo: str, token: str) -> int:
    existing = {ACTIONS: list(repo_variables(repo, token))}
    environments = {
        name: list(environment_variables(repo, token, name)) for name in secrets.repo_environments(repo, token)
    }
    print(secrets.dump_toml(existing, {name: names for name, names in environments.items() if names}, TABLE), end="")
    return 0


def _compare(where: str, names: list[str], existing: dict[str, str], *, fail_on_unexpected: bool) -> tuple[int, int]:
    """Print each variable of names that existing lacks or has empty, and with
    fail_on_unexpected each it has that names lacks. Returns how many are
    missing or empty, and how many unexpected."""
    values = {name.upper(): value for name, value in existing.items()}
    missing = secrets.missing_names(names, existing)
    empty = [name for name in names if name.upper() in values and not values[name.upper()].strip()]
    unexpected = secrets.missing_names(existing, names) if fail_on_unexpected else []
    for name in missing:
        print(f"{where} variable {name}: missing")
    for name in empty:
        print(f"{where} variable {name}: empty")
    for name in unexpected:
        print(f"{where} variable {name}: not listed in pyproject.toml")
    return len(missing) + len(empty), len(unexpected)


def check(
    repo: str,
    token: str,
    listed: secrets.Listed,
    *,
    exit_zero: bool,
    fail_on_unexpected: bool,
) -> int:
    bad_count = unexpected_count = missing_environments = 0
    if ACTIONS in listed.lists:
        bad_count, unexpected_count = _compare(
            "Actions", listed.lists[ACTIONS], repo_variables(repo, token), fail_on_unexpected=fail_on_unexpected
        )
    if listed.environments is not None:
        existing_environments = {name.lower(): name for name in secrets.repo_environments(repo, token)}
        to_check = dict(listed.environments)
        if fail_on_unexpected:
            # Every other environment the repository has may have no variables. They
            # come from existing_environments, so none is reported as missing.
            names = {name.lower() for name in listed.environments}
            to_check.update({name: [] for lower, name in existing_environments.items() if lower not in names})
        for name, wanted in to_check.items():
            actual = existing_environments.get(name.lower())
            if actual is None:
                print(f"Environment {name}: missing" + (f", with its {len(wanted)} variable(s)" if wanted else ""))
                missing_environments += 1
                bad_count += len(wanted)
                continue
            bad, unexpected = _compare(
                f"Environment {actual}",
                wanted,
                environment_variables(repo, token, actual),
                fail_on_unexpected=fail_on_unexpected,
            )
            bad_count += bad
            unexpected_count += unexpected
    total = len(listed.lists.get(ACTIONS, [])) + sum(len(names) for names in (listed.environments or {}).values())
    if missing_environments:
        print(
            f"{missing_environments} environment(s) are missing from {repo}: "
            "create them with `repoutils environments ensure` or on the repository's settings page."
        )
    if bad_count:
        print(
            f"{bad_count} of {total} variable(s) are missing from {repo} or empty: "
            "set them with `gh variable set` (`--env NAME` for an environment's)."
        )
    if unexpected_count:
        print(
            f"{unexpected_count} variable(s) in {repo} aren't listed in pyproject.toml: "
            "add them there (`repoutils variables dump` prints them) or delete them on GitHub."
        )
    if bad_count or unexpected_count or missing_environments:
        return 0 if exit_zero else 1
    print(
        f"All {total} variable(s) pyproject.toml lists exist in {repo} with a value"
        + (", and it has no others of those kinds." if fail_on_unexpected else ".")
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils variables",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="check and dump read variables with $GITHUB_TOKEN, defaulting to `gh auth token`; "
        "it needs the Variables read permission, and Actions and Environments read for environment variables. "
        "dump prints names only, never values. Organization variables don't count.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,dump,check-workflows}")
    check_parser = subparsers.add_parser(
        "check",
        help="fail if a variable pyproject.toml lists is missing or empty",
        description="Fail if the repository lacks a variable, or an environment, pyproject.toml lists, "
        "or a variable's value is empty.",
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's variable names as pyproject.toml settings",
        description=f"Print the name (never the value) of every variable the repository has as {TABLE}, "
        "to paste into pyproject.toml.",
    )
    subparsers.add_parser(
        "check-workflows",
        help="fail if the workflows' variables and pyproject.toml's differ (needs no token)",
        description="Fail if a workflow in .github/workflows uses a variable pyproject.toml doesn't list, "
        "or pyproject.toml lists one no workflow uses, "
        "or a job uses an environment's variable without deploying to that environment. Needs no token.",
    )
    for sub in (check_parser, dump_parser):
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if variables are missing or empty (still exit 1 on an error, unlike `|| true`)",
    )
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="also fail on variables of a kind pyproject.toml lists that it doesn't name",
    )
    args = parser.parse_args(argv)

    try:
        if args.mode == "check-workflows":
            return check_workflows()
        # dump doesn't read pyproject.toml, so it works before there are any variables there.
        listed = secrets.Listed({}, None) if args.mode == "dump" else load()
        if args.mode == "check" and not checks_anything(listed):
            print(
                f"No variables are listed: add {ACTIONS} or environments to {TABLE} in pyproject.toml.",
                file=sys.stderr,
            )
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        return check(repo, token, listed, exit_zero=args.exit_zero, fail_on_unexpected=args.fail_on_unexpected)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1


run = main
