---
name: theseus-recipe
description: Use when planning or replanning a theseus port - choosing the next milestone or round of tasks, deciding what to inventory, decompile, capture or build next, or checking that a phase is really finished before moving on.
---

# The theseus recipe for porting legacy software

A port moves through six phases. Each ends at a gate with evidence; do not start the next phase's implementation work before its gate holds. Every phase reuses what earlier phases recorded: dissect once, index, then only query.

## Phase 0: exploration (research only)

Tasks, one report each:
- Public references: the vendor's support matrix, the hardware chain, current and discontinued software, licenses.
- Ecosystem: open-source projects, file formats and readers, any prior protocol work.
- Artifact inventory: every installer, archive and binary statically extracted and inventoried (`theseus re inventory DIR --out ...`), drivers with their hardware IDs, nothing executed.
- Reference-run feasibility: how the original can run in isolation and what can be observed (compatibility layer, VM with device passthrough, capture tooling).

Gate: a written plan of action (strategy, milestones, decisions for the user), approved by the user.

## Phase 1: evidence base

- Register every binary that matters as `[[re.targets]]` (name, path relative to `${THESEUS_DATA}`, SHA-256).
- Decompile each target once into its corpus, build the catalog and the call graph (`theseus re build`, `theseus re rea decompile-all`, `theseus re calls`). `theseus re status` shows what exists; later tasks query with `theseus re find|show|callers|callees` and never decompile a target again unless its hash changes.
- Inventory the real environment the original ran in (VM image, installed versions, configuration, sample data) without booting or modifying it.
- Build the reference-run and capture tooling and prove it without the hardware.
- Start the new project's skeleton: build system, tests, a transport or I/O layer that a replay backend can drive, every unknown operation returning an explicit unsupported result.

Gate: `theseus re status` complete for every target; capture tooling proven; skeleton builds and passes `theseus verify`.

## Phase 2: ground truth

Script each operation of the original (initialize, configure, each acquisition or computation mode, abort, error cases) and record it: device captures (`cap_<hash>@<frame>`), output files, screenshots, a timed action log. Record once, replay many times. Never send unrecorded commands to real hardware from new code.

Gate: a capture corpus that covers every workflow in the definition of done, each with its output.

## Phase 3: specification and simulator

Dirty-room work (with `[rooms]`): turn corpus queries and captures into a written specification in `spec_paths`, every field labelled Recovered or Reconstruction, in your own words with no copied code. The specification states only externally observable behavior (wire formats, requests, payload layouts, ordering, timing, errors) and points to report sections for evidence; internal addresses, offsets and call structure stay in the dirty report. Build a replay simulator from the capture corpus.

Gate: the specification explains every message in the capture corpus; the simulator replays them.

## Phase 4: core

Clean-room work: implement from the specification and captures only, test against the simulator, then against the hardware. The per-merge smoke exercises the core behavior (open, one complete operation, its output), not only initialization.

Gate: the core workflow's output matches the original's recorded output on real hardware.

## Phase 5: parity and release

Every mode and error path, bindings, packaging, documentation, the user's acceptance tests. Full verification at least daily.

## Rules that apply in every phase

- One milestone at a time; tasks of 30 to 60 minutes per worker; at most six acceptance items.
- Merged, observed at runtime and user-tested are separate claims.
- Reusable scripts go in the repository; vendor files, extracted content, corpora and captures stay under the data root.
- When a phase uncovers a gap in an earlier one, add a task to the earlier phase rather than working around it.
