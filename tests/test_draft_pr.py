"""Draft-PR landing: a bead lands on its own branch and closes when its draft PR opens.

Never calls the real gh: a fake on PATH records each call (args and the body it was piped) and answers from rules.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from abacus_standards import board, finalize
from abacus_standards.common import Refused, bead_branch, branches_for, load_config, show_bead

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
body = sys.stdin.read() if "--body-file" in args else None
log = os.environ["GH_FAKE_LOG"]
calls = open(log).read().count("\n") if os.path.exists(log) else 0
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "body": body}) + "\n")
rules = json.load(open(os.environ["GH_FAKE_RULES"])) if os.path.exists(os.environ["GH_FAKE_RULES"]) else []
for r in rules:
    if all(t in args for t in r["match"]):
        sys.stdout.write(r.get("stdout", "")); sys.stderr.write(r.get("stderr", ""))
        sys.exit(r.get("rc", 0))
if args[:2] == ["pr", "create"]:
    print(f"https://github.com/o/r/pull/{100 + calls}")
elif args[:2] == ["pr", "list"]:
    print("[]")
'''

RETRO = ("---\nid: fin-retro\nproject: fin\nissue: fin-1\ndate: 2026-10-15\ndoc_type: retro\n"
         "trigger: escalation\nsurface: tests\nseverity: sev4\nresolved: true\ntags: [retry]\n---\n\nNotes.\n")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class DraftPrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One remote and adopted clone, copied for each test (bd init is the slow part)."""
        cls.template = tempfile.TemporaryDirectory()
        t = Path(cls.template.name).resolve()
        git(t, "init", "-q", "--bare", "-b", "main", str(t / "remote.git"))
        git(t, "clone", "-q", str(t / "remote.git"), "r")
        repo = t / "r"
        git(repo, "config", "user.email", "t@example.com"); git(repo, "config", "user.name", "t")
        subprocess.run(["bd", "init", "--prefix", "fin", "--quiet", "--skip-agents", "--skip-hooks",
                        "--non-interactive"], cwd=repo, check=True, capture_output=True)
        (repo / ".abacus").mkdir()
        (repo / ".abacus" / "config.json").write_text(json.dumps({"project": "fin", "landing": "draft-pr"}))
        (repo / ".gitignore").write_text(".abacus-runs/\n")
        (repo / "README.md").write_text("demo\n")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "init"); git(repo, "push", "-q", "origin", "main")
        cls.old_main = git(repo, "rev-parse", "HEAD")
        (repo / "NOTES.md").write_text("notes\n")   # main moves on after a parent branch would have forked
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "more"); git(repo, "push", "-q", "origin", "main")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.remote, self.repo = t / "remote.git", t / "r"
        shutil.copytree(Path(self.template.name) / "remote.git", self.remote, symlinks=True)
        shutil.copytree(Path(self.template.name) / "r", self.repo, symlinks=True)
        git(self.repo, "remote", "set-url", "origin", str(self.remote))
        fake = t / "fakebin"
        fake.mkdir()
        (fake / "gh").write_text(FAKE_GH)
        (fake / "gh").chmod(0o755)
        self.gh_log, self.gh_rules = t / "gh.log", t / "gh.json"
        self.env = {k: os.environ.get(k) for k in ("PATH", "GH_FAKE_LOG", "GH_FAKE_RULES")}
        os.environ.update(PATH=f"{fake}:{os.environ['PATH']}", GH_FAKE_LOG=str(self.gh_log),
                          GH_FAKE_RULES=str(self.gh_rules))
        self.verdicts = t / "verdicts.json"
        self.verdicts.write_text(json.dumps([{"criterion": "README has the line", "verdict": "met",
                                              "evidence": "README.md:2 reads 'the line'"}]))

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # ---- helpers
    def config(self, cfg):
        (self.repo / ".abacus" / "config.json").write_text(json.dumps(cfg))

    def bead(self, title, deps=(), ext=None):
        cmd = ["bd", "create", title, "-d", "x", "--acceptance", "- README has the line", "-l", "pipeline:lite",
               "--silent"] + (["--external-ref", ext] if ext else [])
        bid = subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        for d in deps:
            subprocess.run(["bd", "dep", "add", bid, d], cwd=self.repo, check=True, capture_output=True)
        return bid

    def show(self, bid):
        return show_bead(bid, self.repo)

    def rules(self, *rules):
        self.gh_rules.write_text(json.dumps(list(rules)))

    def calls(self, verb=None):
        lines = self.gh_log.read_text().splitlines() if self.gh_log.exists() else []
        calls = [json.loads(x) for x in lines]
        return [c for c in calls if verb is None or c["args"][:2] == ["pr", verb]]

    def parent_with_pr(self, title, number):
        """A dependency that closed when its PR opened: its branch, forked from an older main, is on the remote."""
        pid = self.bead(title)
        subprocess.run(["bd", "close", pid, "--reason", "draft PR opened"], cwd=self.repo, check=True,
                       capture_output=True)
        branch = bead_branch(self.show(pid))
        tree = git(self.repo, "rev-parse", f"{self.old_main}^{{tree}}")
        sha = subprocess.run(["git", "commit-tree", tree, "-p", self.old_main, "-m", f"{title}\n\nBead: {pid}"],
                             cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git(self.repo, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
        url = f"https://github.com/o/r/pull/{number}"
        return pid, branch, sha, {"match": ["list", "--head", branch],
                                  "stdout": json.dumps([{"number": number, "url": url}])}

    def remote_ref(self, branch):
        out = git(self.repo, "ls-remote", "origin", f"refs/heads/{branch}")
        return out.split()[0] if out else None

    def finalize(self, bid, summary="Adds the line to the README."):
        return finalize.finalize(bid, str(self.verdicts), "coder:r", summary, self.repo)

    # ---- config and names
    def test_landing_accepts_push_and_draft_pr_only(self):
        self.assertEqual(load_config(self.repo)["landing"], "draft-pr")
        self.config({"project": "fin"})
        self.assertEqual(load_config(self.repo)["landing"], "push")
        for bad in ("draft_pr", "pr", "", 1):
            self.config({"project": "fin", "landing": bad})
            with self.assertRaises(Refused, msg=bad) as cm:
                load_config(self.repo)
            self.assertIn("landing", str(cm.exception))

    def test_branch_name_and_exact_suffix_lookup(self):
        self.assertEqual(bead_branch({"id": "baud-26f", "title": "Add sidebar: Sign-out!!"}),
                         "abacus/add-sidebar-sign-out-baud-26f")
        self.assertEqual(bead_branch({"id": "x-1", "title": "a" * 39 + " b"}), "abacus/" + "a" * 39 + "-x-1")
        names = ["abacus/a-baud-26f", "abacus/a-baud-26f.1", "abacus/a-baud-26f-record", "abacus/a-xbaud-26f",
                 "feature/a-baud-26f"]
        self.assertEqual(branches_for(names, "baud-26f"), ["abacus/a-baud-26f"])

    # ---- base selection in abacus-board check
    def test_base_is_the_default_branch_when_no_dependency_has_an_open_pr(self):
        parent = self.bead("Parent work")
        child = self.bead("Child work", deps=[parent])
        out = board.check(child, "coder:r", self.repo)
        self.assertEqual(out["base"], "main")
        self.assertEqual(json.loads((self.repo / ".abacus-runs" / child / "start.json").read_text())["base"], "main")
        self.assertEqual(self.show(child)["status"], "in_progress")

    def test_base_is_the_one_dependency_with_an_open_pr(self):
        _, branch, tip, rule = self.parent_with_pr("Parent work", 7)
        self.rules(rule)
        child = self.bead("Child work", deps=[_])
        out = board.check(child, "coder:r", self.repo)
        self.assertEqual(out["base"], branch)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), tip)   # synced to the parent's branch
        start = json.loads((self.repo / ".abacus-runs" / child / "start.json").read_text())
        self.assertEqual((start["base"], start["remote"]), (branch, tip))
        self.assertEqual(board.snapshot(child, self.repo)["remote"], tip)   # the probe watches the base

    def test_two_dependencies_with_open_prs_refuse_before_claiming(self):
        p1, _, _, r1 = self.parent_with_pr("First parent", 7)
        p2, _, _, r2 = self.parent_with_pr("Second parent", 8)
        self.rules(r1, r2)
        child = self.bead("Child work", deps=[p1, p2])
        with self.assertRaises(Refused) as cm:
            board.check(child, "coder:r", self.repo)
        self.assertIn("pull/7", str(cm.exception))
        self.assertIn("pull/8", str(cm.exception))
        self.assertEqual(self.show(child)["status"], "open")

    # ---- finalize
    def test_finalize_pushes_the_bead_branch_and_opens_a_draft_pr(self):
        bid = self.bead("Add a line", ext="gh-12")
        board.check(bid, "coder:r", self.repo)
        (self.repo / "README.md").write_text("demo\nthe line\n")
        out = self.finalize(bid)
        branch = bead_branch(self.show(bid))
        self.assertEqual(self.remote_ref(branch), out["commit"])
        self.assertNotEqual(self.remote_ref("main"), out["commit"])
        [create] = self.calls("create")
        args = create["args"]
        for flag, value in (("--base", "main"), ("--head", branch), ("--title", "Add a line")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--draft", args)
        body = create["body"]
        for text in ("Adds the line to the README.", "README has the line", "met", "README.md:2", "Closes #12"):
            self.assertIn(text, body)
        self.assertNotIn(bid, body + args[args.index("--title") + 1])
        self.assertIn(out["pr"], self.show(bid)["close_reason"])
        self.assertEqual(self.show(bid)["status"], "closed")
        self.assertNotIn("Co-Authored-By", git(self.repo, "log", "-1", "--format=%B", out["commit"]))

    def test_a_refused_push_undoes_the_commit_and_leaves_the_bead_open(self):
        bid = self.bead("Add a line")
        before = git(self.repo, "rev-parse", "HEAD")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected by the test remote' >&2\nexit 1\n")
        hook.chmod(0o755)
        (self.repo / "README.md").write_text("demo\nthe line\n")
        with self.assertRaises(Refused) as cm:
            self.finalize(bid)
        self.assertIn("push failed", str(cm.exception))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "README.md")
        self.assertEqual(self.calls("create"), [])
        self.assertNotEqual(self.show(bid)["status"], "closed")

    def test_a_failed_pr_create_deletes_the_branch_and_a_retry_succeeds(self):
        bid = self.bead("Add a line")
        before = git(self.repo, "rev-parse", "HEAD")
        branch = bead_branch(self.show(bid))
        self.rules({"match": ["pr", "create"], "rc": 1, "stderr": "GraphQL: something went wrong"})
        (self.repo / "README.md").write_text("demo\nthe line\n")
        with self.assertRaises(Refused) as cm:
            self.finalize(bid)
        self.assertIn("something went wrong", str(cm.exception))
        self.assertIsNone(self.remote_ref(branch))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertNotEqual(self.show(bid)["status"], "closed")
        self.rules()
        out = self.finalize(bid)
        self.assertEqual(self.remote_ref(branch), out["commit"])
        self.assertEqual(self.show(bid)["status"], "closed")

    def test_a_stacked_bead_opens_its_pr_against_the_parent_branch(self):
        parent, pbranch, _, rule = self.parent_with_pr("Parent work", 7)
        self.rules(rule)
        child = self.bead("Child work", deps=[parent])
        board.check(child, "coder:r", self.repo)
        (self.repo / "README.md").write_text("demo\nthe line\n")
        out = self.finalize(child)
        [create] = self.calls("create")
        self.assertEqual(create["args"][create["args"].index("--base") + 1], pbranch)
        self.assertIn("Builds on #7", create["body"])
        self.assertNotIn(parent, create["body"])
        self.assertEqual(out["base"], pbranch)
        self.assertIn("README.md", self.show(child)["close_reason"])
        self.assertNotIn("NOTES.md", self.show(child)["close_reason"])   # diffed against the parent, not main

    def test_record_opens_its_own_draft_pr_against_the_default_branch(self):
        parent, _, _, rule = self.parent_with_pr("Parent work", 7)
        self.rules(rule)
        child = self.bead("Child work", deps=[parent])
        board.check(child, "coder:r", self.repo)   # a stacked run: the record still targets main
        head, status = git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain")
        retro = self.repo / "docs" / "retros" / "fin-1.md"
        retro.parent.mkdir(parents=True)
        retro.write_text(RETRO)
        record = bead_branch(self.show(child)) + "-record"
        self.rules(rule, {"match": ["pr", "create"], "rc": 1, "stderr": "GraphQL: something went wrong"})
        with self.assertRaises(Refused):
            finalize.record_only(child, ["docs/retros/fin-1.md"], "coder:r", self.repo)
        self.assertEqual(retro.read_text(), RETRO)   # a failure keeps the record, so a plain retry works
        self.assertIsNone(self.remote_ref(record))
        self.gh_log.unlink()
        self.rules(rule)
        out = finalize.record_only(child, ["docs/retros/fin-1.md"], "coder:r", self.repo)
        self.assertFalse(retro.exists())   # it lives on the record branch now; the run's tree is clean again
        self.assertEqual(board.dirty(self.repo), [])
        self.assertEqual(self.remote_ref(record), out["commit"])
        self.assertNotIn("Co-Authored-By", git(self.repo, "log", "-1", "--format=%B", out["commit"]))
        self.assertEqual(git(self.repo, "rev-parse", f"{out['commit']}^"), self.remote_ref("main"))
        self.assertEqual(git(self.repo, "diff", "--name-only", f"{out['commit']}^", out["commit"]),
                         "docs/retros/fin-1.md")
        [create] = self.calls("create")
        args = create["args"]
        self.assertEqual((args[args.index("--base") + 1], args[args.index("--head") + 1]), ("main", record))
        self.assertIn("--draft", args)
        self.assertNotIn(child, create["body"] + args[args.index("--title") + 1])
        self.assertEqual((git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain")), (head, status))
        self.assertNotEqual(self.show(child)["status"], "closed")


if __name__ == "__main__":
    unittest.main()
