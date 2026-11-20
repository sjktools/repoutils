"""Running Git in the checkout a command reads, which is the current
directory or the one `--directory` names."""

import os
import subprocess
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


def git_env() -> dict[str, str]:
    """This process's environment without REPOSITORY_VARIABLES, for a Git
    command (or gh, which runs Git) to find the repository from the directory
    it runs in, as these commands document, rather than from a variable such
    as the GIT_DIR a Git hook sets. This process's own environment is unchanged."""
    return {name: value for name, value in os.environ.items() if name not in REPOSITORY_VARIABLES}


def head_commit(directory: Path) -> tuple[str, bool] | None:
    """The commit checked out in the Git repository that holds `directory`, and
        whether anything under `directory` differs from it, untracked files included
    (whatever the status.showUntrackedFiles setting).
        None if Git isn't installed, or `directory` isn't in a repository with a commit."""
    try:
        commit = _git(directory, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        status = _git(directory, "status", "--porcelain", "--untracked-files=normal", "--", ".")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return commit.strip(), bool(status.strip())


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, env=git_env(), check=True, capture_output=True, text=True
    ).stdout
