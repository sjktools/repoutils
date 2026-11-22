import base64
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import override
from unittest import TestCase, mock

from repoutils.checkout import git_env
from repoutils.commands import push_bundle
from repoutils.commands.push_bundle import denied_paths


class DeniedPathsTests(TestCase):
    def test_no_rules_allow_everything(self) -> None:
        self.assertEqual(denied_paths(["a", "b/c"], [], []), [])

    def test_allow_paths_alone_deny_every_other_path(self) -> None:
        changed = ["pyproject.toml", "uv.lock", "src/a.py", "uv.lock.bak"]
        self.assertEqual(denied_paths(changed, ["pyproject.toml", "uv.lock"], []), ["src/a.py", "uv.lock.bak"])

    def test_deny_paths_with_allowed_exceptions(self) -> None:
        changed = [".github/workflows/ci.yml", ".github/workflows/actions.lock", ".github/actions/x.yml", "a.py"]
        denied = denied_paths(changed, [".github/workflows/actions.lock"], [".github/workflows/"])
        self.assertEqual(denied, [".github/workflows/ci.yml"])

    def test_the_root_covers_every_path(self) -> None:
        for root in (".", "/", "", "./"):
            self.assertEqual(denied_paths(["a", "b/c"], ["b"], [root]), ["a"], root)

    def test_paths_are_normalized(self) -> None:
        self.assertEqual(denied_paths(["src/a", "b"], [], ["./src/"]), ["src/a"])

    def test_a_directory_covers_only_paths_under_it(self) -> None:
        self.assertEqual(denied_paths(["src", "src/a", "srcs/a"], [], ["src"]), ["src", "src/a"])


