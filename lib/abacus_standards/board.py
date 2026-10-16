"""abacus-board: the board stage and read-only probe for Dynamic Workflows.

  check <bead> --session <name>   verify the board, load config, read the bead,
                                  pick the base (draft-pr landing), sync to it,
                                  claim it, and print the run brief (JSON)
  snapshot <bead>                 print {head, remote, bead_hash}: the probe
                                  re-runs this after every stage; any change
                                  ends the run (remote is the run's base tip)
  brief <bead>                    the run brief without claiming (read-only)
  revert <bead>                   clean escalation: save this run's diff to
                                  .abacus-runs/<bead>/escalated-<n>.patch, then
                                  restore the tree to the run's starting HEAD
  find <words|url|key> [--all]    read-only search of every fleet board by id, title, source link
                                  (external-ref) and WAITS-ON lines; open beads only unless --all
"""
import argparse
import hashlib
import json
import re
import os
import subprocess
import sys
from pathlib import Path

from .common import (RUNS_DIR, Refused, abacus_branches, acceptance_criteria, bd_json, board_path, branches_for,
                     filed_against, load_config, open_pr, pipeline_label, run, show_bead, toplevel)


def brief(bead_id, repo):
    cfg = load_config(repo)
    board = board_path(repo)
    bead = show_bead(bead_id, repo)
    if bead.get("issue_type") == "epic":
        raise Refused(f"{bead_id} is an epic; workflows run one task, bug or feature at a time")
    if "draft" in (bead.get("labels") or []):
        raise Refused(f"{bead_id} is still a draft: the operator hasn't approved it, so it can't run")
    labels = pipeline_label(bead)
    if len(labels) != 1:
        raise Refused(f"{bead_id} must carry exactly one pipeline label (has {labels or 'none'}); "
                      f"the board's owner sets it")
    criteria = acceptance_criteria(bead)
    if not criteria:
        raise Refused(f"{bead_id} has no acceptance criteria in the acceptance field; it cannot be closed "
                      f"on evidence, so it cannot run")
    sha = filed_against(bead)
    stale = None
    if sha and sha != "unknown":
        p = run(["git", "log", "--oneline", f"{sha}..HEAD"], cwd=repo)
        stale = p.stdout.strip().splitlines() if p.returncode == 0 else [f"cannot resolve {sha}"]
    return {
        "ok": True, "repo": str(repo), "project": cfg["project"], "board": str(board), "bead": bead_id,
        "title": bead.get("title"), "status": bead.get("status"), "pipeline": labels[0].split(":", 1)[1],
        "acceptance": criteria, "description": bead.get("description"), "design": bead.get("design"),
        "notes": bead.get("notes"), "filed_against": sha,
        "stale_commits": stale, "stale_note": "staleness is a diagnostic, never a blocker",
        "config": cfg, "runs_dir": f"{RUNS_DIR}/{bead_id}",
        "verdicts_file": f"{RUNS_DIR}/{bead_id}/verdicts.json",
    }


def choose_base(bead, cfg, repo):
    """Draft-pr landing: the branch this bead's PR builds on. A dependency with an open PR is not on the default
    branch yet, so the bead stacks on it; with two or more there is no single parent, so the bead waits."""
    deps = [d.get("id") for d in (bead.get("dependencies") or [])
            if d.get("id") and d.get("dependency_type", "blocks") == "blocks"]
    if not deps:
        return cfg["default_branch"]
    names = abacus_branches(repo)
    found = []
    for dep in deps:
        for branch in branches_for(names, dep):
            pr = open_pr(repo, branch)
            if pr:
                found.append((branch, pr))
    if len(found) > 1:
        raise Refused(f"{bead['id']} depends on {len(found)} open PRs ("
                      + ", ".join(f"{b}: {pr.get('url')}" for b, pr in found)
                      + "); a PR has one base, so it can run once all but one have merged")
    return found[0][0] if found else cfg["default_branch"]


