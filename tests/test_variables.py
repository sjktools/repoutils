import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import config, github
from repoutils.commands import variables

CONFIG = """
[tool.repoutils.variables]
actions = ["EXPORT_KEY", "mode"]
"""


class FakeGitHub:
    """Answers github.request like the variables APIs of o/r would, with the given values by name."""

    def __init__(self, actions: dict[str, str], environments: dict[str, dict[str, str]] | None = None) -> None:
        self.existing = {"actions/variables": actions}
        self.environments = environments or {}
        for name, values in self.environments.items():
            self.existing[f"environments/{urllib.parse.quote(name, safe='')}/variables"] = values
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, token: str, **kwargs: object) -> Any:
        assert token == "tok"
        assert method == "GET"
        self.calls.append((method, url))
        path, _, query = url.removeprefix(f"{github.API_URL}/repos/o/r/").partition("?")
        if path == "environments":
            # The environments command's own page size.
            prefix = "per_page=100&page="
            assert query.startswith(prefix)
            page = int(query.removeprefix(prefix))
            envs = list(self.environments)[(page - 1) * 100 : page * 100]
            return {"total_count": len(self.environments), "environments": [{"name": name} for name in envs]}
        prefix = f"per_page={variables.PER_PAGE}&page="
        assert query.startswith(prefix)
        page = int(query.removeprefix(prefix))
        if path not in self.existing:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        items = list(self.existing[path].items())
        batch = items[(page - 1) * variables.PER_PAGE : page * variables.PER_PAGE]
        return {
            "total_count": len(items),
            "variables": [{"name": name, "value": value, "created_at": "", "updated_at": ""} for name, value in batch],
        }


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHub, argv: list[str], config_text: str = CONFIG
) -> int:
    (tmp_path / "pyproject.toml").write_text(config_text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    return variables.main([*argv, "--repo", "o/r"])


def test_check_passes_ignoring_case_and_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"EXPORT_KEY": "skissane/SHA256:x", "MODE": "fast", "EXTRA": "1"})
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0
    assert capsys.readouterr().out == "All 2 variable(s) pyproject.toml lists exist in o/r with a value.\n"


def test_check_reports_missing_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"MODE": " \n"})
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert capsys.readouterr().out == (
        "Actions variable EXPORT_KEY: missing\n"
        "Actions variable mode: empty\n"
        "2 of 2 variable(s) are missing from o/r or empty: "
        "set them with `gh variable set` (`--env NAME` for an environment's).\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub({"MODE": ""}), ["check", "--exit-zero"]) == 0
    assert "2 of 2 variable(s) are missing from o/r or empty" in capsys.readouterr().out


def test_check_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"EXPORT_KEY": "k", "MODE": "m", "EXTRA": "e"})
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert capsys.readouterr().out == (
        "Actions variable EXTRA: not listed in pyproject.toml\n"
        "1 variable(s) in o/r aren't listed in pyproject.toml: "
        "add them there (`repoutils variables dump` prints them) or delete them on GitHub.\n"
    )


def test_empty_list_means_none_with_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"UNLISTED": "x"})
    config_text = "[tool.repoutils.variables]\nactions = []\n"
    assert _run(tmp_path, monkeypatch, gh, ["check"], config_text) == 0
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], config_text) == 1
    assert "Actions variable UNLISTED: not listed" in capsys.readouterr().out


def test_paginates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    many = {f"V{i:03}": "x" for i in range(variables.PER_PAGE + 1)}
    gh = FakeGitHub(many)
    config_text = f'[tool.repoutils.variables]\nactions = ["{list(many)[-1]}"]\n'
    assert _run(tmp_path, monkeypatch, gh, ["check"], config_text) == 0
    assert len(gh.calls) == 2


def test_dump_round_trips_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"ZED": "secret-ish", "ALPHA": "a"}, {"release": {"B": "b"}, "pypi": {}})
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out = capsys.readouterr().out
    assert out == (
        '[tool.repoutils.variables]\nactions = [\n    "ALPHA",\n    "ZED",\n]\nenvironments.release = [\n    "B",\n]\n'
    )
    assert "secret-ish" not in out
    assert tomllib.loads(out)["tool"]["repoutils"]["variables"] == {
        "actions": ["ALPHA", "ZED"],
        "environments": {"release": ["B"]},
    }
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0
    assert capsys.readouterr().out == (
        "All 3 variable(s) pyproject.toml lists exist in o/r with a value, and it has no others of those kinds.\n"
    )


def test_dump_with_no_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub({}), ["dump"], "") == 0
    assert capsys.readouterr().out == "[tool.repoutils.variables]\nactions = []\nenvironments = {}\n"


