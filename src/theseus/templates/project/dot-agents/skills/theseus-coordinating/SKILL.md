---
name: theseus-coordinating
description: Use when acting as the coordinator of a theseus port - starting or resuming a session, a worker finishing or stalling, integrating a slice, planning or writing task records, choosing the next round, or deciding whether something is verified.
---

# Coordinating a theseus port

The coordinator owns the queue, dispatch, integration, verification, commits, pushes and the governance files. Workers own only their task paths and never commit.

## Session start

1. `AGENTS.md`, `docs/project/STATE.md`, and once per session `docs/project/MANIFEST.md` and `docs/project/ROADMAP.md`.
2. `theseus tasks list --evidence`, `theseus status` (dispatched workers and every other worktree) and `theseus verify --debt`. Never read `tasks-archive.jsonl` or `docs/project/archive/` whole; use `theseus tasks show ID`.
3. Reality check: `git status`, `git log -5 --oneline`, `git worktree list`. Repository state wins over memory.

**Precedence:** a later user instruction recorded in STATE.md (for example "workers paused") overrides the MANIFEST's keep-going loop. While paused: no dispatch, no follow-ups; review, planning and verification only.

## Worker providers

Workers run under an agent profile (`theseus agents profiles`; `workers.default` and `[workers.<profile>]` in `theseus.toml`).

| Mode | Launch | Follow up / stop |
| --- | --- | --- |
| Headless (profiles with a headless command) | `theseus dispatch ID [--provider PROFILE]`; keep `theseus wait` running in the background | `theseus followup NAME MSG` (profiles with resume) / `theseus stop NAME` |
| Subagent (`claude-subagent`, opt in with a capacity) | `theseus dispatch ID --provider claude-subagent`, then launch a subagent whose prompt says to read and follow the printed `prompt.md` | your agent's own messaging and stop tools |
| Persistent instances in tmux | `theseus agents add NAME PANE --profile PROFILE`, then the queue | `theseus instance send|compact|restart` |

`theseus tasks stats` compares profiles (results, minutes to handoff, follow-ups). Size capacity to your plan's rate limits.

## Queue

With agents registered (`theseus agents`), the queue does dispatch for you: write ready task records, keep `theseus queue wait` running in the background, and act on what it reports (`delivered`: review and `theseus merge`; `stalled` or `error`: follow up). `theseus top --once` shows agents, queue, reviews, repository and verification at a glance. The manual commands below remain for one-off control.

## Persistent instance rounds

Instances are long-lived worker sessions in tmux panes listed in `[[instances.panes]]`. They follow `.agents/skills/theseus-worker/instance-brief.md`.

1. `theseus instance compact PANE` between tasks (a session cannot compact itself). `theseus instance restart PANE` resumes the same session in a fresh process, which is how instances pick up a new MCP server, skill or setting (for example after `theseus setup rea`).
2. `theseus instance assign TASK INSTANCE SUFFIX` creates the worktree and branch and commits the assignment.
3. `theseus instance send PANE TASK WORKTREE [DEADLINE]` types the task message once the pane is idle.
4. `theseus instance watch` under a monitor prints `HANDOFF name` and `IDLE-NO-HANDOFF name`.
5. `theseus merge TASK` (exit 3: conflict, nothing committed), then `theseus build`, `theseus smoke`.

## Round loop

Take the first action whose trigger holds, then loop. Occupied slots are never a reason to stop.

1. **Integrate** a delivered slice (handoff present): the checklist below.
2. **Unstick** a worker idle past its budget, `FAILED` in `theseus status`, or finished without a handoff: one follow-up naming the missing step, file or failing check. Second stall: stop it, keep the worktree, `theseus tasks release ID --reason "..."` and redispatch smaller.
3. **Dispatch** while slots are free: `theseus tasks ready`, rank, `theseus dispatch ID --dry-run`, read the prompt, dispatch.
4. **Plan** when fewer tasks are ready than free slots: write records (rules below), following the phase and gate in the `theseus-recipe` skill. Unknown original behavior is a bounded research task, not a blocker.
5. **Verify** when `theseus verify --debt` approaches its limit: run `theseus verify` before more code-only merges.
6. **Review** with slots full: audit the riskiest recent integration with a read-only reviewer given only the diff and the invariants it must keep; verify findings yourself; confirmed ones become fix records.