def sync_with_remote(repo, branch, published=None):
    """Fast-forward a clean checkout to origin/<branch> so the run starts from the published tip.

    A worktree branches from the main checkout's local branch, which goes stale once earlier beads push
    from their own worktrees; without this, finalize's push would be rejected as non-fast-forward.
    With published (the default branch, when the run stacks on another bead's branch), a HEAD that holds nothing
    beyond origin/<published> moves to the base even when the two diverged: the default branch moved on after the
    parent branch forked, and nothing local would be lost.
    """
    if run(["git", "fetch", "-q", "origin", branch], cwd=repo).returncode != 0:
        return "fetch failed; continuing from local HEAD"
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    tip = run(["git", "rev-parse", f"origin/{branch}"], cwd=repo).stdout.strip()
    if head == tip:
        return "up to date"
    if run(["git", "merge-base", "--is-ancestor", head, tip], cwd=repo).returncode == 0:
        run(["git", "merge", "-q", "--ff-only", tip], cwd=repo, check=True)
        return f"fast-forwarded {head[:8]} -> {tip[:8]}"
    if run(["git", "merge-base", "--is-ancestor", tip, head], cwd=repo).returncode == 0:
        return f"ahead of origin/{branch}; finalize will push"
    if published and run(["git", "fetch", "-q", "origin", published], cwd=repo).returncode == 0 and \
            run(["git", "merge-base", "--is-ancestor", head, f"origin/{published}"], cwd=repo).returncode == 0:
        run(["git", "reset", "-q", "--keep", tip], cwd=repo, check=True)
        return f"moved {head[:8]} -> {tip[:8]} (origin/{branch}); HEAD held nothing beyond origin/{published}"
    raise Refused(f"local HEAD {head[:8]} has diverged from origin/{branch} {tip[:8]}; refusing to start a run "
                  f"that could not be pushed (report BLOCKED)")


def run_base(bead_id, repo, cfg):
    """The base abacus-board check recorded for this run, else the default branch."""
    try:
        return json.loads((Path(repo) / RUNS_DIR / bead_id / "start.json").read_text()).get("base") \
            or cfg["default_branch"]
    except (OSError, ValueError, AttributeError):
        return cfg["default_branch"]


def snapshot(bead_id, repo, base=None):
    cfg = load_config(repo)
    base = base or run_base(bead_id, repo, cfg)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    remote = run(["git", "ls-remote", "origin", f"refs/heads/{base}"], cwd=repo).stdout.split()
    bead = show_bead(bead_id, repo)
    stable = {k: bead.get(k) for k in ("title", "description", "acceptance_criteria", "labels", "status",
                                       "issue_type", "design")}
    h = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]
    return {"head": head, "remote": remote[0] if remote else None, "bead_hash": h}


def zlist(cmd, repo):
    """NUL-separated git output -> list of paths (safe for spaces and non-ASCII names)."""
    return [x for x in run(cmd, cwd=repo, check=True).stdout.split("\0") if x]


def dirty(repo):
    entries = zlist(["git", "status", "--porcelain", "-z", "--untracked-files=all"], repo)
    return [e for e in entries if len(e) > 3 and not e[3:].startswith(RUNS_DIR + "/")]


def revert(bead_id, repo):
    """Undo only this run: the board stage guaranteed a clean tree at the start."""
    start = repo / RUNS_DIR / bead_id / "start.json"
    if not start.exists():
        raise Refused(f"no start record for {bead_id}; refusing to revert work this run did not create")
    head = json.loads(start.read_text())["head"]
    if run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip() != head:
        raise Refused("HEAD moved since the run started; refusing to revert (report this instead)")
    n = 1
    while (repo / RUNS_DIR / bead_id / f"escalated-{n}.patch").exists():
        n += 1
    patch = repo / RUNS_DIR / bead_id / f"escalated-{n}.patch"  # never overwrite a saved patch
    untracked = [p for p in zlist(["git", "ls-files", "-z", "--others", "--exclude-standard"], repo)
                 if not p.startswith(RUNS_DIR + "/")]
    if untracked:
        run(["git", "add", "-N", "--", *untracked], cwd=repo, check=True)  # so new files appear in the diff
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True).stdout
    tracked = zlist(["git", "diff", "-z", "--name-only", "HEAD"], repo)
    if untracked:
        run(["git", "reset", "-q", "--", *untracked], cwd=repo, check=True)
    patch.write_bytes(diff)
    if diff and patch.stat().st_size != len(diff):
        raise Refused(f"could not save the run's diff to {patch}; nothing reverted")
    existing = [p for p in tracked if p not in untracked]
    if existing:
        run(["git", "checkout", "HEAD", "--", *existing], cwd=repo, check=True)
    for p in untracked:
        (repo / p).unlink()
    start.rename(start.with_suffix(".reverted"))  # a second revert has nothing of this run's left to undo
    return {"reverted": bead_id, "patch": str(patch.relative_to(repo)), "files": sorted(set(tracked + untracked))}


WAITS_ON = re.compile(r"^WAITS-ON\s+([\w.-]+):([\w.-]+)", re.M)


def fleet_boards():
    """{repo name: path} for every fleet worker repository that has a Beads board."""
    home = Path(os.environ.get("ABACUS_HOME", Path.home() / ".abacus")).expanduser()
    try:
        workers = json.loads((home / "agents" / "fleet.json").read_text()).get("workers", {})
    except (OSError, ValueError):
        return {}
    boards = {}
    for name, w in workers.items():
        repo = Path(os.path.expanduser(str(w.get("repo", ""))))
        if name.startswith("coder:") and (repo / ".beads").is_dir():
            boards[name.split(":", 1)[1]] = repo
    return boards