class PushBundleTests(TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        # The job that made the bundle, and the push job's checkout.
        self.maker = self.root / "maker"
        self.git(self.root, "clone", "-q", str(self.remote), str(self.maker))
        self.write("a.txt", "a\n")
        self.start = self.commit("start")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/topic")
        self.bundle = self.root / "fixes.bundle"
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        self.checkout = self.root / "checkout"
        self.git(self.root, "clone", "-q", str(self.remote), str(self.checkout))
        os.chdir(self.checkout)

    def git(self, directory: Path, *args: str, check: bool = True) -> str:
        git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args]
        result = subprocess.run(git, cwd=directory, check=check, capture_output=True, text=True, env=git_env())
        return result.stdout.strip()

    def write(self, path: str, content: str) -> None:
        (self.maker / path).parent.mkdir(parents=True, exist_ok=True)
        (self.maker / path).write_text(content)

    def commit(self, message: str) -> str:
        self.git(self.maker, "add", "-A")
        self.git(self.maker, "commit", "-qm", message)
        return self.git(self.maker, "rev-parse", "HEAD")

    def make_bundle(self, since: str | None = None) -> str:
        self.git(self.maker, "bundle", "create", "-q", str(self.bundle), f"{since or self.start}..HEAD")
        return self.git(self.maker, "rev-parse", "HEAD")

    def commit_and_bundle(self) -> str:
        self.commit("fix")
        return self.make_bundle()

    def branch_head(self, branch: str = "topic") -> str:
        return self.git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def advance_main(self) -> str:
        """Commit b.txt on main and push it, leaving the maker on topic's start."""
        self.git(self.maker, "checkout", "-qb", "main-work")
        self.write("b.txt", "b\n")
        base = self.commit("on main")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.git(self.maker, "checkout", "-q", self.start)
        self.git(self.checkout, "fetch", "-q", "origin")
        return base

    def push(
        self,
        *,
        expected_head: str | None = None,
        single_commit: bool = False,
        clean_merge_of: str | None = None,
        allow_paths: Sequence[str] = (),
        deny_paths: Sequence[str] = (),
    ) -> int:
        return push_bundle.main(
            self.bundle,
            "topic",
            self.start,
            str(self.remote),
            "secret",
            expected_head=expected_head,
            single_commit=single_commit,
            clean_merge_of=clean_merge_of,
            allow_paths=allow_paths,
            deny_paths=deny_paths,
        )

    def test_pushes_commits_that_add_to_the_start(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit("one")
        self.write("c.txt", "c\n")
        head = self.make_bundle()
        with mock.patch("sys.stdout") as stdout:
            self.assertEqual(self.push(), 0)
        self.assertEqual(self.branch_head(), head)
        stdout.write.assert_any_call(f"Pushed {head} to topic.")

    def test_the_token_goes_in_an_extra_header_and_no_hooks_run(self) -> None:
        with mock.patch.object(push_bundle, "_git", return_value="") as git:
            push_bundle.push("h" * 40, "topic", "s" * 40, "https://github.com/o/r.git", "secret")
        args = git.call_args.args
        env = git.call_args.kwargs["env"]
        self.assertEqual(args[:2], ("-c", "core.hooksPath=/dev/null"))
        self.assertIn(f"--force-with-lease=refs/heads/topic:{'s' * 40}", args)
        self.assertFalse(any("secret" in arg for arg in args))
        credentials = base64.b64encode(b"x-access-token:secret").decode()
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/o/r.git.extraheader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], f"AUTHORIZATION: basic {credentials}")

    def test_refuses_a_head_that_adds_nothing(self) -> None:
        self.git(self.maker, "commit", "-q", "--allow-empty", "-m", "x")
        self.git(self.maker, "reset", "-q", "--hard", self.start)
        self.git(self.maker, "bundle", "create", "-q", str(self.bundle), "HEAD")
        self.assertEqual(self.push(), 1)
        self.assertEqual(self.branch_head(), self.start)

    def test_refuses_a_head_that_doesnt_descend_from_the_start(self) -> None:
        self.git(self.maker, "checkout", "-q", "--orphan", "other")
        self.write("x.txt", "x\n")
        self.commit("unrelated")
        self.git(self.maker, "bundle", "create", "-q", str(self.bundle), "HEAD")
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(), 1)
        self.assertIn("doesn't add commits", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_refuses_an_unexpected_head(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit("one")
        self.make_bundle()
        self.assertEqual(self.push(expected_head=self.start), 1)
        self.assertEqual(self.branch_head(), self.start)

    def test_expected_head(self) -> None:
        self.write("a.txt", "fixed\n")
        head = self.commit("one")
        self.make_bundle()
        self.assertEqual(self.push(expected_head=head), 0)

    def test_single_commit(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit("one")
        self.make_bundle()
        self.assertEqual(self.push(single_commit=True), 0)

    def test_refuses_more_than_one_commit_with_single_commit(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit("one")
        self.write("a.txt", "fixed again\n")
        self.commit("two")
        self.make_bundle()
        self.assertEqual(self.push(single_commit=True), 1)

    def test_refuses_a_denied_path(self) -> None:
        self.write("pyproject.toml", "x\n")
        self.write("src/a.py", "x\n")
        self.commit_and_bundle()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(allow_paths=["pyproject.toml", "uv.lock"]), 1)
        self.assertIn("src/a.py", "".join(c.args[0] for c in stderr.write.call_args_list))
        self.assertEqual(self.branch_head(), self.start)

    def test_a_path_that_isnt_utf_8(self) -> None:
        (self.maker / os.fsdecode(b"caf\xe9.txt")).write_text("x\n")
        self.commit_and_bundle()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(allow_paths=["a.txt"]), 1)
        self.assertIn(os.fsdecode(b"caf\xe9.txt"), "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_a_file_moved_out_of_a_denied_path_is_denied(self) -> None:
        self.git(self.maker, "mv", "a.txt", "b.txt")
        self.commit_and_bundle()
        self.assertEqual(self.push(allow_paths=["b.txt"]), 1)

    def test_refuses_a_push_once_the_branch_has_moved(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit_and_bundle()
        self.git(self.maker, "push", "-q", "--force", "origin", "HEAD:refs/heads/topic")
        self.git(self.maker, "commit", "-q", "--allow-empty", "-m", "later")
        later = self.git(self.maker, "rev-parse", "HEAD")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.push(), 1)
        self.assertEqual(self.branch_head(), later)

    def test_refuses_a_bundle_it_cant_verify(self) -> None:
        self.bundle.write_text("not a bundle")
        self.assertEqual(self.push(), 1)

    def test_clean_merge_and_one_fix(self) -> None:
        self.advance_main()
        self.git(self.maker, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        self.write(".github/workflows/actions.lock", "lock\n")
        head = self.commit_and_bundle()
        self.assertEqual(
            self.push(
                clean_merge_of="origin/main",
                allow_paths=[".github/workflows/actions.lock"],
                deny_paths=[".github/workflows"],
            ),
            0,
        )
        self.assertEqual(self.branch_head(), head)

    def test_clean_merge_alone(self) -> None:
        self.write("t.txt", "topic\n")
        self.start = self.commit("topic work")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/topic")
        topic = self.start
        self.git(self.maker, "checkout", "-qb", "main-work", "origin/main")
        self.write("b.txt", "b\n")
        self.commit("on main")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.git(self.maker, "checkout", "-q", topic)
        self.git(self.checkout, "fetch", "-q", "origin")
        self.git(self.maker, "merge", "-q", "--no-edit", "main-work")
        self.make_bundle()
        # The merge brings in main's b.txt, which the path rules don't cover.
        self.assertEqual(self.push(clean_merge_of="origin/main", deny_paths=["b.txt"]), 0)

    def test_a_fast_forward_to_the_base_and_a_fix(self) -> None:
        self.advance_main()
        self.git(self.maker, "merge", "-q", "--ff-only", "origin/main")
        self.write("a.txt", "fixed\n")
        self.commit_and_bundle()
        self.assertEqual(self.push(clean_merge_of="origin/main", deny_paths=["b.txt"]), 0)

    def test_the_fix_commit_is_checked_against_the_path_rules(self) -> None:
        self.advance_main()
        self.git(self.maker, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        self.write(".github/workflows/ci.yml", "evil\n")
        self.commit_and_bundle()
        self.assertEqual(
            self.push(
                clean_merge_of="origin/main",
                allow_paths=[".github/workflows/actions.lock"],
                deny_paths=[".github/workflows"],
            ),
            1,
        )

    def test_refuses_a_merge_that_changes_more_than_the_merge(self) -> None:
        self.advance_main()
        self.git(self.maker, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
        self.write("extra.txt", "x\n")
        self.git(self.maker, "add", "-A")
        self.git(self.maker, "commit", "-q", "--no-edit")
        self.make_bundle()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(clean_merge_of="origin/main"), 1)
        self.assertIn("isn't exactly what merging", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_refuses_a_merge_of_a_commit_not_on_the_base(self) -> None:
        self.git(self.maker, "checkout", "-qb", "side")
        self.write("s.txt", "s\n")
        self.commit("side")
        self.git(self.maker, "checkout", "-q", self.start)
        self.git(self.maker, "merge", "-q", "--no-ff", "--no-edit", "side")
        self.make_bundle()
        self.assertEqual(self.push(clean_merge_of="origin/main"), 1)

    def test_refuses_two_fix_commits_after_a_merge(self) -> None:
        self.advance_main()
        self.git(self.maker, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        self.write("a.txt", "one\n")
        self.commit("fix one")
        self.write("a.txt", "two\n")
        self.commit_and_bundle()
        self.assertEqual(self.push(clean_merge_of="origin/main"), 1)

    def test_refuses_a_merge_whose_merge_conflicts(self) -> None:
        self.git(self.maker, "checkout", "-qb", "main-work")
        self.write("a.txt", "main\n")
        self.commit("on main")
        self.git(self.maker, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.git(self.maker, "checkout", "-q", self.start)
        self.git(self.checkout, "fetch", "-q", "origin")
        self.write("a.txt", "topic\n")
        self.commit("topic")
        topic_start = self.git(self.maker, "rev-parse", "HEAD")
        self.git(self.maker, "push", "-q", "--force", "origin", "HEAD:refs/heads/topic")
        self.git(self.checkout, "fetch", "-q", "origin")
        self.start = topic_start
        # Resolve the conflict by hand, as no clean merge could.
        self.git(self.maker, "merge", "-q", "main-work", check=False)
        self.write("a.txt", "resolved\n")
        self.git(self.maker, "add", "-A")
        self.git(self.maker, "commit", "-q", "--no-edit")
        self.make_bundle()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(clean_merge_of="origin/main"), 1)
        self.assertIn("conflicts", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_refuses_a_base_that_isnt_a_commit(self) -> None:
        self.write("a.txt", "fixed\n")
        self.commit_and_bundle()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(self.push(clean_merge_of="origin/nope"), 1)
        self.assertIn("origin/nope isn't a commit", "".join(c.args[0] for c in stderr.write.call_args_list))


class RunTests(TestCase):
    def test_needs_a_token(self) -> None:
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "o/r"}, clear=True):
            self.assertEqual(push_bundle.run(["b", "--branch", "x", "--start", "s"]), 1)

    def test_needs_a_repository(self) -> None:
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t"}, clear=True), self.assertRaises(SystemExit) as raised:
            push_bundle.run(["b", "--branch", "x", "--start", "s"])
        self.assertEqual(raised.exception.code, 2)

    def test_passes_its_options_on(self) -> None:
        environ = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_SERVER_URL": "https://example.com"}
        with (
            mock.patch.dict("os.environ", environ, clear=True),
            mock.patch.object(push_bundle, "main", return_value=0) as main,
        ):
            argv = ["b", "--branch", "x", "--start", "s", "--single-commit", "--allow-path", "uv.lock"]
            self.assertEqual(push_bundle.run([*argv, "--clean-merge-of", "origin/main", "--deny-path", "d"]), 0)
        main.assert_called_once_with(
            Path("b"),
            "x",
            "s",
            "https://example.com/o/r.git",
            "t",
            expected_head=None,
            single_commit=True,
            clean_merge_of="origin/main",
            allow_paths=["uv.lock"],
            deny_paths=["d"],
        )
