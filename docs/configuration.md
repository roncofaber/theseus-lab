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
| `[project]` | `name`, `prefix` (used in folder and process names) |
| `[paths]` | `data` (evidence, logs, decompiled code, worker records), `scratch` (builds, temporary files), `worktrees`, `original` (read-only original software) |
| `[commands]` | `build`, `smoke`, `verify`, `smoke_timeout`. Each is a list of shell commands; `$THESEUS_BUILD_DIR` is a build folder unique to each checkout |
| `[limits]` | `heavy_runs`: how many builds and verifications may run at once on this machine |
| `[workers]` | `default` (agent profile), `retries`; `[workers.<profile>]` adjusts or defines a profile (see [Agent profiles](agent-profiles.md)) |
| `[instances]` | Long-running agents: `panes` (hand-made panes `[{ name, pane }]`; pane ids belong to one machine, so put them in `theseus.local.toml`, and `theseus config check` warns when `theseus.toml` lists any), `coordinator` (session name to report to, for agents that support messaging), `brief`, `idle_minutes`, `handoff_settle_seconds`, `send_verify_seconds` (how long a verified send waits for the busy marker, default 10); `busy_pattern`, `launch`, `sessions_dir` override the profile |
| `[agents]` | Supervised agents (see [Supervised agents](workflow.md#supervised-agents)): `managed` (false; true makes theseus start and supervise them), `name` (`"{project}-{profile}-{n}"`; `{project}`, `{profile}` and `{n}`, which is required), `cwd` (`root`, the main checkout; `data`; or a path), `resume` (`task`: resume the conversation after a crash or reboot only for an agent that holds a task; `always`; `never`), `[agents.counts]` (standing sessions per profile, usually in `theseus.local.toml`), `[[agents.extra]]` (`name`, `profile`, `rooms`: named agents outside the counted slots) |
| `[queue]` | `interval` (30 seconds between ticks of `queue run` and `queue wait`), `nudge` (the idle reminder text), `deadline` (added to task messages), `send_budget_seconds` (25: how long one tick waits for its verified sends together; a send still unresolved is finished by the next tick) |
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
| `[workers.<name>]` | Host-wide agent profiles, the same fields as in `theseus.toml` (see [Agent profiles](agent-profiles.md)). A project section with the same name overrides only the keys it sets; `env` and `interactive` merge key by key |
| `[pools.<name>]` | `max`, `priority` (see [Sharing agents between projects](#sharing-agents-between-projects)) |
| `[store]` | `[store.retention]` and `[store.backups]` (see [Host store](#host-store)) |
| `[tools.paths]` | Tool locations on this machine, by tool name (see [External tools](#external-tools)) |
| `[models]` | `context`, as in a project; the host table is the usual place for gateway models |
| `[gateway]` | The host-wide gateway circuit breaker: `breaker_errors` (3 different agents), `breaker_window_minutes` (10), `strong_codes` (`[530]`, one such error pauses the pool); and the cap cut after a `429 parallel cap` (the shared key's `max_parallel_requests`): the pool's working cap drops by `parallel_cap_step` (1) for `parallel_cap_minutes` (10), host-wide, and such errors do not count toward the breaker |
| `[supervisor]` | The private tmux server of supervised agents: `socket` (`theseus`, the `tmux -L` name; `THESEUS_TMUX_SOCKET` names another, `THESEUS_TMUX_PATH` a full socket path), `size` (`220x50`, the fixed agent screen; an attached client resizes it until it detaches, and the console's pane mirror sizes it to what the mirror shows while the mirror is open), `history_limit` (50000), `detach_key` (`C-q`), `max_agents` (12 sessions on the host), `env_passthrough` (names or glob patterns of variables the server passes to agents besides `HOME USER LOGNAME SHELL PATH LANG LC_* TMPDIR XDG_* THESEUS_*`), `launch_shell` (`exec`, or `login` to run the profile command through `$SHELL -lc`), `ready_seconds` (90), `[supervisor.restart]` `backoff_seconds` (`[5, 30, 120, 600]`), `max_crashes` (5), `window_minutes` (30) |
| `[defaults]` | Values for project keys a project leaves unset: `workers.default`, `workers.retries`, `limits.heavy_runs`, `paths.data`, `paths.scratch`, `paths.worktrees`, `instances.idle_minutes`, `instances.handoff_settle_seconds`, `instances.send_verify_seconds`, `queue.interval`, `queue.send_budget_seconds`, `re.clients`, `agents.counts`, `agents.resume`, `agents.cwd`, and the `gateway.*` keys of the project table |

`theseus config check` reports an unknown section, an unknown profile key, pool key or store key as an error naming the file and the key, and an unknown `[defaults]` key as a warning (it is ignored). A host file that does not parse makes every command that reads it (anything that loads a project, `theseus pool`, the host store settings) stop with that message; the console leaves the pools out. An unknown top-level section, in the host file or in a project's `theseus.toml` or `theseus.local.toml`, is ignored by every other command with a warning on stderr, printed once per process, so a long-running `theseus queue wait` or `queue run` keeps going when a newer release adds a section.

```sh
theseus setup host                          # write a commented starter (refuses to replace an existing file)
theseus setup host --print                  # print it instead
theseus setup host --force                  # replace an existing file with the starter
theseus setup host --import-project PATH --dry-run   # what would move from a project
theseus setup host --import-project PATH    # move it
```

`--import-project` copies the project's `[workers.<name>]` tables (from `theseus.toml`, and from `theseus.local.toml` for profiles not already in `theseus.toml`) and the `[tools.paths]` table of its `theseus.local.toml` into the host file, comments included: into a new starter file, or appended to an existing host file, refusing a profile or table the host file already has. It never edits the project files; it prints the line ranges you can remove from them once `theseus config check` passes. Keep project-specific keys such as `capacity` in the project.

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

## Host store

Runtime state shared by every project on the machine (projects, registered agents, queue events, pool slots, deliveries and agent-state samples) lives in one SQLite database, `~/.local/share/theseus/theseus.db`. Task records stay in each project's `tasks.json`. The first command run in a project imports its old `agents.json` and `queue/events.jsonl` once and leaves the files in place; `queue/events.jsonl` still receives a copy of every event as a deprecated audit trail. See [Design notes](architecture.md#host-store) for how it works.

Retention and backups are set in `[store]` of the host configuration; these are the defaults:

```toml
[store.retention]          # rows older than this many days are trimmed; 0 keeps them
samples_days = 14          # agent-state samples
events_days = 365          # queue events and ended coordinator sessions
deliveries_days = 365      # deliveries
runs_days = 90             # ended supervised-agent runs (each agent's latest run is always kept)
gateway_days = 7           # gateway errors and gateway retry episodes no longer waiting

[store.backups]
directory = ""      # default: the backups directory next to the database
daily = 7           # keep the newest backup of each of this many recent days
weekly = 4          # and of each of this many recent ISO weeks
```

```sh
theseus db                     # check: integrity, schema version, sizes, row counts, last backup and maintenance
theseus db maintain            # trim by retention, ANALYZE, optimize, incremental vacuum, truncate the WAL
theseus db backup              # online backup to the backups directory, integrity check, rotation
theseus db restore FILE --yes  # back up the current database, then load FILE (refuses without --yes)
theseus db prune [--yes]       # list (and with --yes delete) projects whose root is gone, with all their rows
```

The work queue runs `maintain` (which trims by retention) and `backup` by itself at most once a day across the host, so a machine that runs a queue needs no cron job and the database stays bounded. `theseus doctor` shows the database path, schema version, size, last backup, last maintenance and any project name claimed by two different roots (give one of them a different `[project] name`).

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

