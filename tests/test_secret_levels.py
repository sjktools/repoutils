import re

import pytest

from repoutils import secret_levels

# The repository sets CLASSIC, FINE and STRAY, and the release environment sets
# DEPLOY_KEY, FINE (overriding the repository's) and EXTRA.
LEVELS = secret_levels.names_json(["CLASSIC", "FINE", "STRAY"], {"Release": ["DEPLOY_KEY", "FINE", "EXTRA"]})


def test_names_json_round_trips() -> None:
    levels = secret_levels.read_levels(LEVELS)
    assert levels == secret_levels.Levels(
        frozenset({"CLASSIC", "FINE", "STRAY"}), {"release": frozenset({"DEPLOY_KEY", "FINE", "EXTRA"})}
    )
    assert levels is not None
    assert levels.environment("RELEASE") == frozenset({"DEPLOY_KEY", "FINE", "EXTRA"})
    # An environment the caller didn't list sets nothing.
    assert levels.environment("pypi") == frozenset()
    assert levels.source("fine") == "the repository's"
    assert levels.source("ORG_TOKEN") == "the organization's"


@pytest.mark.parametrize("text", [None, ""])
def test_read_levels_without_names(text: str | None) -> None:
    assert secret_levels.read_levels(text) is None


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("nope", "isn't JSON"),
        ("[]", "must be a JSON object with actions and environments"),
        ('{"actions": []}', "must be a JSON object with actions and environments"),
        ('{"environments": {}}', "$SECRET_NAMES's actions must be a list of names"),
        ('{"actions": [], "environments": {"release": "A"}}', "environment release must be a list of names"),
        ('{"actions": [1], "environments": {}}', "actions must be a list of names"),
    ],
)
def test_read_levels_errors(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        secret_levels.read_levels(text)
