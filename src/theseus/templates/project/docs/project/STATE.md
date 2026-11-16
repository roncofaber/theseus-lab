# {{PROJECT}} Execution State

Last updated: {{DATE}} by scaffold.

This file is the current snapshot, not a log. Replace stale sections instead of appending; keep it under about 10 KB. Detailed evidence belongs in the task record `result` (`theseus tasks show ID`) and in `docs/investigations/`. History lives in Git and in `docs/project/archive/`.

## Objective status

**Not complete.** Current milestone: none set (`theseus tasks milestone M1`).

## Verified baseline

- Last full verification pass (`theseus verify`): none yet.
- Code commits since then (`theseus verify --debt`): see the command; the guard refuses unverified pushes past `verification.max_unverified_merges`.

## Evidence levels

Keep the three lists separate; a task moves down only with recorded evidence (`theseus tasks list --evidence`).

- Merged, not yet observed at runtime: none.
- Observed at runtime, not yet user-tested: none.
- User-tested: none.

## Workers

None dispatched.

## Open queue

`theseus tasks list` is authoritative.

## Next actions

1. Fill in `AGENTS.md` (objective), `MANIFEST.md` (definition of done) and `ROADMAP.md` (milestones), then set the current milestone.
2. Configure `theseus.toml`: commands, worker providers, the original's path in `theseus.local.toml`.
3. Write the first task records: an inventory of the original (binaries, data files, protocols) and the reference-run setup.

## Blockers

None recorded. Unrecovered original behavior is reverse-engineering work, not a blocker.
