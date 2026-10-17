"""The commit and push gates run a repository's own tests, which may run git against temporary repositories."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GITHOOKS = Path(__file__).resolve().parents[1] / "canonical" / "githooks"

# What a repository's test suite might do: build a scratch repository and force-add into it.
SCRATCH_GIT = 'd=$(mktemp -d) && cd "$d" && git init -q && touch f && git add --force .'


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class GitHookEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        git(root, "init", "-q", "--bare", "-b", "main", str(root / "remote.git"))
        self.main = root / "repo"
        git(root, "clone", "-q", str(root / "remote.git"), "repo")
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            git(self.main, "config", k, v)
        (self.main / ".abacus").mkdir()
        (self.main / ".abacus" / "config.json").write_text(
            '{"gate": "%s", "fast_check": "%s"}\n' % (SCRATCH_GIT.replace('"', '\\"'), SCRATCH_GIT.replace('"', '\\"')))
        shutil.copytree(GITHOOKS, self.main / ".githooks")
        (self.main / ".gitignore").write_text(".claude/worktrees/\n")
        git(self.main, "add", "-A"); git(self.main, "commit", "-q", "-m", "init")
        git(self.main, "push", "-q", "-u", "origin", "main")
        git(self.main, "config", "core.hooksPath", ".githooks")
        self.wt = self.main / ".claude" / "worktrees" / "bead-1"
        git(self.main, "worktree", "add", "-q", str(self.wt), "-b", "worktree-bead-1")
        # abacus-lock stand-in: run the command after "--".
        lock = root / "home" / "bin" / "abacus-lock"
        lock.parent.mkdir(parents=True)
        lock.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n')
        lock.chmod(0o755)
        self.env = dict(os.environ, ABACUS_HOME=str(root / "home"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_gates_in_a_worktree_leave_the_repository_alone(self):
        (self.wt / "app.py").write_text("x = 1\n")
        git(self.wt, "add", "app.py")
        git(self.wt, "commit", "-q", "-m", "bead work", env=self.env)
        git(self.wt, "push", "-q", "origin", "worktree-bead-1", env=self.env)
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        self.assertEqual(git(self.main, "status", "--porcelain"), "")
        self.assertEqual(git(self.main, "config", "core.bare"), "false")
        self.assertEqual(git(self.main, "ls-tree", "--name-only", "worktree-bead-1", "app.py"), "app.py")


if __name__ == "__main__":
    unittest.main()
