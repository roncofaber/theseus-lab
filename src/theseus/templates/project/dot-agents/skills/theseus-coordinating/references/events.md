# Events and failure paths

`theseus queue wait --no-drive` exits on `delivered`, `partial`, `blocked`, `stalled` or `error` and prints every event since it started, one per line: `TIME KIND AGENT TASK DETAIL`. The persistent `theseus queue run` driver keeps assigning meanwhile. Gateway retries, resends, `send_failed` and pauses do not wake you until the retries run out or the error is fatal. `theseus queue log` shows the recent events again. Restart `queue wait --no-drive` after handling each event.

Two kinds of agent need different commands:

- Supervised (`[agents] managed = true`, listed by `theseus agents ls`): `theseus agents logs NAME` (its screen), `theseus agents restart NAME [--fresh --with-task]`, `theseus agents attach NAME` (for the human; C-q detaches).
- Hand-made panes (`theseus agents add NAME PANE`): `theseus console` (agent popup) or `tmux capture-pane -p -t PANE` to look, `theseus instance restart NAME` to restart and resume, `theseus instance compact NAME --clear` to clear.

Both take `theseus instance send NAME "text"` (or `--file PATH`): it waits for an idle pane, types, presses Enter and exits 1 when the agent did not start working.

## Sending a delivered task back to its worker

A task whose handoff arrived is in `verification` and its agent is free; the queue may already have given that agent a new task.

1. Move the old handoff aside first: `mv LOGDIR/handoff.md LOGDIR/handoff-1.md` (LOGDIR is `execution.logDir` in `theseus tasks show ID`, normally `WORKTREE/.theseus`). A reopened task whose `handoff.md` still states a Result is delivered again at the next queue tick.
2. If the agent holds no other task (`theseus agents ls`): `theseus tasks reopen ID --reason "..."`, then `theseus instance send NAME "ID: <exactly what is missing or wrong>. Fix it in WORKTREE, rerun <check>, rewrite the handoff."`
3. If it moved on: `theseus tasks release ID --reason "..."` (release accepts `in_progress` and delivered tasks) and `theseus tasks note ID "<what the next worker must do; the earlier attempt is in WORKTREE>"`. The queue assigns it again; the earlier worktree is kept and recorded in `previousAttempts`.

## partial

The worker finished some acceptance items and listed the rest under Limits. Prefer integrating what works (section 3 of the skill) when it passes its checks and its limits are explicit: complete it with one `--limit "..."` per unmet acceptance item, then write a follow-up record for the rest with `--depends ID`. Send it back (above) only when the partial work is not worth merging alone.

## blocked

Read the handoff: it names what is missing and what would unblock it.

- Something you can supply (a decision, a spec section, an ownership extension, a patch you apply on main): supply it, then send the task back (above). For an ownership extension, `theseus tasks edit ID --add-owned PATH --force`.
- Unknown original behavior: add a research task (`theseus-native-re` or `theseus-reference-runs`) and make this task depend on it: release it and edit `dependencies`.
- An input the team cannot obtain (hardware, credentials, a licence, a user decision): escalate; record `externalBlocker` with `evidence` and `unblockCondition`.

## stalled

The detail says why.

- `still idle after a nudge`: the queue already typed one continue message. Look at the screen, then one `theseus instance send NAME "..."` naming the missing step, file or failing check. A second stall on the same task: release it with the reason and split it into smaller records.
- `thrashing`: the context no longer fits. Supervised agents are restarted automatically on a fresh session with the task (a `restarted` event). Otherwise `theseus instance compact NAME --clear` or `theseus instance restart NAME`, or split the task.
- `restart prompt not confirmed after N attempts`: the agent never acknowledged its restart prompt. Look at its screen; restart it again or escalate.
- `the agent is blocked` / `failed`: see `error` below.
- `waiting for ...`: a permission prompt or another question from the agent CLI (next section).
- `gateway CODE xN`: next sections.

## Permission prompts

The agent CLI asks for approval of a command or file access and waits. Only supervised agents with a status command (Claude Code) report this as `waiting`; a hand-made pane at a prompt looks idle and the queue may nudge it, so prefer supervised agents, and look at any hand-made pane that stalls before sending it anything.

