# Task store: live task state in the host database

Status: design for review, nothing implemented. Written 2026-11-18 against theseus `9c44e63` (store schema v6). It covers the approved direction: live task state leaves the committed `docs/project/tasks.json` for the host store, the theseus CLI becomes the agent API, and a thin MCP server wraps the same service. Section 15 lists the questions that need a decision before phase 1.

## 1. Summary

Today a task record is a JSON object in `tasks.json`, and every state change is a file write plus, for assignment, merge and unmerge, a commit. A worker reads its record from the file at its base commit, which is why an assignment must be committed (and, with `[git] base = origin/main`, pushed) before the worker can start. The field reports all follow from that coupling: one commit per assignment, new tasks waiting for a push the guard can refuse, bookkeeping commits that once swept staged files, no `tasks edit`, conflicts between branches, and ownership that only sees `ownedPaths`.

The design moves the live state into new tables of the host store, behind one service module that the CLI, the MCP server, the queue and the console all call. Workers stop reading files for their task: `theseus task show` gives them the record, `theseus task done` reports the delivery. Assignment becomes a store write plus one typed line. Git keeps the record through a deterministic export in the legacy format, written at verified merges and committed by the coordinator. That export is also the import format and the rollback path, so one property carries most of the safety: importing an export is lossless.

## 2. Scope

Unchanged: the lifecycle, evidence levels, milestones, rooms, profiles, capability, priorities, holds, the merge algorithm, verification and the guard, the queue tick and its verified sends, the supervisor.

Stays in files: handoffs (`.theseus/handoff.md`), notes files, check logs, patches, `changed.sha256`, investigation reports, specs, topics, `STATE.md`, `ROADMAP.md`, `MANIFEST.md`. The store records a handoff's path, SHA-256 and `Result`, and the text of short notes; it never becomes a document store.

Moves to the store: records, status, owner, attempts, dependencies, owned paths, touches, results, merge record, evidence, notes, task events, the current milestone and the project policy text.

## 3. Schema (migration v7)

All new tables are created by one migration appended to `store.MIGRATIONS`. Times follow the store convention (ISO text ending in `Z`, plus an epoch column where queries select ranges). JSON columns are `TEXT` parsed in Python, so no query depends on SQLite's JSON functions and a Postgres repository stays a translation, not a rewrite.

