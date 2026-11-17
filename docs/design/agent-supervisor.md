# theseus agent supervisor: research and design

Status: approved and implemented through the CLI, lifecycle, crash recovery, migration from hand-made panes, store migration and reconcile in the queue tick; the dashboard UI phase follows. Written 2026-11-17 against theseus `e846d0e`, tmux 3.4 and Claude Code 2.1.326. Sections 1 to 5 are the design as researched; the user's decisions, the phase 0 verification results and where the implementation departs from the text are in section 6, which wins where they disagree.

## 1. Summary and key recommendations

Goal: theseus starts, registers and supervises its persistent agents itself, in hidden panes on a private tmux server it owns, instead of driving panes the user created by hand. The dashboard mirrors each agent's pane, can send it a line, and can open the real pane.

Recommendations, in order of importance:

1. One private tmux server per host user, `tmux -L theseus`, started with its own config file (`-f`, never the user's `~/.tmux.conf`) and a clean, allowlisted environment. One tmux session per agent, one window, one pane. Every tmux call goes through one Python seam (`theseus/tmux.py`) that always passes the socket explicitly and never relies on `$TMUX`. For managed agents, the user's server is touched only by the action the user triggers by keypress (open a managed pane in a new window), never by reconcile, the queue, the hook or tests. Two documented exceptions remain while legacy agents exist: the queue keeps driving unmanaged hand-made panes on the user's server as today, and `theseus agents migrate` types `/exit` once into the user's pane it migrates.
2. The agent is the pane's own process, not a child of an interactive shell: the pane runs a theseus wrapper (`theseus agents _exec PROJECT AGENT GEN`) that sets the profile environment and `exec`s the launch argv, with `remain-on-exit on` and a `pane-died` hook. This gives exact exit status and signal, makes the pane pid the agent pid, and keeps secrets out of the pane's start command. It also means managed agents need a restart path of their own (`respawn-pane`), separate from today's typed `/exit` plus typed relaunch line.
3. No daemon, as today (`docs/architecture.md`). Reconcile runs in `theseus agents up` and in every queue tick; the `pane-died` hook only records the exit in the store and may kick a one-agent reconcile. Backoff state lives in the store. A crash loop that exhausts its budget emits an `error` event, so `theseus queue wait` wakes the coordinator.
4. Declarative counts per profile (`[agents.counts]`), deterministic names (`{project}-{profile}-{n}`), and a reconcile that is idempotent and conservative: it starts missing agents, restarts dead ones within the restart policy, never restarts a healthy agent for config drift on its own, and never touches an agent that holds an in-progress task except to restart it after a crash.
5. For Claude profiles, use `claude agents --json` (documented "for scripts; works without a terminal", 0.11 s for every session on the development machine) as the primary busy/idle/waiting signal and as the source of the current session id, matched by pid to the pane. Keep `busy_pattern` screen scraping as the fallback for other CLIs. Do not read `~/.claude/sessions/<pid>.json`: it is undocumented and its `sessionId` reportedly goes stale after `/clear`.
6. Preassign the conversation id with `--session-id <uuid>` at first spawn and resume with `--resume <id>` after a crash, a relaunch or a reboot; refresh the stored id after `/clear`, which assigns a new one.
7. Dashboard mirror: poll `capture-pane -p -e` for the selected agent only, a few times per second, skip redraws when the content hash is unchanged. Measured here: 4.1 ms per capture as a subprocess, so 4 Hz costs about 16 ms of CPU per second. Control mode is an optional later optimization as a command channel (0.09 ms per capture measured), not a `%output` stream parser, which would need a terminal emulator.
8. Verified send everywhere (dashboard and queue): wait for idle, paste literally, check the text reached the input box, press Enter, confirm the agent became busy or the box cleared; record an event either way.
9. Reuse libtmux? No, not as a dependency for now: it is MIT and maintained, but it spawns one subprocess per command exactly like a thin wrapper would, its control-mode engine is an unmerged draft, and it is pre-1.0 with a "pin a range" policy; theseus keeps its standard-library-plus-Textual dependency rule. Learn from its isolated-server pytest fixtures. claude-squad is AGPL-3.0: learn from its behavior and UX only, never copy or translate its code, never depend on it.
10. Claude Code now ships a native background-session supervisor (`claude --bg`, `claude agents`, `attach`, `logs`, `respawn`). It is named here as the native alternative and rejected as the primary mechanism for reasons in section 3.2, but its JSON listing is adopted as a signal.

## 2. How agents are tracked today

- Registry: the host store's `agents` table (`src/theseus/store.py`, migration 1): `project, name, pane, profile, rooms, enabled, registered, source`. Seeded once from `[[instances.panes]]` (`agents.seed`), then edited by `theseus agents add|remove|enable|disable`. `pane` is a tmux pane id such as `%111` on the user's default server.
- Live state: `agents.state()` runs bare `tmux display` and `tmux capture-pane` on the user's server; busy is `busy_pattern` ("esc to interrupt") in the last three screen lines; thrashing is a stall pattern anywhere on screen; delivered is a settled handoff file. States: offline, busy, working, delivered, thrashing, stalled, quiet, free, disabled.
- Driving: `workqueue.tick()` assigns, nudges, compacts and clears through `src/theseus/scripts/instance-*.sh`, which call bare `tmux send-keys -l` after polling `pane_busy` for up to three minutes. Sends are not verified.
- Relaunch: `instance-restart.sh` finds the agent as the child of the pane's shell that owns `~/.claude/sessions/<pid>.json`, types `/exit`, then types the resume line into the same shell (so the user's aliases and environment apply), falling back to a fresh launch when the pane shows `resume_missing`.
- Profiles (`src/theseus/profiles.py`): `CLAUDE_INTERACTIVE` launch is `{command} --name {agent} --permission-mode {permission_mode}`, resume is `{launch} --resume {session}`; `interactive_launch()` prefixes `env K=V ...` for profile env, which is typed into the shell and so lands in scrollback and shell history. `relaunch_compatible()` lets the queue switch an agent onto a profile that differs only in MCP servers or env (for example a variant with one more MCP server), tracked as `launched` in `<data>/queue/agents-state.json`.
- Dashboard: `theseus top` (Textual, `src/theseus/tui/app.py`) lists agents with CPU and memory from the pane's process tree (`top.agent_processes`, bare `tmux display`), and Enter runs `tmux switch-client` on the dashboard's own client (`top.jump_to`).
- Tests use a fake `tmux` on PATH (`tests/test_queue.py`, `tests/test_restart.py`, `tests/test_workers.py`), never a real server.

What breaks when panes move to a private server: every bare `tmux` call (pane ids such as `%3` are per server), the shell-parent assumption of `instance-restart.sh`, and the store's lack of a server column.

## 3. Research findings

### 3.1 Prior art, licenses and reuse

Licenses were checked with the GitHub API (`license.spdx_id`) on 2026-11-17.

| Project | License | What it is | Verdict |
| --- | --- | --- | --- |
| libtmux | MIT | Typed Python ORM over tmux | Learn from; optional later dependency |
| tmuxp | MIT | Declarative YAML/JSON tmux workspaces on libtmux | Learn from (declarative format) |
| tmuxinator | MIT | Ruby, YAML projects, `socket_name` option | Learn from (declarative format) |
| claude-squad | AGPL-3.0 | Go TUI managing Claude/Aider sessions in tmux | Behavior and UX only; no code, no dependency |
| agent-deck | MIT | Go/Bubble Tea session manager on tmux | Learn from |
| ntm | NOASSERTION (MIT plus a rider) | Go "named tmux manager" for agent swarms | Behavior only; non-standard license |
| ccmanager, ccmux, fleet | MIT | TypeScript agent session managers or switchers | Learn from |
| k9s | Apache-2.0 | Kubernetes TUI | UX conventions |
| lazygit | MIT | Git TUI | UX conventions |
| btop | Apache-2.0 | Resource monitor TUI | UX conventions |
| tmux | ISC | The multiplexer | Hard dependency (user decision) |
| Textual | MIT | Existing dashboard dependency | Keep |
| pyte | LGPL-3.0 | VT100 emulator in Python | Optional only, never vendored; not needed by this design |