1. Look at the screen (`theseus agents logs NAME`, or the console popup) to see exactly what it wants to run.
2. Never type into it with `instance send` or `tmux send-keys`: the Enter it presses can approve the prompt. You cannot answer a dialog yourself.
3. Inside the task and safe (a read, a build or test command inside its worktree): ask the human to answer it (`theseus agents attach NAME`), and, if it will recur, to allow it in the profile's permission settings.
4. Outside the task or risky (outside the worktree, deletion of variable or glob paths, network, hardware, credentials, the original): stop the prompt by restarting the supervised agent with its conversation (`theseus agents restart NAME`). A graceful restart kills an agent stuck at a prompt instead of typing into it; the agent keeps its task and worktree. Not for hand-made panes: `theseus instance restart` types the exit command into the pane, which a prompt can take as an answer; the human answers there instead. Then `theseus instance send NAME "That action is not allowed: <why>. <What to do instead, or deliver blocked>."`
5. Not sure which case applies: ask the human.

## Gateway errors

For profiles that detect them (Claude Code), the queue handles retryable errors (429, 5xx including 524, 529, 530) itself: resend after a backoff, a fresh context with the restart prompt from the second error of a task, and a host-wide pause of the gateway pool when several agents fail at once (a `gateway_paused` event). You are woken when:

- `stalled ... gateway CODE x3`: resends ran out. `theseus console --once` shows the GATEWAY line (recent codes, last success, pause). Everyone failing: the gateway is down; `theseus queue pause`, tell the human, `theseus queue resume` later. Only this agent: `theseus agents restart NAME --fresh --with-task`.
- `error` with a fatal gateway error, never resent: 401 or 403 (credentials) and budget errors go to the human; a context-too-long error gets `theseus agents restart NAME --fresh --with-task` and possibly a smaller task.

## error

- `the agent is failed` (crash budget spent): read the crash screen under `<data>/agents/NAME/`, fix the cause, then `theseus agents restart NAME` (or `theseus agents up --reset`).
- `blocked` / `waiting for ... dialog` at startup (folder trust, a new MCP server): the human answers it with `theseus agents attach NAME`.
- `relaunch onto PROFILE failed`, `compact failed`, `task message not submitted`: the assignment was rolled back or skipped for a while; check the agent's screen and profile (`theseus agents ls`, `theseus doctor`), then let the queue retry.
- `supervisor: ...`: a reconcile failed; `theseus agents up --dry-run` shows the plan.

## Merge refusals (exit 2 or 3, main untouched)

- `the main checkout has uncommitted changes`: commit or set aside your own edits first.
- `OUTSIDE PATH` / `changes outside ownedPaths`: the worker changed a file it does not own without a patch. Review it: accept (`theseus merge ID --accept PATH`), or revert it in the worktree (`git -C WORKTREE restore PATH`, or delete the new file) and ask for a patch. `--skip-patch NAME` drops a worker patch you reject.
- `untracked files in main would be overwritten`: move those files out of the way.
- A conflict (`changed on main since the worktree base ... conflicting hunks`): main moved under the worker. Run `theseus tasks rebase ID`: it moves the worktree onto main and replays the worker's edits, leaving conflict markers (`Updated upstream` is main, `Stashed changes` is the worker's) and a stash copy. Resolve trivial ones yourself (both sides' intent is clear and a line or two apart), then `theseus merge ID` again at once; it records the new base. Otherwise send the task back (above) naming the files, and merge when the worker redelivers.
- `the worker committed in its worktree`: inspect the commits (`git -C WORKTREE log BASE..HEAD`), then turn them back into uncommitted edits with `git -C WORKTREE reset BASE` (a mixed reset keeps the files) and merge again. Tell the worker it must not commit.

## Failed verification

`theseus verify` FAIL after a merge:

1. Read the verify log; find the failing check and the merge that caused it (`theseus verify --debt` lists the code commits since the last pass).
2. Move that task's handoff aside (above), then `theseus tasks unmerge ID --reason "verify: <failing check>"`. It reverts the merge commit and puts the task back in `in_progress` with its worktree.
3. Get it fixed: if the owning agent is free, `theseus instance send NAME "ID was unmerged: <failure>. Fix it in WORKTREE, rerun <check>, rewrite the handoff."`. If it moved on, `theseus tasks release ID --reason "..."` and `theseus tasks note ID "..."` as above.
4. Right after the unmerge, `theseus verify` again to confirm main passes without it.
5. Never complete a task whose verification failed. When the cause is not one recent merge, stop merging and escalate.