def test_no_variables_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({})
    assert _run(tmp_path, monkeypatch, gh, ["check"], '[tool.repoutils.variables]\nother = ["ORG"]\n') == 1
    assert gh.calls == []
    assert "No variables are listed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config_text", "error"),
    [
        ("[tool.repoutils]\nvariables = 1\n", "[tool.repoutils.variables] must be a table"),
        ("[tool.repoutils.variables]\ndependabot = []\n", "unknown setting(s): dependabot"),
        ("[tool.repoutils.variables]\nactions = 'A'\n", "actions must be a list of strings"),
        ("[tool.repoutils.variables]\nactions = ['1A']\n", "'1A' isn't a valid variable name"),
        ("[tool.repoutils.variables]\nactions = ['GITHUB_X']\n", "'GITHUB_X' isn't a valid variable name"),
        ("[tool.repoutils.variables]\nactions = ['A', 'a']\n", "'a' is listed twice (variable names ignore case)"),
        ("[tool.repoutils.variables]\nenvironments = []\n", "must be a table of each environment's variables"),
    ],
)
def test_load_errors(tmp_path: Path, config_text: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    with pytest.raises(config.ConfigError, match=re.escape(error)):
        variables.load(tmp_path)


def test_config_error_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.variables]\nactions = [1]\n")
    monkeypatch.chdir(tmp_path)
    assert variables.main(["check", "--exit-zero"]) == 1
    assert "must be a list of strings" in capsys.readouterr().err


def test_http_error_fails_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(method: str, url: str, token: str, **kwargs: object) -> Any:
        raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), None)

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", fail)
    assert variables.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert "HTTP 403 from" in capsys.readouterr().err


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    monkeypatch.setattr(github, "gh_output", gh_output)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert variables.main(["dump", "--repo", "o/r"]) == 1
    assert "gh auth token failed:\nnot logged in" in capsys.readouterr().err


ENV_CONFIG = """
[tool.repoutils.variables]
environments.release = ["EXPORT_KEY"]
environments.autofix = ["MODE"]
"""


def test_check_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({"UNCHECKED": ""}, {"Release": {"EXPORT_KEY": "k"}, "autofix": {"MODE": "m"}, "pypi": {}})
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], ENV_CONFIG) == 0
    assert capsys.readouterr().out == (
        "All 2 variable(s) pyproject.toml lists exist in o/r with a value, and it has no others of those kinds.\n"
    )
    # The repository's own variables aren't listed, so aren't fetched.
    assert not any("actions/variables" in url for _, url in gh.calls)


def test_check_environments_reports_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({}, {"release": {"EXPORT_KEY": "", "EXTRA": "e"}, "my env": {"STRAY": "s"}})
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], ENV_CONFIG) == 1
    assert capsys.readouterr().out == (
        "Environment release variable EXPORT_KEY: empty\n"
        "Environment release variable EXTRA: not listed in pyproject.toml\n"
        "Environment autofix: missing, with its 1 variable(s)\n"
        "Environment my env variable STRAY: not listed in pyproject.toml\n"
        "1 environment(s) are missing from o/r: "
        "create them with `repoutils environments ensure` or on the repository's settings page.\n"
        "2 of 2 variable(s) are missing from o/r or empty: "
        "set them with `gh variable set` (`--env NAME` for an environment's).\n"
        "2 variable(s) in o/r aren't listed in pyproject.toml: "
        "add them there (`repoutils variables dump` prints them) or delete them on GitHub.\n"
    )


def test_check_missing_empty_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = "[tool.repoutils.variables]\nenvironments.release = []\n"
    assert _run(tmp_path, monkeypatch, FakeGitHub({}), ["check"], config_text) == 1
    assert capsys.readouterr().out.startswith("Environment release: missing\n1 environment(s) are missing")


def _workflows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str, **files: str) -> int:
    (tmp_path / "pyproject.toml").write_text(config_text)
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name.replace("_", ".")).write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return variables.main(["check-workflows"])


WORKFLOW = """
env:
  A: ${{ vars.EXPORT_KEY }}
  B: ${{ vars['mode'] }}
  C: ${{ secrets.NOT_A_VARIABLE }}
  # D: ${{ vars.COMMENTED_OUT }}
  E: ${{ vars[ "ORG_VAR" ] }}
"""


def test_check_workflows_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = CONFIG + 'other = ["ORG_VAR"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=WORKFLOW) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 3 variable(s) pyproject.toml lists.\n"


