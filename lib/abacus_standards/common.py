"""Helpers shared by the repository-side Abacus tools (doctor, board, finalize, adopt)."""
import json
import os
import re
import subprocess
from pathlib import Path

STANDARDS_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = STANDARDS_ROOT / "canonical"

CONFIG_KEYS = {
    "abacus_version": int, "project": str, "default_branch": str, "gate": str, "fast_check": str,
    "fix_rounds": dict, "escalation_list": list, "doctrine_docs": list, "context_ceiling_pct": int,
    "secret_env_names": list, "secret_paths": list, "landing": str,
}
CONFIG_DEFAULTS = {"abacus_version": 1, "default_branch": "main", "gate": "make gate",
                   "fast_check": "make check", "fix_rounds": {"lite": 1, "full": 2},
                   "escalation_list": [], "doctrine_docs": ["CLAUDE.md"], "context_ceiling_pct": 60}
LANDINGS = ("push", "draft-pr")  # push: straight to the default branch; draft-pr: a branch and a draft PR per bead
PIPELINES = ("one-shot", "lite", "full", "manual", "operator")
GUARD_NAMES = ("repo-fence", "foreign-bd", "destructive-git", "stage-writes", "database-url", "operator-only",
               "secrets", "human-only")
HOOK_FILES = ("guards.py", "_shell.py", "_secrets.py", "redact.py")
RUNS_DIR = ".abacus-runs"
IGNORED = (RUNS_DIR + "/", ".claude/worktrees/")  # run artifacts; Claude Code's per-session worktrees


class Refused(Exception):
    pass


def run(cmd, cwd=None, check=False, input=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input,
                       env=dict(os.environ, **(env or {})))
    if check and p.returncode != 0:
        raise Refused(f"`{' '.join(cmd)}` failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:500]}")
    return p


def toplevel(path="."):
    p = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if p.returncode != 0:
        raise Refused(f"{Path(path).resolve()} is not inside a git repository")
    return Path(p.stdout.strip()).resolve()


def repo_identity(path="."):
    """The main checkout behind path (worktrees of one repository share it)."""
    p = run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if p.returncode != 0:
        return Path(path).resolve()
    common = Path(p.stdout.strip()).resolve()
    return common.parent if common.name == ".git" else common


def load_config(repo):
    """Load .abacus/config.json, refusing unknown keys by name before anything runs."""
    p = Path(repo) / ".abacus" / "config.json"
    if not p.exists():
        raise Refused(f"{p} not found: this repository has not adopted Abacus (run abacus-adopt)")
    cfg = json.loads(p.read_text())
    unknown = [k for k in cfg if k not in CONFIG_KEYS]
    if unknown:
        raise Refused(f"unknown config key(s) in {p}: {', '.join(unknown)}")
    for k, t in CONFIG_KEYS.items():
        if k in cfg and not isinstance(cfg[k], t):
            raise Refused(f"config key {k} must be {t.__name__}")
    if "project" not in cfg:
        raise Refused(f"{p} must name the project")
    if cfg.get("landing", "push") not in LANDINGS:  # a typo must not quietly fall back to pushing
        raise Refused(f"config key landing must be one of {', '.join(LANDINGS)} (got {cfg['landing']!r})")
    return dict(CONFIG_DEFAULTS, **dict({"landing": "push"}, **cfg))


def bd_json(args, cwd):
    p = run(["bd", *args, "--json"], cwd=cwd)
    if p.returncode != 0:
        raise Refused(f"bd {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:300]}")
    try:
        return json.loads(p.stdout or "null")
    except ValueError as e:
        raise Refused(f"bd {' '.join(args)} returned unparseable JSON: {e}")


def show_bead(bead, cwd):
    data = bd_json(["show", bead], cwd)
    if isinstance(data, list):
        if not data:
            raise Refused(f"bead {bead} not found")
        data = data[0]
    return data


