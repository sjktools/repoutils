import os
from unittest import TestCase, mock

from repoutils.checkout import REPOSITORY_VARIABLES, git_env


class GitEnvTests(TestCase):
    def test_drops_only_the_repository_variables_and_keeps_this_environment(self) -> None:
        variables = dict.fromkeys(REPOSITORY_VARIABLES, "x") | {"GIT_AUTHOR_NAME": "a", "HOME": "/home/a"}
        with mock.patch.dict("os.environ", variables, clear=True):
            self.assertEqual(git_env(), {"GIT_AUTHOR_NAME": "a", "HOME": "/home/a"})
            self.assertEqual(os.environ["GIT_DIR"], "x")
