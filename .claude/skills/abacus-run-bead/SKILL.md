---
name: abacus-run-bead
description: Run one Beads work item through its assigned Abacus pipeline (one-shot, lite or full Dynamic Workflow, or the manual/operator path). Use when asked to "run", "work", "implement" or "pick up" a bead id, or when an Abacus worker receives a DISPATCH.
---

# Run a bead through its pipeline

Input: a bead id (and, for orchestrated workers, the dispatch text that triggered the run).

0. **Isolate (background sessions).** Claude Code requires background sessions to work in their own worktree, and
   it enforces this on file edits. If your cwd is not already under `.claude/worktrees/`, call **EnterWorktree** with
   `name: "<bead id>"` before anything else, and run every later step from that worktree's root. Interactive
   sessions the operator is driving work in place. If EnterWorktree fails, continue in place and say so.
1. **Brief (read-only):** `abacus-board brief <bead>`. If it refuses, stop and report the refusal verbatim.
   The pipeline comes from the bead's `pipeline:` label, set by the board's owner. Never choose or change it.
2. **Session identity:** your session name (`coder:<repo>` for orchestrated workers; otherwise
   `human:<repo>` for a session the operator is driving).
3. **Dispatch by label:**
   - `pipeline:one-shot` | `pipeline:lite` | `pipeline:full`: call the **Workflow** tool with
     `name: "abacus-<pipeline>"` and `args: {"bead": "<id>", "session": "<your name>", "relayed": "<dispatch text, if any>"}`.
     This skill's instruction to run the workflow is the operator's standing opt-in for this repository.
   - `pipeline:manual`: follow the **manual session path** below.
   - `pipeline:operator`: follow the **operator path** below.
4. **Act on the workflow result** (`outcome` field):
   - `closed`: done. `commit` is on origin and the bead is closed (with `"landing": "draft-pr"` in
     `.abacus/config.json`, on the bead's own branch `abacus/<slug>-<bead>`, with a draft PR open). If you isolated,
     confirm nothing is left to save (`git status --porcelain` is empty and `git rev-list --count origin/<branch>..HEAD`
     prints 0 after `git fetch -q origin`, where `<branch>` is the default branch, or the bead's branch in draft-pr
     mode), then call **ExitWorktree** with `action: "remove"` and `discard_changes: true`. The
     discard is safe once those two checks pass, and Claude Code requires it when the main checkout's local branch
     is behind origin. If either check fails, keep the worktree and report it.
     Then bring the main checkout up to date, so anyone running the code there sees the change:
     `git -C <main checkout> pull --ff-only -q`. If it refuses (local changes or a diverged branch), leave the
     checkout alone and mention it in your report; never force it.
   - `escalated` / `needs-ruling`: nothing was committed; the run's diff is saved in `.abacus-runs/<bead>/escalated-<n>.patch`.
     If `retro` names a file, commit it alone: `abacus-finalize --bead <id> --record <retro path> --session <name>`.
     If you isolated, call **ExitWorktree** with `action: "keep"` and put the worktree path in your report, so the
     patch and any partial work survive for whoever picks the bead up.
   - `refused` / `stopped` / `blocked`: report `reason` verbatim. Do not retry by working around a guard or probe.
     Keep the worktree as above.
   - Act on `further_clauses` only if they are within the bead's authority; otherwise surface them.
5. **Orchestrated?** Send the REPORT defined in `/abacus-worker`:
   `CLOSED` for closed, `NEEDS-RULING` for needs-ruling (include the ruling line), `BLOCKED` otherwise.

## Manual session path (no Dynamic Workflow)
Consult memory (`consult_memory`, no project filter) → hold the bead's scope block → implement → run a fresh-agent
adversarial review before commit (`/adversarial-review`) → write a retro on any of the five triggers (`/retro`) →
run the full gate green → write `.abacus-runs/<bead>/verdicts.json` (one `{criterion, verdict, evidence}` per
criterion) → `abacus-finalize --bead <id> --verdicts .abacus-runs/<bead>/verdicts.json --session <name>`.

## Operator path (edits to the agent's own settings, hooks, workflows, gates)
You never edit `.claude/`, `.abacus/` or `.githooks/`. Describe the exact change and ask the operator, in this
session, to apply it in their shell. Then verify it, run the gate, and finalize with a summary noting
"operator-applied".