def board_path(repo):
    """Resolve the active board and confirm it sits inside this repository."""
    if os.environ.get("BEADS_DIR"):
        raise Refused(f"BEADS_DIR is set in the environment ({os.environ['BEADS_DIR']}); an inherited board "
                      f"variable could redirect writes to another repository")
    p = run(["bd", "where", "--json"], cwd=repo)
    if p.returncode != 0 or not p.stdout.strip():
        raise Refused(f"`bd where --json` failed or printed nothing: {(p.stderr or p.stdout).strip()[:300]}")
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise Refused(f"`bd where --json` printed non-JSON: {p.stdout.strip()[:200]}")
    path = data.get("path") or data.get("beads_dir") or data.get("database") or data.get("db_path") \
        if isinstance(data, dict) else None
    if not path:
        raise Refused(f"`bd where --json` did not name a path: {data}")
    path = Path(path).resolve()
    main = repo_identity(repo)  # inside a worktree, bd correctly resolves to the main checkout's board
    if not any(path == r or r in path.parents for r in (repo, main)):
        raise Refused(f"the board resolves to {path}, outside this repository {repo}")
    return path


def acceptance_criteria(bead):
    """Split the acceptance field into criteria: one per bullet or numbered line."""
    text = bead.get("acceptance_criteria") or bead.get("acceptance") or ""
    items = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.)]|\[[ x]\])\s+(.*\S)", line)
        if m:
            items.append(m.group(1))
        elif line.strip() and items and line.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
        elif line.strip() and not items:
            items.append(line.strip())
    return items


def norm(s):
    return " ".join((s or "").split()).lower()


def pipeline_label(bead):
    labels = [l for l in (bead.get("labels") or []) if l.startswith("pipeline:")]
    return labels


def filed_against(bead):
    first = (bead.get("description") or "").splitlines()[:1]
    m = re.match(r"FILED AGAINST:\s*(\S+)", first[0]) if first else None
    return m.group(1) if m else None


def bead_branch(bead):
    """abacus/<slug>-<bead id>: readable in GitHub's branch list, and findable on the remote by its id suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", (bead.get("title") or "").lower()).strip("-")[:40].rstrip("-")
    return f"abacus/{slug}-{bead['id']}"


def branches_for(names, bead_id):
    """The abacus/ branches among names that belong to bead_id (exact id suffix: x-1 never matches x-1.1)."""
    return [n for n in names if n.startswith("abacus/") and n.endswith(f"-{bead_id}")]


def abacus_branches(repo):
    """Every abacus/ branch on origin, read with ls-remote (no fetch)."""
    p = run(["git", "ls-remote", "--heads", "origin", "refs/heads/abacus/*"], cwd=repo)
    if p.returncode != 0:
        raise Refused(f"cannot read origin's branches: {(p.stderr or p.stdout).strip()[:300]}")
    refs = [line.split("\t", 1)[1] for line in p.stdout.splitlines() if "\t" in line]
    return [r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/abacus/")]


def gh(args, repo, input=None):
    try:
        return run(["gh", *args], cwd=repo, input=input)
    except OSError as e:
        raise Refused(f"cannot run gh ({e}); draft-pr landing needs the GitHub CLI")


def open_pr(repo, branch):
    """{number, url} of the open PR whose head is branch, or None."""
    p = gh(["pr", "list", "--head", branch, "--state", "open", "--json", "number,url"], repo)
    if p.returncode != 0:
        raise Refused(f"`gh pr list --head {branch}` failed: {(p.stderr or p.stdout).strip()[:300]}")
    try:
        prs = json.loads(p.stdout or "[]")
    except ValueError:
        raise Refused(f"`gh pr list --head {branch}` printed non-JSON: {p.stdout.strip()[:200]}")
    return prs[0] if prs else None


# Files adoption puts in a repository that every worker's worktree must contain (worktrees hold only tracked files).
MUST_TRACK = (".claude/settings.json", ".claude/hooks/abacus/guards.py", ".claude/workflows/abacus-lite.js",
              ".claude/skills/abacus-worker/SKILL.md", ".abacus/config.json", ".githooks/pre-push", ".mcp.json")
CLAUDE_DIR_RULES = (".claude", ".claude/", "/.claude", "/.claude/")
CLAUDE_OPENED = [".claude/*", "!.claude/settings.json", "!.claude/hooks/", "!.claude/skills/", "!.claude/workflows/"]


def ignored_paths(repo, paths=MUST_TRACK):
    """{path: 'source:line:pattern'} for each path the repository's ignore rules would hide (existing or not)."""
    p = run(["git", "check-ignore", "--no-index", "-v", *paths], cwd=repo)
    out = {}
    for line in p.stdout.splitlines():
        rule, _, path = line.partition("\t")
        if path and not rule.split(":", 2)[-1].startswith("!"):   # -v also lists paths a `!` rule un-ignores
            out[path] = rule
    return out