| Table | Key | Columns (beyond the key) | Notes |
| --- | --- | --- | --- |
| `projects` (altered) | `name` | `+ id TEXT` (ULID, unique index), `+ tasks_root` (the root that imported), `+ milestone`, `+ objective_complete`, `+ policy` (JSON), `+ tasks_imported` (ISO), `+ last_export` (ISO, commit) | Backfilled with one ULID per existing row. |
| `tasks` | `pk INTEGER PRIMARY KEY`; unique `(project_id, id)` | `project_id` FK `projects(id)` ON DELETE RESTRICT, `id` (kebab), `status`, `rev`, `role`, `objective`, `contract` (JSON: string or legacy object), `acceptance` (JSON list), `milestone`, `priority`, `hold`, `room`, `profile`, `min_capability`, `note` (the coordinator's assignment note), `blockers` (JSON: externalBlocker, researchRequired, pause), `merge` (JSON), `result` (JSON), `evidence` (JSON), `extra` (JSON: unknown legacy keys, kept verbatim), `seq` (file order), `created`, `updated`, `updated_epoch` | `rev` increments on every write and is the compare-and-set token. |
| `task_deps` | `(task_pk, depends_on)` | | `depends_on` is the task id text, so a dependency on a missing id is representable (it shows as `missing:`). |
| `task_paths` | `(task_pk, kind, path)` | `kind` in `owned`, `touches`; `source` in `declared`, `observed`; `attempt_id` for observed rows | Indexed on `(kind, path)` for the hold check. |
| `task_attempts` | `id TEXT` (ULID, also the assignment id) | `task_pk`, `n`, `agent_id` (registered agent, or the worker id `dispatch` mints), `owner` (legacy label), `phase` (`claimed`, `active`, `delivered`, `merged`, `released`, `reopened`, `unmerged`, `abandoned`), `token_hash`, `worktree`, `base`, `log_dir`, `execution` (JSON: provider, model, times), `handoff`, `handoff_sha256`, `result`, `reason`, `record` (JSON: the legacy `previousAttempts` entry when imported), `started`, `ended` | Partial unique indexes: one row with `phase IN ('claimed','active')` per `task_pk`, and one per `agent_id`. They make `second_task` a constraint; the release-to-pending fallback of reopen and unmerge (`release_if_taken`) stays in the service. |
| `task_notes` | `id INTEGER PRIMARY KEY` | `task_pk`, `attempt_id`, `author` (`coordinator` or an agent id), `kind` (`note`, `edit`), `text` (bounded, 4 KB), `time` | Append-only. |
| `task_events` | `id INTEGER PRIMARY KEY` | `task_pk`, `attempt_id`, `time`, `epoch`, `actor`, `kind`, `detail` (JSON: fields changed, from and to status) | The task's history. Exempt from `trim`; removed only with its project. |
| `agent_credentials` | `(agent_id, run_id)` | `secret_hash`, `issued`, `revoked` | One row per supervised run or dispatch (`agent_id` is then the minted worker id); revoked when the run ends. |

`task_index` (created in v1, never written) is dropped. Queue events (`events`) are still written for every task transition, in the same transaction as the `task_events` row, so `queue wait`, the console feed and their 365-day retention are unchanged. `task_events` is the durable copy that the export carries.

Project identity. Two behaviors are fine for runtime rows and wrong for tasks: `db prune --yes` deletes a project with cascading rows, and `upsert_project` hands a name, and with it the row and its id, to a new root once the old root's `theseus.toml` is gone. So task tables reference `projects.id` with `ON DELETE RESTRICT`, and `db prune` refuses a project with tasks unless `--with-tasks` is given, which first writes a final export to `<data>/exports/`. `projects.tasks_root` guards the hand-over: the service refuses task calls from any other root until `theseus tasks adopt` records the new root (a moved or renamed checkout). A different project reusing the name runs `tasks import --new-id`. The id also sits in the committed export header, so an import on another host matches by it (Q1).

## 4. Service layer

`theseus.taskrepo` is the only module that writes SQL for task tables. It exposes a `Repo` protocol (get, list, insert, update with expected `rev`, attempts, paths, deps, notes, events) and `SqliteRepo`. `theseus.taskservice` holds every rule. `tasks.py` keeps the pure helpers (`blocking`, `owned_pattern`, `ownership_overlaps`, `handoff_result`, the worktree checks) and loses `Queue.load/save`. A read-only `Queue` facade (`load()` assembles the legacy document from the store) keeps `top.py`, `rules.py` and friends working during phase 2, and is removed once they call the service.

Principal and context:

```python
@dataclass(frozen=True)
class Principal:
    role: Literal["coordinator", "worker", "queue"]
    agent_id: str | None = None
    attempt_id: str | None = None      # set for a worker; scopes every call to its task
    via: str = "cli"                   # cli, mcp, queue, console; recorded in task_events.actor

svc = taskservice.open(conf)           # resolves the project id; one connection per call, as today
```

Signatures (all return plain dicts, raise `TaskError` with a stable `code`):

```python
# coordinator
add(p, spec: dict) -> dict                              # validates; returns record + warnings
edit(p, task_id, changes: dict, *, expected_rev=None, force=False) -> dict
get(p, task_id, *, history=False) -> dict
list(p, *, status=None, milestone=None, include_completed=False) -> list[dict]
ready(p, *, any_milestone=False, include_paused=False) -> list[str]
hold(p, task_id, on: bool); set_milestone(p, name)
release(p, task_id, reason); reopen(p, task_id, reason); unmerge(p, task_id, reason, revert_commit)
rebase(p, task_id, base); mark_merged(p, task_id, commit); observe(p, task_id, how, level)
complete(p, task_id, summary, commit, checks, log, limits, *, force=False)
# queue and dispatch
claim(p, task_id, agent_id, *, expected_rev) -> Claim   # attempt id, plaintext token (returned once)
activate(p, attempt_id, *, worktree, base, log_dir, execution)
abandon(p, attempt_id, reason)                          # undo a claim whose worktree or send failed
# worker (attempt-scoped) and the queue's handoff fallback
show(p) -> dict                                         # own task, rendered for the agent
note(p, text, *, kind="note")
deliver(p, *, handoff_text, handoff_path, result, observed_touches) -> dict
blocked(p, reason, *, handoff_text=None)
# records
export(project) -> Snapshot; import_(snapshot, *, mode) -> report
authenticate(credentials: dict) -> Principal
```

Worker-facing calls take content, not paths: the CLI reads `--handoff PATH` and passes the text, its hash and the path as a label. A remote client in phase 2 sends the same arguments. `integrate.merge` still reads the worktree directly; it runs on the host, and remote worktrees are the real phase 2 blocker (section 14), not something this design solves.

Transactions and locking:

- Every service call is one `BEGIN IMMEDIATE` transaction that checks state, writes the change, the `task_events` row and the queue event, and commits. Updates are compare-and-set: `UPDATE tasks SET ..., rev = rev + 1 WHERE pk = ? AND rev = ? AND status = ?`. A miss raises `TaskError("conflict")`, which the CLI reports and the queue retries next tick.
- No transaction spans a subprocess. `base_commit` can fetch for up to 120 s, against a 30 s busy timeout shared by every project on the host. So assignment is three calls, `claim`, then the worktree outside any transaction, then `activate` or `abandon`.
- The queue flock (`<data>/queue/lock`) stays, with a narrower meaning: it serializes ticks with commands that change the main checkout, worktrees or panes (merge, unmerge, rebase, dispatch, instance assign, retire, cleanup). Worker commands, `tasks add/edit/note/hold/priority/milestone/observe/complete` and all reads do not take it. A tick holds the flock for up to the send budget, and making a worker's `task done` wait behind it would stall the agent and is impossible remotely.
- Invariants that were code become constraints: one active attempt per task and per agent (partial unique indexes), unique task ids per project across open and completed tasks (the archive split disappears), and FK integrity of attempts, paths and notes.

Validation (`add` and `edit` alike): today's `add` checks plus no dependency cycle and room rules. Ownership or touches overlap with `in_progress` and `verification` tasks is a warning, not a refusal, since queuing a follow-up for the same files is legitimate; the hold at claim time (section 7) enforces it. `edit` is allowed on `pending` and held tasks. On `in_progress` it needs `force` and appends an `edit` note listing the changed fields, and the queue types a one-line pointer ("Task X changed, run `theseus task show`") when the agent is next idle. `edit` on `verification`, `merged` or `completed` is refused; reopen first. Import skips the budgets (section 10).

## 5. CLI and MCP surface

Every command gains `--json`: one object on stdout, `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`, and the exit codes keep their meaning (0, 1 refused, 3 conflict). Text output stays the default.

| Command | Role | Notes |
| --- | --- | --- |
| `theseus task show [--field F]` | worker | Own task: record, notes, worktree, base, log dir, the rendered prompt. |
| `theseus task note TEXT` | worker | Appends a note the coordinator sees in `tasks show` and the console. A `--question` flag that also wakes `queue wait` would go beyond the four approved commands (Q8). |
| `theseus task done --handoff PATH` | worker | Reads the handoff, parses `Result`, collects patch targets, calls `deliver`. Partial and blocked handoffs use it too. |
| `theseus task blocked REASON [--handoff PATH]` | worker | A delivery with result `blocked`; frees the slot as today. |
| `theseus tasks add ... [--touches FILE ...]` | coordinator | As today plus touches; prints warnings. |
| `theseus tasks edit ID [--objective --contract --accept --owned --add-owned --touches --depends --milestone --room --profile --priority --note ...] [--force]` | coordinator | Replaces `priority`, `profile`, `capability`, `note`, which stay as aliases. |
| `theseus tasks ls / show ID [--history]` | coordinator | `list` stays an alias. |
| `merge, verify, release, unmerge, reopen, rebase, merged, observe, complete, hold, milestone` | coordinator | Same arguments; state through the service. |
| `theseus tasks export` | coordinator | Writes the snapshot into the working tree. |
| `theseus tasks import [--replace] [--new-id]`, `tasks adopt` | coordinator | Section 10. |
| `tasks archive`, `tasks migrate-ids`, `tasks assign` | deprecated | `archive` runs `export`; `assign` stays for scripting with `--force`. |

`theseus task ...` (singular) refuses any principal other than a worker, except that the coordinator may pass `--task ID` to act for a `claude-subagent` worker, which runs inside the coordinator's own process. `theseus tasks ...` mutating commands refuse a worker principal: a worker that wanders into coordinator commands gets an error naming its own command.

MCP server: `theseus mcp`, stdio, in the optional extra `theseus-lab[mcp]`. Each tool calls one service function with the principal the server authenticated at startup. The role comes from the credentials, never from a client argument, so `THESEUS_MCP` loses its `--role worker`. Worker tools are `task_show`, `task_note`, `task_done` (handoff text or path) and `task_blocked`. The coordinator gets the full set, one tool per coordinator command in the table above, merge, unmerge, verify and complete included. Those four take the queue flock and can run for minutes, longer than some clients' tool timeouts. They return as soon as the work starts, and the result arrives as a queue event (Q9). A worker server registers only the worker tools. Phase 0 verifies the SDK's current high-level server API and version (a summarizing fetch suggested `mcp` 2.x with `mcp.server.MCPServer`; treat that as unverified) and pins a major range.

Wiring: `profiles.mcp_config` writes one file per profile. When a profile lists `"theseus"` in `mcp_servers`, theseus writes a per-agent file instead, `<data>/mcp/<profile>/<agent>.json`. Its theseus entry carries `env = {"THESEUS_CREDENTIALS": <path>}`, so the server does not depend on the agent CLI passing its environment to stdio servers (phase 0 checks Claude Code and Codex). The path is stable per agent, but the changed MCP flag alters `launch_hash` once, so those agents report drift once at upgrade (`agents restart --drifted`). The coordinator adds the server by hand (`claude mcp add theseus -- theseus mcp`); scaffold writes no `.mcp.json`. Caveat: supervised workers start in the main checkout, and `claude mcp add` defaults to local scope keyed by project path. So a worker profile without `mcp_servers` (no `--strict-mcp-config`) loads the coordinator's entry, which authenticates as the worker only if the client passes the worker's environment (Q5).

## 6. Identity and tokens

Constraint: a persistent agent's environment is fixed when `supervisor.exec_agent` execs it, and a hand-made pane's environment is not ours at all, so nothing can arrive by environment at assignment time.

- Credentials file per agent, `<data>/agents/NAME/credentials` (mode 0600, rewritten atomically). It holds `agent_id`, `run_secret`, and, while the agent holds a task, `attempt_id` and `token`. Its path goes in the agent's environment at launch as `THESEUS_CREDENTIALS`, next to `THESEUS_AGENT_ID`. Secrets are never in the environment, so a worker that prints its environment leaks only the path.
- One-shot workers: `dispatch` registers no agent today (`owner_id_of` returns None for them), so it mints a worker ULID per dispatch. That id goes on the attempt and in `agent_credentials`, never in `agents`.
- Run secret: minted for each supervised run (`spawn`) and each dispatch, hashed into `agent_credentials`, and revoked when the run ends (the pane-died hook, `close_run`). It is written to the file, not to the launch spec, so drift detection never sees it.
- Assignment token: minted by `claim` (32 random bytes, only its SHA-256 on the attempt row), written to the file by the queue right after `activate`, and erased at delivery, release and abandon. For a one-shot `dispatch` the launch is the assignment, so the file under `<data>/workers/NAME/credentials` holds both from the start. When the supervisor writes the file at launch for an agent whose active attempt has no token (a restart, or an attempt carried over by import), it mints one then.
- `authenticate` accepts a call when the run secret matches an unrevoked run of `agent_id` and the token matches the active attempt of a task that agent owns. A stale call (an attempt released, reassigned or delivered) fails with code `not-your-task`. The CLI and the MCP server share this function.
- Hand-made panes (`agents add NAME PANE`) are launched by the user, so theseus cannot give them credentials. Two options: a fallback principal scoped to the task whose worktree the call runs in, which is identity by local path and so against decision 2 (local only, recorded as `via=worktree`), or requiring `agents migrate` to supervised agents before cutover. This is Q10. Until it is decided, their deliveries go through the handoff fallback.
- No credentials, outside task worktrees: the coordinator. Once the coordinator is a supervised session (a later round), it gets credentials with role `coordinator`, and the anonymous coordinator can be narrowed behind a setting.
- Honesty: workers run as the same Unix user and can open the database file. Locally these checks stop mistakes (a worker closing another's task, running `tasks complete`, a stale context writing after a reset), not attacks. Phase 2 makes them a boundary, because a remote worker reaches only the transport and holds only its own file.

## 7. Flows

Assignment (queue tick, under the flock):

0. At tick start, every `claimed` attempt of the project is an orphan of a crashed tick or dispatch (claims are made only under the flock), and is abandoned with reason `orphaned claim`.
1. `next_task` reads ready candidates from the service. `ready` now includes the touches hold. Pool acquire, relaunch and reset run as today, before any task row changes, so a full pool writes nothing to the never-trimmed history.
2. `claim(task, agent, expected_rev)` checks, in one transaction: still `pending` and ready, not held, capacity, room, no overlapping held paths, and the agent has no active attempt. It writes the attempt `claimed`, the status `in_progress` and the events. On conflict it skips the task and releases the pool slot.
3. Outside any transaction: `move_previous_attempt`, `create_worktree` at `base_commit`, `exclude_dirty`. No `record_committed` check and no commit: the worker no longer reads its record from Git.
4. `activate(attempt, worktree, base, log_dir, execution)`, then write the token to the agent's credentials file.
5. Type the pointer line through `send_verified`: `Task X is yours: run theseus task show (worktree W). Re-read BRIEF first.` The brief and the full record come from `task show`, so the message stays short and nothing secret is typed.
6. Any failure in 3 to 5: `abandon(attempt, reason)` returns the task to `pending`, the worktree is removed when untouched (today's `roll_back`, minus `commit_record`), the token is erased, and the back-off applies.

Delivery:

1. The worker writes `.theseus/handoff.md` and `changed.sha256` as today and runs `theseus task done --handoff .theseus/handoff.md`. The CLI parses `Result`, lists patch targets from `.theseus/patches/*.patch`, and calls `deliver`.
2. `deliver` (transaction): attempt `delivered`, status `verification`, handoff path and hash, the result, observed touches, a `task_events` row and the `delivered`/`partial`/`blocked` queue event. It also writes a `deliveries` row and erases the token.
3. Fallback for one release: the tick's existing detection (idle, handoff states a Result and has settled) calls `deliver` with principal `queue` when the worker forgot `task done`, and records `source=detected`. Agents assigned before the cutover deliver this way. Removing the fallback is question Q4.

Merge and verify (section 8) move the task to `merged` and `completed`. Release, reopen and unmerge become attempt phase changes, and the token rules above apply.

Soft ownership:

- The conflict set of a task is its owned patterns plus its touches (declared and observed). Two tasks conflict when `ownership_overlaps` holds between any owned patterns, or when a touched file matches the other task's owned pattern or touched file.
- At `add`/`edit`: a warning naming the running tasks it overlaps. At `claim`: the task is held while any `in_progress` or `verification` task conflicts, with the reason `touches:FILE@TASK` in `blocking()`, so `tasks ls`, `ready` and the console show it. Holds are computed, never stored.
- Observed touches are recorded at delivery (patch targets and paths passed with `--accept`) and dropped when the task leaves `verification`. That is the case that bit three parallel tasks: patch targets that no declaration named. Scanning a running worktree's patches each tick would catch them earlier and is left as an option (Q6).

## 8. Merge and integrate changes

- `integrate.assign`, `dispatch`, `instance assign`: drop `commit_record`, `record_committed` and `base_mismatch`. `assign` becomes claim, worktree, activate. `--no-commit` and `commit=` arguments become no-ops.
- `integrate.merge`: reads the task from the service, applies and commits exactly the touched code and generated paths, then calls `mark_merged`. The `chore: X merged in SHA` commit disappears. The "deliver on merge when the handoff exists" step calls `deliver` with principal `coordinator`.
- `integrate.unmerge`: the revert commit stays (it is code); `commit_record` disappears and the record goes through `service.unmerge`.
- `refuse_dirty` exempts the export files (`tasks.json`, `tasks-archive.jsonl`, `tasks-history.jsonl`) instead of only the first two, because an export may sit uncommitted between merges. When a revert or merge conflicts in an export file, it is regenerated like the indexes.
- Export at verified merge: after `theseus verify` passes, and after `tasks complete`, theseus runs `tasks export` into the working tree, and the coordinator commits it with `docs/project/` as step 8 of the loop already says. Exports never commit by themselves (the approved decision), so no theseus code path stages files any more. `projects.last_export` records when and at which HEAD.
- `verify.guard` runs `theseus check` on a snapshot of the pushed commit, which cannot and should not see the store. The Supervisor task test (`rules.task_record_problems`) validates the committed export, the reviewable record. The service enforces the same rules at write time. `rules.governance_files` keeps scanning `tasks.json` as an export.
- Other consumers read the service: `top.py` (`posted_at`'s `git log -S` becomes `created`), `agents.current_task` (one indexed query), `rooms.py`, `gateway.py` (restart prompt), `docs/index.py`, `cleanup.referenced_strings`, `dispatch.py`, `workqueue.summary`. The scaffold templates' empty task files become an empty schema 2 export; a new project's first task command creates its store row.

## 9. Export, history and backups

Export (`theseus tasks export`), deterministic so it diffs well and merges rarely:

- `docs/project/tasks.json`: `{"schema": 2, "project": {"id", "name"}, "currentMilestone", "objectiveComplete", "policy", "tasks": [...]}`. It holds open tasks (`pending` to `merged`) in `seq` order, each record in today's key set plus `touches`. `previousAttempts` is rebuilt from attempt rows, verbatim from `record` for imported ones. Keys come in a fixed order, and absent optional keys stay absent. Notes stay out of the record, so they never push it over `TASK_RECORD_LIMIT` (checked by `rules.task_record_problems`).
- `docs/project/tasks-archive.jsonl`: completed tasks, one per line, in archive order. Imported lines keep their position (97 of opends2's records have no `completedAt`), and new completions are appended. Lines already exported never change.
- `docs/project/tasks-history.jsonl`: `task_events` and notes, one per line, ordered by `(time, id)`, append-only.
- Host paths (worktree, log dir, handoff) stay absolute as today, because older code uses them as literal paths and the rollback depends on that.
- Schema 2 is a superset an older theseus reads: it ignores `project` and `touches`, and the history file is new.

Loss window: Git holds state as of the last committed export, which is the last verified merge. The store backup bounds the rest. Today the queue tick backs up once a day; the design adds a backup at most hourly when task rows changed since the last one (the database is about 3 MB here, so the copy takes milliseconds), and before every v7 migration and import.

Off-machine copies: `[store.backups] offsite = "/path"` in the host config. After each backup passes its integrity check, it is copied to that directory under the same rotation (7 daily, 4 weekly) plus the newest hourly. On this host the setting points at LBL storage. The live database is never placed there: `theseus doctor` warns when the database or its WAL sits on a network filesystem (`stat -f` type `nfs`, `cifs`, `fuse.*`; the data root here is local ext). It also warns when the newest off-site copy is older than two days.

## 10. Upgrade, migration and rollback

The shared store is the headline risk. theseus-lab-stable and any development checkout open the same `~/.local/share/theseus/theseus.db`, and the first v7 connect from any checkout migrates it. After that, stable refuses the file with `SchemaTooNew`. Hence:

- Development and tests run with `THESEUS_DB` (and `THESEUS_DB_GUARD` on the real path), and the worktree's test helpers already enforce this. No developer opens the real store with v7 code until the coordinator schedules the cutover.
- The v7 migration is additive (new tables and `projects` columns, `task_index` dropped), takes a `-pre-v7` backup that rotation never removes, and does not touch any project's tasks. Stable must be upgraded at the same moment as the cutover.

Per-project cutover (`theseus tasks import`, also offered as a step of `theseus upgrade`), opends2 as the example: 15 open tasks, 4 of them `in_progress`, and 1086 archived (2.8 MB).

1. `theseus queue pause`, stop the `queue run` driver, and wait for no tick to hold the flock.
2. `theseus tasks import` reads `tasks.json` and `tasks-archive.jsonl` in one transaction. Ids must be unique across both. Imported records skip the add-time budgets: opends2 has records up to 14 KB, 10 acceptance items, null dependencies, object contracts and ad hoc keys (`coordinatorNotes`, `architectureReview`, `researchResolution`, ...), which land in `extra` verbatim. `previousAttempts` entries become attempt rows with their original object in `record`. Open `in_progress` and `verification` tasks become an active or delivered attempt with no token. `ownerId` maps to `agent_id` when the agent still exists. Legacy rows that break the one-active-attempt-per-agent index keep only the newest attempt bound to the agent; the older ones get `agent_id` null and a warning in the report.
3. Import then exports and compares. The fresh export must equal the input modulo the documented normalizations: key order, `schema: 2` and the `project` header. Otherwise the transaction is rolled back and the diff printed. This is the lossless check, run on real data before anything is committed.
4. Commit the export (now schema 2) with `theseus upgrade`'s refreshed skills, restart free agents so they load the new brief, and leave busy ones alone: their tasks deliver through the handoff fallback (section 7) and get credentials at their next restart or assignment.
5. Resume the queue and the driver.

Until a project is imported, v7 code refuses its task commands with "run `theseus tasks import`". Keeping two backends alive would double every code path. A hand edit of `tasks.json` after cutover is ignored by theseus. `theseus doctor` and the console report it, because the committed file then differs from an export of the store at `last_export`. This stays out of `theseus check`: between verified merges the store is always ahead of the committed export, and the guard and workers run `theseus check` on snapshots that cannot see the store.

Rollback:

- There is no per-project rollback, since v7 code has no file mode. Rollback is a version rollback.
- Version rollback: `theseus tasks export` for every imported project and commit, then `theseus db downgrade 6`, a v7 command that refuses unless every imported project's export is current. It drops the v7 tables and columns and sets `user_version = 6`, and the store stays otherwise intact. Then install the older theseus, which reads the schema 2 files. `theseus db restore <pre-v7 backup>` is the blunt alternative and loses the runtime rows written since.

## 11. Console

- Data source: the service, so the Host tab counts ready, held and review tasks across projects in one query.
- Holds: a task waiting on soft ownership shows `touches FILE (TASK)` in WAITING, as `needs profile` shows today.
- Agent popup, Task tab: the record from `get(..., history=True)`, worker notes and edit notes.
- PROJECT panel: `export` age (since `last_export`, yellow when merged work is not exported) and the last off-site backup.
- Palette: `edit task note`, `hold`, `release`, `reopen` keep running the CLI command of the same name, so the console never writes tables directly.

## 12. Skills and templates

- `theseus-worker` and `instance-brief.md`: "Task record: `theseus task show`" replaces `theseus tasks show TASK_ID`. Finishing gains step 3, `theseus task done --handoff .theseus/handoff.md` (or `task blocked REASON`), before the done line. `task note` records progress the coordinator can read. The task message section describes the pointer line. The rule "never print the environment" stays, and "never read or print the credentials file" joins it.
- `prompt-template.md` (headless and subagent): the same `task show` and `task done` lines, and for `claude-subagent`, `--task ID`.
- `theseus-coordinating`: drop "commit `tasks.json` before the task is assigned", the `tasks.json` hand-editing paragraph in `references/task-records.md` (replaced by `tasks edit`) and the advice that dispatch commits records. Add `--touches` guidance (name shared files a task will patch). Step 8 of the loop becomes "commit `docs/project/` including the export". `references/events.md` gets the `touches` hold.
- `AGENTS.md` template and `docs/workflow.md`, `docs/architecture.md` (the Host store section says tasks are not in the database; that reverses), `docs/configuration.md` (`offsite`).

## 13. Tests

A few tests that each protect a property, in the existing unittest style, against a temporary `THESEUS_DB`:

1. Round trip: import a fixture built from real record shapes (a trimmed sample of the opends2 archive with legacy keys, object contracts, null dependencies, all three `previousAttempts` kinds, plus laserlab-style records), export, import into a fresh store, export again. Both exports must be byte-identical and match the input modulo the documented normalizations.
2. Lifecycle through the service: add, claim, activate, deliver, merge, complete, with release, reopen and unmerge branches. The legacy `Queue` facade's document must match what the old code produced for the same sequence (the existing `test_flow.py` scenarios, retargeted).
3. Concurrency: two processes claim the same task, and two tasks for the same agent. Exactly one claim wins, by constraint, and no transaction stays open while a stubbed `create_worktree` sleeps.
4. Authorization: a worker principal cannot touch another task or run coordinator calls; a stale token after release fails with `not-your-task`; the role comes from credentials even when a client asks for coordinator.
5. Soft ownership: overlapping touches warn at add and hold at claim with the reason. Observed patch targets hold a third task while the first is in `verification`, and release it after merge.
6. Migration: a v6 database with projects, agents and events migrates to v7 with ids backfilled and nothing else changed. `db downgrade 6` restores the v6 shape. `prune` refuses a project with tasks.
7. MCP (skipped without the extra): an in-process client lists the worker tool set for worker credentials and calls `task_show`.

No new TUI tests; the existing waiting-reasons snapshot test (`test_tui.py`) gains a task held by touches.

## 14. Phased plan

Each phase is one to three reviewable commits on `task-store`, tests passing at each, and nothing touches the real store before phase 7.

0. Spikes (no product code): verify the MCP SDK's current server API and version, and stdio environment handling in Claude Code and Codex. Measure import and export time on a copy of opends2's files.
1. Schema v7, `taskrepo`, `taskservice` (all functions, principal checks), import and export with the round-trip test. No caller switched yet.
2. Callers switched: `tasks.py` mutations become service calls, the `Queue` read facade, `integrate`, `workqueue`, `dispatch` and `instance assign` lose their record commits, `rules` validates the export, and `top`, `rooms`, `gateway`, `docs/index`, `cleanup` and `agents.current_task` read the service. The per-project import gate and `tasks edit` land here. This is the largest phase; split it at the integrate and workqueue boundary.
3. Identity: credentials files, run secrets in `spawn` and `dispatch`, tokens at claim, `authenticate`, the worker `task` commands, `--json`, the pointer-line assignment, and the handoff fallback kept.
4. Soft ownership: `--touches`, observed touches at delivery, holds in `blocking()`, console reason.
5. MCP: `theseus mcp`, the extra, the per-agent config, the THESEUS_MCP change.
6. Export at verified merge, the hand-edit check in `doctor`, hourly and off-site backups, the `doctor` checks, `db downgrade 6`, `prune` guards; skills, templates and docs.
7. Cutover on this host, with the coordinator: upgrade stable and the store together, import laserlab first (small), then opends2 with the queue paused.

## 15. Risks and open questions

Risks:

- The shared host store and stable's `SchemaTooNew` lockout at the first v7 connect. Mitigated by `THESEUS_DB` for all development and a scheduled joint upgrade.
- Git stops being the live truth. A lost store loses everything since the last committed export. Mitigated by hourly and off-site backups and the export at each verified merge; residual window about an hour.
- Phase 2 is still blocked by `integrate.merge` and the queue reading worktrees and panes on the host. This design keeps the task API transport-ready but does not make worktrees remote.
- Workers forgetting `task done`: covered by the fallback for now. Without it, a forgotten call is a stall.
- Hand-made panes cannot get credentials (Q10).

Removing records from Git changes neither the guard nor dependencies on code. With `[git] base = origin/main`, a dependent task still needs its dependency's merged code pushed. So `verify --push` is not moot, while a records-only push is, since records no longer need pushing. The later rounds fit as follows. The attention list derives from attempts, holds, deliveries and runs with no new task state. `tasks complete --doc-only` is one `complete` flag. The pool budget and supervised driver and coordinator sessions do not touch these tables.

Questions for the coordinator:

- Q1. Project id: kept in the store and in the committed export header (recommended; `tasks_root` guards the live rows), or also written into `theseus.toml` `[project] id`?
- Q2. Per-assignment token for persistent agents: keep it, as designed, in the credentials file? Or rely on the run secret plus the store's agent-to-attempt mapping, which already rejects stale calls and drops the token file write from assignment?
- Q3. Export layout: three files in today's shape (recommended: rollback and older theseus read it, and the archive and history are append-only), or one Markdown file per task (`tasks/*.md`), which reviews better but breaks both?
- Q4. When to remove the handoff-file delivery fallback: one release after cutover, or keep it permanently as a safety net (it costs one file stat per busy agent per tick)?
- Q5. Coordinator principal: anonymous (no credentials, as designed) until the coordinator is a supervised session, or credentials from day one (`theseus coordinator login` writes a token file)? Anonymous means a worker whose MCP server does not receive its environment authenticates as the coordinator (section 5).
- Q6. Observed touches: only at delivery (as designed), or also from each running worktree's `.theseus/patches/` every tick, to catch overlap before delivery?
- Q7. Off-site directory and cadence: confirm the LBL storage path for `[store.backups] offsite` and whether hourly copies (not only daily) should go off-site.
- Q8. Worker questions: add `task note --question`, which also wakes `queue wait`, or keep the four approved worker commands and let questions go through a `partial` or `blocked` delivery as today?
- Q9. Coordinator MCP: expose merge, unmerge, verify and complete as tools that start the work and report through a queue event (as designed), or keep them CLI-only?
- Q10. Hand-made panes: allow the worktree-scoped fallback principal (identity by local path, local only), or require `agents migrate` to supervised agents before a project's cutover?

## 16. Decisions (coordinator review, 2026-11-18)

- Q1: the project id lives in the store and the export header only; `tasks_root` guards the live rows.
- Q2: no per-assignment token. A worker is authenticated by its run secret, and every worker call names the task it believes it owns (the pointer line gives it: `theseus task show X`, `theseus task done X --handoff ...`); the service refuses with `not-your-task` unless that task is the agent's active attempt. This catches a stale context after a release or reassignment without a file write at assignment. The attempt id stays in the schema, so a token can be added in phase 2 if the transport needs one.
- Q3: the export keeps today's three-file shape.
- Q4: the handoff-file delivery fallback stays permanently, level-triggered robustness over edge-triggered reports. Deliveries record `source` (`command` or `detected`), and the console shows detected ones so a skill that is not followed stays visible.
- Q5: deny by default. A caller is the coordinator only when no `THESEUS_AGENT_ID` or `THESEUS_CREDENTIALS` is set and the working directory is not a task worktree; anything else without valid credentials is refused, never promoted. The MCP server takes its role only from credentials or an explicit `--role coordinator` in the coordinator's own config, worker profiles that list `theseus` always use strict MCP config, and a worker server without credentials refuses to start.
- Q6: observed touches come from delivery and also from a read-only scan of each running worktree's `.theseus/patches/` every tick, since it is cheap and catches overlap before delivery. Holds stay computed.
- Q7: `offsite` is optional and empty by default; the path on this host is the user's decision. When set, daily and weekly copies go off-site (not hourly).
- Q8: add `task note --question`; an open question is an attention item that wakes `queue wait` and stays listed until the coordinator answers with `tasks note ID` or edits the task.
- Q9: coordinator MCP tools in this round are reads plus add, edit, hold, release, reopen and note. Merge, unmerge, verify and complete stay CLI-only until there is a clear need.
- Q10: no path-based identity. Hand-made panes read their task through the read-only `theseus tasks show ID` (reads need no credentials) and deliver through the permanent handoff fallback; `agents migrate` is recommended before cutover but not required.
- The dependency rule landing on main now (a dependency is satisfied only when its merge is an ancestor of the assignment base) moves into `ready()` and `claim()` in phase 2.

## 17. Phase 0 findings (2026-11-18)

### MCP Python SDK

Verified from the PyPI metadata and the source of the published wheel `mcp-2.3.0-py3-none-any.whl` (released 2026-10-02, requires Python 3.10 or later), not from a summary. The 2.x line started with 2.0.0 on 2026-07-28; 1.x is still maintained in parallel (1.30.0 on 2026-09-07).

- The high-level server is `mcp.server.mcpserver.MCPServer`, also exported as `mcp.server.MCPServer`. Tools register with the `@server.tool()` decorator (name, title, description, annotations, `structured_output`), and the input schema comes from the function signature. `server.run()` defaults to the stdio transport; `run_stdio_async()` is the async form.
- `FastMCP` is gone in 2.x: `mcp.server.fastmcp` exists only to raise `ModuleNotFoundError` with a pointer to the migration guide, so code written against 1.x tutorials fails at import, not silently.
- Tests can connect in process: `mcp.Client(server)` accepts an `MCPServer` instance as well as a URL or `StdioServerParameters`, then `await client.list_tools()` and `await client.call_tool(name, args)`. Test 7 in section 13 needs no subprocess.
- The dependency set is heavy for an optional extra: pydantic, starlette, uvicorn, sse-starlette, httpx2, jsonschema, pyjwt[crypto], opentelemetry-api and `mcp-types` pinned to the same version. That supports keeping it out of the base install.
- Pin: `theseus-lab[mcp]` depends on `mcp>=2.3,<3`. Only 2.3.0 was read, so the floor is the verified version rather than 2.0.

### Environment of stdio MCP servers

Each client was probed with a stub stdio server that writes its environment to a file. The probes ran against throwaway config directories (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) and dead API endpoints, so no real configuration changed and no prompt left the machine. The parent environment carried `THESEUS_AGENT_ID`, `THESEUS_CREDENTIALS` and an unrelated variable, and the server entry set one explicit `env` variable.

- Claude Code 2.1.327 passes the whole parent environment plus the entry's `env`. That held for a user-scope entry checked by `claude mcp list` and for `claude -p --strict-mcp-config --mcp-config FILE`, the path the per-agent config uses: the server saw all four variables, 131 in total.
- Codex CLI 0.167.0 passes only a fixed allow list (`HOME`, `LANG`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) plus the entry's `env` table. `THESEUS_AGENT_ID` and `THESEUS_CREDENTIALS` did not arrive. The entry's `env_vars = ["NAME", ...]` list forwards named variables from the parent, and with it both arrived.
- The SDK's own stdio client (`mcp.client.stdio.get_default_environment`) also passes only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` plus `env`, so any client built on it behaves like Codex.

Consequences:

- Writing `env = {"THESEUS_CREDENTIALS": <path>}` into each per-agent entry (section 5) is required, not a precaution. It is the only path that works for Codex and SDK-based clients. For Codex, `env_vars = ["THESEUS_AGENT_ID"]` could forward the agent id as well, but the credentials file already names the agent.
- The Q5 caveat is real for Claude Code. A worker that loads the coordinator's local-scope entry starts that server with the worker's own environment, `THESEUS_AGENT_ID` included. Under the deny-by-default rule (section 16), that server refuses to act as coordinator. It does not quietly become one, which is the outcome the decision wanted.

### Import and export on the real files

Measured with the phase 1 importer on copies of laserlab's and opends2's `tasks.json` and `tasks-archive.jsonl`, against a throwaway `THESEUS_DB`. Each run imported, ran the lossless check, exported, then imported that export into a second fresh store and exported again. Three runs on a shared, loaded workstation:

| Project | Records | Import with lossless check | Export | Re-import and export |
| --- | --- | --- | --- | --- |
| laserlab | 0 open, 116 archived (0.3 MB) | 0.03 to 0.28 s | under 0.06 s | 0.03 to 0.44 s |
| opends2 | 23 open (20 pending, 3 in progress), 1088 archived (2.8 MB) | 0.2 to 1.1 s | 0.03 to 0.26 s | 0.2 to 1.2 s |

- Both archives export byte for byte as imported. Each `tasks.json` differs only in the six header lines (`"schema": 2` and the `project` object). The second export is identical to the first. Import reported no warnings: the three in-progress opends2 tasks belong to three different agents.
- No normalization beyond the header was needed, not even key order. The records come in 22 (laserlab) and 51 (opends2) distinct key orders. The store keeps each imported record's order and the keys it lacks (section 18), so the first committed export is a six-line diff.
- Records needing a stored legacy shape: 9 of 116 in laserlab, 205 of 1111 in opends2 (mostly an absent `dependencies` or `sharedFilesReadOnly`). Records with values outside the columns: 0 and 59, which are the ad hoc keys plus twelve priorities written as strings (`"P1"` to `"P3"`).
- Both projects in one store file take 5.5 MB, against 3.1 MB of JSON text.

The real files also correct some figures in sections 9, 10 and 13:

- opends2 has 23 open tasks, 3 of them in progress, not 15 and 4. It has 1088 archived records, not 1086.
- There are no object contracts: `generalContract` is a string or absent (97 records). `dependencies` is absent (48 records), never null.
- `previousAttempts` comes in four kinds (released, superseded, unmerged, and reopened in laserlab) plus one ad hoc entry, `{owner, outcome, evidence}`.
- opends2's header has `updated` and no `currentMilestone` or `policy`.

## 18. Phase 1 implementation notes

Where phase 1 departs from sections 3 to 9, or fills in what they left open:

- Lossless storage. `tasks.extra` holds unknown keys and any known key whose value its column cannot reproduce exactly, such as `"priority": "P2"`. The new column `tasks.legacy` holds the keys an imported record lacked and its key order; `projects.tasks_legacy` does the same for the `tasks.json` header. A service write normalizes only the keys it changes, so untouched records keep their exported text. Section 10 step 3 can drop key order from the documented normalizations: what remains is `schema: 2`, the `project` header, and completed tasks moving to the archive.
- Schema additions not in the section 3 table:
  - `n` columns on `task_deps` and `task_paths`, since list order is part of the record;
  - a `superseded` attempt phase, the legacy force reassignment;
  - `source` on attempts and deliveries (Q4);
  - a `question` note kind and `task_notes.answered` (Q8).
- Schema left out: `token_hash` (Q2). `Principal` has no `attempt_id`, and worker calls name their task.
- Calls that touch files stay with the caller. Setting the handoff aside on reopen and unmerge is one (the service takes the new path as `handoff=`). The unintegrated-files check and `--from-log` of `complete` are others. The import-time backup is left to the CLI command in phase 2.
- `claim` refuses a held task unless forced, as section 7 says. Today `tasks assign` ignores `hold` (only the queue skips held tasks), so that is a behavior change for scripted assignment.
- `prune_projects` keeps any project with task rows, without asking; `--with-tasks` stays in phase 6. v7 takes the labelled `-pre-v7` backup before migrating a non-empty file.

Questions for the coordinator:

- Project identity. `projects` is keyed by name, so a name has exactly one id. "A different project reusing the name runs `tasks import --new-id`" (section 3) cannot work while the old project's tasks still reference that row. Phase 1 makes `--new-id` mint a fresh id, and an import over existing tasks requires `--replace`, which deletes them. Keeping both projects' tasks would need `projects` keyed by id.
- Reads and deny-by-default. Q5 refuses a caller in a worktree that has no credentials, while Q10 has hand-made panes read through `tasks show`. Phase 1 applies the principal check to mutating calls only: `get`, `list`, `ready` and `questions` take any principal.
- Section 9 says exported archive lines never change. `observe` on a completed task (allowed today while the task is still in `tasks.json`) rewrites that task's archive line. Either refuse it, or accept the change.
- Dispatch workers. The minted worker id becomes the attempt's `agent_id`, so their tasks now export an `ownerId`, which today's `tasks.json` omits for unregistered workers.
- The edit budget. Imported records reach 14 KB, so `edit` refuses only a change that grows a record past `TASK_RECORD_LIMIT`.
