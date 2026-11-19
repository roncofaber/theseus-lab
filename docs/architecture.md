# Design notes

Decisions behind the package layout, for contributors. User documentation starts at the [README](../README.md).

## Shape

- Python package (`src/theseus`, Python 3.11+ for `tomllib`), one CLI entry point, unittest suite. Standard library only apart from Textual, the single runtime dependency, which drives the interactive dashboard and is imported only by the console (`theseus` alone on a terminal, or `theseus console`).
- Logic that benefits from tests (task records, merge, guard, docs, catalog, cleanup, rules) is Python. Process supervision and tmux pane polling stay Bash (`src/theseus/scripts/`), because signal handling, `exec` chains that keep a recorded PID and systemd scopes are simplest there. The CLI runs them as `bash <packaged path>` with the `THESEUS_*` environment from `Config.environment()`; a variable already set in the process environment wins, which lets tests and one-off shells override a value.
- Templates live under `src/theseus/templates/project/`; a leading `dot-` in a path segment becomes `.` at scaffold time, so package data never relies on dotfiles surviving a wheel build.

## Configuration

- `theseus.toml` is the one project configuration. `theseus.local.toml` (Git-ignored) is merged over it only so machine paths such as the original's location stay out of Git, which the portable-path rule requires.
- The loader refuses to run without a `theseus.toml`; it never falls back to home or temporary directories. The data and worktree roots must lie outside the project.

## Task lifecycle

