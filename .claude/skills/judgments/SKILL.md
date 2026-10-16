---
name: judgments
description: Structure a decision for the operator and record the ruling as an append-only JUDGEMENT/STANDING row on the bead, or review due rulings and record OUTCOME rows. Use whenever a decision needs the operator, when the operator gives an instruction or correction, or when asked "should this go through the judgment skill?" / "review due rulings".
---

# Judgments: capture, record, review

Each judgment is a testable prediction: what the machine recommended, what the human decided, and when to check.

## 1. Present the decision
One table (or one card per decision): question, options each with a pro and a con, your recommendation, and a
confidence (e.g. 70%), plus the review clause you propose: a **checkable thing** and a date.

## 2. Capture the ruling verbatim, before anything else
If a logbook is available (orchestrator), run first:
`abacus-logbook ruling add --words "<exact words>" --where <chat|page|session> --question "<q>" --target <bead>`.
Copy the words exactly as typed. "b with a change" is never collapsed to "b". If the words don't resolve the question,
ask a follow-up; that is not a ruling. Never record a ruling the operator did not give.

## 3. Build ONE row and check it
```
JUDGEMENT <YYYY-MM-DD>: question: ... | options: ... | recommended: ... | confidence: ... | review: <checkable thing> by <YYYY-MM-DD> | ruled: "<operator's words>" | agreed: yes|no|partial | ruling: #<id>
STANDING <YYYY-MM-DD>: words: "..." | scope: ... | source: ... | review: <checkable thing> by <YYYY-MM-DD> | ruling: #<id>
```
When a logbook recorded the ruling, end the row with `ruling: #<id>` (the id `ruling add` printed). The relayed
`RULING <bead> (ruling <id>)` message carries the same id, so a worker can match each relay to its row.
An answer to a question with options is always JUDGEMENT; STANDING is only for an unprompted instruction. Put the check
and its date first in the review clause (the first date is the horizon; the first bead id is the evidence source). No
`|` or newline inside a field. Figures must be read from the cited source, never from a summary.

`abacus-judgment check '<row>'` must print `OK` before you write it.

## 4. Record it on the bead
`bd update <bead> --append-notes '<row>'` (on another repository's board, prefix the note text per the foreign-board rule
and use a note instead). Then `abacus-logbook ruling recorded <ruling-id> --on <bead>` if a logbook is in use.

## 5. Repair, never edit
A bad row gets `SUPERSEDED <date>: <why>` directly under it, then a corrected row. Read any earlier SUPERSEDED line and
carry forward clauses it kept.

## 6. Review due rulings
`abacus-judgment due` lists rulings past their horizon with no OUTCOME. For each, read the evidence named in the review
clause yourself and append:
```
OUTCOME <YYYY-MM-DD>: ruling: <ruling date> #<n> | outcome: held|failed|unclear | words: "<evidence, quoted>" | delegated: true|false | lesson: <one line or none>
```
With no readable evidence the outcome is **unclear** and the words say what could not be read; never a guessed held.
If you (not the operator) wrote it, the row says so. A failed ruling reopens its question.
Write each OUTCOME also as a record in `docs/retros/` (doc_type: outcome) so shared memory harvests it.
`abacus-judgment status` reports the loop.
