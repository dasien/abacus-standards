---
name: bead-file
description: File agent-executable work into THIS repository's Beads board as a story, bug, epic, or decompose an epic into vertical-slice children. Use only when the operator explicitly asks to file, scope, write up, or break down work ("file a story", "bead this bug", "decompose epic X") — never because you judged something worth filing.
---

# File work into this repository's board

Modes: `story`, `bug`, `epic`, `decompose <epic-id>`. Run only when asked.

## Filing rules (every mode)
1. **Verify the board first.** Run `bd where --json` and `git rev-parse --show-toplevel`. Proceed only if the board
   path sits inside the repo root. If either errors, prints nothing, or they disagree: stop and report what each printed.
2. **Stamp it.** The first line of every description is `FILED AGAINST: <git rev-parse --short HEAD>`
   (`unknown` if git fails, and say so). Later runs use it to detect a stale bead.
3. **Acceptance criteria go in the acceptance field**, one per `- ` bullet, each naming its observable: a command,
   a path, a count or an artifact. Write them to a file, pass `--acceptance "$(cat file)"`, then read the stored field
   back with `bd show <id> --json` and compare. Fix any mismatch before anything else.
4. A criterion that can only be read after the change runs live (a measure over later days) is **its own bead**,
   created blocked by this one: `bd dep add <measure-bead> <this-bead>`. The parent closes on its other criteria.
5. A dependency edge is only for a true prerequisite (cannot start or succeed without it). "Should go after" is a note.
   **Criteria check this repository only.** The worker runs in its own worktree, where sibling paths such as
   `../other-repo` don't resolve and reads outside the repository may be refused. Never write a criterion that reads
   another repository (`git -C ../other ...`, a sibling file). Put the cross-repository condition in the description
   instead, as `WAITS-ON <repo>:<bead> (<what must be true there>)`, plus the edge
   (`bd dep add <this> external:<repo>:<capability>`). The orchestrator checks it before dispatch and records it in
   the dispatch's preconditions.
6. Create with `--validate` if the board enforces sections; never `--force`.
7. **Do not set the `pipeline:` label** and do not pick a model. The board's owner sizes the work.
8. Finish with a report naming every id and edge you filed.

## Mode: story
Score against INVEST (Independent, Negotiable, Valuable, Estimable, Small, Testable). Fix failures before filing and
include the scorecard in your report. `bd create "<title>" -t feature -d "$(cat desc.md)" --acceptance "$(cat ac.md)" --silent`.

## Mode: bug
Check against RIMGEN: reproducible, isolated, minimal, generalizable, expected-vs-actual, necessary context. One
action per reproduction step. `-t bug`.

## Mode: epic
Description carries: problem statement, desired outcome, success criteria, in scope, out of scope, non-goals,
dependencies, risks. `-t epic`. Epics never get a pipeline label.

## Mode: decompose <epic-id>
Read the epic first (`bd show <epic> --json`). Create children as **vertical slices**: each a thin, independently
checkable, user-visible change. Refuse horizontal layers by name ("the DB layer", "the API layer"). Size each for one
agent session: soft limit 500 changed lines, hard limit 1000. Wire with `--parent <epic>` and real dependency edges.
Never print the plan as markdown to paste; file it.

## Bitwarden note
A bead is the agent-executable plan. The Jira issue remains the product commitment. Put the Jira key in
`--external-ref` when there is one.
