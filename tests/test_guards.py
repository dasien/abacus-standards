"""Negative controls for every guard: run the real hook against bad and good calls.

If a guard is softened, a REFUSE case here starts passing through and fails.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "canonical" / "hooks" / "guards.py"
ALL = ["repo-fence", "foreign-bd", "destructive-git", "stage-writes", "database-url", "operator-only"]


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls.tmp.name).resolve() / "ws"
        for name in ("repo-a", "repo-b"):
            (cls.ws / name).mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=cls.ws / name, check=True)
        cls.home = Path(cls.tmp.name).resolve() / "abacus-home"
        cls.a = str(cls.ws / "repo-a")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_hook(self, tool, tool_input, guards=ALL, agent_type=None, role="worker", cwd=None, project=None):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd or self.a, "session_id": "t",
                   "hook_event_name": "PreToolUse"}
        if agent_type:
            payload["agent_type"] = agent_type
        env = dict(os.environ, ABACUS_WORKSPACE=str(self.ws), ABACUS_HOME=str(self.home), ABACUS_ROLE=role,
                   CLAUDE_PROJECT_DIR=project or self.a)
        p = subprocess.run([sys.executable, str(HOOK), *guards], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stderr

    def assertRefused(self, cmd, guard=None, **kw):
        rc, err = self.run_hook("Bash", {"command": cmd}, **kw)
        self.assertEqual(rc, 2, f"expected refusal for: {cmd}\n{err}")
        if guard:
            self.assertIn(f"[abacus:{guard}]", err)

    def assertAllowed(self, cmd, **kw):
        rc, err = self.run_hook("Bash", {"command": cmd}, **kw)
        self.assertEqual(rc, 0, f"expected allow for: {cmd}\n{err}")

    # ------------------------------------------------------------ repo-fence
    def test_reading_a_siblings_reflog_is_not_a_write(self):
        # heartbeat run 2: the orchestrator's read-only check `git -C <sdk> reflog -5` was refused as a write
        b = self.ws / "repo-b"
        for cmd in (f"git -C {b} reflog", f"git -C {b} reflog -5 --date=iso", f"git -C {b} reflog show main",
                    f"git -C {b} reflog main"):
            self.assertAllowed(cmd, guards=["repo-fence"])
        for cmd in (f"git -C {b} reflog expire --all", f"git -C {b} reflog delete HEAD@{{1}}"):
            self.assertRefused(cmd, "repo-fence", guards=["repo-fence"])
        self.assertRefused("git reflog expire --all", "destructive-git", guards=["destructive-git"])

    def test_interpreter_reads_are_refused_with_a_pointer_to_read_tools(self):
        b = self.ws / "repo-b"
        rc, err = self.run_hook("Bash", {"command": f"python3 -c \"print(open('{b}/x.txt').read())\""},
                                guards=["repo-fence"])
        self.assertEqual(rc, 2)                                   # still refused: reads and writes look alike
        self.assertIn("use cat, head, grep or sed -n instead", err)
        rc, err = self.run_hook("Bash", {"command": f"echo hi > {b}/x.txt"}, guards=["repo-fence"])
        self.assertEqual(rc, 2)
        self.assertNotIn("interpreter one-liner", err)            # plain writes don't get the hint
        self.assertAllowed(f"cat {b}/x.txt", guards=["repo-fence"])

    def test_fence_refuses_writes_into_sibling_repo(self):
        b = self.ws / "repo-b"
        for cmd in (f"echo hi > {b}/x.txt", f"cp README.md {b}/", f"rm -rf {b}/src",
                    f"cd {b} && touch y", f"sed -i '' s/a/b/ {b}/f", f"git -C {b} commit -m x",
                    "cd ../repo-b && git add f.txt", "cd .. && cd repo-b && make build",
                    f"python3 -c \"open('{b}/x','w')\"", f"echo x | tee {b}/log", "mkdir ../repo-b/new"):
            self.assertRefused(cmd, "repo-fence")

    def test_fence_refuses_edit_tool_into_sibling_and_own_controls(self):
        for path in (str(self.ws / "repo-b" / "f.py"), f"{self.a}/.claude/settings.json",
                     f"{self.a}/.claude/hooks/abacus/guards.py", f"{self.a}/.abacus/config.json",
                     os.path.expanduser("~/.claude/settings.json"), str(self.home / "state" / "logbook.db")):
            rc, err = self.run_hook("Edit", {"file_path": path, "old_string": "a", "new_string": "b"})
            self.assertEqual(rc, 2, f"{path}: {err}")

    def test_fence_allows_reads_and_own_repo_writes(self):
        b = self.ws / "repo-b"
        for cmd in (f"cat {b}/README.md", f"git -C {b} log --oneline", f"cd {b} && git status && cd -",
                    "echo hi > notes.txt", "mkdir -p src/new && touch src/new/a.py", "make test",
                    "echo 'rm -rf ../repo-b' > script-notes.md", "cat <<EOF > plan.md\nrm -rf ../repo-b\nEOF",
                    "ls 2>/dev/null", "git commit -m 'echo > ../repo-b/x'"):
            self.assertAllowed(cmd)
        rc, err = self.run_hook("Write", {"file_path": f"{self.a}/src/app.py", "content": "x"})
        self.assertEqual(rc, 0, err)
        mem = os.path.expanduser("~/.claude/projects/foo/memory/note.md")
        rc, err = self.run_hook("Write", {"file_path": mem, "content": "x"})
        self.assertEqual(rc, 0, err)

    def test_unparseable_command_fails_closed(self):
        self.assertRefused("echo 'unterminated", "abacus-guards")

    def test_bad_payload_fails_closed(self):
        env = dict(os.environ, ABACUS_HOME=str(self.home))
        p = subprocess.run([sys.executable, str(HOOK), "repo-fence"], input="not json",
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)

    def test_unknown_guard_name_fails_closed(self):
        rc, _ = self.run_hook("Bash", {"command": "ls"}, guards=["repo-fence", "no-such-guard"])
        self.assertEqual(rc, 2)

    def test_bash_payload_without_tool_input_fails_closed(self):
        env = dict(os.environ, ABACUS_WORKSPACE=str(self.ws), ABACUS_HOME=str(self.home))
        p = subprocess.run([sys.executable, str(HOOK), *ALL], input=json.dumps({"tool_name": "Bash", "cwd": self.a}),
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2, p.stderr)       # exit 1 is non-blocking: a crash would let the call through
        self.assertIn("guard crashed", p.stderr)

    def test_crashing_guard_fails_closed(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import guards\n"
                  "def boom(data, segs): raise RuntimeError('boom')\n"
                  "guards.GUARDS['repo-fence'] = boom\n"
                  "sys.exit(guards.run(['repo-fence']))")
        env = dict(os.environ, ABACUS_WORKSPACE=str(self.ws), ABACUS_HOME=str(self.home), CLAUDE_PROJECT_DIR=self.a)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": self.a, "session_id": "t"}
        p = subprocess.run([sys.executable, "-c", script, str(HOOK.parent)], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("[abacus:abacus-guards] REFUSED: guard crashed: RuntimeError('boom')", p.stderr)

    # ------------------------------------------------------------ foreign-bd
    def test_foreign_bd(self):
        b = self.ws / "repo-b"
        for cmd in (f"cd {b} && bd close b-1 --reason done", f"cd {b} && bd update b-1 --status in_progress",
                    f"cd {b} && bd update b-1 --acceptance x", f"cd {b} && bd label add b-1 lite",
                    f"cd {b} && bd comment b-1 'untagged'", f"bd -C {b} list", f"bd --db {b}/.beads list",
                    f"BEADS_DIR={b}/.beads bd list", f"cd {b} && bd delete b-1", f"cd {b} && bd ship word-count",
                    f"cd {b} && bd create 'x' -l pipeline:lite", f"cd {b} && bd dep add b-2 b-1"):
            self.assertRefused(cmd, None)
        for cmd in (f"cd {b} && bd show b-1 --json", f"cd {b} && bd list --json", f"cd {b} && bd ready",
                    f"cd {b} && bd create 'Need X from repo-b' -t task",
                    f"cd {b} && bd update b-1 --append-notes '[from repo-a] context: API shipped in abc123'",
                    f"cd {b} && bd comment b-1 '[from repo-a] fyi'", "bd close a-1 --reason ok",
                    "bd update a-1 --status in_progress"):
            self.assertAllowed(cmd)

    def test_orchestrator_may_add_dependency_but_not_close(self):
        b = self.ws / "repo-b"
        kw = dict(role="orchestrator", cwd=str(self.home), project=str(self.home))
        self.assertAllowed(f"cd {b} && bd dep add b-2 b-1", guards=["foreign-bd"], **kw)
        self.assertAllowed(f"cd {b} && bd update b-1 --append-notes '[from orchestrator] ruling recorded'",
                           guards=["foreign-bd"], **kw)
        self.assertRefused(f"cd {b} && bd close b-1 --reason x", "foreign-bd", guards=["foreign-bd"], **kw)
        self.assertAllowed(f"cd {b} && bd ship word-count", guards=["foreign-bd"], **kw)
        self.assertRefused(f"cd {b} && bd ship word-count --force", "foreign-bd", guards=["foreign-bd"], **kw)
        self.assertRefused(f"cd {b} && bd update b-1 --append-notes '[from repo-a] x'", "foreign-bd",
                           guards=["foreign-bd"], **kw)

    # ------------------------------------------------------------ destructive-git
    def test_destructive_git(self):
        for cmd in ("git push --force", "git push -f origin main", "git push origin +main",
                    "git push --force-with-lease", "git push origin :main", "git push --delete origin x",
                    "git commit --no-verify -m x", "git reset --hard HEAD~1", "git checkout -- src/a.py",
                    "git checkout .", "git restore src/a.py", "git clean -fd", "git stash", "git stash pop",
                    "git worktree add ../x", "git rebase main", "git filter-branch", "git branch -D x",
                    "git add -A", "git add .", "git config core.hooksPath /dev/null",
                    "git -c core.hooksPath=/tmp commit -m x", "git reflog expire --all", "git gc --prune=now",
                    "git commit --amend -m x", "git push --tags", "timeout 30 git push -f",
                    "FOO=1 env git reset --hard"):
            self.assertRefused(cmd, "destructive-git", guards=["destructive-git"])
        for cmd in ("git push", "git push origin main", "git commit -m 'reset --hard is bad'",
                    "git add src/a.py tests/test_a.py", "git restore --staged src/a.py", "git stash list",
                    "git reset HEAD src/a.py", "git log --oneline", "git checkout -b feature",
                    "git branch", "git worktree list"):
            self.assertAllowed(cmd, guards=["destructive-git"])

    # ------------------------------------------------------------ stage-writes
    def test_stage_writes_only_in_workflow_stages(self):
        for cmd in ("git push", "git commit -m x", "bd close a-1 --reason x", "bd update a-1 --status closed",
                    "claude -p hi", "cd sub && git push"):
            self.assertRefused(cmd, "stage-writes", guards=["stage-writes"], agent_type="workflow-subagent")
            self.assertAllowed(cmd, guards=["stage-writes"])  # main session unaffected
        for cmd in ("abacus-finalize --bead a-1 --verdicts v.json", "bd show a-1 --json", "make gate"):
            self.assertAllowed(cmd, guards=["stage-writes"], agent_type="workflow-subagent")

    # ------------------------------------------------------------ database-url
    def test_database_url(self):
        for cmd in ("APP_TEST_DATABASE_URL=postgres://h/app pytest",
                    "DATABASE_URL=postgres://localhost/prod pytest -q",
                    "DATABASE_URL=postgres://localhost/app make test"):
            self.assertRefused(cmd, "database-url", guards=["database-url"])
        for cmd in ("APP_TEST_DATABASE_URL=postgres://h/app_test pytest",
                    "DATABASE_URL=postgres://localhost/app_test make gate", "pytest -q",
                    "DATABASE_URL=postgres://localhost/app ./run-server"):
            self.assertAllowed(cmd, guards=["database-url"])

    # ------------------------------------------------------------ operator-only
    def test_operator_only(self):
        for cmd in ("make deploy", "make -j4 release", "git tag v1.0", "gh release create v1",
                    "npm publish", "cargo publish"):
            self.assertRefused(cmd, "operator-only", guards=["operator-only"])
        for cmd in ("make deploy-docs", "make test", "git tag -l", "git tag", "gh release list",
                    "echo make deploy"):
            self.assertAllowed(cmd, guards=["operator-only"])

    # ------------------------------------------------------------ adversarial-review regressions
    def test_review_bypasses_are_closed(self):
        b = self.ws / "repo-b"
        cases = [
            "# force push\ngit push --force origin main",           # comment hid the next line
            "curl -s https://example.com/#x; git push --force origin main",
            f"echo a#b; touch {b}/f",
            f"cat <<< hi\ntouch {b}/f",                             # here-string is not a heredoc
            'echo "<<X"\ngit reset --hard',                         # quoted << is not a heredoc
            "cat <<EOF\nnever terminated",                          # unterminated heredoc fails closed
            "git push -uf origin main", "git commit -anm msg",       # clustered short flags
            "git -c core.hookspath=/dev/null commit -m x", "git config core.hookspath /dev/null",
            'git -c alias.p="push --force" p origin main', "git p origin main",
            'bash -c "git push --force origin main"', "sh -c 'git reset --hard'",
            "eval git push --force", "git branch -f main HEAD~5", "git checkout -f main",
            "git switch --discard-changes main", "timeout -s KILL 60 git push --force",
            "sudo -u bob git push --force", "git rm -r .githooks", "git rm .claude/settings.json",
            "touch .CLAUDE/settings.json", "touch ~/.CLAUDE/settings.json",
            f"D={b}; touch $D/f", f"cp -t {b} README", f"tar -xf a.tar -C {b}", f"find {b} -delete",
            f"ln -s {b} lnk && touch lnk/f", f"sed -i '' 's/a/b/' {b}/README.md", f"sed -i -e 's/a/b/' {b}/x",
            "python3 -c \"open('..' + '/repo-b/x','w')\"",
            f"export BEADS_DIR={b}/.beads; bd close b-1 --reason x",
            "export DATABASE_URL=postgres://h/prod; pytest", "DATABASE_URL=postgres://h/prod python -m pytest",
            "echo x > .git/hooks/pre-push", "touch $(echo ../repo-b)/f",
        ]
        for cmd in cases:
            self.assertRefused(cmd)
        self.assertRefused("bd --actor me close x-1", "stage-writes", guards=["stage-writes"],
                           agent_type="workflow")

    def test_review_false_positives_are_fixed(self):
        b = self.ws / "repo-b"
        for cmd in (f"(cd {b} && git log -1); touch notes.txt", "git commit -m 'fix: handle # in urls'",
                    "git commit -mfix", "echo '# not a comment' > notes.md", "git checkout -b feature/x",
                    "git switch main", "curl -s 'https://example.com/a#b' -o /dev/null",
                    "python3 -c 'print(1 + 1)'", f"cat {b}/README.md | grep foo",
                    "for f in a b; do echo $f; done", "make test 2>&1 | tail -5",
                    "echo '- `word_count(text)`: number of words' >> README.md",
                    "sed -i '' 's/NEEDS YOU/DONE/' notes.md", "sed -i.bak -e 's/a/b/' notes.md",
                    'printf "%s\\n" "$HOME" > notes.txt'):
            self.assertAllowed(cmd)

    def test_refusals_are_logged(self):
        self.assertRefused("git push --force", guards=["destructive-git"])
        log = (self.home / "state" / "refusals.jsonl").read_text().splitlines()
        rec = json.loads(log[-1])
        self.assertEqual(rec["guard"], "destructive-git")
        self.assertIsNone(rec["classification"])


if __name__ == "__main__":
    unittest.main()


class RoleCommandsTest(unittest.TestCase):
    def test_allowlist_patterns(self):
        sys.path.insert(0, str(HOOK.parent))
        import guards
        m = guards._matches
        self.assertTrue(m(["abacus-logbook", "digest"], "abacus-logbook digest"))
        self.assertFalse(m(["abacus-logbook", "digest", "--extra"], "abacus-logbook digest"))
        self.assertTrue(m(["/Users/x/.abacus/bin/abacus-logbook", "show", "in-flight"], "abacus-logbook show *"))
        self.assertFalse(m(["abacus-logbook", "dispatch", "add"], "abacus-logbook show *"))
        self.assertTrue(m(["git", "-C", "/r", "log", "-3"], "git -C * log *"))
        self.assertFalse(m(["git", "-C", "/r", "commit"], "git -C * log *"))
        self.assertFalse(m(["abacus-session", "start", "coder:repo-a"], "abacus-session start agent:orchestrator"))
        self.assertFalse(m([], "abacus-status"))


class MultiRootFenceTest(unittest.TestCase):
    """dy3: the fence protects every workspace root and every repository registered in the fleet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.roots = [t / "src", t / "work"]
        self.mine = self.roots[0] / "mine"
        self.sib1 = self.roots[0] / "sib"
        self.sib2 = self.roots[1] / "other"
        self.loose = t / "elsewhere" / "loose"          # outside every root, but in the fleet
        self.stray = t / "elsewhere" / "stray"          # outside every root and not in the fleet
        for r in (self.mine, self.sib1, self.sib2, self.loose, self.stray):
            r.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        self.home = t / "home"
        (self.home / "agents").mkdir(parents=True)
        (self.home / "agents" / "fleet.json").write_text(json.dumps(
            {"workers": {"coder:loose": {"repo": str(self.loose)}, "coder:mine": {"repo": str(self.mine)}}}))
        (self.home / "config.json").write_text(json.dumps(
            {"workspace_root": str(self.roots[0]), "workspace_roots": [str(r) for r in self.roots]}))

    def tearDown(self):
        self.tmp.cleanup()

    def rc(self, cmd, env_ws=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.mine), "session_id": "t"}
        env = {k: v for k, v in os.environ.items() if k != "ABACUS_WORKSPACE"}
        env.update(ABACUS_HOME=str(self.home), CLAUDE_PROJECT_DIR=str(self.mine))
        if env_ws:
            env["ABACUS_WORKSPACE"] = env_ws
        return subprocess.run([sys.executable, str(HOOK), "repo-fence"], input=json.dumps(payload),
                              capture_output=True, text=True, env=env).returncode

    def test_every_root_and_fleet_repo_is_fenced(self):
        for target in (self.sib1, self.sib2, self.loose):
            self.assertEqual(self.rc(f"echo x > {target}/f.txt"), 2, target)
        # negative controls: the session's own repo, and a repo neither under a root nor in the fleet
        self.assertEqual(self.rc("echo x > f.txt"), 0)
        self.assertEqual(self.rc(f"echo x > {self.stray}/f.txt"), 0)

    def test_single_root_config_still_works(self):
        (self.home / "config.json").write_text(json.dumps({"workspace_root": str(self.roots[0])}))
        self.assertEqual(self.rc(f"echo x > {self.sib1}/f.txt"), 2)
        self.assertEqual(self.rc(f"echo x > {self.sib2}/f.txt"), 0)   # the second root was never configured

    def test_env_lists_several_roots(self):
        (self.home / "config.json").write_text("{}")
        ws = os.pathsep.join(str(r) for r in self.roots)
        self.assertEqual(self.rc(f"echo x > {self.sib2}/f.txt", env_ws=ws), 2)


