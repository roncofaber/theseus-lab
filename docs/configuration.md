# Configuration

## Files

- `theseus.toml` in the project root holds all project settings and is committed.
- `theseus.local.toml` is merged over it and is Git-ignored. Use it for machine-specific values such as where the original software or a tool is installed.
- The environment variables `THESEUS_DATA`, `THESEUS_SCRATCH` and `THESEUS_WORKTREES` override the folder settings for one shell.

Useful commands:

```sh
theseus config show              # resolved configuration
theseus config get paths.data    # one value
theseus config check             # validate
```

## Reference

| Section | Keys |
| --- | --- |
| `[project]` | `name`, `prefix` (used in folder and process names) |
| `[paths]` | `data` (evidence, logs, decompiled code, worker records), `scratch` (builds, temporary files), `worktrees`, `original` (read-only original software) |
| `[commands]` | `build`, `smoke`, `verify`, `smoke_timeout`. Each is a list of shell commands; `$THESEUS_BUILD_DIR` is a build folder unique to each checkout |
| `[limits]` | `heavy_runs`: how many builds and verifications may run at once on this machine |
| `[workers]` | `default` (agent profile), `retries`; `[workers.<profile>]` adjusts or defines a profile (see [Agent profiles](agent-profiles.md)) |
| `[instances]` | Long-running agents: `coordinator` (session name to report to, for agents that support messaging), `brief`, `idle_minutes`, `handoff_settle_seconds`; `busy_pattern`, `launch`, `sessions_dir` override the profile |
| `[git]` | `base` (`HEAD`, or `origin/main` to fetch first), `remote`, `branch` |
| `[verification]` | `code_paths`, `doc_paths` (changes there need no verification), `max_unverified_merges`, `max_unverified_hours` |
| `[rooms]` | Clean-room mode: `enabled`, `taint` (`until-clear` or `permanent`), `clean_instances`, `dirty_instances`, `dirty_paths`, `dirty_data`, `spec_paths` |
| `[re]` | `targets` (binaries to decompile: `name`, `path`, `sha256`), `clients` (agent clients for the REA server), `address_low`, `address_high` |
| `[tools]` | `need`: external tools this project requires |
| `[consumers]` | Per file extension, a pattern for the names a file declares (defaults cover common languages) |

The scaffolded `theseus.toml` documents every key with comments.

## External tools

theseus finds reverse-engineering and capture tools on each machine without hard-coded paths. `theseus tools` lists the known tools and where each was found: Ghidra, a JDK, REA, rizin, gdb, Python with pefile, capstone and lief, Kaitai, Wine, Xvfb, xdotool, bubblewrap, QEMU, tshark and ffmpeg.

A tool is looked up in this order:
1. its environment variable (`GHIDRA_INSTALL_DIR`, `JAVA_HOME` or `THESEUS_TOOL_<NAME>`);
2. `theseus tools set NAME PATH`, saved in `theseus.local.toml`;
3. `PATH`;
4. common install locations, newest version first.

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
