---
name: theseus-coordinating
description: Use when acting as the coordinator of a theseus port - starting or resuming a session with no memory, reacting to an attention item or queue event (delivered, partial, blocked, stalled, error), reviewing and merging a delivery, a failed verification, planning or writing task records, or deciding whether something is verified.
---

# Coordinating a theseus port

You own the task records, the queue, integration (`theseus merge`), verification, commits and the governance files (`AGENTS.md`, `theseus.toml`, `docs/project/**`, `.agents/skills/**`). Workers own only their task's paths in their own worktrees and never commit. Repository state wins over memory: after any restart or compaction, orient again.

## 1. Orient

1. Read `AGENTS.md` and `docs/project/STATE.md`; `MANIFEST.md` and `ROADMAP.md` once per session. A user instruction recorded in STATE.md (such as "workers paused") overrides the MANIFEST: while paused, no dispatch and no new tasks, only review, planning and verification.
2. `git status`, `git log -5 --oneline`, `git worktree list`.
3. `theseus attention` (what needs you, each with a command), `theseus console --once`. As needed: `theseus tasks list --evidence`, `theseus agents ls`, `theseus queue status`, `theseus queue log`, `theseus status` (headless workers), `theseus verify --debt`.
4. Finish what is waiting before starting anything new: tasks in `verification` are unreviewed deliveries (section 3); `merged` tasks owe verification and completion.
5. With agents registered, `theseus agents up` runs the supervised queue driver (assigns, nudges, retries); `attention` lists it when down. As the supervised session (`theseus coordinator up`) you are registered: with `[attention] notify = "pane"` the driver types a `theseus:` line into it when something new needs you. Otherwise `theseus coordinator register` your tmux pane, or stream `theseus attention --follow`. Rely on these, not on re-arming `queue wait` (`references/events.md`).
6. Tell the user briefly what you found and will do next.

Never read `tasks-archive.jsonl` or `docs/project/archive/` whole; use `theseus tasks show ID`.

## 2. Loop

Take the first action whose trigger holds, then loop. Busy agents are never a reason to stop.

1. `theseus attention` at the start of every turn and after every action: handle its items (table below); answer a question with `theseus tasks note ID`.
2. A task in `verification`: integrate it (section 3).
3. Fewer ready tasks than free agents: write records (`references/task-records.md`), following the phase gates of the `theseus-recipe` skill. Unknown original behavior is a bounded research task, not a blocker.
4. `theseus verify --debt` near its limit: `theseus verify` before more merges.
5. Otherwise: review the riskiest recent merge with a read-only reviewer given only the diff and its invariants; confirmed findings become fix records.

No agents registered: dispatch by hand (`references/workers.md`).

## Items and events

An item stays listed until its cause is resolved. `theseus queue log` shows the events behind it; details: `references/events.md`.

| Event | Do |
| --- | --- |
| `delivered`, item `delivery` | Integrate (section 3). |
| item `merged`, `debt`, `export` | Section 3, steps 5-8. |
| `partial` | Read Limits. If what works passes its checks, integrate it, complete it with one `--limit` per unmet item, and add a follow-up record for the rest. To send it back instead, see events.md (move the handoff aside first). |
| `blocked` | Read what blocks it. Supply it (spec, decision, patch) and send it back, or release and rewrite the record; inputs only the human has: escalate. |
| `stalled` idle / after a nudge | `theseus agents logs NAME`, then one `theseus instance send NAME "..."` naming the missing step. Second stall: release and split. |
| `stalled` thrashing or context spent | `theseus agents restart NAME --fresh --with-task`. |
| `stalled` waiting for ... | A permission prompt: never `instance send` to it (its Enter can approve). See events.md. |
| `stalled` gateway CODE xN | The queue already retried with backoff; see events.md. |
| `error` | Read the detail: failed agent, startup dialog, fatal gateway error, failed assignment. See events.md. |

## 3. Integrate a delivery

1. Read the handoff (`theseus tasks show ID` gives `execution.logDir`), then `theseus tasks check-delivery ID`. Every `OUTSIDE` path must be in a worker patch (a file under the worktree's `.theseus/patches/`; a mention under Coordinator actions is not one), or reviewed and passed with `--accept PATH`, or reverted. Never merge it silently. A shared or governance file: revert it and make the change yourself on main, or ask for a patch; `--accept` only small changes you read.
2. Check the claims: run the named checks yourself, read the cited lines, prefer tests that assert independently derived values.
3. `theseus merge ID` applies the worktree three-way with its patches, regenerates the indexes and commits only the touched paths. Exit 3: refused or conflicting, main restored, nothing committed (events.md).
4. `theseus build` and `theseus smoke`.
5. `theseus verify`. FAIL: `theseus tasks unmerge ID --reason "..."` (events.md, failed verification); never complete it.
6. `theseus tasks observe ID --how "..."` only after you saw it work in the running application; `--user` only after the user tested it.
7. `theseus tasks complete ID --from-log VERIFY_LOG --summary "..."`, `theseus tasks retire ID`. `--from-log` takes only a passing `theseus verify` log of a commit that holds the merge; a documentation-only or other static check uses `--check "cmd -> outcome" --log PATH`.
8. Rewrite the affected STATE.md rows (replace, never append), then commit `docs/project/` (verify and complete write the export) before the next `theseus merge`, which refuses a checkout with uncommitted tracked files.

## Safety rules

- Commit locally as the loop requires. Push only when the user asked (a standing instruction in MANIFEST.md or STATE.md counts); the pre-push guard refuses unverified code. Never force-push, rewrite history or change remotes without asking.
- Workers never commit, stash, push or move HEAD. In a worker's worktree you only move a stale handoff aside, revert a rejected outside path, resolve a merge conflict or undo a worker commit (events.md).
- Clean and dirty rooms (`[rooms]`): every record gets `--room`; clean workers never see the original's code or dirty paths; the specification is the only channel and you review it for copied code before clean work starts. An agent moves from dirty to clean work only after a clear (the queue does it; `taint = "permanent"` forbids it).
- Secrets: never print the environment; never put credentials, tokens or local configuration in records, messages, reports or commits.
- Type into agents only with `theseus instance send`, never raw `tmux send-keys`. Never kill processes by name pattern; use `theseus stop NAME` or `theseus agents down NAME`.
- When the user asks to close or pause the project: `theseus close --commit --reason "..."` (it stops your own session last); push only if the user asked. Never close on your own initiative; `theseus reopen` brings it back.
- The original is read-only. Real hardware is touched only by a task whose record says so, and never with commands not first seen in recorded original traffic.
- Merged, observed and user-tested are three claims; never report one as another.

## Escalate to the human

Stop and ask when: a plan or milestone gate needs approval; a permission prompt or startup dialog needs an answer; credentials, budget or a fatal gateway error stop agents; hardware must be connected, powered or operated; a push, history rewrite, remote change or deletion of work is needed; a task needs an input the team cannot obtain; a task failed twice after splitting; main fails verification and no single recent merge explains it.

## References

- `references/events.md`: every event and failure path: send-backs, stalls, permission prompts, gateway errors, merge refusals and conflicts, failed verification.
- `references/workers.md`: headless, subagent and persistent workers; supervised agent commands; manual instance rounds.
- `references/task-records.md`: writing and changing records, rooms, profiles, capability.
- `references/maintenance.md`: docs folding, cleanup, the host store and backups, upgrades.
