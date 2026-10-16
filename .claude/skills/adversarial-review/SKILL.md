---
name: adversarial-review
description: Dispatch ONE fresh agent to attack an uncommitted change before commit. Use in the manual session path, before finalizing hand-made changes, or when asked for an adversarial/fresh-eyes review of local changes.
---

# Adversarial review (fresh agent, never a fork)

1. Dispatch **one** fresh subagent with the Agent tool (never a fork: a fork inherits your assumptions). Give it only:
   the bead id and its acceptance criteria, and the instruction below. Not your reasoning, not your conversation.
2. Brief: *"Attack the claim 'this change is correct, safe and honest.' Verify every claim against the working tree,
   not just the diff (`git diff HEAD`, then read surrounding code). Run nothing that changes state. Report findings ranked
   BLOCKING or MINOR, each with file:line and a concrete fix. Judge every acceptance criterion: met / unmet, with evidence."*
3. Treat each finding as a claim. Confirm every BLOCKING one yourself before changing code; decline with a reason
   when it is wrong.
4. Record on the bead:
   `bd update <id> --append-notes "<YYYY-MM-DD> adversarial review: N blocking, M minor. Blocking: ... Declined: ... (why)"`
