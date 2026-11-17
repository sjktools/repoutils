"""The Git checkout a command reads, which `--directory` can name instead of
the current directory."""

import os
from pathlib import Path

# The variables that tie Git to one repository, which a Git command run in
# another checkout would otherwise follow instead of finding that checkout's
# own. They're Git's own list for this, from `git rev-parse --local-env-vars`.
REPOSITORY_VARIABLES = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def use_checkout(directory: Path | None) -> Path:
    """The checkout to read: directory if given, else the current directory.

    Given a directory, it removes REPOSITORY_VARIABLES from this process's
    environment, so the Git commands it runs there read that checkout, and not
    one that such a variable names (as a Git hook's GIT_DIR does).
    """
    if directory is None:
        return Path.cwd()
    for name in REPOSITORY_VARIABLES:
        os.environ.pop(name, None)
    return directory