**Ranking:** (1) inside the current milestone, (2) unblocks the most dependents, (3) general contract over fixture, (4) ownership disjoint from running work, (5) smaller first.

## Integration checklist

1. On a subagent handback, stop the agent. Read the handoff and `theseus tasks check-delivery ID`. Anything OUTSIDE is rejected or moved to a patch, never merged silently.
2. Check claims against code and evidence: cited lines, evidence IDs in the bundles, tests that assert independently derived values.
3. `theseus merge ID` applies the delivery three-way, applies the worker's patches, regenerates the indexes and commits exactly the touched paths; any conflict restores main. It refuses a worktree whose HEAD moved: workers never commit.
4. `theseus build` and `theseus smoke`; record the outcome.
5. Runtime evidence is separate: `theseus tasks observe ID --how "..."` after you saw it work in the running application; `--user` after the user tested it.
6. `theseus tasks complete ID --log PATH --check "cmd -> outcome" --summary "..."` (or `--from-log` with a passing `theseus verify` log), then `theseus tasks archive` and `theseus tasks retire ID`.
7. Rewrite the affected `STATE.md` rows (replace, never append), commit, push.

## Writing task records

`theseus tasks add ID --role R --owned PATHS... --objective "..." --contract "..." --accept ITEMS... [--depends IDS] [--milestone M] [--profile P]`.

- Size for one worker session: one subsystem boundary, one report under `docs/investigations/`.
- `--profile P` only when the task needs what that profile adds (an MCP server's session state); the queue relaunches a compatible agent onto it and back afterwards.
- Acceptance items are observable: a focused command that passes, a named variation covered, an explicit unsupported result. At most six per record, 6 KB per record.
- Never scope a task as a scenario slice; the scenario is one acceptance fixture of a general contract.
- A change to an interface owns its consumers: `theseus tasks consumers ID` lists files outside `ownedPaths` that use names declared inside.
- `researchRequired` and `externalBlocker` each need `evidence` and `unblockCondition`; `externalBlocker` is only for inputs the team cannot obtain.
- With `[rooms]` enabled, give every record `--room dirty` (reads the original's code, writes the specification) or `--room clean` (implements from the specification and captures only). A dirty instance takes clean work only after `theseus instance compact PANE --clear` (a summarizing compact is not enough); assignment refuses it until then. The specification is the only channel between rooms: review it for copied code before clean workers read it.

## Hygiene

- Fold sweep: reports name a `Topic:`; periodically fold them into `docs/topics/` with a documentation worker, then `theseus docs fold-apply --topic NAME` and `theseus docs index`.
- Evidence, logs and handoffs go under the data root (durable); scratch under `paths.scratch`.
- `theseus cleanup` (dry run) after a batch of integrations; `--apply` keeps anything an open task references and saves evidence-like files first.
- Never kill processes by name pattern; `theseus stop NAME` ends a worker's slice.

## Red flags

| Thought | Reality |
| --- | --- |
| "All slots are busy, I'll wait" | Review, plan or verify; the loop has work |
| "The worker says it passed" | Run it yourself before accepting |
| "It is merged, so it works" | Merged is not observed; observed is not user-tested |
| "One more code-only merge is fine" | Check `theseus verify --debt`; the full pass is owed |
| "This needs original behavior we don't have, blocked" | Dispatch a research task with `theseus-native-re` or `theseus-reference-runs` |
| "I'll keep this helper in scratch" | Reusable scripts live in the repository |
| "I'll append a paragraph to STATE.md" | Replace the stale section; detail goes in the task result |
