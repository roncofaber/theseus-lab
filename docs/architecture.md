# Design notes

Decisions behind the package layout, for contributors. User documentation starts at the [README](../README.md).

## Shape

- Python package (`src/theseus`, Python 3.11+ for `tomllib`), one CLI entry point, unittest suite. Standard library only apart from Textual, the single runtime dependency, which drives the interactive dashboard and is imported only by `theseus top` on a terminal.
- Logic that benefits from tests (task records, merge, guard, docs, catalog, cleanup, rules) is Python. Process supervision and tmux pane polling stay Bash (`src/theseus/scripts/`), because signal handling, `exec` chains that keep a recorded PID and systemd scopes are simplest there. The CLI runs them as `bash <packaged path>` with the `THESEUS_*` environment from `Config.environment()`; a variable already set in the process environment wins, which lets tests and one-off shells override a value.
- Templates live under `src/theseus/templates/project/`; a leading `dot-` in a path segment becomes `.` at scaffold time, so package data never relies on dotfiles surviving a wheel build.

## Configuration

- `theseus.toml` is the one project configuration. `theseus.local.toml` (Git-ignored) is merged over it only so machine paths such as the original's location stay out of Git, which the portable-path rule requires.
- The loader refuses to run without a `theseus.toml`; it never falls back to home or temporary directories. The data and worktree roots must lie outside the project.

## Task lifecycle

- `pending -> in_progress -> verification -> merged -> completed`. `merged` satisfies dependencies, so code-only rounds do not stall the queue, but only `completed` tasks are archived, so merged work stays visible until verified.
- `evidence.observed` and `evidence.userTested` are recorded separately from status, because a merge, a runtime observation and a user test are different claims.
- `currentMilestone` gates `ready` and `assign`; the rule tests (`theseus check`) reject in-progress work outside it.

## Integration

- There is one integration path, `theseus merge`. It refuses: tracked changes in main other than the task file, a worker HEAD that moved, paths outside ownership, untracked files the delivery would overwrite. On a failed apply or patch it resets only the touched paths and deletes only files it created.
- The task file may be dirty at merge time because dispatch leaves the assignment uncommitted; merge commits it afterwards together with the merged status.

## Verification

- `theseus verify` runs the configured commands on a clean detached worktree of the commit and writes a log whose first line names the commit and whose last line is the final status. The guard and `tasks complete --from-log` read only that format.
- Unverified code is counted with `git rev-list SHA --not <verified commits> -- <code pathspecs>`, so a full pass of a descendant covers all its ancestors and docs-only commits never count.
- A command kind with nothing configured prints NOT CONFIGURED and exits 4: neither a silent pass nor a hard failure.

## Citations

- The citation index has configurable kinds. Native ports cite addresses; a device-protocol port cites captures (`cap_<hash>@<frame>`), and the fold check protects both the same way.

## Host store

- One SQLite database per machine, `~/.local/share/theseus/theseus.db` (`XDG_DATA_HOME` is respected, `THESEUS_DB` overrides the path), managed by `src/theseus/store.py` with the standard library `sqlite3` module only. It holds host-level and runtime state that spans projects or changes every few seconds: the projects this host runs (root, data directory, coordinator pane, last seen), coordinator sessions, registered agents, queue events, pool leases and claims, deliveries and periodic agent-state samples for activity and utilization graphs.
- Task records are not in the database. `tasks.json` in each project's Git repository stays the source of truth, because a task's history belongs with the code it describes, is reviewed in commits and must survive a fresh clone on another machine. The store may keep a read-only task index (status, priority, room, milestone, last change) refreshed from `tasks.json` for cross-project queries; nothing writes task state through it.
- Pool definitions stay in `pools.toml`, which is configuration a user edits; only the leases and claims, which are runtime state, move to the database.
- There is no daemon. Every CLI call, queue tick or per-agent server opens the same file with `store.connect()`, which applies WAL mode (readers never block the one writer), a busy timeout (writers wait for each other instead of failing) and foreign keys. Every write runs in its own `BEGIN IMMEDIATE` transaction, so a check and the write that depends on it are atomic across processes: `store.acquire()` checks a pool's room and priority and takes the slot in one step, which the separate `may_start` and `take` of the file-based pools cannot guarantee once several processes race.
- The schema is a list of migrations applied in order and recorded in `PRAGMA user_version`; the first process that opens an older database migrates it under the write lock, and every other process sees the result.
- Times are stored as UTC ISO 8601 text ending in `Z` with a numeric epoch column next to them wherever queries select a range (events, deliveries, samples, leases, claims).
- Importers copy the existing JSON state (a project's `agents.json` and `queue/events.jsonl`, the pool state files, the host `projects.json` registry) into the database and can run any number of times: events are keyed by their line in the source file, agents and pool entries are upserted without overwriting newer rows.