class PlannerGuardTest(unittest.TestCase):
    """agent:planner files and shapes drafts on any board; approval and pipeline labels need an operator ruling."""

    @classmethod
    def setUpClass(cls):
        import sqlite3
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name).resolve()
        cls.board = t / "sdk"
        cls.board.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=cls.board, check=True)
        subprocess.run(["bd", "init", "--prefix", "sdk", "--quiet", "--skip-agents", "--skip-hooks",
                        "--non-interactive"], cwd=cls.board, check=True, capture_output=True)
        bd = lambda *a: subprocess.run(["bd", *a], cwd=cls.board, check=True, capture_output=True,
                                       text=True).stdout.strip()
        cls.draft = bd("create", "Draft one", "-d", "x", "-l", "draft", "--silent")
        cls.approved_draft = bd("create", "Draft two", "-d", "x", "-l", "draft", "--silent")
        cls.owned = bd("create", "Owner's bead", "-d", "x", "--silent")
        cls.planner = t / "agents" / "planner"
        cls.planner.mkdir(parents=True)
        cls.logbook = t / "logbook.db"
        c = sqlite3.connect(str(cls.logbook))
        c.execute("CREATE TABLE rulings (id INTEGER PRIMARY KEY, words TEXT, target_item TEXT)")
        c.execute("CREATE TABLE ruling_targets (id INTEGER PRIMARY KEY, ruling_id INTEGER, bead TEXT)")
        c.execute("INSERT INTO rulings(id, words, target_item) VALUES (1, 'approve', NULL)")
        c.execute("INSERT INTO ruling_targets(ruling_id, bead) VALUES (1, ?)", (cls.approved_draft,))
        c.commit(); c.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rc(self, cmd, **extra_env):
        payload = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.board} && {cmd}"},
                   "cwd": str(self.planner), "session_id": "t"}
        env = dict(os.environ, ABACUS_ROLE="planner", ABACUS_LOGBOOK_DB=str(self.logbook),
                   ABACUS_HOME=str(self.planner.parent.parent), CLAUDE_PROJECT_DIR=str(self.planner), **extra_env)
        p = subprocess.run([sys.executable, str(HOOK), "foreign-bd"], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stderr

    def test_files_and_shapes_drafts(self):
        for cmd in (f"bd show {self.draft} --json", "bd list --json",
                    "bd create 'Add heartbeat' -t feature -l draft --silent --body-file - <<'EOF'\nx\nEOF",
                    "bd create 'Two labels' -l draft,export:heartbeat --silent",
                    f"bd update {self.draft} --acceptance '- a check'",
                    f"bd update {self.draft} --title 'Better title'",
                    f"bd dep add {self.draft} external:other:cap",
                    f"bd label add {self.draft} export:heartbeat",
                    f"bd close {self.draft} --reason 'dropped by the operator'",
                    f"bd update {self.owned} --append-notes '[from planner] see plan for issue #1'"):
            self.assertEqual(self.rc(cmd)[0], 0, cmd)

    def test_never_runs_anything_before_approval(self):
        for cmd, why in (("bd create 'No draft label' --silent", "drafts only"),
                         ("bd create 'Sneaky' -l draft,pipeline:lite --silent", "no pipeline label"),
                         (f"bd label add {self.draft} pipeline:lite", "no operator ruling"),
                         (f"bd label remove {self.draft} draft", "no operator ruling"),
                         (f"bd update {self.owned} --title 'mine now'", "not a draft"),
                         (f"bd label add {self.owned} pipeline:full", "not a draft"),
                         (f"bd close {self.owned}", "only drafts"),
                         (f"bd update {self.draft} --status in_progress", "not ['--status']"),
                         (f"bd update {self.owned} --append-notes 'untagged note'", "not a draft"),
                         (f"bd delete {self.draft} --force", "not something the planner does")):
            rc, err = self.rc(cmd)
            self.assertEqual(rc, 2, cmd)
            self.assertIn(why, err, cmd)

    def test_operator_ruling_unlocks_labels_on_the_beads_it_names(self):
        self.assertEqual(self.rc(f"bd label add {self.approved_draft} pipeline:lite")[0], 0)
        self.assertEqual(self.rc(f"bd label remove {self.approved_draft} draft")[0], 0)

    def test_missing_bd_fails_closed(self):
        # is_draft asks bd for the bead's labels; with bd off PATH the guard must refuse, not crash open
        path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                               if not os.path.exists(os.path.join(d, "bd")))
        rc, err = self.rc(f"bd update {self.draft} --title 'Better title'", PATH=path)
        self.assertEqual(rc, 2, err)
        self.assertIn("guard crashed", err)
