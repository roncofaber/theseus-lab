# Workflow

This page explains how a theseus project runs: who does what, how a task moves from idea to merged code, and which rules the tooling enforces.

## Roles

- **Coordinator:** one agent session, working with you. It plans tasks, assigns them, reviews deliveries, merges, verifies and keeps the project state up to date. Only the coordinator commits.
- **Workers:** agent sessions that each take one task at a time. A worker edits only the files its task owns, in its own Git worktree, and ends by writing a handoff report.
- **You:** approve plans and decisions, provide hardware or files, and test results.

## Project layout

`theseus scaffold` creates:

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Rules every agent reads first: objective, where things are, team rules |
| `theseus.toml` | Project configuration; machine-specific values go in the Git-ignored `theseus.local.toml` |
| `docs/project/STATE.md` | Current snapshot of the project, rewritten as it changes |
| `docs/project/ROADMAP.md`, `MANIFEST.md` | Milestones and the coordinator's standing instructions |
| `docs/project/tasks.json` | Open tasks; completed ones move to `tasks-archive.jsonl` |
| `docs/investigations/` | One report per task, with a generated index |
| `docs/topics/` | One living document per subsystem; reports are merged into these over time |
| `.agents/skills/theseus-*` | Workflow skills for the coordinator and workers |
| `tools/Supervisor/` | Tests that enforce the project rules (`theseus check`) |
| `tools/git-hooks/pre-push` | Refuses pushes that break the rules or contain unverified code |

Original software, decompiled code, captures, logs and worktrees live in the data folder (`~/.local/share/<project>` by default), never in Git.

## Life of a task

1. **Add.** The coordinator writes a task with `theseus tasks add`: an ID, the files it owns (always including its report), a one-sentence objective, at most six acceptance criteria, optional dependencies and a milestone. Oversized or vague tasks are refused.
2. **Assign.** `theseus tasks ready` lists tasks whose dependencies are done and that belong to the current milestone. A task is started as a one-shot run (`theseus dispatch ID`) or given to a long-running agent (`theseus queue`, below).
3. **Work.** The worker edits only its owned files in its worktree. Changes to shared files (a build file, for example) go in `.theseus/patches/` as patches. It never commits.
4. **Hand off.** The worker writes `.theseus/handoff.md`: the result (`delivered`, `partial` or `blocked`), changed files with hashes, the exact checks it ran with their output, and known limits.
5. **Merge.** `theseus merge ID` checks that only owned files changed and the worker did not commit, applies the work and its patches, and commits. Any conflict leaves the main checkout untouched.
6. **Verify.** `theseus verify` runs the configured build and test commands on a clean checkout of the commit and writes a log.
7. **Complete.** `theseus tasks complete ID --from-log LOG` closes the task; `theseus tasks archive` moves it out of the open list.

Three claims are kept separate: *merged* (the code is in), *observed* (the coordinator saw it work, `theseus tasks observe`) and *user-tested* (you confirmed it, `--user`).

## Running workers

**One-shot runs.** `theseus dispatch ID` starts a worker in the background with the default agent profile. `theseus status` lists running workers; `theseus wait` returns when one finishes; `theseus followup NAME "message"` resumes a finished worker with new instructions; `theseus stop NAME` ends one.

**Long-running agents and the queue.** Start your agent CLI in tmux panes and register each one:

```sh
theseus agents add worker-1 %3 --profile claude-code
```

`theseus queue run` (or `theseus queue wait` from the coordinator) then repeats this cycle:

- When an agent's task is done, the task moves to review. A task counts as done when the agent is idle, its handoff states a result, and the handoff has stopped changing.
- Before an agent takes its next task, its context is reset. Normally the queue compacts it; when it moves from reading original code to clean-room work, the queue clears it completely.
- A free agent gets the next ready task that it is allowed to work on.
- An agent that sits idle without a handoff gets one reminder, then is reported as stalled.

`theseus queue wait` exits as soon as something needs the coordinator (a delivery, a stall, an error), so the coordinator can run it in the background and react. `theseus queue pause|resume` stops and restarts assignment, and a task with `hold` set is never assigned.

**Dashboard.** `theseus top` shows what needs attention, host load, repository and verification state, each agent's state and resource use, the queue, and recent events. `--once` prints a single frame and `--json` prints machine-readable output.

## Verification and pushing

- `theseus build` and `theseus smoke` run quick checks in the working copy; `theseus verify` runs the full set on a clean checkout.
- The pre-push hook refuses code that no passing `theseus verify` covers. To push urgently anyway, set `THESEUS_PUSH_UNVERIFIED="reason"`. The hook logs the override and allows it only for a limited number of commits (`max_unverified_merges`) and hours (`max_unverified_hours`).
- If a check kind has no command configured, it reports `NOT CONFIGURED` (exit code 4), never a silent pass.

## Evidence and reports

- Every statement in a report says where it comes from: recovered from the original, read from source data, measured, reconstructed, or a test fixture.
- Reverse-engineering lookups go cheapest first: the citation index, then the indexed decompiled code (`theseus re find|show|callers|callees`), then a live decompiler. Each binary is decompiled once.
- Questions about runtime behavior are answered by running the original in isolation, in a compatibility layer or a VM, and recording what it does. Captures are cited as `cap_<hash>@<frame>`.
- Reports are merged into topic documents over time (`theseus docs fold-check`, `fold-apply`). Folding refuses to drop a citation.

## Clean-room mode

When the new implementation will be published, enable `[rooms]` in `theseus.toml`:

- **Dirty tasks** read the original's code and write a specification in their own words, covering only externally visible behavior: formats, messages, timing, errors.
- **Clean tasks** implement from that specification, captures and public documentation only. Their worktrees do not even contain the dirty files.

An agent that worked a dirty task must have its context cleared (`theseus instance compact PANE --clear`) before it may take clean work. The queue does this automatically. With `taint = "permanent"`, an agent that has worked a dirty task never takes clean work.

## Rules enforced by `theseus check`

- Tasks are well formed, owned, and within the current milestone.
- Reusable scripts are in the repository, not in temporary folders.
- Shell scripts pass shellcheck.
- No developer-specific paths in tracked files.
- Skills and instruction files stay within their size budgets.
- In clean-room mode, every task has a room and clean tasks never own dirty paths.
