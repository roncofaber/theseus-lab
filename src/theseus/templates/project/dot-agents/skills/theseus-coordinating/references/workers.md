# Workers

Workers run under an agent profile (`theseus agents profiles`; `workers.default` and `[workers.<profile>]` in `theseus.toml` or the host `config.toml`). Capacity per profile is `workers.<profile>.capacity`; size it to the plan's rate limits. `theseus tasks stats` compares profiles by results and time to handoff.

| Mode | Start | Follow up / stop |
| --- | --- | --- |
| Persistent, supervised | `[agents] managed = true` and `[agents.counts]` in `theseus.local.toml`, then `theseus agents up`; the queue assigns | `theseus instance send NAME TEXT`, `theseus agents restart|down NAME` |
| Persistent, hand-made pane | start the agent CLI in a tmux pane, `theseus agents add NAME PANE --profile P`; the queue assigns | `theseus instance send|compact|restart NAME` |
| Headless one-shot | `theseus dispatch ID [--profile P]`, then keep `theseus wait` running | `theseus followup NAME MSG` (profiles that resume), `theseus stop NAME` |
| Subagent (`claude-subagent`, needs a capacity) | `theseus dispatch ID --profile claude-subagent`, then launch a subagent told to read and follow the printed `prompt.md` | your agent's own messaging and stop tools; stop it once it hands back |

Before a by-hand dispatch: `theseus tasks ready`, rank, `theseus dispatch ID --dry-run` and read the rendered prompt. Dispatch writes the worker's credentials; a subagent's prompt says how to pass them to `theseus task`. `theseus status` lists headless workers (`FAILED` = exited without a handoff, `LEAKING` = processes left in its slice).

**Ranking** ready tasks: (1) inside the current milestone, (2) unblocks the most dependents, (3) general contract over fixture, (4) ownership disjoint from running work, (5) smaller first. `theseus tasks priority ID N` makes the queue assign it first; `theseus tasks hold ID` keeps it from being assigned (`release-hold` undoes); `theseus queue pause|resume` stops and restarts all assignment.

## The queue

Run exactly one driver per project, `theseus queue run`. `theseus agents up` starts it as a supervised session on theseus's private tmux server (`PROJECT--queue`), restarts it when it exits (with the `[supervisor.restart]` backoff) and keeps it out of your terminal; `theseus agents down --all` stops it. With only hand-made panes, `theseus agents up` starts just the driver. `[queue] driver = false` leaves it to you. `theseus attention` and the console report a project with agents and no live driver. Never start a second one by hand: `agents up` skips the start when a live `queue run` already drives the project.

The coordinator follows `theseus attention` (push into its pane, or `--follow`; `events.md`). A plain `queue wait` also drives (ticks) while it waits: use it only when no `queue run` is running, never both. The driver ticks every `--interval` seconds and rereads the configuration when it changes. Each tick: records deliveries (an idle agent whose `handoff.md` states a Result and has not changed for `instances.handoff_settle_seconds`), nudges an idle agent once, then reports it stalled, retries gateway errors, resets a free agent's context (compact; a full clear when it moves from dirty to clean work) and assigns it the next ready task it may take (room, profile, capability, pools). `theseus queue tick --no-assign` runs one pass that only reports. `theseus queue status` counts tasks by state.

## Supervised agents

```sh
theseus agents up                 # start missing agents toward [agents.counts]; --dry-run, --reset, --all-projects
theseus agents ls                 # state, profile, task, restarts, drift, session (--json)
theseus agents logs NAME          # its screen (--lines N, --transcript)
theseus agents attach NAME        # the human looks or answers a dialog; C-q detaches (--read-only, --window)
theseus agents restart NAME       # graceful restart resuming the conversation; --fresh for a new one
theseus agents restart NAME --fresh --with-task   # new conversation for an agent holding a task, plus one restart prompt
theseus agents restart --drifted  # apply a changed launch configuration to free agents
theseus agents scale PROFILE N    # set the count in theseus.local.toml and reconcile
theseus agents down NAME          # stop it and take it out of the count (--replace, --force, --now; --all pauses supervision)
theseus agents migrate NAME       # move a hand-made pane's agent under supervision (--all, --with-task)
```

States: `starting`, `ready`/`free`, `working`, `waiting` (the CLI waits for input such as a permission prompt), `blocked` (a startup dialog needs the human), `failed` (crash budget spent), `gateway_error`, `thrashing`, `stalled`, `offline`. Crashes restart on their own with backoff.

## Hand-made panes

`theseus agents add NAME PANE --profile P [--rooms clean,dirty]`, `theseus agents remove|enable|disable NAME`. Pane ids are per machine: any `[[instances.panes]]` seed list goes in `theseus.local.toml`.

## Manual instance rounds (no queue)

1. `theseus instance compact NAME` between tasks (`--clear` before clean work after dirty work).
2. `theseus instance assign TASK NAME SUFFIX` creates the worktree and branch and commits the assignment (`--in-place` edits the main checkout: docs-only rounds or before a first commit).
3. `theseus instance send NAME TASK WORKTREE [DEADLINE]` types the task message once the pane is idle.
4. `theseus instance watch` under a monitor prints `HANDOFF name` and `IDLE-NO-HANDOFF name`.
5. Integrate as usual; `theseus tasks deliver ID --handoff PATH` records a delivery the queue did not see.

`theseus instance restart NAME` resumes the session in a fresh process to load a new MCP server, skill or setting.