def bead_summary(repo_name, repo, b):
    labels = b.get("labels") or []
    pipeline = [l for l in labels if l.startswith("pipeline:")]
    desc = b.get("description") or ""
    return {"repo": repo_name, "path": str(repo), "id": b.get("id"), "title": b.get("title"),
            "status": b.get("status"), "labels": labels, "source": b.get("external_ref"),
            "waits_on": [f"{r}:{i}" for r, i in WAITS_ON.findall(desc)],
            "draft": "draft" in labels,
            # ready = filed, sized by its owner, open and not started: what an operator means by "staged"
            "ready": b.get("status") == "open" and len(pipeline) == 1 and "draft" not in labels}


def find(query, include_closed=False):
    """Every bead on any fleet board matching the query (case-insensitive) in its id, title, source or WAITS-ON."""
    q = query.strip().lower()
    hits = []
    for name, repo in sorted(fleet_boards().items()):
        try:
            beads = bd_json(["list", "--all"] if include_closed else ["list"], repo) or []
        except Refused:
            continue
        for b in beads:
            s = bead_summary(name, repo, b)
            source = (s["source"] or "").lower().rstrip("/")
            number = q.lstrip("#")
            if "/" in q:      # a link names exactly one source (issues/1 must not match issues/11)
                hit = source == q.rstrip("/") or (source and source.endswith("/" + q.rstrip("/")))
            elif q.startswith("#") and number.isdigit():
                hit = bool(re.search(rf"[/#]{number}$", source))
            else:
                hit = q in " ".join(filter(None, [s["id"], s["title"], " ".join(s["waits_on"])])).lower()
            if hit:
                hits.append(s)
    # pull in what the hits wait on, so a caller sees the whole chain
    ids = {h["id"] for h in hits}
    for h in list(hits):
        for dep in h["waits_on"]:
            r, bead = dep.split(":", 1)
            if bead not in ids and r in fleet_boards():
                try:
                    hits.append(dict(bead_summary(r, fleet_boards()[r], show_bead(bead, fleet_boards()[r])),
                                     prerequisite_of=h["id"]))
                    ids.add(bead)
                except Refused:
                    pass
    return hits


def check(bead_id, session, repo):
    if not session:
        raise Refused("no session identity supplied (--session <role>:<repo>); refusing before any claim")
    out = brief(bead_id, repo)
    if out["status"] == "closed":
        raise Refused(f"{bead_id} is already closed")
    if dirty(repo):
        raise Refused("the working tree has uncommitted changes; a run must start clean so its own "
                      "changes can be told apart:\n" + "\n".join(dirty(repo)[:20]))
    cfg = out["config"]
    base = cfg["default_branch"]
    if cfg["landing"] == "draft-pr":
        base = out["base"] = choose_base(show_bead(bead_id, repo), cfg, repo)
    out["synced"] = sync_with_remote(repo, base, cfg["default_branch"] if base != cfg["default_branch"] else None)
    owner = (show_bead(bead_id, repo).get("assignee") or "").strip()
    if out["status"] == "in_progress" and owner and owner != session:
        raise Refused(f"{bead_id} is already claimed by {owner}; one session per bead")
    if out["status"] != "in_progress":
        run(["bd", "update", bead_id, "--claim"], cwd=repo, check=True, env={"BEADS_ACTOR": session})
        out["status"] = "in_progress"
    (repo / out["runs_dir"]).mkdir(parents=True, exist_ok=True)
    out["snapshot"] = snapshot(bead_id, repo, base)
    start = dict(out["snapshot"], base=base) if cfg["landing"] == "draft-pr" else out["snapshot"]
    (repo / out["runs_dir"] / "start.json").write_text(json.dumps(start))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-board", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("bead"); c.add_argument("--session")
    s = sub.add_parser("snapshot"); s.add_argument("bead")
    b = sub.add_parser("brief"); b.add_argument("bead")
    r = sub.add_parser("revert"); r.add_argument("bead")
    f = sub.add_parser("find"); f.add_argument("query"); f.add_argument("--all", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "find":  # searches every fleet board, so it needs no repository of its own
        print(json.dumps(find(a.query, a.all), indent=2))
        return 0
    try:
        repo = toplevel()
        if a.cmd == "check":
            out = check(a.bead, a.session, repo)
        elif a.cmd == "brief":
            out = brief(a.bead, repo)
        elif a.cmd == "revert":
            out = revert(a.bead, repo)
        else:
            out = snapshot(a.bead, repo)
    except Refused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 3
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
