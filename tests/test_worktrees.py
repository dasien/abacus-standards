"""Worktree-native behaviour: Claude Code isolates background sessions in .claude/worktrees/<name>."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from abacus_standards import board, common
from abacus_standards.common import Refused

HOOK = Path(__file__).resolve().parents[1] / "canonical" / "hooks" / "guards.py"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.ws = root / "ws"
        self.ws.mkdir()
        git(root, "init", "-q", "--bare", "-b", "main", str(root / "remote.git"))
        self.main = self.ws / "repo"
        git(self.ws, "clone", "-q", str(root / "remote.git"), "repo")
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            git(self.main, "config", k, v)
        (self.main / "app.py").write_text("x = 1\n")
        (self.main / ".gitignore").write_text(".claude/worktrees/\n.abacus-runs/\n")
        git(self.main, "add", "app.py", ".gitignore"); git(self.main, "commit", "-q", "-m", "init")
        git(self.main, "push", "-q", "-u", "origin", "main")
        self.wt = self.main / ".claude" / "worktrees" / "bead-1"
        git(self.main, "worktree", "add", "-q", str(self.wt), "-b", "worktree-bead-1")
        self.other = self.main / ".claude" / "worktrees" / "bead-2"
        git(self.main, "worktree", "add", "-q", str(self.other), "-b", "worktree-bead-2")
        self.home = root / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, cwd, tool, tool_input, project=None):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd), "session_id": "t"}
        env = dict(os.environ, ABACUS_WORKSPACE=str(self.ws), ABACUS_HOME=str(self.home),
                   CLAUDE_PROJECT_DIR=str(project or self.main))
        p = subprocess.run([sys.executable, str(HOOK), "repo-fence", "destructive-git", "foreign-bd", "human-only"],
                           input=json.dumps(payload), capture_output=True, text=True, env=env)
        return p.returncode, p.stderr

    def bash(self, cwd, cmd, **kw):
        return self.hook(cwd, "Bash", {"command": cmd}, **kw)

    def test_session_writes_in_its_own_worktree(self):
        for cmd in ("echo y > app.py", "mkdir -p src && touch src/new.py", "git add app.py", "git commit -m x",
                    "make test", "mkdir -p .abacus-runs/bead-1 && echo '[]' > .abacus-runs/bead-1/verdicts.json"):
            self.assertEqual(self.bash(self.wt, cmd)[0], 0, cmd)
        self.assertEqual(self.hook(self.wt, "Write", {"file_path": str(self.wt / "app.py"), "content": "x"})[0], 0)
        # CLAUDE_PROJECT_DIR may name the worktree or the main checkout; both must behave the same
        self.assertEqual(self.bash(self.wt, "echo y > app.py", project=self.wt)[0], 0)

    def test_worktree_controls_stay_protected(self):
        for target in (".claude/settings.json", ".abacus/config.json", ".githooks/pre-push",
                       ".claude/hooks/abacus/guards.py"):
            rc, err = self.hook(self.wt, "Edit", {"file_path": str(self.wt / target), "old_string": "a",
                                                  "new_string": "b"})
            self.assertEqual(rc, 2, target)
        self.assertEqual(self.bash(self.wt, "git rm .githooks/pre-push")[0], 2)

    def test_main_checkout_and_other_worktrees_are_refused_from_a_worktree(self):
        for cmd in (f"echo x > {self.main}/app.py", f"touch {self.other}/app.py", "echo x > ../bead-2/app.py",
                    f"cd {self.main} && git commit -m x"):
            rc, err = self.bash(self.wt, cmd)
            self.assertEqual(rc, 2, f"{cmd}\n{err}")

    def test_after_close_the_main_checkout_fast_forwards(self):
        # abacus-run-bead: after ExitWorktree remove, the worker fast-forwards its own main checkout
        git(self.wt, "config", "user.email", "t@example.com"); git(self.wt, "config", "user.name", "t")
        (self.wt / "app.py").write_text("x = 2\n")
        git(self.wt, "commit", "-q", "-am", "bead-1"); git(self.wt, "push", "-q", "origin", "HEAD:main")
        for cmd in (f"git -C {self.main} pull --ff-only -q", "git pull --ff-only -q"):
            self.assertEqual(self.bash(self.main, cmd)[0], 0, cmd)
        git(self.main, "pull", "--ff-only", "-q")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), git(self.main, "rev-parse", "origin/main"))
        # negative controls: never another repository, never a forced update of the main checkout
        foreign = self.ws / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q")
        self.assertEqual(self.bash(self.main, f"git -C {foreign} pull --ff-only -q")[0], 2)
        self.assertEqual(self.bash(self.main, "git reset --hard origin/main")[0], 2)

    def test_main_checkout_session_cannot_write_into_worktrees(self):
        rc, err = self.bash(self.main, f"echo x > {self.wt}/app.py")
        self.assertEqual(rc, 2)
        self.assertIn("another session's worktree", err)
        self.assertEqual(self.bash(self.main, "echo y > app.py")[0], 0)

    def test_sibling_repository_still_refused_from_a_worktree(self):
        (self.ws / "other-repo").mkdir()
        git(self.ws / "other-repo", "init", "-q")
        self.assertEqual(self.bash(self.wt, f"touch {self.ws / 'other-repo' / 'f'}")[0], 2)

    def test_board_in_main_checkout_is_own_board_from_a_worktree(self):
        self.assertEqual(self.bash(self.wt, "bd close bead-1 --reason done")[0], 0)
        self.assertEqual(self.bash(self.wt, "bd update bead-1 --status in_progress")[0], 0)

    def test_repo_identity_is_the_main_checkout(self):
        self.assertEqual(common.repo_identity(self.wt), self.main)
        self.assertEqual(common.repo_identity(self.main), self.main)

    def test_sync_fast_forwards_a_stale_worktree(self):
        # another worktree's bead pushed to origin; this worktree branched from the old tip
        (self.other / "app.py").write_text("x = 2\n")
        git(self.other, "commit", "-qam", "other bead"); git(self.other, "push", "-q", "origin", "HEAD:main")
        self.assertIn("fast-forwarded", board.sync_with_remote(self.wt, "main"))
        self.assertEqual((self.wt / "app.py").read_text(), "x = 2\n")
        self.assertEqual(board.sync_with_remote(self.wt, "main"), "up to date")

    def test_sync_refuses_a_diverged_branch(self):
        (self.other / "app.py").write_text("x = 2\n")
        git(self.other, "commit", "-qam", "other"); git(self.other, "push", "-q", "origin", "HEAD:main")
        (self.wt / "app.py").write_text("x = 3\n")
        git(self.wt, "commit", "-qam", "local")
        with self.assertRaises(Refused):
            board.sync_with_remote(self.wt, "main")

    @unittest.skipUnless(shutil.which("bd"), "needs Beads (bd)")
    def test_board_path_accepts_main_board_from_worktree(self):
        subprocess.run(["bd", "init", "--prefix", "wt", "--quiet", "--skip-agents", "--skip-hooks",
                        "--non-interactive"], cwd=self.main, check=True, capture_output=True,
                       env=dict(os.environ, BD_NON_INTERACTIVE="1"))
        env = os.environ.pop("BEADS_DIR", None)
        try:
            self.assertEqual(common.board_path(self.wt), (self.main / ".beads").resolve())
        finally:
            if env:
                os.environ["BEADS_DIR"] = env


if __name__ == "__main__":
    unittest.main()


class BoardFindTest(unittest.TestCase):
    """abacus-board find: one read-only search across every fleet board (planner, concierge, orchestrator)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.home = t / "home"
        (self.home / "agents").mkdir(parents=True)
        self.repos = {}
        for name, prefix in (("sdk", "sdk"), ("cli", "cli")):
            r = t / name
            r.mkdir()
            git(r, "init", "-q")
            subprocess.run(["bd", "init", "--prefix", prefix, "--quiet", "--skip-agents", "--skip-hooks",
                            "--non-interactive"], cwd=r, check=True, capture_output=True)
            self.repos[name] = r
        (self.home / "agents" / "fleet.json").write_text(json.dumps({"workers": {
            "coder:sdk": {"repo": str(self.repos["sdk"])}, "coder:cli": {"repo": str(self.repos["cli"])},
            "agent:orchestrator": {"repo": str(t / "nope")}}}))
        os.environ["ABACUS_HOME"] = str(self.home)
        src = "https://github.com/o/r/issues/1"
        self.sdk = self.bd("sdk", "create", "Add heartbeat to the SDK", "-d", "x", "--external-ref", src, "--silent")
        self.bd("sdk", "label", "add", self.sdk, "pipeline:lite")
        self.cli = self.bd("cli", "create", "Add bw heartbeat", "-d", f"WAITS-ON sdk:{self.sdk} (verified)",
                           "--external-ref", src, "--silent")
        self.bd("cli", "label", "add", self.cli, "draft")
        self.other = self.bd("cli", "create", "Unrelated", "-d", "y", "--external-ref",
                             "https://github.com/o/r/issues/11", "--silent")

    def tearDown(self):
        os.environ.pop("ABACUS_HOME", None)
        self.tmp.cleanup()

    def bd(self, repo, *args):
        return subprocess.run(["bd", *args], cwd=self.repos[repo], check=True, capture_output=True,
                              text=True).stdout.strip()

    def ids(self, q):
        return {h["id"]: h for h in board.find(q)}

    def test_finds_by_title_source_and_issue_number_across_boards(self):
        self.assertEqual(set(self.ids("heartbeat")), {self.sdk, self.cli})
        self.assertEqual(set(self.ids("https://github.com/o/r/issues/1")), {self.sdk, self.cli})
        self.assertEqual(set(self.ids("#1")), {self.sdk, self.cli})          # not issue #11
        self.assertEqual(set(self.ids("#11")), {self.other})

    def test_a_hit_brings_its_prerequisite_and_says_what_is_ready(self):
        hits = self.ids(self.cli)
        self.assertEqual(hits[self.sdk]["prerequisite_of"], self.cli)
        self.assertEqual(hits[self.cli]["waits_on"], [f"sdk:{self.sdk}"])
        self.assertTrue(hits[self.sdk]["ready"])                              # labeled, open, not a draft
        self.assertFalse(hits[self.cli]["ready"])
        self.assertTrue(hits[self.cli]["draft"])

    def test_a_draft_never_briefs_even_with_a_pipeline_label(self):
        self.bd("cli", "label", "add", self.cli, "pipeline:lite")      # mid-approval: labeled, still a draft
        (self.repos["cli"] / ".abacus").mkdir(exist_ok=True)
        (self.repos["cli"] / ".abacus" / "config.json").write_text(json.dumps(
            {"project": "cli", "default_branch": "main", "gate": "true", "fast_check": "true"}))
        with self.assertRaises(Refused) as e:
            board.brief(self.cli, self.repos["cli"])
        self.assertIn("still a draft", str(e.exception))

    def test_cli_needs_no_repository(self):
        p = subprocess.run([sys.executable, "-m", "abacus_standards.board", "find", "heartbeat"],
                           cwd=self.tmp.name, capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "lib")))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)), 2)
