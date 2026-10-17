import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from abacus_standards import adopt, doctor, finalize
from abacus_standards.common import Refused


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def tree_state(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        out[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
    return out


class AdoptDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve() / "demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "CLAUDE.md").write_text("# demo\n\nExisting instructions.\n")
        os.environ["ABACUS_HOME"] = str(Path(self.tmp.name) / "home")

    def tearDown(self):
        self.tmp.cleanup()

    def verdicts(self, only=None):
        return {r["check"]: r["verdict"] for r in doctor.doctor(self.repo, only)}

    def test_fresh_repo_lists_what_is_missing_with_remedies(self):
        results = doctor.doctor(self.repo)
        for r in results:
            if r["verdict"] != doctor.ADOPTED:
                self.assertIn("remedy", r)
        v = self.verdicts()
        self.assertEqual(v["guards"], doctor.NOT)
        self.assertEqual(v["config"], doctor.NOT)

    def test_adopt_then_doctor_reports_adopted(self):
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        v = self.verdicts(["config", "guards", "workflows", "skills", "gate", "instruction", "memory-block",
                           "runs-ignored", "instruction-size"])
        self.assertTrue(all(x == doctor.ADOPTED for x in v.values()), v)
        self.assertIn("Existing instructions.", (self.repo / "CLAUDE.md").read_text())
        # idempotent: a second adoption changes nothing but the version stamp
        changed = adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        self.assertEqual(changed, [".abacus/adopted.json"])

    def gate_detail(self):
        return next(r for r in doctor.doctor(self.repo, ["gate"]))

    def test_gate_judges_where_hooks_path_points_not_its_spelling(self):
        """Claude Code rewrites a relative core.hooksPath to an absolute one when it creates a worktree."""
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        git(self.repo, "add", "-A"); git(self.repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "adopt")
        self.assertEqual(self.gate_detail()["verdict"], doctor.ADOPTED)
        git(self.repo, "config", "core.hooksPath", str(self.repo / ".githooks"))        # Claude Code's rewrite
        self.assertEqual(self.gate_detail()["verdict"], doctor.ADOPTED)
        wt = self.repo / ".claude" / "worktrees" / "w1"                                  # seen from a worktree
        git(self.repo, "worktree", "add", "-q", str(wt), "-b", "w1")
        self.assertEqual(doctor.doctor(wt, ["gate"])[0]["verdict"], doctor.ADOPTED)
        cases = {
            str(Path(self.tmp.name) / "moved" / ".githooks"): "doesn't exist",           # repository moved
            str(self.repo / ".beads" / "hooks"): "doesn't exist",
            ".beads/hooks": "gates are bypassed",                                        # another tool took over
        }
        (Path(self.tmp.name) / "elsewhere").mkdir()
        cases[str(Path(self.tmp.name) / "elsewhere")] = "not this repository's .githooks"
        for value, words in cases.items():
            git(self.repo, "config", "core.hooksPath", value)
            r = self.gate_detail()
            self.assertEqual(r["verdict"], doctor.NOT, value)
            self.assertIn(words, r["detail"], value)
            self.assertIn("git config core.hooksPath .githooks", r["detail"])
        git(self.repo, "config", "--unset", "core.hooksPath")
        self.assertIn("unset", self.gate_detail()["detail"])

    def ignored(self, path):
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=self.repo).returncode == 0

    def test_a_gitignored_claude_folder_is_narrowed_for_abacus_files_only(self):
        # abacus-standards-goq: bwcli-rs ignored .claude entirely, so workers ran without guards
        (self.repo / ".gitignore").write_text("target\n.claude\n.env\n")
        changed = adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        self.assertTrue(any(c.startswith(".gitignore (narrowed") for c in changed), changed)
        self.assertFalse(self.ignored(".claude/settings.json"))
        self.assertFalse(self.ignored(".claude/hooks/abacus/guards.py"))
        self.assertTrue(self.ignored(".claude/settings.local.json"))      # your local settings stay private
        self.assertIn(".env", (self.repo / ".gitignore").read_text())       # nothing else touched
        self.assertEqual(self.verdicts(["tracked"])["tracked"], doctor.ADOPTED)

    def test_an_unfixable_ignore_rule_is_refused_before_anything_is_written(self):
        (self.repo / ".gitignore").write_text(".abacus/\n")
        with self.assertRaises(Refused) as e:
            adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        self.assertIn(".abacus/config.json", str(e.exception))
        self.assertIn("Nothing was written", str(e.exception))
        self.assertFalse((self.repo / ".claude").exists())

    def test_doctor_flags_guards_that_became_ignored(self):
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        self.assertEqual(self.verdicts(["tracked"])["tracked"], doctor.ADOPTED)
        with open(self.repo / ".gitignore", "a") as f:
            f.write("\n.claude\n")
        self.assertEqual(self.verdicts(["tracked"])["tracked"], doctor.NOT)

    def test_drifted_or_unwired_guard_is_not_adopted(self):
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        g = self.repo / ".claude" / "hooks" / "abacus" / "guards.py"
        g.write_text(g.read_text() + "\n# softened\n")
        self.assertEqual(self.verdicts(["guards"])["guards"], doctor.NOT)
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        s = self.repo / ".claude" / "settings.json"
        s.write_text(s.read_text().replace(" destructive-git", ""))
        r = doctor.doctor(self.repo, ["guards"])[0]
        self.assertEqual(r["verdict"], doctor.NOT)
        self.assertIn("destructive-git", r["detail"])

    def test_declared_deviation_reads_deviating_but_obligation_cannot_be_declined(self):
        (self.repo / "docs" / "adr").mkdir(parents=True)
        (self.repo / "docs" / "adr" / "0001.md").write_text(
            "---\ntags: [deviates:mutation-guide, deviates:emit]\n---\n# We use property tests instead\n")
        v = self.verdicts(["mutation-guide", "emit"])
        self.assertEqual(v["mutation-guide"], doctor.DEVIATING)
        self.assertNotEqual(v["emit"], doctor.DEVIATING)

    def test_unknown_config_key_is_broken_not_ignored(self):
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        cfg = self.repo / ".abacus" / "config.json"
        data = json.loads(cfg.read_text()); data["gaet"] = "typo"
        cfg.write_text(json.dumps(data))
        r = doctor.doctor(self.repo, ["config"])[0]
        self.assertEqual(r["verdict"], doctor.BROKEN)
        self.assertIn("gaet", r["detail"])

    def test_a_throwing_check_is_unchecked(self):
        doctor.REGISTRY.append({"name": "boom", "tier": "CANDIDATE", "remedy": "-", "why": "-",
                                "fn": lambda repo: 1 / 0})
        try:
            self.assertEqual(self.verdicts(["boom"])["boom"], doctor.UNCHECKED)
        finally:
            doctor.REGISTRY.pop()

    def test_doctor_is_read_only(self):
        adopt.adopt(self.repo, "demo", "make gate", "make check", "main")
        before = tree_state(self.repo)
        doctor.doctor(self.repo)
        self.assertEqual(before, tree_state(self.repo))


class FinalizeVerdictTest(unittest.TestCase):
    BEAD = {"id": "demo-1", "status": "in_progress",
            "acceptance_criteria": "- `make test` passes with the new parser test\n- README documents --flag"}

    def test_every_criterion_needs_met_with_evidence(self):
        ok = [{"criterion": "`make test` passes with the new parser test", "verdict": "met", "evidence": "12 passed"},
              {"criterion": "README documents --flag", "verdict": "met", "evidence": "README.md:40"}]
        self.assertEqual(len(finalize.check_verdicts(self.BEAD, ok, "/nonexistent")), 2)
        cases = [
            ok[:1],  # missing a criterion
            [ok[0], dict(ok[1], evidence="")],  # met without evidence
            [ok[0], dict(ok[1], verdict="unmet")],  # unmet cannot close
            [ok[0], {"criterion": "README documents --flag", "verdict": "moved"}],  # moved without a bead
            {"not": "a list"},
        ]
        for bad in cases:
            with self.assertRaises(Refused, msg=str(bad)):
                finalize.check_verdicts(self.BEAD, bad, "/nonexistent")


if __name__ == "__main__":
    unittest.main()


class RevertTest(unittest.TestCase):
    """revert must undo only this run, keep every saved patch, and survive awkward file names."""

    def setUp(self):
        from abacus_standards import board
        self.board = board
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve() / "r"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        (self.repo / "my file.txt").write_text("orig\n")
        (self.repo / "file.txt").write_text("keep me\n")
        (self.repo / ".gitignore").write_text(".abacus-runs/\n")
        git(self.repo, "add", "my file.txt", "file.txt", ".gitignore"); git(self.repo, "commit", "-q", "-m", "init")
        runs = self.repo / ".abacus-runs" / "b-1"
        runs.mkdir(parents=True)
        (runs / "start.json").write_text(json.dumps({"head": git(self.repo, "rev-parse", "HEAD")}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_revert_handles_spaces_and_keeps_the_patch(self):
        (self.repo / "my file.txt").write_text("changed by the run\n")
        (self.repo / "new file.txt").write_text("created by the run\n")
        (self.repo / "naïve.txt").write_text("unicode\n")
        out = self.board.revert("b-1", self.repo)
        self.assertEqual((self.repo / "my file.txt").read_text(), "orig\n")
        self.assertFalse((self.repo / "new file.txt").exists())
        self.assertFalse((self.repo / "naïve.txt").exists())
        self.assertEqual((self.repo / "file.txt").read_text(), "keep me\n")  # never split "new file.txt"
        patch = (self.repo / out["patch"]).read_text()
        self.assertIn("changed by the run", patch)
        self.assertIn("created by the run", patch)

    def test_second_revert_refuses_and_never_overwrites(self):
        (self.repo / "my file.txt").write_text("changed\n")
        first = self.board.revert("b-1", self.repo)
        size = (self.repo / first["patch"]).stat().st_size
        with self.assertRaises(Refused):
            self.board.revert("b-1", self.repo)
        self.assertEqual((self.repo / first["patch"]).stat().st_size, size)

    def test_duplicate_verdicts_refused(self):
        bead = {"id": "b-1", "acceptance_criteria": "- one"}
        v = {"criterion": "one", "verdict": "met", "evidence": "x"}
        with self.assertRaises(Refused):
            finalize.check_verdicts(bead, [v, dict(v, evidence="y")], str(self.repo))


class LandedEvidenceTest(unittest.TestCase):
    """6mv: diff-scope criteria are recorded against the commit that lands, not the pre-commit tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        git(t, "init", "-q", "--bare", "-b", "main", str(t / "remote.git"))
        git(t, "clone", "-q", str(t / "remote.git"), "r")
        self.repo = t / "r"
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        (self.repo / "core").mkdir()
        (self.repo / "core" / "a.rs").write_text("a\n")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, *paths):
        for p in paths:
            f = self.repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("changed\n")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "work")
        return git(self.repo, "rev-parse", "HEAD")

    def reason(self, sha, criterion, evidence):
        land = finalize.landed(self.repo, "origin/main", sha)
        return finalize.close_reason([criterion], [{"criterion": criterion, "verdict": "met", "evidence": evidence}],
                                     sha, "lite", "coder:r", land)

    def test_range_criteria_quote_what_actually_landed(self):
        sha = self.commit("core/a.rs", "other/stray.rs")   # the commit picked up a file outside the claimed scope
        crit = "`git diff --stat origin/main...HEAD` lists files under `core/` only"
        out = self.reason(sha, crit, "working tree diff shows core/a.rs only")
        self.assertIn(f"re-read against the landed commit {sha[:12]}: 2 file(s): core/a.rs, other/stray.rs", out)
        self.assertIn(f"Files in {sha[:12]}", out)

    def test_other_criteria_are_left_as_the_worker_wrote_them(self):
        sha = self.commit("core/a.rs")
        out = self.reason(sha, "`make test` passes", "12 passed")
        self.assertIn("- `make test` passes => met: 12 passed", out)
        self.assertNotIn("re-read", out)

    def test_git_dash_c_forms_count_as_range_criteria(self):
        self.assertTrue(finalize.RANGE_CRITERION.search("`git -C ../sdk log --oneline -1 -- x.rs` names sdk-1"))
        self.assertFalse(finalize.RANGE_CRITERION.search("README mentions git"))


class FinalizeRetryTest(unittest.TestCase):
    """A refused push takes its commit back: nothing is stranded locally, so a plain retry lands the same changes."""

    RETRO = ("---\nid: fin-retro\nproject: fin\nissue: fin-1\ndate: 2026-10-15\ndoc_type: retro\n"
             "trigger: escalation\nsurface: tests\nseverity: sev4\nresolved: true\ntags: [retry]\n---\n\nNotes.\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.remote = t / "remote.git"
        git(t, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(t, "clone", "-q", str(self.remote), "r")
        self.repo = t / "r"
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        subprocess.run(["bd", "init", "--prefix", "fin", "--quiet", "--skip-agents", "--skip-hooks",
                        "--non-interactive"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".abacus").mkdir()
        (self.repo / ".abacus" / "config.json").write_text(json.dumps({"project": "fin"}))
        (self.repo / "README.md").write_text("demo\n")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "origin", "main")
        self.bead = subprocess.run(["bd", "create", "Add a line", "-d", "x", "--acceptance", "- README has the line",
                                    "--silent"], cwd=self.repo, check=True, capture_output=True,
                                   text=True).stdout.strip()
        self.verdicts = t / "verdicts.json"
        self.verdicts.write_text(json.dumps([{"criterion": "README has the line", "verdict": "met",
                                              "evidence": "README.md:2"}]))
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected by the test remote' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.hook = hook

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        from abacus_standards.common import show_bead
        return show_bead(self.bead, self.repo)["status"]

    def test_a_refused_push_leaves_the_changes_staged_for_a_plain_retry(self):
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("demo\nthe line\n")
        with self.assertRaises(Refused) as cm:
            finalize.finalize(self.bead, str(self.verdicts), "coder:r", None, self.repo)
        self.assertIn("push failed", str(cm.exception))
        self.assertNotIn("local only", str(cm.exception))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "README.md")
        self.assertNotEqual(self.status(), "closed")
        self.hook.unlink()
        out = finalize.finalize(self.bead, str(self.verdicts), "coder:r", None, self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "main"), out["commit"])
        self.assertEqual(self.status(), "closed")

    def test_a_refused_record_push_leaves_nothing_committed_for_a_plain_retry(self):
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "docs" / "retros").mkdir(parents=True)
        (self.repo / "docs" / "retros" / "fin-1.md").write_text(self.RETRO)
        with self.assertRaises(Refused):
            finalize.record_only(self.bead, ["docs/retros/fin-1.md"], "coder:r", self.repo)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.hook.unlink()
        out = finalize.record_only(self.bead, ["docs/retros/fin-1.md"], "coder:r", self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "main"), out["commit"])
        self.assertNotEqual(self.status(), "closed")


class JudgmentRulingIdTest(unittest.TestCase):
    """5rg: rows may end with `ruling: #<id>` so a relayed RULING message can be matched to its row."""

    BASE = ('JUDGEMENT 2026-10-14: question: q | options: a, b | recommended: a | confidence: 70% | '
            'review: x by 2026-11-01 | ruled: "a" | agreed: yes')

    def fields(self, row):
        from abacus_standards.judgment import parse_row
        out = parse_row(row)
        return next(x for x in (out if isinstance(out, tuple) else (out,)) if isinstance(x, dict))

    def test_ruling_id_is_optional_and_parsed(self):
        self.assertNotIn("ruling_id", self.fields(self.BASE))                     # older rows still pass
        self.assertEqual(self.fields(self.BASE + " | ruling: #4")["ruling_id"], 4)
        standing = ('STANDING 2026-10-14: words: "build it" | scope: x | source: y | review: z by 2026-11-01'
                    ' | ruling: #5')
        self.assertEqual(self.fields(standing)["ruling_id"], 5)

    def test_a_malformed_extra_field_is_refused(self):
        from abacus_standards.judgment import parse_row
        for bad in (" | ruling: 4", " | rulings: #4", " | ruling: #four", " | note: x"):
            with self.assertRaises(Refused, msg=bad):
                parse_row(self.BASE + bad)
