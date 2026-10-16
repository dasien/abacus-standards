"""abacus-finalize: the only door that commits, pushes and closes a bead.

It refuses unless every acceptance criterion has a recorded verdict:
  met    with non-empty evidence, or
  moved  to a named measurement bead that exists, is open, is blocked by this
         bead, and holds the criterion word for word.

Then, in this order (a closed bead whose commit is not on origin is the worst
false signal, so the close comes last):
  1. stage the changed paths by name (never `git add -A`)
  2. commit with Abacus trailers, and check the trailers on the committed object
  3. push (the pre-push hook runs the repository gate under the heavy lock)
  4. close the bead with a reason naming the commit and the evidence per criterion

With "landing": "draft-pr", step 3 pushes the bead's own branch (abacus/<slug>-<bead id>) and opens a draft PR
against the run's base; the close reason names the PR. A failed push or PR undoes the commit (and the pushed
branch), so the bead stays open and a plain retry starts clean. --record opens its own draft PR.
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from .board import run_base
from .common import (RUNS_DIR, Refused, acceptance_criteria, bead_branch, gh, load_config, norm, open_pr,
                     pipeline_label, run, show_bead, toplevel)

TRAILERS = ("Bead", "Abacus-Pipeline", "Abacus-Session")
# Criteria that name a diff or commit range. Their evidence is usually gathered before the commit exists, so the
# close reason also records what the landed commit actually contains.
RANGE_CRITERION = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:diff|log|show)\b")


def check_verdicts(bead, verdicts, repo):
    criteria = acceptance_criteria(bead)
    if not isinstance(verdicts, list):
        raise Refused("the verdict file must be a JSON array of {criterion, verdict, evidence}")
    by_crit = {}
    for v in verdicts:
        if not isinstance(v, dict) or "criterion" not in v or "verdict" not in v:
            raise Refused(f"malformed verdict entry: {v}")
        if norm(v["criterion"]) in by_crit:
            raise Refused(f"duplicate verdicts for criterion {v['criterion']!r}")
        by_crit[norm(v["criterion"])] = v
    problems = []
    for c in criteria:
        v = by_crit.get(norm(c))
        if v is None:
            problems.append(f"no verdict for criterion: {c!r}")
        elif v["verdict"] == "met":
            if not str(v.get("evidence") or "").strip():
                problems.append(f"criterion met without evidence: {c!r}")
        elif v["verdict"] == "moved":
            target = v.get("moved_to")
            if not target:
                problems.append(f"criterion moved without naming a bead: {c!r}")
                continue
            try:
                m = show_bead(target, repo)
            except Refused as e:
                problems.append(f"moved to {target}, which cannot be read: {e}")
                continue
            if m.get("status") == "closed":
                problems.append(f"moved to {target}, which is closed")
            deps = [d.get("id") for d in (m.get("dependencies") or [])
                    if d.get("dependency_type", "blocks") == "blocks"]
            if bead["id"] not in deps:
                problems.append(f"moved to {target}, which is not blocked by {bead['id']}")
            if norm(c) not in norm(m.get("acceptance_criteria")):
                problems.append(f"moved to {target}, whose acceptance does not hold the criterion word for word")
        else:
            problems.append(f"criterion {c!r} has verdict {v['verdict']!r}; only met or moved can close")
    if problems:
        raise Refused("refusing to finalize:\n  - " + "\n  - ".join(problems))
    return criteria


def changed_paths(repo):
    out = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo, check=True).stdout
    entries, paths, i = out.split("\0"), [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        if e[0] in "RC":
            i += 1  # -z puts a rename's source path in the next entry
        path = e[3:]
        if not path.startswith(RUNS_DIR + "/"):
            paths.append(path)
    return paths


def landed(repo, base, sha):
    """What the commit being pushed changes relative to the branch it lands on."""
    files = run(["git", "diff", "--name-only", f"{base}..{sha}"], cwd=repo, check=True).stdout.split("\n")
    return {"base": base, "sha": sha, "files": [f for f in files if f]}


def close_reason(criteria, verdicts, sha, pipeline, session, land):
    short = sha[:12]
    files = land["files"]
    shown = ", ".join(files[:20]) + (f" (+{len(files) - 20} more)" if len(files) > 20 else "")
    lines = [f"Landed in {short} ({pipeline}, {session}).",
             f"Files in {short} (git diff --name-only {land['base']}..{short}): {len(files)}: {shown}"]
    for c in criteria:
        v = next(x for x in verdicts if norm(x["criterion"]) == norm(c))
        line = f"- {c} => {v['verdict']}: {v.get('evidence') or 'moved to ' + v.get('moved_to', '')}"
        if v["verdict"] == "met" and RANGE_CRITERION.search(c):
            line += f" [re-read against the landed commit {short}: {len(files)} file(s): {shown}]"
        lines.append(line)
    friction = next((x.get("friction") for x in verdicts if x.get("friction")), None)
    if friction:
        lines.append(f"Friction: {friction}")
    return "\n".join(lines)


def uncommit(repo, sha, mode, left):
    """Take back the commit this run just made, so a plain retry finds the changes again. Only that commit: if HEAD
    has moved past it, it stays and the refusal says so."""
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if head == sha and run(["git", "reset", "-q", mode, "HEAD~1"], cwd=repo).returncode == 0:
        return f"commit {sha[:12]} undone; {left}, so a plain retry works."
    return f"commit {sha[:12]} could not be undone and is still local."


GITHUB_ISSUE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$", re.I)
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)


def closes_line(external_ref, origin_url):
    """`Closes #N` when the bead's source is a GitHub issue (gh-N, or an issue URL); an issue in another repository
    is named in full so merging never closes the wrong one. Anything else (Jira keys, files) gets no line."""
    ref = (external_ref or "").strip()
    m = re.fullmatch(r"gh-(\d+)", ref)
    if m:
        return f"Closes #{m.group(1)}"
    m = GITHUB_ISSUE.match(ref)
    if not m:
        return None
    here = GITHUB_REMOTE.search((origin_url or "").strip())
    if here and (here.group(1).lower(), here.group(2).lower()) == (m.group(1).lower(), m.group(2).lower()):
        return f"Closes #{m.group(3)}"
    return f"Closes {m.group(1)}/{m.group(2)}#{m.group(3)}"


def pr_body(criteria, verdicts, summary, closes=None, parent=None):
    """The PR body, from what finalize already holds. It names no bead: boards are local to each clone."""
    parts = [f"Builds on #{parent['number']}."] if parent else []
    if summary and summary.strip():
        parts.append(summary.strip())
    lines = ["## Acceptance criteria"]
    for c in criteria:
        v = next(x for x in verdicts if norm(x["criterion"]) == norm(c))
        lines.append(f"- {c}: met. {v.get('evidence')}" if v["verdict"] == "met" else f"- {c}: moved to a follow-up.")
    parts.append("\n".join(lines))
    if closes:
        parts.append(closes)
    return "\n\n".join(parts) + "\n"


def open_draft_pr(repo, base, branch, title, body):
    """gh pr create --draft; on failure delete the pushed branch, so no branch is left on the remote without a PR."""
    p = gh(["pr", "create", "--draft", "--base", base, "--head", branch, "--title", title, "--body-file", "-"],
           repo, input=body)
    url = next((x.strip() for x in reversed(p.stdout.splitlines()) if x.strip().startswith("http")), None)
    if p.returncode == 0 and url:
        return url
    # a deletion gives the gate nothing to check, so the pre-push hook is skipped; the retry runs it again
    gone = run(["git", "push", "--no-verify", "origin", "--delete", branch], cwd=repo)
    left = (f"branch {branch} deleted from origin" if gone.returncode == 0
            else f"branch {branch} could NOT be deleted from origin: {(gone.stderr or gone.stdout).strip()[:300]}")
    raise Refused(f"gh pr create failed ({left}); the bead stays OPEN:\n{(p.stderr or p.stdout).strip()[-1500:]}")


def finalize(bead_id, verdict_file, session, summary, repo, dry_run=False):
    cfg = load_config(repo)
    bead = show_bead(bead_id, repo)
    if bead.get("status") == "closed":
        raise Refused(f"{bead_id} is already closed")
    try:
        verdicts = json.loads(Path(verdict_file).read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read verdict file {verdict_file}: {e}")
    criteria = check_verdicts(bead, verdicts, repo)
    labels = pipeline_label(bead)
    pipeline = labels[0].split(":", 1)[1] if labels else "manual"
    paths = changed_paths(repo)
    if not paths:
        raise Refused("nothing to commit: a bead that changed nothing should be closed by its owner, not here")
    if dry_run:
        return {"would_commit": paths, "criteria": len(criteria)}
    draft = cfg["landing"] == "draft-pr"
    base = run_base(bead_id, repo, cfg) if draft else cfg["default_branch"]
    if draft:
        branch = bead_branch(bead)
        parent = open_pr(repo, base) if base != cfg["default_branch"] else None
        origin = run(["git", "remote", "get-url", "origin"], cwd=repo).stdout
        body = pr_body(criteria, verdicts, summary, closes_line(bead.get("external_ref"), origin), parent)
    run(["git", "add", "--", *paths], cwd=repo, check=True)
    msg = (f"{bead.get('title')} ({bead_id})\n\n{summary or 'Closes ' + bead_id}\n\n"
           f"Bead: {bead_id}\nAbacus-Pipeline: {pipeline}\nAbacus-Session: {session}\n")
    run(["git", "commit", "-q", "-F", "-"], cwd=repo, check=True, input=msg)
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    try:
        trailers = run(["git", "log", "-1", "--format=%(trailers:only,unfold)"], cwd=repo, check=True).stdout
        missing = [t for t in TRAILERS if f"{t}:" not in trailers]
        if missing:
            raise Refused(f"commit {sha[:12]} is missing trailers {missing}; not pushing")
        land = landed(repo, f"origin/{base}", sha)
        push = run(["git", "push", "origin", f"HEAD:refs/heads/{branch}" if draft else f"HEAD:{base}"], cwd=repo)
        if push.returncode != 0:
            raise Refused(f"push failed; the bead stays OPEN. "
                          f"Report BLOCKED with this output:\n{(push.stderr or push.stdout).strip()[-1500:]}")
        if draft:
            url = open_draft_pr(repo, base, branch, bead.get("title"), body)
    except Refused as e:  # a commit left behind would make the retry refuse with "nothing to commit"
        raise Refused(f"{uncommit(repo, sha, '--soft', 'its changes are staged again')}\n{e}")
    reason = close_reason(criteria, verdicts, sha, pipeline, session, land)
    if draft:
        reason += f"\nDraft PR: {url} ({branch} -> {base})"
    run(["bd", "close", bead_id, "--reason-file", "-"], cwd=repo, check=True, input=reason,
        env={"BEADS_ACTOR": session})
    if draft:
        return {"closed": bead_id, "commit": sha, "pushed": True, "branch": branch, "base": base, "pr": url}
    return {"closed": bead_id, "commit": sha, "pushed": True, "branch": cfg["default_branch"]}


def record_only(bead_id, paths, session, repo):
    """Commit and push only validated records (e.g. a retro after an escalation). Never closes a bead."""
    cfg = load_config(repo)
    for pth in paths:
        if not pth.startswith(("docs/retros/", "docs/adr/", "docs/decisions/", "docs/postmortems/")):
            raise Refused(f"--record accepts record files only (docs/retros, adr, decisions, postmortems): {pth}")
    tool = Path(__file__).resolve().parents[2] / "bin" / "abacus-validate-records"
    v = run([str(tool), *paths], cwd=repo)
    if v.returncode != 0:
        raise Refused(f"records fail validation:\n{(v.stderr + v.stdout).strip()}")
    msg = f"Record for {bead_id}\n\nBead: {bead_id}\nAbacus-Pipeline: record\nAbacus-Session: {session}\n"
    if cfg["landing"] == "draft-pr":
        return record_pr(bead_id, paths, msg, repo, cfg)
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=repo).stdout.split()
    if staged:
        raise Refused(f"other changes are already staged ({staged}); refusing to mix them into a record commit")
    run(["git", "add", "--", *paths], cwd=repo, check=True)
    run(["git", "commit", "-q", "-F", "-"], cwd=repo, check=True, input=msg)
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    push = run(["git", "push", "origin", f"HEAD:{cfg['default_branch']}"], cwd=repo)
    if push.returncode != 0:
        # unstaged too (--mixed): nothing else was staged, and a staged record would trip that check on the retry
        undone = uncommit(repo, sha, "--mixed", "the records are back in the working tree, unstaged")
        raise Refused(f"{undone}\nrecord push failed:\n{(push.stderr or push.stdout)[-1000:]}")
    return {"recorded": paths, "commit": sha, "pushed": True, "bead_closed": False}


def record_pr(bead_id, paths, msg, repo, cfg):
    """Draft-pr landing: the records alone, committed onto origin/<default branch> (even when the run was stacked)
    and opened as their own draft PR. The commit is built in a temporary index, so the run's index and HEAD are left
    as they were and a refusal has nothing local to undo. Once the PR is open the record files leave the working tree."""
    bead = show_bead(bead_id, repo)
    default = cfg["default_branch"]
    branch = bead_branch(bead) + "-record"
    run(["git", "fetch", "-q", "origin", default], cwd=repo, check=True)
    parent = run(["git", "rev-parse", f"origin/{default}"], cwd=repo, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run(["git", "read-tree", parent], cwd=repo, check=True, env=index)
        run(["git", "add", "--", *paths], cwd=repo, check=True, env=index)
        tree = run(["git", "write-tree"], cwd=repo, check=True, env=index).stdout.strip()
    sha = run(["git", "commit-tree", tree, "-p", parent, "-F", "-"], cwd=repo, check=True, input=msg).stdout.strip()
    push = run(["git", "push", "origin", f"{sha}:refs/heads/{branch}"], cwd=repo)
    if push.returncode != 0:
        raise Refused(f"record push failed; nothing was committed here, so a plain retry works:\n"
                      f"{(push.stderr or push.stdout)[-1000:]}")
    body = "Records from a run that did not land:\n\n" + "\n".join(f"- {pth}" for pth in paths) + "\n"
    url = open_draft_pr(repo, default, branch, f"Record: {bead.get('title')}", body)
    for pth in paths:  # they live on the record branch now; left here, a resumed run would refuse the dirty tree
        (Path(repo) / pth).unlink()
    return {"recorded": paths, "commit": sha, "pushed": True, "bead_closed": False, "branch": branch, "pr": url}


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-finalize", description=__doc__.split("\n")[0])
    p.add_argument("--bead", required=True)
    p.add_argument("--verdicts", help="per-criterion verdict file (JSON array)")
    p.add_argument("--record", action="append", default=[],
                   help="commit only this validated record file; never closes the bead (repeatable)")
    p.add_argument("--session", required=True, help="<role>:<repo> of the session finalizing")
    p.add_argument("--summary", help="one paragraph for the commit body")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.record:
            out = record_only(a.bead, a.record, a.session, toplevel())
        elif not a.verdicts:
            raise Refused("--verdicts is required to close a bead (or --record to commit a record only)")
        else:
            out = finalize(a.bead, a.verdicts, a.session, a.summary, toplevel(), a.dry_run)
    except Refused as e:
        print(json.dumps({"finalized": False, "refused": str(e)}, indent=2))
        return 3
    print(json.dumps(dict(out, finalized=not (a.dry_run or a.record)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
