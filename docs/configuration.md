# Configuration

## Configuration layers

theseus itself is user- and site-agnostic: anything specific to one person or machine (agent launchers, gateway models and environment, MCP server choices, client settings files, tool locations, pools, store retention) lives in configuration, not in the package. Values come from five layers; each overrides the ones above it, and tables merge key by key.

| Layer | Where | Holds | Shared |
| --- | --- | --- | --- |
| package | built into theseus | defaults and the agent presets | everyone |
| host | `~/.config/theseus/config.toml` (`XDG_CONFIG_HOME` is respected; `THESEUS_CONFIG` overrides the path) | agent profiles, pools, host store settings, tool locations and defaults for this user on this machine | every project on this machine |
| project | `theseus.toml` in the project root, committed | how the project builds, verifies and works: commands, paths, rooms, targets, its profile choice and capacity | everyone working on the project |
| project-local | `theseus.local.toml` in the project root, Git-ignored | this machine's values for this project: where the original software lives, a different data root | this checkout |
| env | `THESEUS_DATA`, `THESEUS_SCRATCH`, `THESEUS_WORKTREES` and other `THESEUS_*` variables, and command line flags such as `--profile` | one shell or one command | one command |

What belongs where:
- A launcher, a gateway model and its environment, the MCP servers your agents start with, a client settings file passed with `extra_args`, the profile's `capability` rank (default 1, see [Agent profiles](agent-profiles.md#capability)): a `[workers.<name>]` profile in the host file. A project then names the profile (`[workers] default = "gateway"`) and sets only what is about the project, such as `capacity`.
- Pools and their priorities, host store retention and backups: the host file (`[pools.<name>]`, `[store]`).
- Tool locations: `[tools.paths]` in the host file for every project, or `theseus.local.toml` for one checkout. `[tools] need` stays in `theseus.toml`.
- Everything that describes the project itself: `theseus.toml`.

