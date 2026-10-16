---
name: bead-close
description: Compose a close reason for a bead whose work landed outside a Dynamic Workflow, or prepare the per-criterion verdict file for abacus-finalize. Use when asked to close, wrap up, or finish a bead.
---

# Close a bead on evidence

1. Read the bead (`bd show <id> --json`) and the board's recent closes (`bd list --status closed --sort closed -n 5 --json`)
   to match the house style.
2. For each acceptance criterion, collect evidence you can show: the command you ran and what it printed, a
   `path:line`, a count, or an artifact. Re-run checks rather than trusting memory.
3. If the change still needs committing: write `.abacus-runs/<id>/verdicts.json` as a JSON array of
   `{"criterion": "<exact text>", "verdict": "met", "evidence": "..."}` (or `"moved"` with `"moved_to": "<measure bead>"`),
   plus an optional `"friction": "<one line>"` on any entry, then run
   `abacus-finalize --bead <id> --verdicts .abacus-runs/<id>/verdicts.json --session <name>`. It commits, pushes and closes.
4. If the work already landed (commit on origin) and only the close is missing, compose the reason (what landed with
   the commit sha, how each criterion was met, one friction line) and run `bd close <id> --reason-file -`.
5. Never close a bead whose criteria lack evidence. Never commit or push from this skill directly.
