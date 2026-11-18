# Getting started

This walks one person from nothing to a coordinator agent running tasks with workers. An agent can do the same steps for you: install the `theseus-setup` skill (`theseus setup skill`) and ask it to set theseus up.

## 1. Install

```sh
python3 --version          # 3.11 or newer
uv tool install git+https://github.com/roncofaber/theseus-lab
# or: pipx install git+https://github.com/roncofaber/theseus-lab
theseus --help
```

Optional tools by feature:

| Tool | Needed for |
| --- | --- |
| `jq` | `theseus status`, `stop`, `wait` for headless workers |
| `tmux` | required: persistent agents (supervised on a private server, `theseus agents up`), `theseus queue`, `theseus instance` |
| `shellcheck` | the project's shell lint (`theseus check`) |
| systemd user manager | CPU and memory limits per worker (Linux); without it workers run without limits |
| `nvidia-smi` | the GPU line in the console |

Linux is the primary platform. On macOS the workflow runs, with fewer features: no per-worker limits, no per-agent memory in the console, and only a liveness check for headless workers.

## 2. Pick your agent

```sh
theseus agents profiles
```

The default profile is `claude-code`, which uses your own Claude Code login. If you use another agent, note its profile name (`codex`, `opencode`, `gemini-cli`) for step 4, and read [agent-profiles.md](agent-profiles.md) for what verified means and how to add an agent theseus does not know.

## 3. Create the project

```sh
theseus scaffold ~/software/my-port --name my-port
cd ~/software/my-port
git init -q && git add -A && git commit -qm "theseus scaffold"
theseus check
```

For an existing repository that used another workflow, run `theseus scaffold PATH --name NAME --adopt` instead: it overwrites nothing and puts the theseus versions of differing files under `theseus-adopt/` for a manual merge (see the theseus-setup skill).

Point theseus at the original software, outside the repository, in the Git-ignored `theseus.local.toml`:

```toml
[paths]
original = ["~/legacy/original-install"]
```

Durable artifacts (evidence, decompiled corpus, captures, worker records) go under the data root, `$XDG_DATA_HOME/my-port` by default (`~/.local/share/my-port` when `XDG_DATA_HOME` is unset), never in Git.

## 4. Configure

In `theseus.toml`:

```toml
[commands]
build = ["make -C src"]
verify = ["make -C src test"]

[workers]
default = "claude-code"      # or codex, opencode, gemini-cli, a variant

[workers.claude-code]
capacity = 2                 # concurrent workers; keep within your plan's limits
```

`theseus config check` validates the file; `theseus config show` prints the resolved values.

## 5. Check the host

```sh
theseus doctor
```

Every FAIL must be fixed; WARN rows are optional features. For reverse engineering, add the tools the project needs to `[tools] need`, locate them with `theseus tools` (and `theseus tools set NAME PATH`), and wire REA with `theseus setup rea`.

## 6. Run the first round

Open your agent in the project root as the coordinator and say:

> Read AGENTS.md, then follow the theseus-coordinating skill. We are in phase 0 of the theseus-recipe skill: plan the exploration tasks and report to me before dispatching.

The coordinator writes task records with `theseus tasks add`, then launches workers in one of three ways:

- **Headless:** `theseus dispatch TASK` starts one worker in its own worktree. `theseus wait` reports when it finishes, and `theseus merge TASK` integrates its work.
- **Persistent:** let theseus start and supervise the agents: set `[agents] managed = true` and `[agents.counts]` (for example `claude-code = 2`) in `theseus.local.toml`, then run `theseus agents up` (see [Supervised agents](workflow.md#supervised-agents)). Panes you start by hand still work: register each with `theseus agents add NAME PANE --profile PROFILE`. One `theseus queue run`, in a hidden session of the private tmux server (after `theseus agents up` has started that server, `tmux -L theseus new-session -d -s PROJECT--queue -c ROOT "$(command -v theseus) queue run"`; with only hand-made panes, a session on your own tmux), then assigns ready tasks, resets context between tasks and records deliveries, while the coordinator watches with `theseus queue wait --no-drive`; watch it in the console (`theseus` alone on a terminal). `theseus agents scale PROFILE N` changes the count later.
- **Subagents** (`claude-subagent`): the coordinator launches them itself from the prepared prompt.

Workers never commit. The coordinator reviews each handoff, runs `theseus merge`, then `theseus verify`, and finally `theseus tasks complete`.

## 7. Keep it current

```sh
uv tool upgrade theseus-lab
theseus upgrade --dry-run && theseus upgrade
```

`theseus upgrade` refreshes the generic skills, hook and helpers that theseus owns in the project and lists new configuration keys; project-specific files are never touched.