`theseus setup host` writes a commented starter host file; see [Host configuration](#host-configuration).

```sh
theseus config show              # resolved project configuration (JSON)
theseus config show --sources    # every resolved value with the layer it came from
theseus config get paths.data    # one value
theseus config check             # validate the host file and, inside a project, the project files
```

### Example

`~/.config/theseus/config.toml`:

```toml
[defaults]
limits.heavy_runs = 4

[workers.gateway]
preset = "claude-code"
command = "agent-launcher"
model = "provider/model-name"
model_flag = ""
env = { GATEWAY_MODEL = "{model}" }
mcp_servers = ["theseus"]
extra_args = ["--settings", "~/.config/theseus/worker-settings.json"]
pool = "gateway"

[pools.gateway]
max = 4
priority = ["first-project", "second-project"]

[tools.paths]
ghidra = "/opt/ghidra"
```

A project's `theseus.toml`:

```toml
[workers]
default = "gateway"

[workers.gateway]
capacity = 2       # everything else comes from the host profile
```

`theseus config show --sources` then prints lines such as `workers.gateway.command = "agent-launcher"    # host`, `workers.gateway.capacity = 2    # project` and `workers.gateway.interactive.busy_pattern = "esc to interrupt"    # package (preset claude-code)`.

## Files

- `theseus.toml` in the project root holds all project settings and is committed.
- `theseus.local.toml` is merged over it and is Git-ignored. Use it for machine-specific values such as where the original software or a tool is installed.
- The environment variables `THESEUS_DATA`, `THESEUS_SCRATCH` and `THESEUS_WORKTREES` override the folder settings for one shell.

## Reference

| Section | Keys |
| --- | --- |
| `[project]` | `name`, `prefix` (used in folder and process names), `color` (the project's color on the private tmux server and in the console: `#rrggbb` or one of `steel`, `olive`, `tan`, `rose`, `teal`, `violet`, `gold`, `sea`, `orange`, `lavender`; unset, one is derived from the name; see [The private tmux server](#the-private-tmux-server)) |
| `[paths]` | `data` (evidence, logs, decompiled code, worker records), `scratch` (builds, temporary files), `worktrees`, `original` (read-only original software) |
| `[commands]` | `build`, `smoke`, `verify`, `smoke_timeout`. Each is a list of shell commands; `$THESEUS_BUILD_DIR` is a build folder unique to each checkout |
| `[limits]` | `heavy_runs`: how many builds and verifications may run at once on this machine |
| `[workers]` | `default` (agent profile), `retries`; `[workers.<profile>]` adjusts or defines a profile (see [Agent profiles](agent-profiles.md)) |
| `[instances]` | Long-running agents: `panes` (hand-made panes `[{ name, pane }]`; pane ids belong to one machine, so put them in `theseus.local.toml`, and `theseus config check` warns when `theseus.toml` lists any), `coordinator` (session name to report to, for agents that support messaging), `brief`, `idle_minutes`, `handoff_settle_seconds`, `send_verify_seconds` (how long a verified send waits for the busy marker, default 10); `busy_pattern`, `launch`, `sessions_dir` override the profile |
| `[agents]` | Supervised agents (see [Supervised agents](workflow.md#supervised-agents)): `managed` (false; true makes theseus start and supervise them), `name` (`"{project}-{profile}-{n}"`; `{project}`, `{profile}` and `{n}`, which is required), `cwd` (`root`, the main checkout; `data`; or a path), `resume` (`task`: resume the conversation after a crash or reboot only for an agent that holds a task; `always`; `never`), `[agents.counts]` (live agents per profile, usually in `theseus.local.toml`; down or disabled agents do not count, a downed name is never reused automatically and names need not be contiguous), `[[agents.extra]]` (`name`, `profile`, `rooms`: named agents outside the counted slots) |
| `[queue]` | `interval` (30 seconds between ticks of `queue run` and `queue wait`), `nudge` (the idle reminder text), `deadline` (added to task messages), `send_budget_seconds` (25: how long one tick waits for its verified sends together; a send still unresolved is finished by the next tick), `assign_backoff_seconds` (300: how long the queue skips a task whose assignment failed, after its `error` event; a failure that outlasts it becomes an attention item), `driver` (true: `theseus agents up` starts and supervises the `queue run` driver; false leaves it to you) |
| `[attention]` | The attention list's push into the coordinator (see [Attention](workflow.md#attention)): `notify` (empty: off; `"pane"`: after each tick the queue driver types one line into the pane `theseus coordinator register` recorded whenever items were added), `remind_minutes` (30: one reminder for items still open that long after a push; 0 turns it off), `escalate_minutes` (60: an item open that long, or the coordinator pane gone or stuck that long, shows a red ESCALATED line in the console and runs the host's `escalate_command`; 0 turns escalation off) |
| `[coordinator]` | The supervised coordinator session (see [Coordinator session](workflow.md#coordinator-session)): `profile` (no default: the agent profile it runs, which needs interactive settings; usually set in `theseus.local.toml` or the host's `[defaults] coordinator.profile`), `autostart` (false; true makes `theseus agents up` also run `theseus coordinator up`), `prompt` (`.agents/skills/theseus-coordinating/session-prompt.md`: the project-owned template of a new conversation's first prompt, with `{project}`, `{root}` and `{session}`; the packaged one when the file is missing) |
| `[models]` | `context`: model ids (or `prefix*`) to context windows in tokens, for the console's context use (see [Model context windows](#model-context-windows)) |
| `[gateway]` | Gateway error recovery in persistent panes: `attempts` (3), `backoff` (`[120, 240, 480]` seconds), `slow_codes` (`[530]`) and `slow_factor` (2), `stagger_seconds` (30, spacing of 429 resends across a pool), `resend` (the continue text), `clear_tokens` (120000), `fresh_after_errors` (2), `screen_lines` (15, bottom lines searched for an error), `recovery_seconds` (900: a resent agent counts as recovered, and its attempts start over, once its worktree changed after the resend or after this long) |
| `[git]` | `base` (`HEAD`, or `origin/main` to fetch first; workers then start from the remote, so task record changes the queue commits locally, such as a release or a new profile, must be pushed before the task is assigned again), `remote`, `branch` |
| `[verification]` | `code_paths`, `doc_paths` (changes there need no verification), `max_unverified_merges`, `max_unverified_hours` |
| `[rooms]` | Clean-room mode: `enabled`, `taint` (`until-clear` or `permanent`), `clean_instances`, `dirty_instances`, `dirty_paths`, `dirty_data`, `spec_paths` |
| `[re]` | `targets` (binaries to decompile: `name`, `path`, `sha256`, and optionally `corpus` to point at an existing decompiled corpus), `citation_index` (default `docs/re/citation-index.md`), `clients` (agent clients for the REA server), `address_low`, `address_high` |
| `[docs]` | A project with its own index generator: `index_command` (writes the indexes), `check_command` (exits nonzero when stale), `generated` (paths they produce). theseus then runs these instead of its own writers in merge, `theseus docs index|check` and the push guard |
| `[tools]` | `need`: external tools this project requires |
| `[consumers]` | Per file extension, a pattern for the names a file declares (defaults cover common languages) |

The scaffolded `theseus.toml` documents every key with comments. It sets `[workers] default` and `[workers.claude-code]` explicitly; remove a key there to let the host file decide it.

## Host configuration

`~/.config/theseus/config.toml` is optional. Its sections:

| Section | Keys |
| --- | --- |
| `[workers.<name>]` | Host-wide agent profiles, the same fields as in `theseus.toml` (see [Agent profiles](agent-profiles.md)). A project section with the same name overrides only the keys it sets; `env` and `interactive` merge key by key. Two keys are read from the host file only, since every project must agree on them: `fallback` (another profile to move this profile's agents to while it is down) and `[workers.<name>.health]` (`probe`, `probe_seconds`, `recover_after`, `down_errors`, `down_window_minutes`, `probe_timeout`; see [Profile fallback](#profile-fallback)) |
| `[pools.<name>]` | `max`, `priority` (see [Sharing agents between projects](#sharing-agents-between-projects)); `tokens_per_minute` (optional, no default: the shared key's token rate the working agents' estimate must stay under) and `turns_per_minute` (6: requests a working agent is assumed to send a minute, each the size of its context; see [Key-level rate limits](#key-level-rate-limits)) |
| `[store]` | `[store.retention]` and `[store.backups]` (see [Host store](#host-store)) |
| `[tools.paths]` | Tool locations on this machine, by tool name (see [External tools](#external-tools)) |
| `[models]` | `context`, as in a project; the host table is the usual place for gateway models |
| `[gateway]` | The host-wide gateway circuit breaker: `breaker_errors` (3 different agents), `breaker_window_minutes` (10), `strong_codes` (`[530]`, one such error pauses the pool); and the cap cut after a key-level 429 (the shared key's `max_parallel_requests`, `Rate limit exceeded for api_key`, an account's per-minute rate limit): the pool's working cap drops by `parallel_cap_step` (1) and holds for `parallel_cap_minutes` (10) after the latest such error, then rises one slot every `cap_step_up_minutes` (10), host-wide; such errors never count toward the breaker or a profile's health (see [Key-level rate limits](#key-level-rate-limits)) |
| `[supervisor]` | The private tmux server of supervised agents: `socket` (`theseus`, the `tmux -L` name; `THESEUS_TMUX_SOCKET` names another, `THESEUS_TMUX_PATH` a full socket path), `size` (`220x50`, the fixed agent screen; an attached client resizes it until it detaches, and the console's pane mirror sizes it to what the mirror shows while the mirror is open), `history_limit` (50000), `detach_key` (`C-q`), `max_agents` (12 sessions on the host), `env_passthrough` (names or glob patterns of variables the server passes to agents besides `HOME USER LOGNAME SHELL PATH LANG LC_* TMPDIR XDG_* THESEUS_*`), `launch_shell` (`exec`, or `login` to run the profile command through `$SHELL -lc`), `ready_seconds` (90), `[supervisor.restart]` `backoff_seconds` (`[5, 30, 120, 600]`), `max_crashes` (5), `window_minutes` (30) |
| `[tmux]` | The private tmux server's keys and personal additions (see [The private tmux server](#the-private-tmux-server)): `prefix` (`C-b`), `extra_conf` (empty: a file sourced last on the private server only) |
| `[attention]` | `escalate_command` (empty by default): a command line (run with `sh -c`) or argument list, run once when an attention item stays open past `escalate_minutes` or the coordinator pane is gone or stuck, with the summary on stdin; for example a desktop notification command. Nothing else runs it |
| `[defaults]` | Values for project keys a project leaves unset: `workers.default`, `workers.retries`, `limits.heavy_runs`, `paths.data`, `paths.scratch`, `paths.worktrees`, `instances.idle_minutes`, `instances.handoff_settle_seconds`, `instances.send_verify_seconds`, `queue.interval`, `queue.send_budget_seconds`, `re.clients`, `agents.counts`, `agents.resume`, `agents.cwd`, `attention.notify`, `attention.remind_minutes`, `attention.escalate_minutes`, `coordinator.profile`, `coordinator.autostart`, and the `gateway.*` keys of the project table |

`theseus config check` reports an unknown section, an unknown profile key, pool key or store key as an error naming the file and the key, and an unknown `[defaults]` key as a warning (it is ignored). A host file that does not parse makes every command that reads it (anything that loads a project, `theseus pool`, the host store settings) stop with that message; the console leaves the pools out. An unknown top-level section, in the host file or in a project's `theseus.toml` or `theseus.local.toml`, is ignored by every other command with a warning on stderr, printed once per process, so a long-running `theseus queue wait` or `queue run` keeps going when a newer release adds a section.

```sh
theseus setup host                          # write a commented starter (refuses to replace an existing file)
theseus setup host --print                  # print it instead
theseus setup host --force                  # replace an existing file with the starter
theseus setup host --import-project PATH --dry-run   # what would move from a project
theseus setup host --import-project PATH    # move it
```

`--import-project` copies the project's `[workers.<name>]` tables (from `theseus.toml`, and from `theseus.local.toml` for profiles not already in `theseus.toml`) and the `[tools.paths]` table of its `theseus.local.toml` into the host file, comments included: into a new starter file, or appended to an existing host file, refusing a profile or table the host file already has. It never edits the project files; it prints the line ranges you can remove from them once `theseus config check` passes. Keep project-specific keys such as `capacity` in the project.

## The private tmux server

Supervised agents, the queue driver and the coordinator session run on theseus's own tmux server (`tmux -L theseus`), with a configuration theseus generates (`theseus tmux conf` prints its path, `--print` its text) and applies to that server only. It never touches your own tmux server and never sources your `~/.tmux.conf`, whose session-persistence plugins (tmux-resurrect, tmux-continuum) must not see theseus sessions. Applying it (`theseus tmux apply`, prefix `r`, and automatically when an older theseus started the server) only sets options and key bindings, so running agents keep their size, process and pane-died hook.

The configuration: `tmux-256color` with true color, OSC 8 hyperlinks passed through, extended keys, `escape-time` 0, focus events, `set-clipboard`, a 50000-line history (`[supervisor] history_limit`), windows and panes numbered from 1, `window-size latest`, the mouse on with the wheel entering copy mode, and vi copy mode (`v` selects, `y` copies and leaves copy mode, since keys typed into a pane in copy mode never reach the agent; theseus also leaves copy mode itself before it types into a pane).

| Key | Does |
| --- | --- |
| prefix `a` | a popup of `theseus attention` |
| prefix `T` | a popup of the console |
| prefix `C` | switch to the project's coordinator session |
| prefix `n`, `p` | the next or previous coordinator or agent session of the same project |
| prefix `w` | choose-tree of every theseus session, sorted by name and so grouped by project, with role and task |
| prefix `d`, or `C-q` | detach (`[supervisor] detach_key`, without the prefix) |
| prefix `r` | regenerate and apply the configuration |
| prefix prefix | send the prefix key itself to the agent |

**Prefix and nesting.** The prefix is `[tmux] prefix` in the host config, `C-b` by default. Attaching from inside your own tmux nests a second client (theseus unsets `TMUX` for it), so the two prefixes must differ: with an outer prefix of `C-a`, `C-b` reaches the theseus client and `C-a` stays with your own tmux. With the same prefix on both, press it twice to reach the inner one (and bind `send-prefix` on the outer one). The agents themselves see the prefix key only when it is pressed twice (Claude Code uses `C-b` to send a command to the background; press `C-b C-b`).

**Colors and status bar.** Each project has a stable color, `[project] color` or one derived from its name from a palette of ten readable colors; its sessions' status bar and active pane border take it, and the console uses it as the project's accent. The status bar shows, on the left, the project, the session's role (`coordinator`, the agent's short name, `queue driver`) and the agent's current task; on the right, the attention count (red when nonzero), the pool's working agents over its slots, a gateway pause or cap cut, `queue paused` and the time:

```text
 demo | claude-code-1 | T-0042               [ 2 attention ] | working 2/3 | [ gateway paused ] | 14:05
```

It refreshes every 5 seconds from a small JSON cache the queue driver rewrites after each tick (`theseus tmux status` prints it; `--json` the file), so the status line never runs a query; with no driver it says so, and a cache older than three minutes is marked `driver stale`. The terminal title (`set-titles`) shows the project, role and task too.

**Personal additions.** `[tmux] extra_conf = "~/.config/theseus/tmux.conf"` in the host config sources that file last, on the private server only, for your own bindings or styles. It must never load a session-persistence plugin or source your `~/.tmux.conf`; `theseus config check` warns when it mentions one.

### Deprecated host files

`~/.config/theseus/pools.toml` (`THESEUS_POOLS`) and `~/.config/theseus/store.toml` (`THESEUS_STORE_CONFIG`) are still read, below `config.toml`: `config.toml` wins on each conflicting key. `theseus doctor` and `theseus config check` print a one-line note for each one found. Move their tables into `[pools.<name>]` and `[store]` of `config.toml` and delete them; they will stop being read in a later release.

## Sharing agents between projects

When several projects run on one machine against the same provider limit (for example a model gateway that allows four concurrent agents), declare a pool in the host configuration, `~/.config/theseus/config.toml`:

```toml
[pools.gateway]
max = 4                                   # agents working a task at once, across all projects
priority = ["laser-port", "game-port"]    # earlier projects win when the pool is full
```

A profile joins it with `pool = "gateway"` in `[workers.<profile>]`, usually in the host file too. Only agents working a task hold a slot; idle sessions cost nothing. When a project has ready work but the pool is full, it records a claim; lower-priority projects then start no new tasks, so their running tasks finish and the slots come back. `theseus pool` shows who holds each slot, and the console shows the pool on the PROJECT line. The slots themselves are kept in the host store.

### Profile fallback

Two profiles that reach different models through the same gateway can stand in for each other: the primary is used whenever it is available and the fallback only while the primary is down. In the host file:

```toml
[workers.model-a]
pool = "gateway"
fallback = "model-b"

[workers.model-a.health]
probe = "probe-model model-a"   # exit 0 means healthy; leave it out for passive health
probe_seconds = 600             # how often a down profile is probed (default 600)
recover_after = 3               # consecutive OK probes before switching back (default 3)

[workers.model-b]
pool = "gateway"
```

- Health is host state in the store, the same for every project. A profile goes down only on model-level failures, never on a 429: with a `probe` (set `probe_timeout` above the probe's own request timeout; the probe runs inside a queue tick, which waits for it) (a command string, split like a shell would with a leading `~` expanded, or a list), a 5xx, a connection lost or a server error mid-response from one of its agents makes the next queue tick on the host run the probe (at most once a minute, and once per `probe_seconds` after an OK probe), and a failed probe marks the profile down; without one, such errors from `down_errors` (2) different agents within `down_window_minutes` (10) do. A probe that prints a 429 or a rate limit, or cannot run, says nothing either way; one that does not answer within `probe_timeout` (60 s) failed. theseus knows nothing about the provider: the probe is any command that sends one small request to the model.
- While the primary is down and its fallback is not, each project's supervisor moves the primary's count in `[agents.counts]` onto the fallback, keeping the project's number of agents (the `theseus agents scale` semantics): idle agents of the primary stop at once and fallback agents start; a working one finishes first, unless it sits on a gateway error, in which case the queue releases its task with the worktree kept and the next attempt's task message points at that worktree. A task that names the primary as its profile may run on the fallback, which also takes over the primary's `capacity`; a `minCapability` the fallback does not meet keeps the task waiting. Hand-made (unsupervised) agents of the primary get no new task meanwhile. A primary whose fallback is down too (the fallback needs its own `[workers.<fallback>.health]` for theseus to know) stays where it is, and once on its fallback it stays there even if the fallback fails as well. While a profile with a fallback is down or waiting for its probe, its errors stay out of the gateway circuit breaker; once a probe finds it healthy they count again.
- Recovery: one process on the host probes the down profile every `probe_seconds` and switches back after `recover_after` consecutive OK probes (a failed probe starts the count over); a profile without a probe is retried after `recover_after` x `probe_seconds`. Agents drain back the same way: fallback agents beyond their own count finish their tasks first.
- Every switch is one `fallback` event in each project that uses either profile, and `theseus queue wait` wakes for it. The console shows a red line naming the profile on fallback, since when and why, with the probe count; `theseus pool` prints the same.

### Key-level rate limits

Profiles that share one API key share its limits (requests in flight, requests and tokens a minute). Put them in one pool; a key-level 429 on any of its agents then acts on the whole pool, host-wide:

- The pool is paused for the first backoff (no resends, reminders or new tasks), its working cap drops by `[gateway] parallel_cap_step`, holds for `parallel_cap_minutes` after the latest such error and then rises one slot every `cap_step_up_minutes`. While the cut is in force the newest working agents beyond the cap get nothing typed into them (no resend, reminder or restart prompt; `turn_held` and `turn_resumed` events, neither wakes the coordinator), so the oldest tasks finish first and the held agents resume one at a time as the cap rises. A working agent's own requests between theseus messages are not interrupted.
- With `tokens_per_minute` set on the pool, each working agent is estimated at its context size (from its transcript, else the pane's hint) times `turns_per_minute` tokens a minute; the newest working agents beyond that budget are held the same way, and the pool starts no new task while any agent is held or while the new agent's estimate would not fit. The oldest working agent is never held. The console's pool line shows the estimate against the budget and the held agents. The estimate is rough: it ignores prompt caching and sessions on the same key that theseus does not run (a coordinator, say), so leave headroom or calibrate `turns_per_minute` from a saturation you observed.

## Host store

Runtime state shared by every project on the machine (projects, registered agents, queue events, pool slots, deliveries and agent-state samples) lives in one SQLite database, `~/.local/share/theseus/theseus.db`, and so do the task records (`theseus tasks import` loads a project's files once; `theseus tasks export` writes them back to `docs/project/` for the coordinator to commit, as a passing verify and `tasks complete` do). The first command run in a project imports its old `agents.json` and `queue/events.jsonl` once and leaves the files in place; `queue/events.jsonl` still receives a copy of every event as a deprecated audit trail. See [Design notes](architecture.md#host-store) for how it works.

Retention and backups are set in `[store]` of the host configuration; these are the defaults:

```toml
[store.retention]          # rows older than this many days are trimmed; 0 keeps them
samples_days = 14          # agent-state samples
events_days = 365          # queue events and ended coordinator sessions
deliveries_days = 365      # deliveries
runs_days = 90             # ended supervised-agent runs (each agent's latest run is always kept)
gateway_days = 7           # gateway errors and gateway retry episodes no longer waiting
credentials_days = 30      # agent credentials of runs that ended (task rows and their history are never trimmed)

[store.backups]
directory = ""      # default: the backups directory next to the database
daily = 7           # keep the newest backup of each of this many recent days
weekly = 4          # and of each of this many recent ISO weeks
offsite = ""        # optional: a directory (another disk, network storage) the daily and weekly copies also go to
```

```sh
theseus db                     # check: integrity, schema version, sizes, row counts, last backup and maintenance
theseus db maintain            # trim by retention, ANALYZE, optimize, incremental vacuum, truncate the WAL
theseus db backup              # online backup to the backups directory, integrity check, rotation
theseus db restore FILE --yes  # back up the current database, then load FILE (refuses without --yes)
theseus db prune [--yes]       # list (and with --yes delete) projects whose root is gone, with all their rows
theseus db prune --with-tasks --yes   # also projects with tasks, after exporting them to the project's data directory
theseus db downgrade 6 [--yes] # remove the task store for an older theseus, once every project's export is current
```

The work queue runs `maintain` (which trims by retention) and `backup` by itself at most once a day across the host, and another backup at most hourly while task rows changed since the newest one (rotation keeps the newest of each day), so a machine that runs a queue needs no cron job and the database stays bounded. Keep the live database on a local disk: `theseus doctor` warns when it sits on a network filesystem, and when the newest off-site copy is older than two days. `theseus doctor` shows the database path, schema version, size, last backup, last maintenance and any project name claimed by two different roots (give one of them a different `[project] name`).

Environment overrides, mainly for tests: `THESEUS_DB` (database file), `THESEUS_BACKUPS` (backup directory, above `[store.backups] directory`), `THESEUS_CONFIG` (the host `config.toml`), `THESEUS_STORE_CONFIG` (the deprecated `store.toml`).

## External tools

theseus finds reverse-engineering and capture tools on each machine without hard-coded paths. `theseus tools` lists the known tools and where each was found: Ghidra, a JDK, REA, rizin, gdb, Python with pefile, capstone and lief, Kaitai, Wine, Xvfb, xdotool, bubblewrap, QEMU, tshark and ffmpeg.

A tool is looked up in this order:
1. its environment variable (`GHIDRA_INSTALL_DIR`, `JAVA_HOME` or `THESEUS_TOOL_<NAME>`);
2. `[tools.paths]` in the project's `theseus.local.toml`, written by `theseus tools set NAME PATH`;
3. `[tools.paths]` in the project's `theseus.toml` (discouraged: locations are per machine);
4. `[tools.paths]` in the host configuration, written by `theseus tools set --host NAME PATH`;
5. `PATH`;
6. common install locations, newest version first.

A configured location that does not work (a missing directory, a file that is not executable) is skipped and the next step is tried. `theseus tools` shows which step found each tool.

Found tools are passed to every command and worker as `THESEUS_TOOL_<NAME>`. `eval "$(theseus tools env)"` sets the same variables in your shell. If a tool listed in `[tools] need` is missing, `theseus doctor` reports a failure.

## REA and Ghidra

[REA](https://www.npmjs.com/package/rea-agents) is an MCP server that gives agents decompilation and binary analysis tools.

```sh
npm install --global rea-agents
theseus setup rea            # registers the server and skill for the clients in [re] clients
theseus setup rea --update   # update REA first
theseus doctor               # shows whether REA is registered for each client
```

Run `theseus setup rea` once per machine; it passes the Ghidra and JDK that theseus found. Restart agent sessions afterwards so they load the server. For a long-running agent in tmux, `theseus instance restart PANE` restarts the agent and resumes its session.

## Upgrading a project

After upgrading the CLI, refresh the files theseus manages in each project:

```sh
theseus upgrade --dry-run
theseus upgrade
```

This updates the workflow skills, the Git hook and helper scripts, adds missing template files, and lists configuration keys added since the project was created. It never changes project-specific files.

## Model context windows

The console measures context use against the window of the model each session's transcript names. `[models] context` maps model ids to windows; a key ending in `*` matches by prefix and the longest match wins. The defaults cover current Claude models (1M, with 200K for Claude Haiku 4.5 and the 4.5 generation). Add gateway models in the host config, for example:

```toml
[models.context]
"gateway/model-name" = 229376
```

A model not in the table falls back to the profile's `context_limit`.


## OpenCode

The `opencode` preset runs [OpenCode](https://opencode.ai) (checked with 2.3.0) as supervised agents and headless workers. Each agent runs `opencode --standalone`, its own private OpenCode server: without `--standalone` the TUI attaches to OpenCode's shared background service, which never sees the agent's environment, so the per-agent model and MCP file would not apply. A standalone agent takes about 190 MB of memory.

What the preset does:
- Session: theseus preassigns `ses_<uuid>` (`session_prefix`; OpenCode refuses other ids) and starts the agent with `--session ID`; a restart resumes with the same flag, which also creates a session that no longer exists, so there is no "resume missing" case.
- Model: the TUI has no model flag, so `model` reaches it as `OPENCODE_CONFIG_CONTENT = {"model": "<model>"}` (`model_env`); `opencode run` also gets `--model`. A resumed session keeps the model it was started with; a model change applies to fresh sessions.
- MCP: `mcp_servers` writes `<data>/mcp/<profile>/<agent>.json` in OpenCode's format and names it in `OPENCODE_CONFIG` (`mcp_format = "opencode"`, `mcp_env`). The `theseus` entry carries the agent's `THESEUS_CREDENTIALS` in `environment`; OpenCode passes local servers its whole environment plus that table, with the project root as working directory. OpenCode merges the file over its own configuration and ignores a partial entry, so the file also lists each server of your global OpenCode configuration (`~/.config/opencode/opencode.json`, `opencode.jsonc`, `config.json`) that `mcp_servers` leaves out as a complete entry with `"disabled": true`; `mcp_servers = []` thus starts an agent with no MCP servers at all, as for Claude Code. A server defined in the project's own `opencode.json` still loads.
- Updates: the preset sets `OPENCODE_DISABLE_AUTOUPDATE=1` in `env`, so agents never replace the binary under each other; update OpenCode yourself (a profile that sets its own `env` replaces this, so keep it there).
- Screen: busy is `esc interrupt` on the bottom row (also while compacting and while OpenCode retries a gateway error); its permission prompt (`Allow once  Always allow  Reject`) and question dialog (`enter submit  esc dismiss`) count as waiting for a human, and nothing is typed into them; the context hint is the `7.0K (3%)` figure of the bottom row; `/compact`, `/clear` and `/exit` are its commands; `C-u` empties the input box in one press, wrapped or not.
- Transcript: `opencode-sqlite` reads the agent's session from OpenCode's database for the context meter, the gateway classification and the Conversation tab (see [Transcript adapters](agent-profiles.md#transcript-adapters)).
- Gateway errors: OpenCode retries a 429 or 5xx itself, about ten times over a minute and a half, showing `Retrying in 5s · attempt 4 · <message>` while busy; theseus sees a working agent until the turn fails, then the `Error: <message>` row and the transcript's error (status and kind) start the usual recovery.

Skills and instructions: OpenCode reads `AGENTS.md` from the working directory up to the project root (and `~/.config/opencode/AGENTS.md`), and discovers skills in the `.agents/skills`, `.claude/skills` and `.opencode/skills` directories of the project and of your home directory (checked for the project's `.agents/skills`). The skills `theseus scaffold` installs in `.agents/skills` (with the `.claude/skills` link) therefore load as they are; `/skills` in the TUI lists them, once each.

An example host configuration, with OpenCode's providers defined in your OpenCode configuration and their API key in an environment variable (`"apiKey": "{env:GATEWAY_API_KEY}"`):

```toml
# ~/.config/theseus/config.toml
[supervisor]
env_passthrough = ["GATEWAY_API_KEY"]       # supervised agents start with an allowlisted environment

# Workers on the gateway's free coding model ("provider/model" as OpenCode names it)
[workers.opencode-gateway]
preset = "opencode"
model = "gateway/coder-model"
context_limit = 229376
capacity = 4
pool = "gateway"
mcp_servers = ["theseus"]

# A coordinator on a larger model from another provider block of the same OpenCode configuration
[workers.opencode-coordinator]
preset = "opencode"
model = "gateway-openai/large-model"
context_limit = 200000
capacity = 1
```

`env_passthrough` takes effect when the private tmux server starts, from the environment of the command that starts it; with the server already running, stop every supervised session (`theseus agents down --all` in each project) so the next `theseus agents up` starts it again. A launcher script that exports the key itself and runs `opencode "$@"`, set as the profile's `command`, needs neither. A project then runs workers with `[agents.counts] opencode-gateway = 2` (and `[agents] managed = true`) in `theseus.local.toml`. Raise `[instances] send_verify_seconds` for a slow model: a verified send waits that long for the busy marker.

Limits:
- After `/clear` OpenCode starts a new session, which it creates with an id of its own on the next message; the database names no process or pane, so theseus keeps the earlier id: the context meter and Conversation tab show the earlier session until the agent restarts, and a restart resumes the conversation from before the clear.
- A hand-made OpenCode pane (`theseus agents add`) gets no transcript, only the context hint, and `theseus instance restart` does not support it.
- Before the first message the TUI centers its input box, above the bottom rows the input checks read, so text typed there is not seen as typed; a verified send then relies on the busy marker alone.
- The failed-turn `Error:` row is searched in the bottom `[gateway] screen_lines` (15) rows, and OpenCode draws a conversation shorter than the screen from the top, so a turn that fails early in a session is seen as an idle agent (the stall reminder then nudges it); `[gateway] screen_lines = 50` searches the whole default screen.
- Below 80 columns OpenCode shortens the busy marker (`esc ...rrupt`), so theseus no longer sees the agent working; keep supervised panes at least 80 columns wide (the default `[supervisor] size` is 220x50).
- No peer messaging: workers report through the handoff file and the task commands.
- Permission prompts appear only for actions an agent's permission rules mark `ask` (OpenCode's default build agent: reading `.env` files); no startup dialog appeared with a new configuration.
