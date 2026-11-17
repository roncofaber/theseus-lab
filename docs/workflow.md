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

**Long-running agents and the queue.** Let theseus start and supervise them ([Supervised agents](#supervised-agents) below), or start your agent CLI in tmux panes yourself and register each one:

```sh
theseus agents add worker-1 %3 --profile claude-code
```

Pane ids belong to one machine and one tmux server, so a `[[instances.panes]]` list that seeds the registry goes in the Git-ignored `theseus.local.toml`, never in the tracked `theseus.toml` (`theseus config check` warns when it finds one there).

`theseus queue run` (or `theseus queue wait` from the coordinator) then repeats this cycle:

- When an agent's task is done, the task moves to review. A task counts as done when the agent is idle, its handoff states a result, and the handoff has stopped changing.
- Before an agent takes its next task, its context is reset. Normally the queue compacts it; when it moves from reading original code to clean-room work, the queue clears it completely.
- A free agent gets the next ready task that it is allowed to work on. A task that requires a profile (`theseus tasks add --profile NAME`, `theseus tasks profile ID NAME`) goes only to agents of that profile, or to agents the queue can relaunch onto it because their profiles differ only in MCP servers or environment (see [Agent profiles](agent-profiles.md#per-task-profiles)).
- An agent that sits idle without a handoff gets one reminder, then is reported as stalled.
- An agent whose pane shows one of its profile's stall patterns (Claude Code: "Autocompact is thrashing") is shown as `thrashing`, even while it is still busy, and reported as stalled at once, without a reminder: more input only refills a context that no longer fits. Restart or clear it (`theseus instance restart PANE`, `theseus instance compact PANE --clear`) or split the task. A supervised agent is instead restarted on a fresh session at once, keeps its task and gets the task's restart prompt (a `restarted` event).
- An idle agent whose pane shows a gateway error of its profile (Claude Code: `API Error: 524 ...`, a Cloudflare `Error 52x`, `Resend your message to try again`) is in state `gateway_error` and gets no idle reminder. The status and `retry_after` are read from the error JSON when the pane still shows them; an Anthropic-format error type (`rate_limit_error`, `overloaded_error`) wins over the bare status. A retryable error (429 and every 5xx, 529 and 530 included) records a `gateway_error` event with the attempt and the next retry time, waits `max(retry_after, backoff)` (120, 240, 480 s; doubled for a 530 tunnel error; 429 resends are also staggered across the pool), then resends a short continue message (a `resent` event). From the second gateway error of a task, or when Claude Code's hint `/clear to save N tokens` shows at least `gateway.clear_tokens` (120000), the queue sends `/clear` and a restart prompt that names the task, its worktree, a rendered task prompt in the log directory and asks the agent to continue its partial work in place; the idle reminder does the same above that threshold. After three resends a `stalled` event (`gateway 524 x3`) wakes the coordinator; the count starts over only once the agent made progress after a resend (its worktree changed, or `gateway.recovery_seconds` passed), since a request that ends in a 524 shows the busy marker for minutes first; a fatal error (401, 403, 400, context too long, any other 4xx) records an `error` event at once and is never resent. The retry state lives in the host store, so a restarted coordinator picks it up, and ticks never sleep through a backoff.
- Host-wide circuit breaker: when agents of three different (project, agent) pairs hit retryable errors on one gateway pool within ten minutes, or any agent hits a 530, the queue of every project on the host stops assigning to that pool and stops resends and reminders there until `max(retry_after, first backoff)` has passed (one `gateway_paused` event). The pool is the profile's `pool`, else the profile name. `theseus top` shows the recent codes, last success and pause per pool (a GATEWAY line on the plain frame, a GATEWAY table on the Host tab).

Every message the queue types into a pane (task messages, reminders, resends, restart prompts) goes through one verified send: it empties the input line (Claude Code: Ctrl-U), types the text, presses Enter and checks within `instances.send_verify_seconds` (10) that the agent shows its busy marker; if the input line still holds the text it presses Enter once more and checks again, and otherwise records a `send_failed` event. Coordinators use the same path instead of raw `tmux send-keys`: `theseus instance send AGENT TEXT` or `theseus instance send AGENT --file PATH` (an agent name or a pane; it waits up to `--wait` seconds for an idle pane and exits 1 when the text was not submitted). `theseus instance send PANE TASK WORKTREE [DEADLINE]` still types a task message.

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
theseus agents down NAME     # graceful stop that stays stopped (--all, --force with a task, --now)
```

Agents start in the project's main checkout (already trusted by the agent CLI) and work in their task worktrees as before. Names are deterministic, so `[rooms]` lists, events and task owners keep working across restarts. The pane runs `theseus agents _exec`, which applies the profile's `env` and `interactive.env` in its own process and then becomes the agent: the pane's command line never holds a secret, and the profile's `command` must be an executable on `PATH` (an alias or shell function does not apply; `[supervisor] launch_shell = "login"` runs it through a login shell). For Claude Code theseus preassigns the conversation id (`--session-id`) and keeps it current from `claude agents --json`, which also tells it whether an agent is busy, idle or waiting for input.

There is no daemon. `theseus agents up`, and every queue tick of a managed project, reconcile toward the configuration: start missing agents, restart dead ones within the restart policy, report a changed launch configuration as `drift` without restarting (`theseus agents restart --drifted` applies it to free agents), stop agents whose slot was removed once they are free. After a reboot the first queue tick (or `theseus agents up`) brings the agents back. A crashed agent is recorded at once by a tmux hook (`exited`, with its exit status and its last screen in `<data>/agents/NAME/crash-GEN.txt`) and restarted after a backoff (`[supervisor.restart]`: 5, 30, 120, 600 seconds); after `max_crashes` within `window_minutes` it is `failed` and an `error` event wakes the coordinator until `theseus agents restart NAME`. An agent that holds a task resumes its conversation after a crash, a reboot or a lost server; one without a task starts fresh. When a resumed conversation no longer exists, or an agent is `thrashing`, it restarts on a fresh session, keeps its task and receives the task's restart prompt once it is ready. A startup dialog (the folder trust question, a new MCP server) is never answered: the agent becomes `blocked`, an `error` event names it, and `theseus agents attach NAME` lets you answer it. New agent states: `starting`, `blocked`, `failed`, and `waiting` (the agent waits for input such as a permission prompt; with a task it raises a `stalled` event).

Every run is a row in the host store (`agent_runs`: generation, intent, profile, a hash of the launch, session id, pid, exit status or signal, stop reason); events `spawned`, `ready`, `exited`, `restarted`, `stopped` and `drift` record the lifecycle. `theseus agents add|remove` refuse supervised names; `enable|disable` work as before. The dashboard's agent popup mirrors a supervised agent's pane and sends to it through the same verified send; Open tmux pane opens it in a new window of your own tmux.

**Moving hand-made panes over.** With `[agents] managed = true` set and the counts and `name` matching the registered names, `theseus agents up --dry-run` lists each live hand-made pane as `legacy`. `theseus agents migrate NAME...` (or `--all`) waits until the agent is free, reads its conversation id, types `/exit` into your pane once, and starts the supervised agent under the same name resuming that conversation; your pane is left at its shell prompt with a one-line note. Agents with a task are deferred unless `--with-task`. Remove the `[[instances.panes]]` entries afterwards (`theseus config check` lists them).

**Dashboard.** On a terminal `theseus top` opens an interactive dashboard of every project on the host; it also works outside a project. Any command that loads a project's configuration registers it in the host store, `~/.local/share/theseus/theseus.db` (`theseus projects` lists them). Every project has a stable accent color, picked by a hash of its name, used for its tab label, the borders of its sections and its name in host tables. Colors follow the active Textual theme (Ctrl+P, Theme): errors use the theme's $error, assigned the theme's $success, and the other activity kinds get computed hues kept away from the error hue and readable on the theme's background; ANSI themes use the terminal's own palette, and a 16-color terminal gets the exact standard colors so every kind keeps its own ANSI color. The Host tab shows the machine's resources (CPU as the mean of every thread with the busiest one, a history of the last minutes with a time axis, load average, memory, swap and, when `nvidia-smi` is present, one compact line per GPU with utilisation, memory and temperature, queried at most every 2 seconds), one activity chart of all projects together (events per 15 minutes over 12 hours, one color per project), a card per project (milestone progress, agents working of registered, waiting counts, pool use, last event), the shared pools with their holders, claims and any cap cut, GATEWAY (one row per recent gateway error: time, project, agent, task, the typed error such as `429 parallel cap`, `429 model TPM (model)`, `524 origin timeout`, `530 tunnel down` or `conn lost` in its own color, attempt n/N, the outcome or next retry, and the pool's state; the panel title sums up every pool), and the merged events. Sections shrink with the window and scroll on their own; on short terminals the activity charts hide, and on narrow ones the CPU history hides and the GPU block moves under the meters. Each project tab shows the milestone, repository and verification state, anything that needs attention, agents, waiting tasks, one activity chart with a series per kind group (assigned, delivered, partial, blocked, merged or completed, errors or stalls) and events. AGENTS shows `CPU (core %)` (percent of one core, so 250% is two and a half cores), `MEM (RAM)` (resident memory and its share of system RAM, yellow from 10%, red from 25%) and `context` (tokens in the agent's context after its last answer of the profile's `context_limit`, with a meter yellow from 50% and red from 80%; read incrementally from the Claude Code session transcript of the pane, else from the pane's `/clear to save N tokens` hint, marked `~`). No table row is highlighted until you click one, press a cursor key or press `a`, `w` or `e` (agents, waiting, events); Escape or a click elsewhere clears the highlight. Enter or a double-click on a row opens its details: a waiting task's full record, an event's untruncated detail with its linked task, a gateway error with its redacted error text, or an agent's state, usage and current task over a live mirror of its tmux pane (refreshed every second while open, scrollable sideways for wide panes) with a line to send text through the verified send, type-through mode (`i` or its button: every key goes to the pane, Ctrl-C asks first, Esc Esc or Ctrl-] leaves, the mirror refreshes four times a second), one-row buttons for Enter, Escape, Interrupt (Ctrl-C, after a confirmation) and /compact, and Open tmux pane (`o`, shown only inside tmux). Escape, Close or a click outside closes a popup. After that jump the tmux status line names the key that returns (last pane, window or session), and `theseus top --back` switches the same client back to the dashboard pane. tmux is needed only for that jump and for interactive agents. EVENTS sort by a header click or `s` (next column) and `S` (reverse), with an arrow in the sorted header. Other keys: `?` or F1 lists every key and mouse action, `/` filters by task, agent, kind, room or status (Esc clears), Tab or `1` to `9` switch tabs, `r` refreshes, `p` pauses or resumes the current project's queue, `c` shows per-core CPU on the Host tab, Enter or a click on a project card opens its tab, `q` quits. The resource history lives in memory only and restarts with the dashboard. `theseus top --interval SECONDS` sets the refresh (default 5), `--host --once` prints a plain host summary, and `--once` or `--json` print one project frame as before. The project view shows:
- the milestone's progress;
- anything that needs attention;
- host load;
- the repository, verification state and today's throughput (deliveries, completions, median time to handoff);
- each agent's task, how long it has been on it, how long since its last file change, and its CPU and memory;
- tasks waiting for review, verification or an agent;
- recent events.

Durations turn yellow and then red as they grow. Status changes linger for about 90 seconds so they are easy to follow: a task that just started stays in the waiting list as "started → agent", and an agent's row notes a recent delivery, merge, completion or context reset. Tasks with a priority (`theseus tasks add --priority N`, `theseus tasks priority ID N`) show a ▲N marker and are listed, and assigned, before others; equal priorities keep task-file order. `--once` prints a single frame and `--json` prints machine-readable output.

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
