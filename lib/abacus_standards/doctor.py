"""abacus-doctor: which Abacus conventions has this repository adopted?

Strictly read-only. It never opens a file for writing and never runs a gate or
formatter; every subprocess it spawns is named below and read-only by contract:
  git rev-parse / git config --get, bd where --json, bd list --json,
  abacus-validate-records (reads only), abacus-memory status (reads only)

Verdicts:
  ADOPTED      present and working
  NOT ADOPTED  absent
  BROKEN       present and failing; reserved for safety findings
  UNCHECKED    could not look -- never read as green
  DEVIATING    absent, but declined on the record (docs/adr with deviates:<check>)

Exit status is 1 only when something is BROKEN. NOT ADOPTED and DEVIATING do
not move it.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import common
from .common import CANONICAL, GUARD_NAMES, HOOK_FILES, PIPELINES, Refused, run

ADOPTED, NOT, BROKEN, UNCHECKED, DEVIATING = "ADOPTED", "NOT ADOPTED", "BROKEN", "UNCHECKED", "DEVIATING"
INSTRUCTION_PHRASES = ("instruction edits", "delegated", "changes authority")
MAX_INSTRUCTION_LINES = 200
REGISTRY = []


def check(name, tier, remedy, why_query):
    def wrap(fn):
        REGISTRY.append({"name": name, "tier": tier, "fn": fn, "remedy": remedy, "why": why_query})
        return fn
    return wrap


def same_bytes(a, b):
    try:
        return Path(a).read_bytes() == Path(b).read_bytes()
    except OSError:
        return False


def settings(repo):
    p = repo / ".claude" / "settings.json"
    return json.loads(p.read_text()) if p.exists() else {}


def claude_md_paragraphs(repo):
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = repo / name
        if p.exists():
            text = re.sub(r"```.*?```", "", p.read_text(), flags=re.S)
            yield from (para for para in re.split(r"\n\s*\n", text))


# ---------------------------------------------------------------- obligations

@check("emit", "OBLIGATION", "write a retro with the /retro skill and commit it",
       "retrospective emission validator zero validated")
def c_emit(repo):
    tool = common.STANDARDS_ROOT / "bin" / "abacus-validate-records"
    p = run([str(tool)], cwd=repo)
    m = re.search(r"validated (\d+)", p.stdout)
    if not m:
        return UNCHECKED, f"validator output unreadable: {(p.stdout + p.stderr).strip()[:200]}"
    if p.returncode != 0 and int(m.group(1)) > 0:
        return BROKEN, f"invalid records would be refused by harvest: {p.stderr.strip()[:300]}"
    if int(m.group(1)) == 0:
        return NOT, "zero records validated (zero validated is not green)"
    return ADOPTED, p.stdout.strip()


@check("consult", "OBLIGATION", "abacus-adopt registers the abacus-memory MCP server in .mcp.json",
       "consult shared memory before settling an approach")
def c_consult(repo):
    mcp = repo / ".mcp.json"
    if not mcp.exists() or "abacus-memory" not in json.loads(mcp.read_text()).get("mcpServers", {}):
        return NOT, "abacus-memory MCP server not registered in .mcp.json"
    if "abacus-memory" not in settings(repo).get("enabledMcpjsonServers", []):
        return NOT, ("abacus-memory is registered but not enabled in .claude/settings.json (enabledMcpjsonServers); "
                     "background sessions cannot answer the approval prompt, so consult_memory never loads")
    p = run(["abacus-memory", "status"])
    if p.returncode != 0:
        return UNCHECKED, f"abacus-memory status failed: {(p.stderr or p.stdout).strip()[:200]}"
    cov = json.loads(p.stdout).get("coverage", {})
    if not sum(cov.values()):
        return NOT, "memory server registered but the corpus has no rows"
    return ADOPTED, f"corpus coverage {cov}"


# ---------------------------------------------------------------- conventions

@check("config", "CONVENTION", "abacus-adopt writes .abacus/config.json", "abacus repository config keys")
def c_config(repo):
    try:
        cfg = common.load_config(repo)
    except Refused as e:
        return NOT if "not found" in str(e) else BROKEN, str(e)
    return ADOPTED, f"project={cfg['project']} gate={cfg['gate']!r}"


@check("guards", "CONVENTION", "abacus-adopt copies the canonical guards and wires them in .claude/settings.json",
       "guard refused agent action outside boundary")
def c_guards(repo):
    problems = []
    for f in HOOK_FILES:
        local = repo / ".claude" / "hooks" / "abacus" / f
        if not local.exists():
            problems.append(f"{f} absent")
        elif not same_bytes(local, CANONICAL / "hooks" / f):
            problems.append(f"{f} drifted from canonical")
    wired = json.dumps(settings(repo).get("hooks", {}).get("PreToolUse", []))
    unwired = [g for g in GUARD_NAMES if g not in wired] if "guards.py" in wired else list(GUARD_NAMES)
    if unwired:
        problems.append(f"unwired: {', '.join(unwired)}")
    pre = settings(repo).get("hooks", {}).get("PreToolUse", [])
    if not any("Read" in (e.get("matcher") or "") and "guards.py" in json.dumps(e) for e in pre):
        problems.append("secrets guard not wired for Read/Grep")
    post = json.dumps(settings(repo).get("hooks", {}).get("PostToolUse", []))
    if "redact.py" not in post:
        problems.append("redact.py not wired as a PostToolUse hook (tool output is not redacted)")
    return (NOT, "; ".join(problems)) if problems else (ADOPTED, f"{len(GUARD_NAMES)} guards + redaction"
                                                                 " byte-identical and wired")


def _bundle(repo, kind, dest, pattern):
    problems, n = [], 0
    for src in sorted((CANONICAL / kind).glob(pattern)):
        rel = src.relative_to(CANONICAL / kind)
        local = repo / dest / rel
        n += 1
        if not local.exists():
            problems.append(f"{rel} absent")
        elif not same_bytes(local, src):
            problems.append(f"{rel} drifted")
    return (NOT, "; ".join(problems)) if problems else (ADOPTED, f"{n} file(s) byte-identical")


@check("workflows", "CONVENTION", "abacus-adopt copies the one-shot/lite/full workflows into .claude/workflows",
       "dynamic workflow stage contract")
def c_workflows(repo):
    return _bundle(repo, "workflows", ".claude/workflows", "abacus-*.js")


@check("skills", "CONVENTION", "abacus-adopt copies the Abacus skills into .claude/skills",
       "scoping skills bead filing")
def c_skills(repo):
    return _bundle(repo, "skills", ".claude/skills", "*/SKILL.md")


@check("board", "CONVENTION", "run `bd init --skip-agents --skip-hooks` at the repository root",
       "board redirected outside repository")
def c_board(repo):
    try:
        path = common.board_path(repo)
    except Refused as e:
        if "outside this repository" in str(e) or "BEADS_DIR" in str(e):
            return BROKEN, str(e)
        if not (repo / ".beads").exists():
            return NOT, "no .beads board in this repository"
        return UNCHECKED, str(e)
    except OSError as e:
        return UNCHECKED, f"bd not runnable: {e}"
    return ADOPTED, f"board at {path}"


@check("labels", "CONVENTION", "the board's owner sets one pipeline:<one-shot|lite|full|manual|operator> label",
       "pipeline label missing bead")
def c_labels(repo):
    try:
        items = common.bd_json(["list", "--status", "open,in_progress", "-n", "0"], repo) or []
    except (Refused, OSError) as e:
        return UNCHECKED, str(e)
    bad = []
    for it in items:
        if it.get("issue_type") == "epic":
            continue
        labels = [l for l in it.get("labels") or [] if l.startswith("pipeline:")]
        if len(labels) != 1 or labels[0].split(":", 1)[1] not in PIPELINES:
            bad.append(f"{it.get('id')}({','.join(labels) or 'none'})")
    if bad:
        return NOT, f"{len(bad)} open bead(s) without exactly one pipeline label: {', '.join(bad[:10])}"
    return ADOPTED, f"{len(items)} open bead(s) labelled"


def hooks_path_problem(repo, hp):
    """What's wrong with core.hooksPath, judged by where it points rather than how it's spelled, or None.

    Adoption sets `.githooks`. Claude Code rewrites a relative hooksPath to the absolute path of the main checkout's
    folder when it creates a session's worktree, in the shared config, so that spelling is fine too. What matters is
    that git finds this repository's gates: a moved repository leaves an absolute path pointing nowhere, and git
    then runs no hooks at all without saying so."""
    fix = "run `git config core.hooksPath .githooks`"
    if not hp:
        return f"core.hooksPath is unset, so git runs .git/hooks instead of the gates; {fix}"
    target = Path(hp).expanduser()
    if not target.is_absolute():
        if target == Path(".githooks"):
            return None
        return f"core.hooksPath is {hp}, not .githooks: the gates are bypassed; {fix}"
    if not target.is_dir():
        return (f"core.hooksPath is {hp}, which doesn't exist, so git runs no gates at all (was the repository "
                f"moved?); {fix}")
    common_dir = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo).stdout.strip()
    ours = {(repo / ".githooks").resolve()}
    if common_dir:
        ours.add((Path(common_dir).parent / ".githooks").resolve())  # the main checkout's, seen from a worktree
    if target.resolve() not in ours:
        return f"core.hooksPath is {hp}, not this repository's .githooks: the gates are bypassed; {fix}"
    return None


@check("gate", "CONVENTION", "abacus-adopt installs .githooks and sets core.hooksPath",
       "agent skipped tests pre-existing failure gate")
def c_gate(repo):
    hp = run(["git", "config", "--get", "core.hooksPath"], cwd=repo).stdout.strip()
    pre_push = repo / ".githooks" / "pre-push"
    problems = []
    why = hooks_path_problem(repo, hp)
    if why:
        problems.append(why)
    for h in ("pre-commit", "pre-push"):
        local = repo / ".githooks" / h
        if not local.exists():
            problems.append(f"{h} absent")
        elif not same_bytes(local, CANONICAL / "githooks" / h):
            problems.append(f"{h} drifted")
        elif not os.access(local, os.X_OK):
            problems.append(f"{h} not executable")
    if pre_push.exists() and "abacus-lock" not in pre_push.read_text():
        problems.append("pre-push does not take the heavy lock")
    return (NOT, "; ".join(problems)) if problems else (ADOPTED, "commit and push gates installed")


@check("close-out-door", "CONVENTION", "put abacus-finalize on PATH (install.sh)", "finishing script close out")
def c_door(repo):
    p = run(["abacus-finalize", "--help"])
    if p.returncode != 0:
        return NOT, "abacus-finalize not runnable"
    if "only door" not in p.stdout:
        return BROKEN, "abacus-finalize on PATH is not the Abacus finishing script"
    return ADOPTED, "abacus-finalize present with its refusal contract"


@check("instruction", "CONVENTION",
       "add one CLAUDE.md paragraph covering instruction edits, what is delegated, and what changes authority",
       "instruction edits delegated authority")
def c_instruction(repo):
    for para in claude_md_paragraphs(repo):
        low = para.lower()
        if all(ph in low for ph in INSTRUCTION_PHRASES):
            return ADOPTED, "authority paragraph present"
    return NOT, f"no single paragraph carries all of {INSTRUCTION_PHRASES}"


@check("instruction-size", "CONVENTION",
       f"keep CLAUDE.md under {MAX_INSTRUCTION_LINES} lines; move procedure into skills",
       "instruction file too long")
def c_size(repo):
    p = repo / "CLAUDE.md"
    if not p.exists():
        return NOT, "no CLAUDE.md"
    n = len(p.read_text().splitlines())
    return (ADOPTED, f"{n} lines") if n <= MAX_INSTRUCTION_LINES else (NOT, f"{n} lines > {MAX_INSTRUCTION_LINES}")


@check("memory-block", "CONVENTION", "add the Abacus memory block (consult_memory) to CLAUDE.md",
       "consult memory retrieval block")
def c_memory_block(repo):
    for para in claude_md_paragraphs(repo):
        if "consult_memory" in para:
            return ADOPTED, "CLAUDE.md tells sessions to consult shared memory"
    return NOT, "CLAUDE.md never mentions consult_memory"


@check("runs-ignored", "CONVENTION", "add .abacus-runs/ and .claude/worktrees/ to .gitignore (abacus-adopt)",
       "workflow run artifacts committed")
def c_runs(repo):
    gi = repo / ".gitignore"
    lines = [l.strip().rstrip("/") for l in gi.read_text().splitlines()] if gi.exists() else []
    missing = [e for e in common.IGNORED if e.rstrip("/") not in lines]
    if not missing:
        return ADOPTED, "run artifacts and session worktrees ignored"
    return NOT, f"not in .gitignore: {', '.join(missing)} (could be committed or make the checkout look dirty)"


@check("tracked", "CONVENTION", "let abacus-adopt narrow the ignore rule, or add `!<path>` lines for Abacus's files",
       "worker worktree missing guards because .claude was gitignored")
def c_tracked(repo):
    hidden = common.ignored_paths(repo)
    if not hidden:
        return ADOPTED, "Abacus's files are tracked, so every worker worktree gets the guards"
    return NOT, ("ignored, so workers' worktrees run without them: " +
                 ", ".join(f"{p} ({r.split(':', 2)[2]})" for p, r in sorted(hidden.items())))


# ---------------------------------------------------------------- candidates

@check("mutation-guide", "CANDIDATE", "document how to run mutation testing in docs/testing/mutation.md",
       "tests passed but did not bind mutation")
def c_mutation(repo):
    return (ADOPTED, "present") if (repo / "docs" / "testing" / "mutation.md").exists() else (NOT, "absent")


# ---------------------------------------------------------------- runner

def deviations(repo):
    out = {}
    for adr in sorted((repo / "docs" / "adr").glob("*.md")) if (repo / "docs" / "adr").exists() else []:
        text = adr.read_text()
        m = re.match(r"\A---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        for tag in re.findall(r"deviates:([a-z0-9-]+)", m.group(1)):
            out[tag] = str(adr.relative_to(repo))
    return out


def doctor(repo, only=None):
    devs = deviations(repo)
    results = []
    for c in REGISTRY:
        if only and c["name"] not in only:
            continue
        try:
            verdict, detail = c["fn"](repo)
        except Exception as e:  # a check that throws is UNCHECKED, never a crashed report
            verdict, detail = UNCHECKED, f"{type(e).__name__}: {e}"
        if verdict == NOT and c["name"] in devs:
            if c["tier"] == "OBLIGATION":
                detail += f" (deviation declared in {devs[c['name']]}, but obligations cannot be declined)"
            else:
                verdict, detail = DEVIATING, f"declined on the record: {devs[c['name']]}"
        r = {"check": c["name"], "tier": c["tier"], "verdict": verdict, "detail": detail}
        if verdict != ADOPTED:
            r["remedy"] = c["remedy"]
            r["why"] = f"abacus-memory consult \"{c['why']}\""
        results.append(r)
    return results


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-doctor", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    p.add_argument("--check", action="append", help="run only this check (repeatable)")
    p.add_argument("--list", action="store_true", help="list registered checks")
    a = p.parse_args(argv)
    if a.list:
        for c in REGISTRY:
            print(f"{c['tier']:<11} {c['name']:<16} {c['remedy']}")
        return 0
    try:
        repo = common.toplevel()
    except Refused as e:
        print(f"UNCHECKED: {e}", file=sys.stderr)
        return 2
    results = doctor(repo, a.check)
    if a.json:
        print(json.dumps({"repo": str(repo), "checks": results}, indent=2))
    else:
        print(f"abacus-doctor: {repo}")
        for r in results:
            print(f"  {r['verdict']:<12} {r['tier']:<11} {r['check']:<16} {r['detail'].splitlines()[0][:110]}")
            if "remedy" in r:
                print(f"  {'':<12} {'':<11} {'':<16} -> {r['remedy']}")
        counts = {}
        for r in results:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 1 if any(r["verdict"] == BROKEN for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
