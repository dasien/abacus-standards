"""Negative controls for the secrets guard (PreToolUse) and redact.py (PostToolUse).

The environment is scrubbed per test so "secrets loaded" is controlled, never inherited.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "canonical" / "hooks"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
VALUE = "s3cr3t-Value-For-Tests-9931"


class SecretsGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name).resolve() / "repo"
        cls.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=cls.repo, check=True)
        cls.home = Path(cls.tmp.name).resolve() / "abacus-home"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def env(self, loaded):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "ABACUS_HOME": str(self.home),
             "CLAUDE_PROJECT_DIR": str(self.repo), "ABACUS_WORKSPACE": str(self.repo.parent),
             "TERM_SESSION_ID": "w0t0p0:9F2C1A7E-1111-2222-3333-444455556666", "SSH_AUTH_SOCK": "/tmp"}
        if loaded:
            e["GH_TOKEN"] = TOKEN
        return e

    def run_hook(self, tool, tool_input, loaded=True):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.repo), "session_id": "t"}
        p = subprocess.run([sys.executable, str(HOOKS / "guards.py"), "secrets"], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env(loaded))
        return p.returncode, p.stderr

    def refused(self, cmd, loaded=True):
        rc, err = self.run_hook("Bash", {"command": cmd}, loaded)
        self.assertEqual(rc, 2, f"expected refusal: {cmd}\n{err}")
        self.assertNotIn(TOKEN, err, "a refusal message must never echo the secret")

    def allowed(self, cmd, loaded=True):
        rc, err = self.run_hook("Bash", {"command": cmd}, loaded)
        self.assertEqual(rc, 0, f"expected allow: {cmd}\n{err}")

    def test_environment_dumps(self):
        for cmd in ("env", "env | grep TOKEN", "printenv", "printenv GH_TOKEN", "set", "export", "export -p",
                    "declare -p", "declare -x", "typeset -px", "ps eww", "ps -E", "cat /proc/1/environ",
                    "python3 -c 'import os; print(os.environ)'", "node -e 'console.log(process.env)'",
                    "ruby -e 'p ENV[\"GH_TOKEN\"]'"):
            self.refused(cmd, loaded=False if cmd.startswith("ps") else True)

    def test_ps_refused_only_while_secrets_loaded(self):
        self.refused("ps aux")
        self.refused("ps -ef | grep node")
        self.refused("pgrep -fl node")
        self.allowed("ps aux", loaded=False)
        self.allowed("pgrep -x node")

    def test_tracing_refused_while_loaded(self):
        self.refused("set -x; make test")
        self.refused("bash -x ./script.sh")
        self.allowed("bash -x ./script.sh", loaded=False)

    def test_printing_secret_variables(self):
        for cmd in ("echo $GH_TOKEN", 'printf "%s" "$GH_TOKEN"', 'echo "token=${GH_TOKEN}" > out.txt',
                    "echo $MY_SERVICE_API_KEY", "echo $DATABASE_URL | tee log.txt", "echo $BWS_ACCESS_TOKEN"):
            self.refused(cmd)

    def test_credential_files(self):
        for cmd in ("cat .env", "head -5 .env.local", "grep KEY .env", "cp .env /tmp/x", "base64 < .env",
                    "cat ~/.aws/credentials", "cat ~/.ssh/id_ed25519", "cat ~/.npmrc", "source .env",
                    "less '~/Library/Application Support/Bitwarden CLI/data.json'", "git add .env",
                    "cat prod.pem", "cat ~/.config/gh/hosts.yml", "cat ~/.docker/config.json",
                    "cat ~/.microsoft/usersecrets/abc/secrets.json", "cat ~/.zsh_history", "tail terraform.tfstate"):
            self.refused(cmd)

    def test_credential_retrieval_commands(self):
        for cmd in ("bw get password github", "bw list items", "bw unlock --raw", "bw export",
                    "bws secret get 00000000-0000-0000-0000-000000000000", "bws secret list",
                    "bws run -- env", "security find-generic-password -s x -w", "security dump-keychain",
                    "gh auth token", "gh auth status --show-token", "git credential fill",
                    "aws sts get-session-token", "aws secretsmanager get-secret-value --secret-id x",
                    "aws ssm get-parameter --name x --with-decryption", "op read op://vault/item/field",
                    "vault kv get secret/x", "kubectl get secret db -o yaml", "dotnet user-secrets list",
                    "gcloud secrets versions access latest --secret x", "az keyvault secret show --name x"):
            self.refused(cmd)

    def test_read_and_edit_tools(self):
        for tool, ti in (("Read", {"file_path": str(self.repo / ".env")}),
                         ("Read", {"file_path": os.path.expanduser("~/.ssh/id_rsa")}),
                         ("Grep", {"pattern": "KEY", "path": str(self.repo / ".env.production")}),
                         ("Grep", {"pattern": "KEY", "glob": "*.pem"}),
                         ("Edit", {"file_path": str(self.repo / ".env"), "old_string": "a", "new_string": "b"})):
            rc, err = self.run_hook(tool, ti)
            self.assertEqual(rc, 2, f"{tool} {ti}: {err}")
        for tool, ti in (("Read", {"file_path": str(self.repo / "README.md")}),
                         ("Read", {"file_path": str(self.repo / ".env.example")}),
                         ("Write", {"file_path": str(self.repo / ".env"), "content": "API_KEY=dummy"}),
                         ("Grep", {"pattern": "TODO", "path": str(self.repo / "src")})):
            rc, err = self.run_hook(tool, ti)
            self.assertEqual(rc, 0, f"{tool} {ti}: {err}")

    def test_ordinary_work_is_untouched(self):
        for cmd in ("env FOO=1 make test", "printenv PATH", "echo $HOME", "cat .env.example", "cat README.md",
                    "ls -la .env", "git check-ignore .env", "cat ~/.ssh/known_hosts", "cat ~/.ssh/id_ed25519.pub",
                    "cat src/environment.py",
                    "curl -sH \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/user > out.json",
                    "gh auth status", "bw status", "bws --version", "python3 -c 'print(1 + 1)'",
                    "gh pr list", "make test 2>&1 | tail -5", "grep -r TODO src/"):
            self.allowed(cmd)

    def test_repo_configured_names_and_paths(self):
        (self.repo / ".abacus").mkdir(exist_ok=True)
        (self.repo / ".abacus" / "config.json").write_text(json.dumps(
            {"project": "r", "secret_env_names": ["VENDOR_THING"], "secret_paths": ["config/prod.json"]}))
        try:
            self.refused("echo $VENDOR_THING")
            self.refused("cat config/prod.json")
            self.allowed("cat config/dev.json")
        finally:
            (self.repo / ".abacus" / "config.json").unlink()


class RedactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def redact(self, response, extra_env=None, raw=None):
        env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "ABACUS_HOME": str(self.home),
               "MY_API_KEY": VALUE, "BW_SHORT": "abc"}
        env.update(extra_env or {})
        payload = raw if raw is not None else json.dumps(
            {"tool_name": "Bash", "tool_response": response, "cwd": self.tmp.name, "session_id": "t"})
        p = subprocess.run([sys.executable, str(HOOKS / "redact.py")], input=payload, capture_output=True,
                           text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["updatedToolOutput"] if p.stdout.strip() else None

    def test_env_values_and_encodings_are_redacted_structure_preserved(self):
        out = self.redact({"stdout": f"key={VALUE}\nb64={base64.b64encode(VALUE.encode()).decode()}",
                           "stderr": "", "interrupted": False, "nested": [{"x": VALUE}]})
        self.assertNotIn(VALUE, json.dumps(out))
        self.assertIn("[REDACTED:MY_API_KEY]", out["stdout"])
        self.assertEqual(out["interrupted"], False)
        self.assertEqual(out["nested"][0]["x"], "[REDACTED:MY_API_KEY]")

    def test_token_shapes_are_redacted_without_env(self):
        key_block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
        bws = "0.48b4774c-68e0-4f12-a2b6-0123456789ab.Xk2jL9pQ8mN7vB6cZ5xW:aGVsbG8gd29ybGQgdGhpcyBpcw=="
        text = (f"{TOKEN} AKIAABCDEFGHIJKLMNOP xoxb-1234567890-abcdefghij "
                f"https://user:hunter2pass@example.com/x {key_block} {bws} "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        out = self.redact({"stdout": text})
        for leaked in (TOKEN, "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890", "hunter2pass", "b3BlbnNzaC1rZXk",
                       "Xk2jL9pQ8mN7vB6cZ5xW", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"):
            self.assertNotIn(leaked, out["stdout"], leaked)

    def test_clean_output_passes_through_untouched(self):
        self.assertIsNone(self.redact({"stdout": "3 tests OK, abc"}))  # short values are never redacted

    def test_unprocessable_payload_is_withheld_not_passed(self):
        out = self.redact(None, raw="this is not json " + VALUE)
        self.assertIn("withheld", out)
        self.assertNotIn(VALUE, out)

    def test_log_records_counts_never_values(self):
        self.redact({"stdout": VALUE})
        log = (self.home / "state" / "redactions.jsonl").read_text()
        self.assertIn("MY_API_KEY", log)
        self.assertNotIn(VALUE, log)


if __name__ == "__main__":
    unittest.main()
