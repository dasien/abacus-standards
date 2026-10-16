"""abacus-adopt: roll the Abacus conventions into a repository (operator-run).

Agents never edit the controls that govern them, so adoption is an operator
act: run it in your own shell, review the diff, run abacus-doctor, commit.

It vendors canonical files byte-for-byte (so the doctor can verify them):
  .claude/hooks/abacus/*.py                    guards + secret redaction, wired in .claude/settings.json
  .claude/workflows/abacus-*.js                the one-shot / lite / full Dynamic Workflows
  .claude/skills/<name>/SKILL.md               scoping, worker-contract, retro, judgment skills
  .githooks/{pre-commit,pre-push}              commit and push gates (core.hooksPath=.githooks)
and writes, only when absent: .abacus/config.json, the CLAUDE.md Abacus block,
the abacus-memory entry in .mcp.json, and .abacus-runs/ in .gitignore.
"""
import argparse
import json
import shutil
import sys

from . import common
from .common import CANONICAL, CONFIG_DEFAULTS, Refused, run

BEGIN, END = "<!-- abacus:begin", "<!-- abacus:end -->"


def standards_version():
    pub = common.STANDARDS_ROOT / ".published"
    if pub.exists():
        return pub.read_text().strip()
    p = run(["git", "-C", str(common.STANDARDS_ROOT), "rev-parse", "--short", "HEAD"])
    return p.stdout.strip() or "unknown"


def copy(src, dst, changed, repo):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return
    shutil.copy2(src, dst)
    changed.append(str(dst.relative_to(repo)))


def merge_settings(repo, changed):
    path = repo / ".claude" / "settings.json"
    cur = json.loads(path.read_text()) if path.exists() else {}
    tmpl = json.loads((CANONICAL / "templates" / "settings.hooks.json").read_text())
    hooks = cur.setdefault("hooks", {})
    for event, entries in tmpl["hooks"].items():
        kept = [e for e in hooks.get(event, []) if not any(
                m in json.dumps(e) for m in ("abacus/guards.py", "abacus/redact.py", "bd prime"))]
        hooks[event] = kept + entries
    enabled = cur.setdefault("enabledMcpjsonServers", [])
    for name in tmpl.get("enabledMcpjsonServers", []):
        if name not in enabled:
            enabled.append(name)
    deny = cur.setdefault("permissions", {}).setdefault("deny", [])
    # Claude Code matches path deny rules only for Edit(...) (it covers every file-editing tool); drop the
    # Write(...) rules earlier Abacus versions installed.
    deny[:] = [r for r in deny if r not in ("Write(.claude/**)", "Write(.abacus/**)", "Write(.githooks/**)")]
    for rule in tmpl["permissions"]["deny"]:
        if rule not in deny:
            deny.append(rule)
    new = json.dumps(cur, indent=2) + "\n"
    if not path.exists() or path.read_text() != new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
        changed.append(".claude/settings.json")


def open_gitignore(repo, changed):
    """Workers run in git worktrees, which hold only tracked files: Abacus's own files must not be ignored.
    A rule ignoring all of .claude/ is narrowed (local settings stay ignored); anything else is refused, unwritten."""
    hidden = common.ignored_paths(repo)
    if not hidden:
        return
    gi = repo / ".gitignore"
    lines = gi.read_text().split("\n") if gi.exists() else []
    if any(r.startswith(".gitignore:") and r.split(":", 2)[2] in common.CLAUDE_DIR_RULES for r in hidden.values()):
        out = []
        for l in lines:
            out += common.CLAUDE_OPENED if l.strip() in common.CLAUDE_DIR_RULES else [l]
        gi.write_text("\n".join(out))
        changed.append(".gitignore (narrowed the .claude rule so Abacus's files are tracked)")
        hidden = common.ignored_paths(repo)
    if hidden:
        detail = "\n".join(f"  {path}  (hidden by {rule})" for path, rule in sorted(hidden.items()))
        raise Refused("these files would be ignored, so workers' worktrees wouldn't have them:\n" + detail +
                      "\nAdd matching `!<path>` lines to the ignore rules (or remove the rule),"
                      " then run abacus-adopt again."
                      " Nothing was written.")


def adopt(repo, project, gate, fast_check, branch):
    changed = []
    open_gitignore(repo, changed)
    for f in common.HOOK_FILES:
        copy(CANONICAL / "hooks" / f, repo / ".claude" / "hooks" / "abacus" / f, changed, repo)
    for wf in sorted((CANONICAL / "workflows").glob("abacus-*.js")):
        copy(wf, repo / ".claude" / "workflows" / wf.name, changed, repo)
    for skill in sorted((CANONICAL / "skills").glob("*/SKILL.md")):
        copy(skill, repo / ".claude" / "skills" / skill.parent.name / "SKILL.md", changed, repo)
    for h in ("pre-commit", "pre-push"):
        copy(CANONICAL / "githooks" / h, repo / ".githooks" / h, changed, repo)
        (repo / ".githooks" / h).chmod(0o755)
    run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)
    merge_settings(repo, changed)

    cfg_path = repo / ".abacus" / "config.json"
    if not cfg_path.exists():
        cfg = dict(CONFIG_DEFAULTS, project=project, gate=gate, fast_check=fast_check, default_branch=branch)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        changed.append(".abacus/config.json")
    (repo / ".abacus" / "adopted.json").write_text(
        json.dumps({"standards": standards_version()}, indent=2) + "\n")
    changed.append(".abacus/adopted.json")

    md = repo / "CLAUDE.md"
    block = (CANONICAL / "templates" / "CLAUDE.abacus.md").read_text()
    text = md.read_text() if md.exists() else f"# {project}\n"
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        new = head + block.rstrip("\n") + rest.split(END, 1)[1]
    else:
        new = text.rstrip("\n") + "\n\n" + block
    if new != text:
        md.write_text(new)
        changed.append("CLAUDE.md")

    mcp_path = repo / ".mcp.json"
    mcp = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = mcp.setdefault("mcpServers", {})
    want = {"type": "stdio", "command": "${HOME}/.abacus/bin/abacus-memory", "args": ["serve-mcp"]}
    if servers.get("abacus-memory") != want:  # ${HOME} expands in .mcp.json; a bare name depends on PATH
        servers["abacus-memory"] = want
        mcp_path.write_text(json.dumps(mcp, indent=2) + "\n")
        changed.append(".mcp.json")

    gi = repo / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    missing = [e for e in common.IGNORED if not any(l.strip().rstrip("/") == e.rstrip("/") for l in lines)]
    if missing:
        gi.write_text("\n".join(lines + missing) + "\n")
        changed.append(".gitignore")
    for d in ("docs/retros", "docs/adr"):
        (repo / d).mkdir(parents=True, exist_ok=True)
    return changed


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-adopt", description=__doc__.split("\n")[0])
    p.add_argument("--repo", default=".")
    p.add_argument("--project", help="lowercase project id (default: directory name)")
    p.add_argument("--gate", default="make gate", help="full repository gate command")
    p.add_argument("--fast-check", default="make check", help="fast commit-time check command")
    p.add_argument("--branch", default="main")
    a = p.parse_args(argv)
    try:
        repo = common.toplevel(a.repo)
        project = (a.project or repo.name).lower()
        changed = adopt(repo, project, a.gate, a.fast_check, a.branch)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    print(f"abacus-adopt: {repo} (standards {standards_version()})")
    for c in changed:
        print(f"  wrote {c}" if not c.startswith(".gitignore (") else f"  changed {c}")
    print("next: review `git diff`, run `abacus-doctor`, then commit the adoption yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