libtmux. MIT, "Development Status :: 5 - Production/Stable" on PyPI, latest 0.62.0, Python >= 3.10, tmux >= 3.2a; its README says "libtmux is pre-1.0. Pin a range in projects to avoid surprises" ([PyPI](https://pypi.org/pypi/libtmux/json), [README](https://github.com/tmux-python/libtmux)). Every command is one `subprocess.Popen` plus `communicate()` (`src/libtmux/common.py`, `tmux_cmd`), so it is no faster than a thin wrapper. `Server(socket_name=..., socket_path=...)` handles private servers well, and its pytest plugin gives "fresh server and session fixtures per test, each on an isolated tmux socket", which is the pattern to copy for theseus tests. A control-mode engine (persistent `tmux -C` connection) exists only as draft PR #605 ([PR](https://github.com/tmux-python/libtmux/pull/605)). Verdict: theseus needs about ten tmux verbs (new-session, respawn-pane, kill-session, list-panes with formats, capture-pane, send-keys, load-buffer, paste-buffer, set-option, set-hook); a 150-line seam keeps the dependency rule ("Standard library only apart from Textual") and keeps the Bash scripts and Python on one addressing model. Revisit libtmux if its control-mode engine lands and the dashboard needs it.

tmuxp and tmuxinator. Both load a declarative description and create or attach sessions; neither documents reconcile of a running session ([tmuxp](https://github.com/tmux-python/tmuxp), [tmuxinator](https://github.com/tmuxinator/tmuxinator)). tmuxinator sends commands with `send-keys` "as if you typed them in", which is the pattern theseus wants to leave behind. Take the declarative shape (a named project with a list of windows and commands, an optional socket name) but not their create-or-attach semantics.

claude-squad (AGPL-3.0, verified). Behavior observed from its README and source, described here only as behavior: each instance is a detached tmux session named with a fixed prefix plus the sanitized title, created on the user's default tmux server (no `-L`/`-S`); git worktree per session; it detects activity by hashing successive `capture-pane -p -e -J` output and detects prompts by matching known strings; it auto-answers the folder-trust and new-MCP-server dialogs with Enter; attach is a PTY bridge with Ctrl-Q to detach; keys are `n`/`N` new, `D` kill, `j`/`k` move, Enter/`o` attach, Ctrl-Q detach, `tab` preview/diff, `c` checkout (commit and pause), `r` resume, `s` commit and push, `?` help ([README](https://github.com/smtg-ai/claude-squad)). Lessons: a preview plus diff tab per agent works well; a hash of the capture is a cheap change detector. Things theseus should do differently: use a private server, not the user's; do not auto-accept trust or MCP dialogs silently.

Other agent managers. agent-deck (MIT) is a Go/Bubble Tea tmux session manager showing which sessions are "working, waiting, or done", with forking and MCP management ([repo](https://github.com/asheshgoplani/agent-deck)). ntm names panes per harness (`session__cc_1`), broadcasts with `ntm send --all`, reads each TUI's visual state for status, and refuses automatic replay when an outcome is uncertain ("requires an explicit decision"), a good principle for restarts that would repeat side effects ([repo](https://github.com/Dicklesworthstone/ntm)). Claude Code's own agent teams can place teammates in tmux split panes and warn about orphaned tmux sessions after the lead ends ([docs](https://code.claude.com/docs/en/agent-teams)).

### 3.2 The native alternative: Claude Code background sessions

Claude Code ships background sessions: `claude --bg` starts a session under a per-user supervisor process, and `claude agents`, `attach`, `logs`, `stop`, `respawn`, `rm` manage them ([CLI reference](https://code.claude.com/docs/en/cli-reference), [agent view](https://code.claude.com/docs/en/agent-view)). The supervisor restarts a session that "exited unexpectedly", keeps permission mode and model across restarts, and `claude agents --json` lists sessions with `status` (`busy`, `waiting`, `idle`), `waitingFor` (for example `permission prompt`, `dialog open`), `pid`, `sessionId` and `name`.

Why theseus should not build on it as the primary mechanism:

- theseus is multi-vendor (codex, opencode, gemini-cli presets); background sessions are Claude only.
- No documented non-interactive send to a running session: a prompt passed with `claude --resume <id> "prompt"` to a running background session goes to it as its next turn, but through `claude attach` in the calling terminal; the other options are the peek panel reply box and `claude --resume <id> --bg "prompt"`, which may start a copy under a new id. The queue needs to type task messages, `/compact` and `/clear` without a terminal.
- A finished or waiting unattached session is stopped after about an hour unless pinned; resumes then happen on the next reply. Persistent workers that hold context between tasks would churn.
- It moves sessions into its own worktrees under `.claude/worktrees/` before editing (configurable with `worktree.bgIsolation`), which competes with theseus's per-task worktrees and merge path.
- The supervisor stops its sessions on Linux if no supervisor reconnects within about a minute, and shutdown stops them; it is not more durable than a tmux server.
- Inference, not from the docs: profiles whose `command` is a wrapper launcher (a script that sets a gateway configuration and ends with `exec claude`) have no obvious place in `claude --bg`, which starts `claude` itself under its supervisor's environment.

Adopted from it: `claude agents --json` as the busy/idle/waiting signal and session-id source for Claude profiles (it also lists ordinary interactive sessions, so it covers agents in tmux panes). Locally it returned in 0.11 s and listed interactive sessions with `pid, sessionId, name, status`.

### 3.3 Claude Code specifics

- Name and permission mode: `--name/-n` sets a display name "shown in `/resume` and the terminal title" and named sessions resume with `claude --resume <name>`; `--permission-mode` accepts `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`, `manual` ([CLI reference](https://code.claude.com/docs/en/cli-reference)).
- Session ids: `--session-id <uuid>` "Use a specific session ID for the conversation (must be a valid UUID)". `--resume <id|name>` searches the current project, its worktrees, then every project on the machine (v2.1.223 and later), and also accepts a transcript path. `--continue` picks the most recent conversation in the current directory, which is ambiguous with several agents in one directory: never use it for supervised agents. `--fork-session` creates a new id on resume; not needed here.
- `/clear` assigns a new session id. Several issue reports say `~/.claude/sessions/<pid>.json` keeps the old id after `/clear` and disagree on whether the `SessionStart` hook fires with source `clear` ([issue reports](https://claudeissues.com/issue/36213-bug-clear-creates-new-session-jsonl-but-never-updates-claude-sessions-pid-json-b), [another](https://claudeissues.com/issue/28692-clear-does-not-fire-sessionstart-hook-despite-assigning-a-new-session-id)). The queue types `/clear` on room changes, so the stored id must be refreshed from a documented source after a clear; phase 0 verifies that `claude agents --json` reports the new id.
- Readiness and state: documented signals are `claude agents --json` (above) and hooks: `SessionStart` (matchers `startup`, `resume`, `clear`, `compact`), `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification` (matchers include `permission_prompt`, `idle_prompt`, `agent_needs_input`), `SessionEnd`; every hook gets `session_id`, `transcript_path`, `cwd` ([hooks](https://code.claude.com/docs/en/hooks)). `--settings` takes a file or inline JSON applied above user, project and local files, and "lists merge instead of overriding" across scopes ([settings](https://code.claude.com/docs/en/settings)). Whether a `hooks` object passed with `--settings` merges with the user's own hooks and with a profile's `extra_args` settings file is not stated; phase 0 verifies before any hook is relied on. Hooks are therefore a phase 3 option for push-based state, not the base design.
- Startup dialogs: the folder-trust dialog appears for an untrusted launch directory; trust decisions live in `~/.claude.json` ([settings](https://code.claude.com/docs/en/settings)). Launching agents in the project root, which the user already trusts for the coordinator, avoids it; worktrees are entered by instruction, not by launch cwd.
- Headless versus interactive. `-p` with `--output-format json|stream-json` gives exact results, `session_id` in the output, exit codes, `system/api_retry` events and SIGTERM semantics, and `--input-format stream-json` allows multi-turn input on stdin ([headless](https://code.claude.com/docs/en/headless)). theseus already uses `-p` for `theseus dispatch`. For supervised persistent workers the interactive pane stays the better fit: the user can look at and take over any agent, slash commands (`/compact`, `/clear`) work as typed, the profile abstraction already covers interactive CLIs of other vendors, and nothing new has to be built to show progress. The cost is screen-derived state, which this design reduces by using `claude agents --json` and verified sends. A stream-json "headless persistent" worker is a reasonable future profile kind but out of scope.
- Process signals: SIGTERM to a `-p` run exits 143 and leaves the current turn unfinished; SIGINT ends the turn first ([headless](https://code.claude.com/docs/en/headless)). For interactive agents, theseus stops gracefully by typing the profile's exit command (`/exit`) at an idle prompt, then kills the tmux session only after a timeout.

### 3.4 tmux mechanics

All from `man tmux` (tmux 3.4) unless noted; the measurements were taken on the development machine on a throwaway server with a socket under `/tmp`, killed afterwards.

- Private sockets: `-L name` puts the socket in `tmux-UID` under `TMUX_TMPDIR` or `/tmp`; "The tmux-UID directory is created by tmux and must not be world readable, writable or executable". `-S path` gives a full path and ignores `-L`. Unix socket paths are limited to about 108 bytes: `-S` under the scratchpad path failed with "File name too long", so prefer `-L` for real use and short `-S` paths in tests.
- Access: `server-access` controls other users; "By default, the access list is empty and tmux creates sockets with file system permissions preventing access by any user other than the owner (and root)". The test socket was mode 600 in a 700 directory.
- Crash detection: `remain-on-exit [on | off | failed]` keeps a pane whose program exited; `respawn-pane [-k]` reactivates it; hooks `pane-died` (exited while remain-on-exit is on) and `pane-exited`, `session-closed`, `client-detached`; formats `pane_dead`, `pane_dead_status`, `pane_dead_signal`, `pane_dead_time`. Verified: with `remain-on-exit on`, a pane running `exit 3` fired `pane-died` with `#{pane_dead_status}` = 3 and stayed listed as dead.
- Capture: `capture-pane -p` prints; `-e` keeps colors and attributes; `-J` joins wrapped lines and implies `-T`; `-S`/`-E` select history lines (`-` is the start of history). Measured: `capture-pane -p -e` as a subprocess 4.14 ms, `list-panes -a -F ...` 2.78 ms, the same capture over a control-mode connection 0.09 ms.
- Logging: `pipe-pane -O 'cmd'` pipes pane output to a command; one pipe per pane; `-o` toggles. The stream is raw terminal output, including every TUI repaint.
- Control mode (`-C`): commands on stdin answered in `%begin`/`%end` blocks, notifications such as `%output` (octal-escaped raw bytes), `%pause`/`%continue` flow control with `pause-after`, `refresh-client -f no-output` to suppress output, and `refresh-client -B` format subscriptions reported "at most once a second" ([wiki](https://github.com/tmux/tmux/wiki/Control-Mode)). The wiki notes output "may not be valid UTF-8 and may contain escape sequences" and that after a pause "It is up to the client to update the content of the pane if necessary, for example using capture-pane". Rendering `%output` would need a terminal emulator (pyte is LGPL); using control mode as a cheap command channel and change notifier avoids that.
- Sizes: a session created with `new-session -d` takes `default-size` (80x24 by default); `window-size` (`largest`, `smallest`, `manual`, `latest`) decides what attached clients do.
- Nesting: the tmux binary carries the message "sessions should be nested with care, unset $TMUX to force". Observed on 3.4 under a pty: attaching to a throwaway server proceeded both with `TMUX` naming that same socket and with `TMUX` naming another one. When the refusal triggers was not established. Rule for theseus regardless: run every inner attach as `env -u TMUX tmux -L theseus attach ...` so the inner client cannot pick up the outer server.
- Target forms, verified on 3.4: `new-session -s =foo` creates a session literally named `=foo`, so `-s` takes the plain name; `respawn-pane -t =bar` fails with "can't find pane", while `=bar:` (exact session, its current window and pane) works for pane commands (`respawn-pane`, `capture-pane`, `display`). Session commands (`attach`, `kill-session`, `has-session`) take `=bar`.
- Server lifetime: `exit-empty` (default on) ends the server when it has no sessions; `destroy-unattached` off by default keeps detached sessions; `update-environment` decides which client variables are copied into the session on attach.

## 4. Design

### 4.1 Architecture

```
theseus agents up/down/... --+
queue tick (reconcile) ------+--> theseus.supervisor --> theseus.tmux --> tmux -L theseus
theseus top (mirror, send) --+          |                (seam)            |
                                        v                                  +- session S1
pane-died hook --> agents _died --> host store                             |   pane: _exec -> launcher
                                    (agents, agent_runs, events)           +- session S2 ...
```

- `theseus/tmux.py`: `Server(socket=None, path=None)` with `run(*args)`, `list_panes(fmt)`, `new_session(name, argv, env, size, cwd)`, `respawn(target, argv, env)`, `kill_session(name)`, `capture(target, history=0, ansi=True)`, `send_literal(target, text)`, `paste(target, text)`, `send_key(target, key)`, `set_option(...)`, `set_hook(...)`. `Server.legacy()` is the user's default server, used only for unmanaged agents and for the explicit open-in-window action. Every call passes `-L`/`-S` (or, for legacy, explicitly no socket flag with `TMUX` removed from the child environment, so a coordinator running inside the user's tmux behaves the same as one outside it).
- Bash scripts get a `tmux_()` helper in `lib.sh` that prepends `$THESEUS_TMUX_ARGS` (empty for legacy agents, `-L theseus` for managed ones); `run_script()` sets it per agent. The scripts stay for legacy agents; managed agents use the Python supervisor for restart and send, so `instance-restart.sh` keeps its shell-parent logic unchanged.
- `theseus/supervisor.py`: config resolution, naming, reconcile, spawn, readiness, restart policy, stop, send. No long-running process.
- The tmux server is started on demand by the first `up` (or reconcile) with `-f <host data>/tmux.conf`, under `systemd-run --user --scope --unit=theseus-agents` when systemd is available, so logout cleanup and the user's terminal closing do not kill it, and per-agent memory is visible in cgroups (theseus already uses user slices for dispatched workers).

### 4.2 Private server configuration

Written by theseus to `~/.local/share/theseus/tmux.conf` and loaded with `-f`, so the user's `~/.tmux.conf` never applies:

```
set -g exit-empty on
set -g remain-on-exit on
set -g history-limit 50000
set -g default-size 220x50
set -g window-size manual
set -g update-environment ""
set -g status on
set -g status-left "[theseus #{@theseus_project}] "
set -g status-right "#{@theseus_agent}  C-q detach "
set -g prefix None
bind -n C-q detach-client
set-hook -g pane-died "run-shell -b '<abs theseus> agents _died #{pane_id} #{pane_dead_status} #{pane_dead_signal}'"
```

The hook passes only tmux-generated values (pane id and exit fields), never project or agent names, so nothing user-controlled is interpolated into a shell command; `_died` reads the pane's `@theseus_*` options (or the store row with that pane id) to find the agent.

- `prefix None` plus a root-table `C-q` detach avoids the prefix clash when the private client runs inside a window of the user's tmux (the outer server gets `C-b`); `C-q` follows claude-squad's convention and is configurable (`supervisor.detach_key`).
- `window-size manual` with a fixed `default-size` keeps the agent screen stable, so captures, busy-marker positions and the mirror do not change when someone attaches. On interactive attach theseus runs `resize-window -A` for that window and a `client-detached` hook restores the default size; phase 3 tests verify this behavior on 3.4 before it ships, and if it misbehaves the fallback is a fixed size with the attaching terminal showing it cropped.
- Server environment: started under `env -i` with an allowlist (`HOME USER LOGNAME SHELL PATH LANG LC_* TERM TMPDIR XDG_* THESEUS_*` plus `supervisor.env_passthrough` names and glob patterns), so variables of the process that happened to start the server (for example a coordinator's Claude Code shell variables) never leak into every agent. `THESEUS_*` must pass so `_exec` and `_died` use the same store (`THESEUS_DB`) and host config (`THESEUS_CONFIG`) as the CLI that started the server. `_exec` and `_died` get only a project name or pane id; they resolve the project root from the store's `projects` table and load its `theseus.toml` from there. Profile `env` and `interactive.env` are applied by the `_exec` wrapper per agent.
- This reverses one documented behavior: today the relaunch line is typed into the pane's own shell so that "the same aliases and environment (for example a model gateway configuration) apply" (`instance-restart.sh`). Under the supervisor, `command` must be an executable on PATH, not a shell alias or function, and anything a launcher needs from interactive shell startup files is missing. A typical gateway launcher is fine: a script on PATH that reads its key from a file of the user's own when the key variable is unset, sources its gateway settings and ends with `exec claude ...` (so the pane pid becomes the `claude` pid). For other launchers, `supervisor.launch_shell = "login"` runs the argv through `$SHELL -lc` instead of a direct `exec`; `theseus config check` warns when `command` is not found on the allowlisted PATH.
- Each pane carries user options `@theseus_project`, `@theseus_agent`, `@theseus_gen`, `@theseus_profile`, so reconcile can rebuild the picture from tmux alone and cross-check the store.

### 4.3 Declarative configuration

Host layer, `~/.config/theseus/config.toml` (host-only keys):

```toml
[supervisor]
socket = "theseus"              # tmux -L name; THESEUS_TMUX_SOCKET overrides (tests)
size = "220x50"
history_limit = 50000
detach_key = "C-q"
max_agents = 12                 # host-wide cap across projects (RAM guard)
env_passthrough = ["SSL_CERT_FILE"]
trust_dialog = "ask"            # "ask" (surface it) or "accept" (press Enter)

[supervisor.restart]
backoff_seconds = [5, 30, 120, 600]
max_crashes = 5
window_minutes = 30

[supervisor.logs]
raw = false                     # pipe-pane raw stream to <data>/agents/<name>/pane.log
max_mb = 20
keep = 3

[defaults]
"agents.counts" = { gateway = 2 } # fallback when a project sets no counts
```

Project layer, `theseus.toml`, with machine-specific counts usually in `theseus.local.toml` (same merge order as today: host < project < local < environment):

```toml
[agents]
managed = true                  # theseus starts and supervises this project's agents
name = "{project}-{profile}-{n}"
cwd = "root"                    # "root" (trusted main checkout), "data", or a path
resume = "always"               # "always", "never": resume the stored conversation on restart

[agents.counts]
gateway = 3
gateway-rea = 0                 # relaunch-only profile: the queue switches a gateway agent onto it

[[agents.extra]]                # optional named agents outside the counted slots
name = "demo-clean-1"
profile = "gateway"
rooms = ["clean"]
```

Rules:

- A count is the number of standing sessions of a profile; `capacity` stays the cap on concurrent tasks of that profile. `theseus config check` warns when a count exceeds capacity (idle sessions cost memory, not tokens) and fails when a counted profile has no `interactive` settings.
- Names are deterministic: slot `n` of profile `p` is always the same name, so `[rooms] clean_instances`/`dirty_instances`, event history, samples and tainted-agent records keep working across restarts. The template must contain `{n}` and either `{project}` or a project-unique prefix; the validator enforces this.
- The tmux session name is `{project}--{agent}` with characters outside `[A-Za-z0-9_-]` replaced by `_` (tmux does not allow `.` or `:` in session names). It is created with the plain name (`new-session -s S`); session commands target `=S` and pane commands target `=S:` or the stored `%N`, the forms verified in 3.4.
- `[[instances.panes]]` keeps working for unmanaged agents. A name that appears in both is a `config check` error.

### 4.4 CLI and reconcile semantics

The existing `theseus agents` positional actions (`list`, `add`, `remove`, `enable`, `disable`, `profiles`) stay; new actions:

| Command | Effect |
| --- | --- |
| `theseus agents up [NAME...] [--dry-run] [--all-projects]` | Reconcile toward config: start missing, restart dead within policy, report drift |
| `theseus agents down [NAME...\|--all] [--force] [--now]` | Graceful stop; sets desired state `down` so reconcile leaves it stopped |
| `theseus agents ls [--json]` (alias of `list`) | Name, state, profile, gen, pid, session id, task, restarts, drift, uptime |
| `theseus agents restart NAME... [--drifted] [--fresh]` | Graceful restart, resuming the conversation unless `--fresh` |
| `theseus agents attach NAME [--read-only] [--window]` | Attach in this terminal (`tmux attach -t =S`, `-r` for read-only) or open a window in the user's tmux |
| `theseus agents logs NAME [--lines N] [--raw] [--transcript]` | Scrollback capture; raw pipe-pane log; path of the conversation transcript |
| `theseus agents migrate NAME...\|--all` | Move a hand-made pane's agent onto the private server (section 4.10) |
| `theseus agents _exec` / `_died` | Internal: pane wrapper and hook target |

Reconcile (one pass, also run at the start of every queue tick for managed projects, under the existing queue lock):

1. Desired set: the slots from `[agents.counts]` and `[[agents.extra]]`, minus agents whose desired state is `down`.
2. Observed set: `list-panes -a -F` on the private server filtered by `@theseus_project`, joined with store rows and the latest `agent_runs` row per agent.
3. For each desired agent:
   - no session: spawn (4.5), subject to `max_agents`;
   - session with a dead pane: apply the restart policy (4.6);
   - live and its launch hash differs from the config: mark `drift` and report it; do not restart. `theseus agents restart --drifted` applies drift to free agents only;
   - live, its runtime profile differs from its registered profile because the queue relaunched it for a task (`launched`): leave it alone; the queue switches it back before its next task, as today;
   - live and healthy: no action.
4. For each observed agent not desired (count lowered, extra removed): if free, stop it gracefully; if it holds an in-progress task, disable it (no new work) and stop it on a later pass once free.
5. `up` prints one line per agent: `kept`, `started`, `restarted`, `drift`, `stopping`, `deferred (task T)`, `blocked (startup dialog)`. Running `up` twice in a row with healthy agents prints only `kept`.

Store and command semantics for managed agents: `agents add`/`remove` are refused with a hint to edit `[agents]` (or `theseus agents up` after editing); `enable`/`disable` keep working (a disabled managed agent keeps running but takes no new work).

### 4.5 Lifecycle

Spawn:

1. Allocate generation `gen = last + 1`, insert an `agent_runs` row with intent `start` (or `restart`, `relaunch`, `resume`).
2. Session id: if `resume` is on and a stored id exists, resume mode; otherwise generate a UUID4. The supervisor appends the session arguments to the argv it builds from `interactive.launch` (`--session-id <uuid>` for a new conversation, the profile's resume form, `--resume <id>` for Claude, otherwise), instead of adding a placeholder to the template: `profiles.fill` leaves unknown placeholders literal, so a new `{session_args}` token would be typed verbatim by `instance-restart.sh` for legacy agents and silently skipped by user-overridden `interactive.launch` values. A new optional `interactive.session_flag` (Claude: `--session-id`) says how to preassign; profiles with `session = "none"` get nothing.
3. `new-session -d -s S -x W -y H -c CWD -- <abs theseus> agents _exec PROJECT AGENT GEN`, then set the pane user options. The wrapper resolves the profile, builds the argv with `profiles.interactive_launch` logic minus the `env` prefix, applies env in-process, appends to `<data>/agents/<name>/runs.log` one line without env values, and `exec`s. The pane start command shown by tmux therefore never contains a secret, and the pane pid is the launcher's pid (the agent itself when the launcher `exec`s; otherwise a descendant, found with the existing `top.descendants`).

Readiness (poll every 0.5 s, up to 90 s, when run from `theseus agents up`; inside a queue tick, reconcile spawns, marks the agent `starting` and returns, and the next tick evaluates readiness, so the queue lock is never held for a slow start):

- ready: for Claude profiles, `claude agents --json` lists a pid in the pane's process tree with `status` `idle`; for others, the screen is stable for two polls, `busy_pattern` absent, and the profile's new optional `interactive.ready_pattern` present;
- blocked: `status` `waiting` with `waitingFor` `dialog open`, or a profile `interactive.startup_dialogs` pattern on screen (Claude defaults: the trust question and the new-MCP-server question). With `trust_dialog = "accept"` only the trust dialog is answered with Enter; MCP approvals are never auto-answered. Otherwise the agent state becomes `blocked`, an `error` event says `startup dialog: run theseus agents attach NAME`, and the queue assigns it nothing;
- resume failed: the profile's `resume_missing` text with the stored id on screen: respawn once in fresh mode with a new UUID, event `restarted` with detail `session S no longer exists; started fresh`;
- dead or timeout: counts as a crash for the restart policy.

On ready: update the store row (`pid`, `session_id`, `state`), event `ready`.

Registration (store migration 3, appended to `store.MIGRATIONS`):

```sql
ALTER TABLE agents ADD COLUMN managed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN server TEXT;      -- NULL: user's default server (legacy)
ALTER TABLE agents ADD COLUMN target TEXT;      -- tmux session name, stable across restarts
ALTER TABLE agents ADD COLUMN session_id TEXT;  -- agent CLI conversation id
ALTER TABLE agents ADD COLUMN slot INTEGER;
ALTER TABLE agents ADD COLUMN desired TEXT NOT NULL DEFAULT 'up';
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    gen INTEGER NOT NULL,
    intent TEXT NOT NULL,           -- start, restart, relaunch, resume, migrate
    profile TEXT,
    launch_hash TEXT,               -- sha256 of argv, env names and cwd; no values
    session_id TEXT,
    pid INTEGER,
    started TEXT NOT NULL, started_epoch REAL NOT NULL,
    ready_epoch REAL,
    ended TEXT, ended_epoch REAL,
    exit_status INTEGER, exit_signal TEXT,
    stop_reason TEXT);              -- crash, down, restart, relaunch, server-lost
CREATE INDEX agent_runs_agent ON agent_runs(project, agent, started_epoch);
```

`pane` keeps holding the current pane id for compatibility with every reader; `target` is the stable key, since pane ids die with the server. New event kinds: `spawned`, `ready`, `exited`, `restarted`, `stopped`, `drift`, `sent`, `send-unconfirmed`; failures use `error` (already in `workqueue.ATTENTION`). Rollout note: every installed copy of theseus on a machine shares `~/.local/share/theseus/theseus.db`, and `store.migrate()` refuses a database newer than the running code, so the migration must reach every installed theseus at the same time (or ship first in a release that knows the schema without using it).

State: `agents.state()` gains three states for managed agents, `starting`, `blocked` and `failed` (restart budget exhausted), and one for Claude profiles, `waiting` (`status` `waiting`, needs input), shown in red in the dashboard and treated like `stalled` by `queue wait`. The busy test becomes: Claude profile and a matching `claude agents --json` entry, else `busy_pattern`. One `claude agents --json` call per tick serves all agents.

Session id refresh: after the queue types `/clear` or `/compact`, and once per tick, the stored `session_id` is updated from the `claude agents --json` entry for the agent's pid. Phase 0 confirms this field follows `/clear`; if it does not, the fallback is a theseus `SessionStart` hook passed with `--settings`, writing `{session_id, source}` to `<data>/agents/<name>/session.json`, but only after verifying that hooks passed this way merge with the user's hooks.

Crash detection and restart policy:

- `pane-died` runs `theseus agents _died ...`, which closes the `agent_runs` row with exit status and signal. If the run's intent is a planned stop (`down`, `restart`, `relaunch`), it records `stopped`; otherwise `exited` with `stop_reason = crash`, captures the last 200 lines (`capture-pane -p -S -200`) into `<data>/agents/<name>/crash-<gen>.txt` (mode 600) before anything respawns the pane, and starts `theseus agents up NAME` in the background.
- Restart decision (in reconcile): crashes of this agent within `window_minutes`; the next attempt waits `backoff_seconds[k]` after the last exit; after `max_crashes` the agent becomes `failed`, an `error` event explains it, and only `theseus agents restart NAME` or `up --reset NAME` clears it. A clean exit (status 0) that was not planned (for example a worker typing `/exit`) is restarted once without backoff, then treated as a crash.
- Restart uses `respawn-pane -k -t =S:` with the next generation and resume mode, so the conversation, and with it any in-progress task, continues; the existing stalled path of the queue then nudges it ("Continue the current task ...") if it sits idle. Following ntm's principle, an automatic restart never re-sends the last task message; the conversation and the nudge carry the work.
- Server loss (the whole private server gone, for example a `tmux kill-server` run by an agent inside its pane, whose `TMUX` points at the private server): every live run is closed with `server-lost` on the next reconcile, the server is recreated and all agents resume. Agent settings should deny `Bash(tmux kill-server*)`; the agents' `TMUX` deliberately keeps pointing at the private server, because unsetting it would send an agent's own tmux commands to the user's server.

Relaunch onto another profile (queue `relaunch()` for managed agents): wait idle, record intent `relaunch`, type the profile's exit command, wait for the pane to die (30 s, then `respawn-pane -k`), respawn with the target profile and `--resume`, wait ready, update `launched` as today. `relaunch_compatible()` is unchanged.

Resume after reboot: nothing runs at boot by default. The first `theseus agents up`, `queue run`, `queue wait` or `queue tick` of a managed project finds no server, closes stale runs with `server-lost`, starts the server and resumes every desired agent with its stored session id. An optional `theseus setup supervisor --systemd` installs a user unit that runs `theseus agents up --all-projects` at login (with `loginctl enable-linger` for headless hosts); see open questions.

Pools and capacity: idle sessions hold no pool slot (pools count tasks, `pools.sync` already builds holders from in-progress tasks). Counts never bypass `capacity` or pools: the queue's assignment checks are unchanged. `max_agents` caps the total number of sessions on the host. Scaling down never interrupts a task (4.4 step 4).

Clean shutdown: `down NAME` waits up to three minutes for idle, types the exit command, waits 30 s for the pane to die, then kills the session, records `stopped` and sets `desired = 'down'`. With an in-progress task it refuses unless `--force` (then the task stays assigned for the coordinator to release, as `agents remove` says today). `down --all` stops every agent of the project; when the last session on the server goes, `exit-empty` ends the server. `--now` skips the graceful phase.

### 4.6 Dashboard integration and TUI patterns

Prior art for layout and keys (all permissive or behavior-only, see 3.1):

- claude-squad (AGPL-3.0; behavior and UX only, no code may be copied, translated or depended on): a session list with a status marker per instance, a right pane with Preview and Diff tabs switched by `tab`, Enter or `o` to attach to the real session, Ctrl-Q to detach back to the list, `n`/`N` new, `D` kill, `?` help, and a bottom bar of key hints. Each session lives in its own git worktree.
- k9s (Apache-2.0): `:` command mode, `/` filter with regex and `/!` inversion, `?` "Show active keyboard mnemonics and help", single-letter actions (`d` describe, `l` logs, `e` edit), `ctrl-d` delete with confirmation and `ctrl-k` kill without, `esc` back out of a view ([commands](https://k9scli.io/topics/commands/)).
- lazygit (MIT): `?` keybindings menu, `esc` cancel, `R` refresh, `J`/`K` or PgUp/PgDn scroll the main view while the list keeps focus, `ctrl+z` suspend ([keybindings](https://github.com/jesseduffield/lazygit/blob/master/docs/keybindings/Keybindings_en.md)).
- btop (Apache-2.0): a configurable update interval ("recommended 2000 ms or above for better sample times for graphs"), every highlighted key clickable with the mouse, boxes toggled by number keys ([README](https://github.com/aristocratos/btop)).

Recommended project-tab layout for `theseus top` (the Host tab is unchanged):

```
┌ AGENTS ───────────────────────────┐┌ project-gateway-2  working  T-0412  gen 3 ───────────┐
│● project-gateway-1  free    -     ││ [Live] Diff  Task  Events                             │
│● project-gateway-2  working T-0412││                                                       │
│● project-gateway-3  waiting T-0415││   live mirror of the pane (capture-pane -p -e)        │
│○ project-clean-1    blocked -     ││                                                       │
└───────────────────────────────────┘│ > send line (i to focus, Enter sends, Esc leaves)     │
 WAITING / EVENTS panels as today    └───────────────────────────────────────────────────────┘
 Enter attach  o window  i send  R restart  x stop  d disable  tab views  / filter  ? help  q quit
```

- Agent list: the existing AGENTS DataTable, with state glyph and color, task, idle minutes, restarts and drift; the selection drives the detail panel.
- Detail panel tabs: Live (mirror), Diff (`git diff --stat` and the patch of the agent's current task worktree against its base, refreshed every 5 s only while visible; theseus keeps worktrees per task, not per session, because tasks own paths and merges while the session lives across tasks), Task (task record and handoff), Events (this agent's events).
- Keys, keeping today's bindings (`q`, `tab`, `1`-`9`, `r`, `p`, `c`, `/`, `a`, `w`, `e`) and adding: Enter attach in place, `o` open in a new window of the user's tmux (only when `$TMUX` is set), `i` focus the send line, `R` restart (confirm), `x` stop (confirm, refuses with a task unless confirmed twice), `d` toggle disable, `J`/`K` scroll the mirror into history, `?` Textual's built-in help panel (`App.action_show_help_panel`), `ctrl+p` Textual's command palette for the rarer actions (`up`, `down --all`, `migrate`). The Footer already shows key hints. Today's Enter (jump to a legacy pane with `switch-client`) remains for unmanaged agents.

Mirror:

- Poll `capture-pane -p -e -t %N` for the selected agent only, in a Textual worker thread, at 4 Hz by default (`top --mirror-hz`), with a SHA-1 of the output to skip unchanged frames; render with `rich.text.Text.from_ansi(...)`, no wrap, cropped to the panel (the agent screen is a fixed 220x50; the panel scrolls horizontally rather than resizing the agent). No `-J`: the mirror shows the screen as laid out.
- Unselected agents get only their state from the existing 5 s snapshot; no captures.
- Scrollback (`J`/`K` in the mirror, `theseus agents logs`) depends on tmux history, which can be empty when the agent draws on the alternate screen (Claude Code's fullscreen renderer, the `tui` setting in the CLI reference). Fallback: `logs --raw` (pipe-pane, opt-in) and `logs --transcript` (the conversation transcript of the stored session id); the mirror then shows the visible screen only.
- Cost: measured 4.1 ms per capture, about 16 ms CPU per second at 4 Hz, independent of the number of agents.
- Control mode later (phase 5, optional): one persistent `tmux -L theseus -C` client per dashboard with `refresh-client -f no-output`, used to run `capture-pane` at 0.09 ms and to subscribe (`refresh-client -B`) to `pane_dead` and `pane_current_command` for instant state changes. Worth it only if the mirror moves to 10 Hz or more, or several mirrors show at once.

Send path (one implementation, `Supervisor.send(agent, text, verify=True)`, used by the dashboard, `instance send/type` for managed agents and the queue):

1. Refuse or queue while the agent is busy (Claude `status` or `busy_pattern`); the dashboard offers "send anyway" on a second Enter.
2. Single line: `send-keys -l -t %N -- TEXT` (the `--` keeps text that starts with `-` from being read as a flag). Multi-line: `load-buffer -b theseus-send -` from stdin, then `paste-buffer -p -d -b theseus-send -t %N` (bracketed paste, so newlines do not submit early).
3. Verify the input: within 1 s, the last screen lines contain the first 40 characters of the text (whitespace-normalized); otherwise report `send-unconfirmed`. How Claude Code's input box shows a long line or a bracketed paste (it may collapse a paste into a placeholder) is untested, so phase 0 checks it; until then the queue path only warns and still presses Enter, and only the dashboard offers to clear the line (`C-u`) instead.
4. Press Enter (`send-keys -t %N Enter`).
5. Verify acceptance: within 10 s the agent is busy or the input box no longer shows the text; otherwise event `send-unconfirmed`, and the dashboard shows a warning on the agent row.
6. Event `sent` records the agent, length and a short hash of the text, never the text itself.

Open pane:

- In place (Enter): `with app.suspend(): subprocess.run(["env", "-u", "TMUX", "tmux", "-L", sock, "attach", "-t", "=" + S])`; C-q detaches and the dashboard resumes. Works inside or outside the user's tmux (inside, it nests one client in the dashboard's pane, which the root-table detach key handles).
- New window (`o`, `theseus agents attach --window`): run against the user's server `tmux new-window -n NAME "env -u TMUX tmux -L theseus attach -t =S"`; the window closes when the inner client detaches. For managed agents this is the only command theseus sends to the user's server, on an explicit keypress; the legacy exceptions (queue driving unmanaged panes, the legacy `switch-client` jump, `migrate` typing `/exit`) are listed in section 1.
- Read-only peek (`theseus agents attach --read-only`): `attach -r`.

### 4.7 Security and safety

- Never touch the user's tmux server on behalf of managed agents (legacy exceptions in section 1): the seam has no default-server path except `Server.legacy()`; reconcile, the queue, the hook and every test use the private socket; a unit test fails if any managed code path builds a tmux argv without `-L`/`-S`. Test environments set `TMUX` to a bogus path and put a fake `tmux` first on PATH that fails when called without a socket flag in managed tests.
- Socket permissions: rely on tmux's 700 `tmux-UID` directory and owner-only socket; never call `server-access`; `theseus doctor` checks that the socket directory is owned by the user and not group or world accessible. Prefer `-L` over a socket under the data root (path length limit, network home directories).
- Secrets: profile env is applied by the `_exec` wrapper, never on a command line, never typed into a pane, and never in `agent_runs` (only a hash of names and argv); the server starts with an allowlisted environment and `update-environment ""`; events never contain sent text or env values; crash captures, raw pane logs and `runs.log` are mode 600 under the data root and excluded from `theseus db backup` (they are files, not store rows); `logs --raw` is off by default because a raw stream records everything the agent prints.
- No silent consent: startup dialogs are surfaced, not answered, unless the user opted into `trust_dialog = "accept"`; MCP approvals and permission prompts are never answered by theseus.
- Blast radius: agents' `TMUX` points at the private server, so an agent's tmux commands stay off the user's server; deny `tmux kill-server` in agent settings; server loss is recovered by reconcile.

### 4.8 Migration from hand-made panes

1. Nothing changes until a project sets `[agents] managed = true`. Unmanaged rows keep `server = NULL` and today's behavior.
2. `theseus agents up --dry-run` lists the plan, including registered legacy agents that collide with slot names.
3. `theseus agents migrate NAME|--all` per agent: wait until free (or `--with-task`), read its session id from `claude agents --json` by the pane's process tree, type `/exit` into the user's pane, wait for exit, spawn the managed agent under the same name with `--resume <id>`, set `managed = 1`, `server`, `target`, `pane`, and record intent `migrate`. The user's pane is left at its shell prompt with a one-line note; theseus never kills it.
4. Agents with in-progress tasks are deferred unless `--with-task` (resume keeps the task context; the queue's owner field uses the agent name, which does not change).
5. `[[instances.panes]]` entries for migrated names are reported by `theseus doctor` as removable. Mixed projects (some managed, some legacy) are supported.

### 4.9 Phased implementation plan with tests

Phase 0, spikes (half a day, no product code): confirm on Claude Code 2.1.326 that `claude agents --json` reports the new `sessionId` after `/clear` and `/compact` for an interactive session; whether hooks passed with `--settings` merge with user hooks and with an `extra_args` settings file; `resize-window -A` plus a `client-detached` restore under `window-size manual`; how Claude Code's input box renders a long single line and a bracketed paste (for send verification); whether history is empty under the fullscreen renderer. Already checked while writing this design: the gateway launcher in use ends with `exec claude`, and the tmux target forms in 3.4. Results are recorded in section 6.

Phase 1, tmux seam (no behavior change): `theseus/tmux.py`; route `agents.tmux`, `pane_alive`, `pane_screen`, `top.agent_processes`, `top.jump_to` through it; `tmux_()` in `lib.sh`. Tests: a real private-server fixture (skipped when tmux is missing) with a short `-S` path such as `/tmp/th-<rand>/s` and `-f /dev/null`, killed in `tearDown`; existing fake-tmux tests updated to accept `-L`/`-S`; a test that the legacy path never passes a socket and always strips `TMUX`.

Phase 2, spawn and reconcile: migration 3; `[agents]` and `[supervisor]` parsing and `config check` rules; naming; `_exec`; `agents up/down/ls/restart/attach/logs`; readiness. Tests: a fake agent CLI (Bash) with modes `normal` (prints a banner and a `> ` prompt, shows the busy marker while "working" on input, writes a handoff on request), `crash-after N`, `exit-clean`, `trust-dialog`, `resume-missing`, and argument checks for `--name`, `--session-id`, `--resume`; `HOME` and `THESEUS_DB` in a temp directory; for Claude-signal tests a fake `claude agents --json` stub. Cases: `up` twice is a no-op; killing one pane makes `up` restart only that one; config drift is reported, not applied; lowering a count stops a free agent and defers a busy one; trust dialog gives `blocked` and an `error` event; the pane start command contains no env value.

Phase 3, supervision and queue: `pane-died` hook and `_died`; restart policy with backoff and give-up; crash capture; server-lost recovery (kill the private test server, run `up`, every agent resumes with its stored id); managed `relaunch()` via `respawn-pane`; verified send in the queue; `waiting`/`blocked`/`failed` states in `agents.state()` and `queue wait`. Tests: a crash loop gives up after `max_crashes` with one `error` event; exit status and signal recorded; a full queue cycle on the private test server with the fake agent (assign, handoff, delivered, compact, next task); a send whose text never appears yields `send-unconfirmed`.

Phase 4, dashboard: Live/Diff/Task/Events tabs, mirror worker with hash skip, send line, in-place attach via `App.suspend`, `o` new window, confirm dialogs, help panel. Tests with Textual Pilot on the private test server: the mirror shows the fake agent's banner; a send reaches the fake agent's log; `o` issues exactly one `new-window` against a fake user tmux and nothing else (assert the recorded argv); a micro-benchmark that the mirror loop stays under 5 percent of one core at 4 Hz.

Phase 5, migration and polish: `agents migrate`, doctor checks, docs (`getting-started.md`, `workflow.md`, `configuration.md`, `architecture.md`), optional systemd user unit, optional control-mode channel, optional `SessionStart` hook if phase 0 showed it is needed.

### 4.10 Open questions for the user

1. Launch agents in the project's main checkout (already trusted, so no trust dialog), or elsewhere?
2. Start agents at login with a systemd user unit (and linger), or only on the first `theseus` command after a reboot?
3. After a crash or reboot, always resume the previous conversation, or start fresh when the agent held no task?
4. Should agent counts live in `theseus.local.toml` (per machine) rather than `theseus.toml`?
5. Restart a thrashing agent automatically (fresh session, task kept), or keep reporting it to the coordinator as today?
6. One private server for all projects on the host (`-L theseus`), or one per project?
7. Migration 3 makes older theseus copies refuse the shared host database: upgrade every installed copy together?
8. Allow the opt-in `trust_dialog = "accept"`, or always require a human?

## 5. Sources

- theseus at `e846d0e`: `src/theseus/profiles.py`, `agents.py`, `workqueue.py`, `store.py`, `top.py`, `tui/app.py`, `scripts/instance-*.sh`, `scripts/lib.sh`, `docs/architecture.md`, `docs/workflow.md`, `docs/agent-profiles.md`, `docs/configuration.md`
- `man tmux` (tmux 3.4); tmux binary strings for the nesting message
- tmux control mode wiki: https://github.com/tmux/tmux/wiki/Control-Mode
- Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Claude Code background sessions (agent view): https://code.claude.com/docs/en/agent-view
- Claude Code hooks: https://code.claude.com/docs/en/hooks
- Claude Code settings and precedence: https://code.claude.com/docs/en/settings
- Claude Code headless / Agent SDK CLI: https://code.claude.com/docs/en/headless
- Claude Code agent teams (tmux display mode): https://code.claude.com/docs/en/agent-teams
- `claude --help`, `claude agents --help`, `claude agents --json` output fields (Claude Code 2.1.326)
- Issue reports on `/clear` and session ids: https://claudeissues.com/issue/36213-bug-clear-creates-new-session-jsonl-but-never-updates-claude-sessions-pid-json-b, https://claudeissues.com/issue/28692-clear-does-not-fire-sessionstart-hook-despite-assigning-a-new-session-id, https://claudeissues.com/issue/70606-clear-changes-session-id-in-hook-input-without-re-running-sessionstart-hooks-bre
- libtmux: https://pypi.org/pypi/libtmux/json, https://github.com/tmux-python/libtmux, `src/libtmux/common.py`, control-mode draft https://github.com/tmux-python/libtmux/pull/605
- tmuxp: https://github.com/tmux-python/tmuxp
- tmuxinator: https://github.com/tmuxinator/tmuxinator
- claude-squad (AGPL-3.0): https://github.com/smtg-ai/claude-squad and its README
- agent-deck: https://github.com/asheshgoplani/agent-deck
- ntm: https://github.com/Dicklesworthstone/ntm
- ccmanager https://github.com/kbwo/ccmanager, ccmux https://github.com/epilande/ccmux, fleet https://github.com/nicknisi/fleet (licenses via GitHub API)
- k9s commands: https://k9scli.io/topics/commands/
- lazygit keybindings: https://github.com/jesseduffield/lazygit/blob/master/docs/keybindings/Keybindings_en.md
- btop: https://github.com/aristocratos/btop
- Textual `App.suspend` and `App.action_show_help_panel` (Textual 8.2.8 installed with theseus)
- Measurements on the development machine (throwaway private tmux server): capture-pane 4.14 ms per subprocess call, list-panes 2.78 ms, capture-pane over control mode 0.09 ms; `claude agents --json` 0.11 s

## 6. Decisions, phase 0 results and implementation notes

### 6.1 Decisions (answers to section 4.10)

1. Agents start in the project's main checkout, which the agent CLI already trusts; they work in task worktrees as before (`[agents] cwd = "root"`).
2. No systemd: no user unit and no `systemd-run --scope` wrapper. Agents start on `theseus agents up` and are reconciled by the first `theseus agents up` or queue tick after a reboot.
3. After a crash, a reboot or a lost server an agent resumes its previous conversation only when it holds a task, otherwise it starts fresh (`[agents] resume = "task"`, the default). An explicit restart, a relaunch onto another profile and a migration always resume.
4. Counts live in `theseus.local.toml`, with a host-wide default in `[defaults] agents.counts`.
5. A thrashing agent restarts at once on a fresh session, keeps its task and gets the task's restart prompt once ready.
6. One private server for all projects of a host user, `tmux -L theseus`.
7. The store change is migration 5 (migration 3 holds the gateway tables and migration 4, merged meanwhile, the typed gateway error columns); every installed copy of theseus on a machine must be upgraded together, because an older copy refuses a newer database.
8. No `trust_dialog` option: startup dialogs are always surfaced, never answered.
9. No libtmux dependency: one internal module, `theseus.tmux`, which always names the socket. Its isolated-server test approach is borrowed; no libtmux code is used.

### 6.2 Phase 0 results

Run on Claude Code 2.1.326 and tmux 3.4 on a throwaway private server with a short socket path, never the user's tmux server, with Claude Code on a free on-prem gateway; every agent process was killed afterwards.

- `--session-id`: `claude agents --json` reported exactly the preassigned id, and the gateway launcher's `exec claude` made the pane's pid the agent's.
- `/clear`: the JSON entry showed the new session id immediately (in this version the session record file did too). The supervisor therefore refreshes the stored id from the JSON every tick and needs no `SessionStart` hook.
- `/compact`: not verified to completion. The gateway answered with its parallel-request limit and the spike was interrupted so as not to compete with live agents for gateway slots; during the attempt the id was unchanged. Either outcome is covered by the per-tick refresh.
- Startup trust dialog: a session that shows the folder trust question has no entry in `claude agents --json` at all. `blocked` is detected from the screen (`interactive.startup_dialogs`), and a missing entry never counts as a dead agent.
- Hooks passed with `--settings`: a `SessionStart` hook from `--settings` fired together with one from the user's own settings (they merge), but of two `--settings` flags only the last one applied. theseus therefore injects no `--settings`; a profile's `extra_args` may already carry one.
- Input box: a 1500-character text typed with `send-keys -l` arrives fast enough to count as a paste and is shown as `[Pasted text #N]`; a multi-line bracketed paste shows `[Pasted text #N +3 lines]`. A long line that wraps over several visual lines loses only its last visual line to one `C-u`. The verified send treats the placeholder as text still in the input line (`interactive.paste_placeholder`), presses the clear key until the input line is empty, and confirms by the busy marker rather than by the first characters of the text.
- History: under the fullscreen renderer the pane is on the alternate screen (`alternate_on` 1) and `history_size` stays 0. `theseus agents logs` prints the screen and `--transcript` names the conversation file; the mirror shows the visible screen.
- Sizes: `set -g window-size manual` made tmux 3.4 exit ("server exited unexpectedly") at the first `new-session` when set in the `-f` configuration, reproducibly, and once at run time. It is never set globally. `resize-window -x W -y H` on the new window pins its size (it sets `manual` for that window only, which is safe); hooks `client-attached` `resize-window -A` and `client-detached` `resize-window -x W -y H` resize the agent to an attaching client and restore it on detach, checked with pty clients of 100x30 and 300x70. With the default `window-size latest` and no hooks, a window keeps the size of the last client after it detaches.
- tmux 3.4 `display-message -p -t TARGET` exits 0 with empty output for a pane or session that does not exist, so existence is read from the output (the seam prints `#{pane_id}` first). This also made hand-made panes that were gone look alive; fixed.
- Control characters in a format are printed as octal escapes (`\037`), so fields are joined with printable text.
- When a pane dies under `remain-on-exit`, tmux 3.4 scrolls its screen into the history and shows only "Pane is dead (status N, ...)"; the last words of an exited agent (for example Claude Code's "No conversation found with session ID") are read with the history (`capture-pane -S -100`).
- An agent that exits within milliseconds of `new-session` can fire its `pane-died` hook before theseus has set the pane options. The hook therefore identifies the agent by its session name and the run by the pane's pid, and a missed hook is caught by the next reconcile, which sees the dead pane.

### 6.3 Implementation notes

- Store, migration 5: `agents` gains `managed`, `server`, `target`, `session_id`, `slot`, `desired`, and `phase` with `phase_epoch` (`starting`, `ready`, `blocked`, `failed`); `agent_runs` as in 4.5 plus `resume`. A migration step may be a function; columns are added only when missing, so the migration runs over a partly migrated file.
- Addresses: a supervised agent's registry `pane` is its session target `=project--agent:`, not the volatile `%N`. `theseus.tmux.resolve()` sends addresses to the private server and everything else to the user's server; the shell scripts use `tmux_at PANE ...` with the same rule. A caller that forgets the socket fails ("can't find session") instead of typing into an unrelated pane. The existing dashboard popup works unchanged on supervised agents: its capture, keys and send go through this resolution, and Open tmux pane opens a new window of the user's tmux.
- Hand-made panes are driven with the same argv as before. `$TMUX` is dropped for them only when it points at the private server (inside a supervised pane or a hook); a user whose own tmux uses another socket keeps working.
- The pane command pins this interpreter and import root (`python -c` with a `sys.path` insert) instead of exporting `PYTHONPATH` to agents; `THESEUS_*` of the caller is set on each session with `new-session -e`, so `_exec` uses the caller's store and configuration. The pane-died hook is a small script in the host data directory; a data path containing a quote is refused.
- Readiness: for Claude profiles a status entry in the pane's process tree; for others (or when the status command fails) a non-empty screen without the busy marker, `ready_pattern` if set, after a short settle time. A start that is not ready within `ready_seconds` becomes `blocked` with an `error` event instead of counting as a crash, so a slow start is never restarted in a loop.
- A clean exit nobody planned (a worker typing `/exit`) restarts once without backoff and then counts like a crash. `theseus agents restart` and `up --reset` start the crash budget over.
- A graceful stop types the exit command only at an idle prompt (phase `ready` and no `waiting` status); a `blocked` or `waiting` agent is ended with SIGTERM, then SIGKILL, so no keystroke ever answers a dialog or a permission prompt.
- `[defaults] agents.counts` in the host configuration is a fallback for projects whose own files set no counts; it is never merged into a project's counts.
- Events: `spawned`, `ready`, `exited`, `restarted`, `stopped`, `drift`; failures use `error`, and a supervised agent that is `waiting`, `blocked` or `failed` while it holds a task raises one `stalled` event. The existing `send_failed` stays the send failure event (no `sent` or `send-unconfirmed`).
- `theseus queue wait` and `queue run` sleep until the next interval or the earliest pending wake time (a gateway resend, the end of a restart backoff), so both fire within a second or two of their time.
- Tests run real private tmux servers on short throwaway sockets with a fake agent CLI; a wrapper `tmux` on `PATH` sends every socket-less call to a separate throwaway "user" server and logs it, which the guard assertions require to be empty for supervised paths (the migration test uses it as the user's server). `THESEUS_TMUX_GUARD` makes the default private socket refuse to open under the test harness.
- Not built yet: the dashboard UI of 4.6 (only the snapshot API: `supervisor.snapshot`, `capture`, `send`, `attach_argv`, `open_window`), raw pane logs (`logs --raw`), the control-mode channel, `theseus doctor` socket checks, and the systemd unit (declined).
