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
5. **Merge.** `theseus merge ID` checks that only owned files changed and the worker did not commit, applies the work and its patches, and commits. Any conflict leaves the main checkout untouched. A worktree HEAD moved only onto commits of the base branch (a rebase) is recorded as the new base, as `theseus tasks rebase ID` does by hand. Outside-owned worktree changes are compared with the cumulative result of all shipped patches applied in order to the worktree base; when they equal it, the patches are applied once onto main. The patches are authoritative: in-place copies that differ are refused, and when the base is older than main the refusal names the files to revert in the worktree.
6. **Verify.** `theseus verify` runs the configured build and test commands on a clean checkout of the commit and writes a log. A merged task that fails verification goes back with `theseus tasks unmerge ID --reason TEXT`: it reverts the merge commit (regenerating the indexes when they conflict, refusing any other conflict) and returns the task to in progress with its worktree, so the worker redelivers and `theseus merge ID` runs again. With `--no-revert` it only checks that HEAD no longer holds the merge and updates the record.
7. **Complete.** `theseus tasks complete ID --from-log LOG` closes the task; `theseus tasks archive` moves it out of the open list.

Three claims are kept separate: *merged* (the code is in), *observed* (the coordinator saw it work, `theseus tasks observe`) and *user-tested* (you confirmed it, `--user`).

## Running workers

**One-shot runs.** `theseus dispatch ID` starts a worker in the background with the default agent profile. `theseus status` lists running workers; `theseus wait` returns when one finishes; `theseus followup NAME "message"` resumes a finished worker with new instructions; `theseus stop NAME` ends one.

