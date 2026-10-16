---
name: retro
description: Write a blameless, structured retrospective record into docs/retros/ that the validator accepts and shared memory can harvest. Use when the operator asks for a retro, or when a Dynamic Workflow emit trigger fires (intractable, fix-loop-cap, gate-red-on-main, escalation, manual) — never because something merely feels worth reviewing.
---

# Retrospective record

1. **Id:** `abacus-record-id` (never typed by hand; retros and decisions share one daily sequence).
2. **File:** `docs/retros/<id>.md` with exactly these ten header keys and nothing else:
```
---
id: <from abacus-record-id>
project: <lowercase [a-z0-9-], from .abacus/config.json>
issue: <bead id or null>
date: YYYY-MM-DD
doc_type: retro
trigger: intractable | fix-loop-cap | gate-red-on-main | escalation | manual | incident | release | audit | mined | unknown
surface: workflow | build | tests | deps | infra | design | process | data | retrieval | security | cost | docs | unknown
severity: sev1 | sev2 | sev3 | sev4 | unknown
resolved: true | false | partial | unknown
tags: [lowercase, words]
---
```
   Every enum is closed. Never write `era`, `source_path`, `source_format` or `inferred` (those are harvest facts).
3. **Body (blameless).** State the Prime Directive: *everyone did the best they could given what they knew*. Frame
   everything as systems and process, never a person. Sections (five phases: set the stage, gather data, generate
   insights, decide what to do, close):
   - `## Scope Reviewed`
   - `## Key Observations`: Continue / Stop / Start per theme
   - `## Insights`: "the practice caused X because Y"
   - `## SMART Action Items`: **at most three** (a cap, not a floor), each with an owner and a measurable check
   - `## Monitored, not action items`: low-value insights; they become a bead only if a later retro records them again
   - `## Appreciation and Wins`
   - `## Summary`
   When an action is monitored rather than done, `resolved` is `partial` or `false`, never forced to `true`.
4. **Validate:** `abacus-validate-records docs/retros/<id>.md` must pass. Fix and re-run until it does.
5. **Track the actions.** File each SMART action as a bead on this repository's board (`bd create "<action>" -t task
   -l retro-action -d "From retro <id>. ..."`). If an open `retro-action` bead already covers it, add a note instead.
   Don't set a pipeline label; the owner sizes it.
6. Commit it with the change through `abacus-finalize`, or on its own with
   `abacus-finalize --bead <id> --record docs/retros/<id>.md --session <name>`.
