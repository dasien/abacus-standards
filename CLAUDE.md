# abacus-standards

<!-- abacus:begin (managed by abacus-adopt; edit the canonical copy in abacus-standards) -->
## Abacus

This repository participates in Abacus, Bitwarden's orchestrated multi-agent development system.
The Beads board in this repository (`bd`) is the system of record for agent-executable work.

**Authority.** Instruction edits (this file, `.claude/`, `.abacus/`, `.githooks/`) are made by the
operator, never by an agent session: describe the change and ask. What is delegated to an agent is
named on the bead and its pipeline label; anything that changes authority (guards, hooks, gates,
workflows, credentials, releases, production data, spending, anything irreversible) is the operator's.

**Memory.** Before settling an approach, call the `consult_memory` tool with the shape of the problem
(not the bead id) and no project filter, open each hit's source, and say what came back, including
nothing.

**Work.** Run beads through their pipeline with `/abacus-run-bead <id>`. Only `abacus-finalize`
commits, pushes and closes a bead, and only when every acceptance criterion has evidence. When
running as an orchestrated worker, follow `/abacus-worker` (the worker contract).
<!-- abacus:end -->