**Long-running agents and the queue.** Let theseus start and supervise them ([Supervised agents](#supervised-agents) below), or start your agent CLI in tmux panes yourself and register each one:

```sh
theseus agents add worker-1 %3 --profile claude-code
```

Pane ids belong to one machine and one tmux server, so a `[[instances.panes]]` list that seeds the registry goes in the Git-ignored `theseus.local.toml`, never in the tracked `theseus.toml` (`theseus config check` warns when it finds one there).

`theseus queue run` (or `theseus queue wait` from the coordinator) then repeats this cycle:

- When an agent's task is done, the task moves to review. A task counts as done when the agent is idle, its handoff states a result, and the handoff has stopped changing.
- Before an agent takes its next task, its context is reset. Normally the queue compacts it; when it moves from reading original code to clean-room work, the queue clears it completely.
- A free agent gets the next ready task that it is allowed to work on. A task that requires a profile (`theseus tasks add --profile NAME`, `theseus tasks profile ID NAME`) goes only to agents of that profile, or to agents the queue can relaunch onto it because their profiles differ only in MCP servers or environment (see [Agent profiles](agent-profiles.md#per-task-profiles)). A task with a minimum capability (`--min-capability N`, `theseus tasks capability ID N`) goes only to agents whose profile `capability` is at least N, unless they are registered with exactly the task's profile (see [Capability](agent-profiles.md#capability)).
- An agent that sits idle without a handoff gets one reminder, then is reported as stalled.
- An agent whose pane shows one of its profile's stall patterns (Claude Code: "Autocompact is thrashing") is shown as `thrashing`, even while it is still busy, and reported as stalled at once, without a reminder: more input only refills a context that no longer fits. Restart or clear it (`theseus instance restart PANE`, `theseus instance compact PANE --clear`) or split the task. A supervised agent is instead restarted on a fresh session at once, keeps its task and gets the task's restart prompt (a `restarted` event).
- An idle agent whose pane shows a gateway error of its profile (Claude Code: `API Error: 524 ...`, a Cloudflare `Error 52x`, `Resend your message to try again`) is in state `gateway_error` and gets no idle reminder. The status and `retry_after` are read from the error JSON when the pane still shows them; an Anthropic-format error type (`rate_limit_error`, `overloaded_error`) wins over the bare status. A retryable error (429 and every 5xx, 529 and 530 included) records a `gateway_error` event with the attempt and the next retry time, waits `max(retry_after, backoff)` (120, 240, 480 s; doubled for a 530 tunnel error; 429 resends are also staggered across the pool), then resends a short continue message (a `resent` event). From the second gateway error of a task, or when Claude Code's hint `/clear to save N tokens` shows at least `gateway.clear_tokens` (120000), the queue sends the profile's clear command (Claude Code: `/clear`) and then the task's restart prompt (below); the idle reminder does the same above that threshold. After three resends a `stalled` event (`gateway 524 x3`) wakes the coordinator; the count starts over only once the agent made progress after a resend (its worktree changed, or `gateway.recovery_seconds` passed), since a request that ends in a 524 shows the busy marker for minutes first; a fatal error (401, 403, 400, context too long, any other 4xx) records an `error` event at once and is never resent. The retry state lives in the host store, so a restarted coordinator picks it up, and ticks never sleep through a backoff.
- Host-wide circuit breaker: when agents of three different (project, agent) pairs hit retryable errors on one gateway pool within ten minutes, or any agent hits a 530, the queue of every project on the host stops assigning to that pool and stops resends and reminders there until `max(retry_after, first backoff)` has passed (one `gateway_paused` event). The pool is the profile's `pool`, else the profile name. The console shows the recent codes, last success and pause per pool (a GATEWAY line on the plain frame, a GATEWAY table on the Host tab).
- An agent whose context was cleared while it holds a task (the fresh-context recovery above, a supervised agent restarted on a fresh session, `theseus agents restart NAME --fresh --with-task`) is owed one restart prompt: the task id with its objective and owned paths, the worktree, the rendered task prompt, the partial report and `notes.md` when they exist, and how to resume (check `git status` and `git diff`, read large files in line ranges, work in small steps). Until the agent confirms it (the verified send below), nothing else is typed into it: no reminder, no gateway resend (a scheduled one is called off) and no new task. An unconfirmed prompt is retried on the next ticks, and after `gateway.attempts` tries a `stalled` event wakes the coordinator; the plain continue message never stands in for it. `--file PROMPT` sends your own text as that one prompt.

Every message the queue types into a pane (task messages, reminders, resends, restart prompts) goes through one verified send: it empties the input line (the profile's `clear_input`, Claude Code: Ctrl-U), types the text, presses Enter and checks within `instances.send_verify_seconds` (10) that the agent shows its profile's busy marker; if the input line still holds the text it presses Enter once more and checks again, and otherwise records a `send_failed` event. A tick types all its messages first and verifies them together, in a small pool of threads, within `queue.send_budget_seconds` (25), so agents that never respond cost one verification window per tick rather than one each; a send still unresolved then is finished by the next tick, where a text that has left the input line counts as submitted. A profile without a busy marker gets its messages typed unverified. Coordinators use the same path instead of raw `tmux send-keys`: `theseus instance send AGENT TEXT` or `theseus instance send AGENT --file PATH` (an agent name or a pane; it waits up to `--wait` seconds for an idle pane and exits 1 when the text was not submitted). `theseus instance send PANE TASK WORKTREE [DEADLINE]` still types a task message.

`theseus queue wait` exits as soon as something needs the coordinator (a delivery, a stall, an error), so the coordinator can run it in the background and react; gateway retries, resends, `send_failed` and pauses do not wake it until the resends run out or the error is fatal. Between ticks it sleeps until the next `--interval` or the earliest pending wake time, whichever comes first: a scheduled gateway resend or the end of a supervised agent's restart backoff, so those fire within a second or two of their time (`queue run` does the same). `theseus queue pause|resume` stops and restarts assignment, and a task with `hold` set is never assigned.

### Supervised agents

With `[agents] managed = true` theseus starts the project's long-running agents itself, each in a hidden pane of a private tmux server (`tmux -L theseus`, one per host user, separate from your own tmux and configured by theseus, never by your `~/.tmux.conf`). Put the counts in the Git-ignored `theseus.local.toml`, since how many agents a machine runs is a per-machine choice; `[defaults] agents.counts` in the host configuration applies to projects that set none:

```toml
# theseus.local.toml
[agents]
managed = true
name = "{project}-{profile}-{n}"   # the default; it must contain {n}

[agents.counts]
claude-code = 2
```

```sh
theseus agents up            # start the missing agents, wait until each is ready, report each one
theseus agents ls            # state, profile, generation, pid, task, restarts, drift, uptime, session id
theseus agents attach NAME   # the agent's own pane in this terminal; C-q detaches (--read-only, --window)
theseus agents logs NAME     # its screen and history (--transcript prints the conversation file)
theseus agents restart NAME  # graceful restart, resuming the conversation (--fresh: a new one; --drifted)
theseus agents restart NAME --fresh --with-task [--file PROMPT]   # a new conversation for an agent with a task
theseus agents scale PROFILE N   # set [agents.counts] in theseus.local.toml and reconcile
theseus agents down NAME     # graceful stop that stays stopped; it leaves the count (--replace, --all pauses, --force, --now)
```

A count is the number of live agents of that profile; agents that are down or disabled do not count, and names need not be contiguous. Scaling down stops live agents only, free ones first and then the most recently started, never by slot number; a busy one is disabled and stops once free. Scaling up, or replacing an agent taken out by `down NAME`, starts the lowest name never used or since removed; a downed name stays down until `theseus agents up NAME`. So with an unchanged count the next reconcile replaces a downed agent under another name, and `down --replace NAME` does it at once. A disabled agent keeps running uncounted. For example, with `gw = 4`, `agents down P-gw-2` and then `agents scale gw 2` stop `P-gw-4` (the newest) and leave `P-gw-1` and `P-gw-3` running. `theseus agents down --all` (also the console's Stop all agents) stops every agent and pauses supervision for the project: no reconcile, queue tick or crash hook starts anything until `theseus agents up` resumes it; `theseus agents ls` and the project panel show `paused`.

`theseus agents restart NAME --fresh` refuses an agent that holds a task; with `--with-task` it restarts it on a new conversation and sends it the task's one restart prompt (above) before anything else.

Agents start in the project's main checkout (already trusted by the agent CLI) and work in their task worktrees as before. Names are deterministic, so `[rooms]` lists, events and task owners keep working across restarts. The pane runs `theseus agents _exec`, which applies the profile's `env` and `interactive.env` in its own process and then becomes the agent: the pane's command line never holds a secret, and the profile's `command` must be an executable on `PATH` (an alias or shell function does not apply; `[supervisor] launch_shell = "login"` runs it through a login shell). With a profile's `session_flag` and `status_command` (Claude Code: `--session-id` and `claude agents --json`) theseus preassigns the conversation id, keeps it current and learns whether an agent is busy, idle or waiting for input; without them readiness and busy come from the screen (see [what depends on the preset](agent-profiles.md#what-depends-on-the-preset)).

There is no daemon. `theseus agents up`, and every queue tick of a managed project, reconcile toward the configuration: start missing agents, restart dead ones within the restart policy, report a changed launch configuration as `drift` without restarting (`theseus agents restart --drifted` applies it to free agents), stop agents beyond the count once they are free (only when the project's files, read again at that moment, no longer want them, so a long-running queue never retires an agent another command just added). `theseus queue run` and `queue wait` read the project and host configuration again whenever one of their files changes. `theseus agents down --force` does not wait for a busy agent: it ends the agent's process (SIGTERM, then SIGKILL after 5 seconds) and records the exit. After a reboot the first queue tick (or `theseus agents up`) brings the agents back. A crashed agent is recorded at once by a tmux hook (`exited`, with its exit status and its last screen in `<data>/agents/NAME/crash-GEN.txt`) and restarted after a backoff (`[supervisor.restart]`: 5, 30, 120, 600 seconds); after `max_crashes` within `window_minutes` it is `failed` and an `error` event wakes the coordinator until `theseus agents restart NAME`. An agent that holds a task resumes its conversation after a crash, a reboot or a lost server; one without a task starts fresh. When a resumed conversation no longer exists, or an agent is `thrashing`, it restarts on a fresh session, keeps its task and receives the task's restart prompt once it is ready. A startup dialog (the folder trust question, a new MCP server) is never answered: the agent becomes `blocked`, an `error` event names it, and `theseus agents attach NAME` lets you answer it. New agent states: `starting`, `blocked`, `failed`, and `waiting` (the agent waits for input such as a permission prompt; with a task it raises a `stalled` event).

Every run is a row in the host store (`agent_runs`: generation, intent, profile, a hash of the launch, session id, pid, exit status or signal, stop reason); events `spawned`, `ready`, `exited`, `restarted`, `stopped` and `drift` record the lifecycle. `theseus agents add|remove` refuse supervised names; `enable|disable` work as before. The dashboard's agent popup mirrors a supervised agent's pane and sends to it through the same verified send; `o` (or Ctrl+P, Open in my tmux) opens it in a new window of your own tmux.

**Moving hand-made panes over.** With `[agents] managed = true` set and the counts and `name` matching the registered names, `theseus agents up --dry-run` lists each live hand-made pane as `legacy`. `theseus agents migrate NAME...` (or `--all`) waits until the agent is free, reads its conversation id, types the profile's exit command (Claude Code: `/exit`) into your pane once, and starts the supervised agent under the same name resuming that conversation; your pane is left at its shell prompt with a one-line note. Agents with a task are deferred unless `--with-task`. Remove the `[[instances.panes]]` entries afterwards (`theseus config check` lists them).

**Console.** On a terminal `theseus` alone (or `theseus console`, also `theseus top`) opens the control console of every project on the host; it also works outside a project. `theseus console --once` and `--json` print one frame for scripts. Any command that loads a project's configuration registers it in the host store, `~/.local/share/theseus/theseus.db` (`theseus projects` lists them). Every project has a stable accent color (its tab, borders and name in host tables), and colors follow the active Textual theme. Panels carry short labels only; every explanation is in the `?` help.

- Host tab: PROJECTS, one row per project with its key figures (milestone progress, coordinator state, queue watcher, ready, in progress, review with the age of the oldest delivery, the first item needing attention, agents working and in trouble, pool slots with a cap cut, gateway, merges today; Enter opens its tab), the machine (CPU with history, load, memory, swap, GPUs), activity over 12 hours, pools, gateway errors and the merged events.
- Project tab: PROJECT, laid out in aligned labeled columns (fewer per row on a narrow terminal): milestone, branch, verification and queue state; the coordinator (state, last activity, context use) and the queue watcher (`queue wait` or `run`, its last tick and next wake, from a heartbeat it writes each tick); ready, held, blocked, in progress, review and attention counts; agents by state, pool slots and gateway; today's merges, completions and deliveries. Then AGENTS, WAITING, ACTIVITY and EVENTS.
- AGENTS shows short names (`claude-code-2` for a supervised slot, the name without the project prefix otherwise), state, task, CPU, memory, context, uptime, restarts, session, model and pool; optional columns drop out on narrow terminals. Every table keeps stable column widths that follow the window, and a cut cell ends in an ellipsis; double-click or Enter shows the whole row.
- An agent's popup lists its fields (name, profile, model, launcher, short session, pool, pane, generation, restarts, uptime, worktree, context, with `~` marking an estimated context) over five tabs: Live (the pane mirror, a send line through the verified send and type-through with `i`; while it is open a supervised agent's window on the private server takes the mirror's size, following it about 200 ms after the console is resized, so the agent redraws to fit without sideways scrolling, and gets its `[supervisor] size` back when the popup closes; hand-made panes on your own tmux keep their size and scroll sideways), Conversation (`c`; the agent's conversation from its transcript adapter, see [Agent profiles](agent-profiles.md#transcript-adapters): user and assistant messages, each tool call and result folded to one line that a click or Enter expands, thinking left out, following new entries unless you scroll up; for a CLI without an adapter the pane's scrollback, or no tab), Diff (the task worktree against its base, refreshed every 5 seconds while shown), Task (the record and its handoff) and Events. Enter attaches this terminal to a supervised agent's own pane (the console suspends; C-q detaches back), `o` opens it in a new window of your own tmux, and `R`, `X`, `U` restart, stop and start it after a confirmation. `C` (or a double-click on PROJECT) opens the coordinator's mirror and send line.
- Ctrl+P opens the action palette for the active project: queue tick, agents up, add or remove an agent (`theseus agents scale PROFILE N`, written to `theseus.local.toml`), restart, stop, start or attach an agent, dispatch, hold, release, change the priority or the minimum capability of or reopen a task, hold or release a pool slot, open the coordinator or a delivered task's handoff. Each action runs the theseus command of the same name in a worker and reports its last line; destructive ones ask first.
- Other keys: `?` or F1 help, `/` filter (Esc clears), Tab or `1` to `9` tabs, `r` refresh, `p` pause or resume the queue, `c` per-core CPU, `a`, `w`, `e` highlight a table, `s`/`S` sort EVENTS, `q` quit. After a jump to a hand-made pane, `theseus console --back` switches the same tmux client back.

`theseus console --once` prints a plain project frame with:

- the milestone's progress;
- anything that needs attention;
- host load;
- the repository, verification state and today's throughput (deliveries, completions, median time to handoff);
- each agent's task, how long it has been on it, how long since its last file change, and its CPU and memory;
- tasks waiting for review, verification or an agent;
- recent events.

Durations turn yellow and then red as they grow. Status changes linger for about 90 seconds so they are easy to follow: a task that just started stays in the waiting list as "started → agent", and an agent's row notes a recent delivery, merge, completion or context reset. Tasks with a priority (`theseus tasks add --priority N`, `theseus tasks priority ID N`) show a ▲N marker and are listed, and assigned, before others; equal priorities keep task-file order. A task no agent may take shows why: `needs profile NAME`, `needs capability >= N`, a full pool or profile, or busy agents. `--once` prints a single frame and `--json` prints machine-readable output.

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
