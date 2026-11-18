# Maintenance

## Verification and pushing

- `theseus build` and `theseus smoke` run in the main checkout; `theseus verify [REV]` runs `commands.verify` on a clean detached worktree of the commit and writes a log under `<data>/logs/`. A command kind with nothing configured reports `NOT CONFIGURED` (exit 4), never a pass.
- `theseus verify --debt` lists the code commits since the last passing full verification and the limit (`verification.max_unverified_merges`). The pre-push guard refuses unverified code past it. `THESEUS_PUSH_UNVERIFIED="reason"` overrides it, logged and bounded; use it only when the user asked.
- `theseus heavy -- CMD` runs a heavy command in one of the shared `limits.heavy_runs` slots.

## Documentation

- Every report names a `Topic:`. Periodically fold reports into `docs/topics/` with a documentation worker, then `theseus docs fold-check --topic NAME`, `theseus docs fold-apply --topic NAME` and `theseus docs index`. Folding refuses to drop a citation.
- `docs/investigations/README.md` and `docs/re/citation-index.md` are generated: `theseus merge` regenerates them; `theseus docs check` reports drift.

## Disk and records

- Evidence, logs and handoffs go under the data root (`theseus config get paths.data`); scratch under `paths.scratch`.
- `theseus tasks retire ID` archives a worker's `.theseus` records to `<data>/workers/` and removes its worktree.
- `theseus cleanup` is a dry run after a batch of integrations; `--apply` keeps anything an open task references and saves evidence-like files first.
- Never kill processes by name pattern; `theseus stop NAME` ends a headless worker and its slice.

## Host store

The host database (`~/.local/share/theseus/theseus.db`, `THESEUS_DB` overrides) holds projects, agents, events, pool slots and deliveries for every project on the machine. The queue runs maintenance and a backup at most once a day by itself.

- `theseus db check`: integrity, sizes, last backup and maintenance.
- `theseus db backup`: an online copy with rotation. `theseus db restore FILE --yes` backs up the current database first; ask the human before restoring.
- `theseus db prune` lists projects whose root is gone (`--yes` deletes them).
- `theseus pool status` shows who holds the slots of host-wide pools shared across projects.

## Upgrades

After the theseus package is upgraded, `theseus upgrade --dry-run` then `theseus upgrade` refresh the theseus-owned files (these skills, the worker brief and prompt, the push hook, helpers), add missing template files and list new configuration keys; project-owned files (`AGENTS.md`, `STATE.md`, Supervisor tests) are never touched. Commit the refreshed files, then restart free agents (`theseus agents restart NAME`, or `theseus instance restart NAME`) so they reload the skills. `theseus doctor` checks host tools and the REA wiring.
