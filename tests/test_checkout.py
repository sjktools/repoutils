import os
import subprocess
import tempfile
from pathlib import Path
from typing import override
from unittest import TestCase, mock

from repoutils.checkout import REPOSITORY_VARIABLES, git_env, head_commit


class GitEnvTests(TestCase):
    def test_drops_only_the_repository_variables_and_keeps_this_environment(self) -> None:
        variables = dict.fromkeys(REPOSITORY_VARIABLES, "x") | {"GIT_AUTHOR_NAME": "a", "HOME": "/home/a"}
        with mock.patch.dict("os.environ", variables, clear=True):
            self.assertEqual(git_env(), {"GIT_AUTHOR_NAME": "a", "HOME": "/home/a"})
            self.assertEqual(os.environ["GIT_DIR"], "x")


class HeadCommitTests(TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        (repo / "file.txt").write_text(name)
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", name]):
            git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
            subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True, env=git_env())
        return repo

    def head(self, repo: Path) -> str:
        git = ["git", "rev-parse", "HEAD"]
        return subprocess.run(git, cwd=repo, check=True, capture_output=True, text=True, env=git_env()).stdout

    def test_the_commit_and_whether_it_has_changes(self) -> None:
        repo = self.repo("a")
        self.assertEqual(head_commit(repo), (self.head(repo).strip(), False))
        (repo / "file.txt").write_text("changed")
        self.assertEqual(head_commit(repo), (self.head(repo).strip(), True))

    def test_untracked_files_count_whatever_the_config(self) -> None:
        repo = self.repo("a")
        subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=repo, check=True, env=git_env())
        (repo / "new.txt").write_text("")
        self.assertEqual(head_commit(repo), (self.head(repo).strip(), True))

    def test_reads_the_directory_rather_than_git_dir(self) -> None:
        a, b = self.repo("a"), self.repo("b")
        expected = (self.head(a).strip(), False)
        with mock.patch.dict("os.environ", {"GIT_DIR": str(b / ".git")}):
            self.assertEqual(head_commit(a), expected)

    def test_none_outside_a_repository(self) -> None:
        self.assertIsNone(head_commit(self.root))

    def test_none_in_a_repository_without_a_commit(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.assertIsNone(head_commit(self.root))

    def test_none_without_git(self) -> None:
        repo = self.repo("a")
        with mock.patch.dict("os.environ", {"PATH": str(self.root)}):
            self.assertIsNone(head_commit(repo))