def test_check_workflows_reports_unlisted_and_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = CONFIG + 'other = ["UNUSED"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=WORKFLOW) == 1
    assert capsys.readouterr().out == (
        "ORG_VAR: used by ci.yml, but not listed in pyproject.toml\n"
        "UNUSED: listed in pyproject.toml, but no workflow uses it\n"
        "1 variable(s) the workflows use aren't listed in pyproject.toml: add each to [tool.repoutils.variables], "
        "under actions or environments.<name>, or under other if the repository doesn't hold it.\n"
        "1 variable(s) pyproject.toml lists aren't used by any workflow: remove them from [tool.repoutils.variables], "
        "or list them under unused too if that's on purpose.\n"
    )


def test_check_workflows_needs_no_table_or_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert variables.main(["check-workflows"]) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 0 variable(s) pyproject.toml lists.\n"


ENV_WORKFLOW = """
jobs:
  export:
    environment: release
    env:
      K: ${{ vars.EXPORT_KEY }}
  bare:
    steps:
      - run: echo ${{ vars.EXPORT_KEY }} ${{ vars.SHARED }}
  dynamic:
    environment: ${{ inputs.environment }}
    env:
      K: ${{ vars.EXPORT_KEY }}
"""


def test_check_workflows_reports_job_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # SHARED is the repository's too, so a job without the environment gets it.
    config_text = '[tool.repoutils.variables]\nactions = ["SHARED"]\nenvironments.release = ["EXPORT_KEY", "SHARED"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=ENV_WORKFLOW) == 1
    assert capsys.readouterr().out == (
        "EXPORT_KEY: used by job bare in ci.yml, which declares no environment, "
        "but only environment release holds it\n"
        "1 use(s) of an environment's variable are in a job that doesn't deploy to it, "
        "so the variable is empty there: give the job that environment.\n"
    )


def test_check_workflows_reports_job_level_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = """
jobs:
  gated:
    if: ${{ vars.ENABLED == 'true' }}
    environment: release
    steps:
      - run: echo ${{ vars.ENABLED }}
  runner:
    runs-on:
      - ${{ vars.RUNNER }}
    environment: release
  shared:
    name: ${{ vars.SHARED }}
    environment: release
  fine:
    environment: release
    steps:
      - if: ${{ vars.ENABLED }}
        run: echo ${{ vars.RUNNER }}
"""
    config_text = (
        '[tool.repoutils.variables]\nactions = ["SHARED"]\nenvironments.release = ["ENABLED", "RUNNER", "SHARED"]\n'
    )
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=workflow) == 1
    assert capsys.readouterr().out == (
        "ENABLED: used by job gated in ci.yml in its if, where an environment's variables are empty\n"
        "RUNNER: used by job runner in ci.yml in its runs-on, where an environment's variables are empty\n"
        "2 use(s) of an environment's variable are in a job's if, runs-on, environment, name, "
        "which GitHub evaluates before the job enters its environment: "
        "make it a repository variable (under actions) or move the use into a step.\n"
    )


def test_job_level_use_is_reported_once_without_an_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = "jobs:\n  a:\n    if: vars.ENABLED\n    steps:\n      - run: echo\n"
    config_text = '[tool.repoutils.variables]\nenvironments.release = ["ENABLED"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=workflow) == 1
    out = capsys.readouterr().out
    assert "ENABLED: used by job a in ci.yml in its if" in out
    assert "doesn't deploy to it" not in out


def test_check_workflows_accepts_unused_on_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = CONFIG + 'other = ["ORG_VAR"]\nenvironments.release = ["LATER"]\nunused = ["later"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=WORKFLOW) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 4 variable(s) pyproject.toml lists.\n"


def test_check_workflows_fails_if_unused_on_purpose_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = CONFIG + 'other = ["ORG_VAR"]\nunused = ["mode"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=WORKFLOW, b_yml="env:\n  M: ${{ vars.MODE }}\n") == 1
    assert capsys.readouterr().out == (
        "mode: listed as unused in pyproject.toml, but used by b.yml, ci.yml\n"
        "1 variable(s) pyproject.toml lists as unused are used by a workflow: "
        "remove them from unused in [tool.repoutils.variables].\n"
    )


def test_unused_must_be_listed_elsewhere(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoutils.variables]\nactions = ["A"]\nunused = ["B"]\n')
    with pytest.raises(
        config.ConfigError, match=re.escape("unused 'B' isn't listed under actions, environments or other")
    ):
        variables.load(tmp_path)


def test_unused_alone_is_not_enough_for_check(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoutils.variables]\nother = ["A"]\nunused = ["A"]\n')
    listed = variables.load(tmp_path)
    assert listed.lists == {"other": ["A"]}
    assert not variables.checks_anything(listed)
