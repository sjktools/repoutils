import os
from pathlib import Path
from unittest import TestCase, mock

from repoutils.checkout import REPOSITORY_VARIABLES, use_checkout


class UseCheckoutTests(TestCase):
    def test_defaults_to_the_current_directory_and_keeps_the_environment(self) -> None:
        with mock.patch.dict("os.environ", {"GIT_DIR": "/elsewhere/.git"}):
            self.assertEqual(use_checkout(None), Path.cwd())
            self.assertEqual(os.environ["GIT_DIR"], "/elsewhere/.git")

    def test_a_directory_drops_only_the_repository_variables(self) -> None:
        variables = dict.fromkeys(REPOSITORY_VARIABLES, "x") | {"GIT_AUTHOR_NAME": "a", "HOME": "/home/a"}
        with mock.patch.dict("os.environ", variables):
            self.assertEqual(use_checkout(Path("head")), Path("head"))
            self.assertFalse(set(REPOSITORY_VARIABLES) & set(os.environ))
            self.assertEqual((os.environ["GIT_AUTHOR_NAME"], os.environ["HOME"]), ("a", "/home/a"))
