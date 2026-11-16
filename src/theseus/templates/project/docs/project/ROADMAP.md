# {{PROJECT}} Roadmap and Acceptance Contract

## Definition of done

Restate the MANIFEST's definition of done as checkable acceptance items: workflows, outputs, device behavior, platforms, performance on named hardware. Limitations are explicit rather than silently omitted.

## Deferred scope

What is outside the current definition of done. Keep architecture compatible where it costs nothing, but do not schedule work for it.

## Architecture policy

- Recover before you build: every behavior traces to the original (decompiled code, data files, recorded device traffic) or is labelled a Reconstruction with what would confirm it.
- Keep the domain logic testable without hardware or UI: hardware access, file formats and presentation enter through bounded interfaces with explicit ownership.
- Prefer established platform and framework mechanisms over hand-rolled ones; record a decision before replacing one.
- Measure before optimizing, and label the environment of every number.

## Milestones

Work one milestone at a time. The current one is `currentMilestone` in `docs/project/tasks.json`; `theseus tasks ready` offers only its tasks.

### M0: Foundation

- [ ] Repository, configuration and the agent workflow in place; Supervisor tests green.
- [ ] Inventory of the original: binaries with hashes, data files, configuration, the protocols it speaks.
- [ ] Reference-run backend working: the original runs in isolation and a ground-truth record is captured.
- [ ] Build, smoke and full verification commands configured.

### M1: Recovered core

- [ ] Replace with the first recovered subsystem and its acceptance items.

### M2: Feature parity

- [ ] Replace with the remaining workflows.

### M3: Release acceptance

- [ ] Full verification on the target hardware, documented limits, installable release.