- `pending -> in_progress -> verification -> merged -> completed`. `merged` satisfies dependencies once the merge commit is in the assignment base (`[git] base`, so a dependent's worktree never starts without it), so code-only rounds do not stall the queue, but only `completed` tasks are archived, so merged work stays visible until verified.
- `evidence.observed` and `evidence.userTested` are recorded separately from status, because a merge, a runtime observation and a user test are different claims.
- `currentMilestone` gates `ready` and `assign`; the rule tests (`theseus check`) reject in-progress work outside it.

## Integration

- There is one integration path, `theseus merge`. It refuses: tracked changes in main other than the task file, a worker HEAD with commits not on the base branch (a HEAD moved only along it is a rebase and becomes the new base; files whose worktree copy lacks what main changed since the recorded base are merged three-way against that base, and conflicts refuse), paths outside ownership that the shipped patches, applied in order to the worktree base, do not reproduce exactly (merge then applies the patches to main and never takes those paths from the worktree, whose copies carry the worker's possibly older base), untracked files the delivery would overwrite. On a failed apply or patch it resets only the touched paths and deletes only files it created.
- The task file may be dirty at merge time because dispatch leaves the assignment uncommitted; merge commits it afterwards together with the merged status.

## Verification

- `theseus verify` runs the configured commands on a clean detached worktree of the commit and writes a log whose first line names the commit and whose last line is the final status. The guard and `tasks complete --from-log` read only that format.
- Unverified code is counted with `git rev-list SHA --not <verified commits> -- <code pathspecs>`, so a full pass of a descendant covers all its ancestors and docs-only commits never count.
- A command kind with nothing configured prints NOT CONFIGURED and exits 4: neither a silent pass nor a hard failure.

## Citations

- The citation index has configurable kinds. Native ports cite addresses; a device-protocol port cites captures (`cap_<hash>@<frame>`), and the fold check protects both the same way.

## Supervised agents

`theseus.supervisor` starts and supervises long-running agents on a private tmux server; `theseus.tmux` is the only module that runs tmux, and every command for the private server names its socket (a guard test checks this). There is still no daemon: `theseus agents up` and the queue tick reconcile, and a tmux `pane-died` hook records crashes and kicks a one-agent reconcile. The project's `queue run` driver is the one long-running loop; `agents up` starts it in a session of the same private server and the same hook restarts it.

`theseus.attention` derives the attention list (what needs the coordinator) from the task service, the store's events and samples and the live agent, driver and gateway state on every call; nothing about an item is stored. After each tick the driver pushes it into a registered coordinator pane through `agents.send_verified` (outside the tick's batch, after checking for a prompt and typed input), and keeps only push bookkeeping in `<data>/queue/attention.json`. Runs live in the host store (`agent_runs`). A supervised agent's address in the registry is its session target `=project--agent:`, which names no pane on the user's own server, so a caller that forgets the socket fails instead of typing into an unrelated pane. The design, the research behind it and the verification spikes are in [design/agent-supervisor.md](design/agent-supervisor.md).

## Verified sends and restart prompts

- `agents.send_verified` is the one way theseus types into an agent: it empties the input line, types, presses Enter and watches for the profile's busy marker, pressing Enter once more for text still in the input line. Outside the queue it waits for the result. Inside a queue tick (`agents.batched`) it only types: the tick presses Enter in every pane after one shared pause and verifies all its sends together in a bounded thread pool (`workqueue.verify_sends`) that only reads panes and presses keys, within `queue.send_budget_seconds`. The results, their events and every store or runtime write are applied on the tick's own thread. A send still unresolved when the budget runs out is kept in the runtime state and finished by the next tick.
- A fresh context with a task owes the agent one restart prompt (`workqueue.owe_prompt`), kept in the runtime state. While it is owed, or while a send to the agent is still being verified, the tick types nothing else into that agent, so a plain continue can never reach a fresh session first.

## Host store

- One SQLite database per machine, `~/.local/share/theseus/theseus.db` (`XDG_DATA_HOME` is respected, `THESEUS_DB` overrides the path), managed by `src/theseus/store.py` with the standard library `sqlite3` module only. It is the runtime source of truth for host-level and runtime state that spans projects or changes every few seconds: the projects this host runs (name, main-checkout root, data directory, coordinator, last seen), coordinator sessions, registered agents, queue events, pool leases and claims, deliveries and periodic agent-state samples for activity and utilization graphs.
- Task records live in the database (schema 7; [the design](design/task-store.md)). Messages to agents are still typed into their terminals; [a planned store inbox with per-harness delivery](design/message-delivery.md) would replace that. `theseus.taskrepo` is the only code that writes their SQL, and `theseus.taskservice` holds every rule. The CLI, the queue, the MCP server (`theseus mcp`) and the console all call the service, which checks who calls (the coordinator; a worker by its run credentials, only on its own active task; the queue). Git keeps the record through a deterministic, lossless export in the legacy shape (`docs/project/tasks.json`, `tasks-archive.jsonl`, `tasks-history.jsonl`), written after a passing verify and `tasks complete` and committed by the coordinator. The export is also the import format and the rollback path (`theseus db downgrade 6`). Workers read their task with `theseus task show` and report with `theseus task done`; a worker that cannot reach the store uses the copy `.theseus/task.json` and the `.theseus/outbox/` its tick takes in.
- Pool definitions stay in the host configuration (`[pools.<name>]` in `~/.config/theseus/config.toml`, or the deprecated `pools.toml`), which a user edits; only the leases and claims, which are runtime state, live in the database.
- There is no daemon. Every CLI call, queue tick or dashboard refresh opens the same file with `store.connect()`, which applies WAL mode (readers never block the one writer), a busy timeout (writers wait for each other instead of failing) and foreign keys. Every write runs in its own `BEGIN IMMEDIATE` transaction, so a check and the write that depends on it are atomic across processes. Connections are opened per call, never shared between threads.
- The schema is a list of migrations applied in order and recorded in `PRAGMA user_version`; the first process that opens an older database migrates it under the write lock, and every other process sees the result. A database whose version is newer than the running code knows is refused with an error that says to upgrade theseus, instead of being read or written by code that does not understand it.
- Times are stored as UTC ISO 8601 text ending in `Z` with a numeric epoch column next to them wherever queries select a range (events, deliveries, samples, leases, claims).

### Integration

- Events: `workqueue.event()` writes to the store under the project's name and `workqueue.events()` reads from it, with the same signature as before. Every event is still appended to `<data>/queue/events.jsonl` as a plain-text audit trail; that file is deprecated, nothing reads it after the migration, and it will stop being written in a later release.
- Pools: `pools.*` delegate to the store. The queue tick takes a slot with `store.acquire()`, which checks the pool's room and priority and takes the slot in one transaction, and releases it again if the reset or assignment that follows fails. Hand holds (`theseus pool hold`) keep their semantics: they count against the pool, yield to a higher-priority claim and expire at their deadline.
- Agents: the registry (`theseus agents add|remove|enable|disable`) is the store's agents table. It is seeded once from `[[instances.panes]]`; after that only commands change it, so removing every agent never brings the configured panes back. Enabling or disabling changes only the flag.
- Agent IDs: every registration (supervised or hand-registered) gets a ULID in `agents.id`; updating a registration keeps it and a name registered again after removal gets a new one. Events, deliveries, samples, pool leases, gateway errors and retry episodes and `agent_runs` carry it in `agent_id`, so per-agent history (runs, crash budget, samples, a retry episode) never mixes two agents that shared a name, and a removed agent's records keep its ID. Users still type names: the store resolves a name to the agent registered under it now. `pool_claims` are per project and name no agent. Run generations stay numbered per name, so `NAME GEN` stays unique. A task's attempt records the agent's ID (`ownerId` in the export, `agentId` on earlier attempts); a task whose `ownerId` differs from the agent registered under the owner name now is not that agent's task. Schema version 6 adds the columns and backfills one ID per existing agent, mapping existing records to it by name.
- Projects: every command that loads a project records it under its `[project] name` with the canonical main-checkout root (a worker's worktree never registers). When a second live root claims a name another live root holds, the first keeps it and the conflict is recorded; `theseus doctor` reports it. A root whose `theseus.toml` is gone gives its name up to the next root that claims it. Projects are never removed automatically, because removing one deletes its events, agents, deliveries and samples with it; dead roots are hidden from the dashboard and `theseus db prune --yes` removes them on request.
- Samples and deliveries: each queue tick writes one sample per registered agent (state and task), and `tasks.deliver()` (also used by `theseus merge`) writes a delivery.
- Migration: the first time a project opens the store, its `agents.json` and `queue/events.jsonl` are imported; the first open on the host imports the pool state files and the `projects.json` registry. A marker in the store's `meta` table makes each import run exactly once (checked and set in the same transaction, so two processes cannot both import), and the old files stay where they are. The importers themselves are idempotent, but they are only ever used as these one-time steps.

### Retention and maintenance

- Every growing table has a retention, set in `[store.retention]` of `~/.config/theseus/config.toml` (see [Configuration](configuration.md#host-store)): samples 14 days; events, deliveries and ended coordinator sessions 365 days; ended agent runs 90 days, always keeping each agent's latest run, whose generation numbers the next one; gateway errors and gateway retry episodes that are no longer waiting 7 days. Events are indexed by project and id for the latest events and by project, kind and time for activity bins. `theseus db prune --yes` removes a gone project with all its rows, including its gateway and pool rows, which have no foreign key.
- `theseus db maintain` trims by retention, runs `ANALYZE` and `PRAGMA optimize`, returns free pages with an incremental vacuum (a database created before incremental vacuum is converted once with a full `VACUUM`) and truncates the write-ahead log. The queue tick runs it at most once a day; the day is claimed in a transaction, so concurrent ticks of several projects run it once.
- A benchmark test loads 200,000 events and 100,000 samples and checks that the dashboard's 12-hour activity bins, the latest 200 events and a pool acquire each stay under 50 ms and that their query plans use the indexes.

### Backups

- `theseus db backup` copies the live database with SQLite's online backup API to `~/.local/share/theseus/backups/theseus-YYYYmmddTHHMMSSZ.db` (by default the `backups` directory next to the database; `THESEUS_BACKUPS` or `[store.backups] directory` in the host configuration override it), runs `PRAGMA integrity_check` on the copy before giving it its final name and rotates the directory to the newest copy of each of the 7 most recent days and of each of the 4 most recent ISO weeks. The queue tick runs a backup at most once a day; a failure is recorded and shown by `theseus db check` and `theseus doctor` but never stops the queue.
- `theseus db restore FILE --yes` refuses without `--yes`, checks the file's integrity and schema version, saves the current database as a `-pre-restore` copy that rotation never removes, then loads the file into the live database with the backup API, so processes that have it open see the restored content. It skips the version guard on the live file, so it also recovers from a database written by a newer theseus; a live file that is not a database at all is copied aside byte for byte and replaced.
- `theseus db check` prints the integrity check, schema version, sizes, row counts, last backup and last maintenance.
