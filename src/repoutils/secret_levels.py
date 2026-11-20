"""The secret names set at the repository and in each environment, which
``secrets inspect-remote`` and ``secrets export-remote`` list and pass to the
workflow they start, and which ``secrets inspect`` and ``secrets export`` read
from $SECRET_NAMES.

A job that deploys to an environment gets the repository's and the
organization's secrets as well as the environment's, and nothing in the run
says which level each came from. The run's own token can't list secrets, so
the caller, whose token can, tells each job which names its environment sets.
Kept apart from secret_inspect for length.
Stdlib-only, like every repoutils module."""

import json
from collections.abc import Iterable
from dataclasses import dataclass

# The environment variable the workflow passes the names set at each level in,
# as names_json() gives them.
NAMES_ENV_VAR = "SECRET_NAMES"


@dataclass(frozen=True)
class Levels:
    """The names of the secrets set at each level, upper-cased, as the caller listed them."""

    repository: frozenset[str]
    # By lower-cased environment name.
    environments: dict[str, frozenset[str]]

    def environment(self, name: str) -> frozenset[str]:
        """The names set in environment name, which has none if the caller didn't list it."""
        return self.environments.get(name.lower(), frozenset())

    def source(self, name: str) -> str:
        """Where a job gets the secret called name from, when its own environment doesn't set it."""
        return "the repository's" if name.upper() in self.repository else "the organization's"


def names_json(repository: Iterable[str], environments: dict[str, list[str]]) -> str:
    """The names set at each level, as JSON for $SECRET_NAMES: an object with
    the repository's under ``actions`` and each environment's under ``environments``."""
    body = {"actions": sorted(repository), "environments": {env: sorted(names) for env, names in environments.items()}}
    return json.dumps(body, separators=(",", ":"))


def _name_list(value: object, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError(f"${NAMES_ENV_VAR}'s {where} must be a list of names")
    return frozenset(name.upper() for name in value)


def read_levels(text: str | None) -> Levels | None:
    """The names names_json() gave, or None if text is None or empty, as when a
    run is started by hand without them."""
    if not text:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        raise ValueError(f"${NAMES_ENV_VAR} isn't JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("environments"), dict):
        raise ValueError(f"${NAMES_ENV_VAR} must be a JSON object with actions and environments")
    environments: dict[str, frozenset[str]] = {}
    for env, names in body["environments"].items():
        environments[str(env).lower()] = _name_list(names, f"environment {env}")
    return Levels(_name_list(body.get("actions"), "actions"), environments)


def unknown_levels(remote: str) -> str:
    """What an environment's job says when it wasn't given the names set at
    each level, which the command remote passes."""
    return (
        f"Started without ${NAMES_ENV_VAR} (as `repoutils {remote}` passes it), so a secret here "
        "may be the repository's or the organization's of the same name rather than the environment's own."
    )
