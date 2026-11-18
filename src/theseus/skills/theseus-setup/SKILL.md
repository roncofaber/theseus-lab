---
name: theseus-setup
description: Use when installing or setting up theseus-lab (the `theseus` CLI) for someone - installing it, choosing the agent profile for their coding agent (Claude Code, Codex, OpenCode, Gemini CLI or another), scaffolding a new port or adopting an existing repository, registering persistent worker agents, wiring REA and Ghidra, or when `theseus doctor` reports a failure.
---

# Setting up theseus-lab

theseus-lab runs a coordinator agent and a pool of worker agents that recover how legacy software behaves and rebuild it. You are helping a person set it up on their machine. Work through the steps in order, run every command yourself when you have a shell, show its real output, and ask the person only for the decisions marked **Ask**.

## 1. Install the CLI

Requirements: Python 3.11 or newer, Git, Bash. Check with `python3 --version && git --version`.

```sh
uv tool install git+https://github.com/roncofaber/theseus-lab      # or: pipx install git+https://github.com/roncofaber/theseus-lab
theseus --help
```

From a local checkout use `uv tool install --editable PATH`. Upgrade later with `uv tool upgrade theseus-lab` (or `pipx upgrade theseus-lab`), then `theseus upgrade` inside each project.

Required: `tmux` (persistent agents run in it; theseus supervises them on a private tmux server of its own). Optional host tools, by feature: `jq` (headless worker status, stop, wait), `shellcheck` (the project's shell lint), a systemd user manager (per-worker resource slices; Linux only). Install the ones the person's workflow needs with their package manager; `theseus doctor` reports which are missing.

## 2. Choose the agent profile

A profile tells theseus how to launch, resume and drive one coding agent CLI. List them, with which CLIs are installed:

```sh
theseus agents profiles
```

| Profile | Use when | Verified |
| --- | --- | --- |
| `claude-code` (default) | The person has Claude Code with their own subscription or API key | yes |
| `claude-subagent` | The coordinator is a Claude Code session that launches workers with its own Agent tool | yes |
| `codex` | OpenAI Codex CLI | no |
| `opencode` | OpenCode, with any provider it supports | no |
| `gemini-cli` | Google Gemini CLI | no |

**Ask** which agent and plan the person uses if it is not obvious from the installed CLIs. Unverified profiles follow the CLI's documented flags but were not run end to end: before relying on one, dispatch a trivial task and check that the worker starts, writes its handoff and that `theseus followup` resumes it. Fix any mismatch with an override (in the host profile, step 3, or `theseus.toml`, step 5) rather than editing the package.

Size `capacity` (concurrent workers) to the person's rate limits: two is a safe start for a personal subscription.

## 3. Write the host configuration

Settings about the person and this machine, rather than a project, go in `~/.config/theseus/config.toml`: the agent profile they actually launch (launcher command, gateway model and its environment, MCP servers, `extra_args` such as a client settings file), pools shared by several projects, host store retention and backups, tool locations, and host defaults such as `workers.default`. Projects then only name the profile and set project matters such as `capacity`.

```sh
theseus setup host --print     # look at the commented starter
theseus setup host             # write it (refuses to replace an existing file; --force does)
theseus config check           # validates the host file, and the project when run inside one
```

Uncomment and fill in what applies; every example in the starter is neutral. Layers, lowest to highest: package defaults and presets, this file, the project's `theseus.toml`, its `theseus.local.toml`, then `THESEUS_*` variables and command line flags. `theseus config show --sources` prints each resolved value with the layer it came from. For a person who already has profiles in a project's `theseus.toml`, `theseus setup host --import-project PATH --dry-run` shows which `[workers.*]` tables and local `[tools.paths]` would move into the host file; without `--dry-run` it writes them and prints the project lines they can remove (it never edits the project). If `theseus doctor` reports `pools.toml` or `store.toml` as deprecated, move their tables into `[pools.<name>]` and `[store]` of `config.toml`.

## 4. Create or adopt the project

New port:

```sh
theseus scaffold PATH --name NAME
cd PATH && git init -q && git add -A && git commit -qm "theseus scaffold"
theseus check
```

Existing repository (adopting theseus after another workflow):

```sh
theseus scaffold PATH --name NAME --adopt
```

`--adopt` overwrites nothing. Files that differ from the theseus template (a live `docs/project/STATE.md`, `tasks.json`, your own `tools/Supervisor` tests with the same names) keep their content, and the theseus version is written under `theseus-adopt/` for a manual merge. Merge what you need (for Supervisor tests, keep your project budgets and add entries for the `theseus-*` skills), delete `theseus-adopt/`, and run `theseus check`. The push hook and generated indexes are left alone: switch the hook with `git config core.hooksPath tools/git-hooks` after a passing `theseus verify`, and run `theseus docs index` once your docs layout matches. An existing decompiled corpus can be reused instead of decompiling again: set `corpus = "<directory>"` on its `[[re.targets]]` entry. A citation index with another name is set with `[re] citation_index = "docs/re/<name>.md"`; a project that keeps its own index generators sets `[docs] index_command`, `check_command` and `generated`, and theseus runs those instead of writing its own indexes.

**Ask** where the original software lives (installer, binaries, data) and record it in the Git-ignored `theseus.local.toml`:

```toml
[paths]
original = ["/path/to/original"]
```

The original is read-only for the whole project; never copy it into the repository.

## 5. Configure

Edit `theseus.toml`; `theseus config show` prints the resolved values and `theseus config check` validates them.

- `[commands]`: `build`, `smoke` and `verify` for the new code. Until `verify` is set, verification reports NOT CONFIGURED and the push gate refuses code.
- `[workers]`: `default = "<profile>"` (a host profile from step 3 or a preset); a `[workers.<profile>]` section adjusts `model`, `capacity`, `permission_mode`, `command`, `env` or `interactive`. A section with any other name plus `preset = "<profile>"` defines a variant, for example a cheaper model for research tasks.
- `[rooms]`: enable for a clean-room port that will be published (dirty paths, spec paths).
- `[tools] need`: external tools the project requires; `theseus tools` shows how each resolves and `theseus tools set NAME PATH` records a machine-specific location in `theseus.local.toml` (`--host` records it in the host file for every project).
- `[re] targets`: original binaries to decompile once and index (name, path, sha256).

Example variant:

```toml
[workers]
default = "claude-code"

[workers.claude-code]
model = ""
capacity = 2

[workers.research]
preset = "opencode"
model = "provider/model-name"
capacity = 1
```

## 6. Check the host

```sh
theseus doctor
```

Fix every FAIL; WARN rows are optional features. For reverse engineering, install REA and Ghidra, then `theseus setup rea` registers the REA MCP server and skill for the clients in `re.clients`.

## 7. Run workers

Pick one mode, or mix them:

- **Headless one-shot workers:** `theseus dispatch TASK [--provider PROFILE]`, then keep `theseus wait` running; `theseus followup NAME MESSAGE` resumes a stopped worker, `theseus stop NAME` interrupts it.
- **Persistent workers in tmux** (needs tmux): let theseus start and supervise them (`[agents] managed = true` and `[agents.counts]`, for example `claude-code = 2`, in `theseus.local.toml`, then `theseus agents up`), or start the agent CLI in a pane yourself and register it with `theseus agents add NAME PANE --profile PROFILE`; then one `theseus queue run` per project, in a hidden session of theseus's private tmux server (after `theseus agents up` has started that server, `tmux -L theseus new-session -d -s PROJECT--queue -c ROOT "$(command -v theseus) queue run"`; with only hand-made panes, a session on your own tmux), assigns ready tasks, resets context between tasks (compact; clear when moving from dirty to clean work) and records deliveries, while the coordinator watches with `theseus queue wait --no-drive`. `theseus` alone on a terminal (or `theseus console`) opens the interactive console of every project on the host (`theseus console --once` for a plain frame).
- **Subagents:** with `claude-subagent` and a capacity, `theseus dispatch TASK --provider claude-subagent` prepares the worktree and prompt, and the coordinator launches the subagent.

Set `[instances] coordinator` to the coordinator session's name when the agent supports peer messaging (Claude Code); otherwise workers reply in their pane and the queue detects the handoff file.

## 8. Start the coordinator

Open the coordinator agent in the project root and tell it: "Read AGENTS.md and follow the theseus-coordinating skill." The `theseus-recipe` skill gives the phases, from exploration to release. Check that the agent sees the skills in `.agents/skills/`; for agents that read skills from another directory, link them there.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `unknown agent profile` | `theseus agents profiles`; set `workers.default` or `preset` to a listed name |
| Worker exits at once, no handoff | read `<data>/workers/NAME/stderr.log`; usually the CLI's login, a missing model, or a flag the installed version does not accept (override `headless` in `[workers.<profile>]`) |
| `followup` says no resume command | the profile cannot resume headless runs; follow up through a new dispatch or a persistent instance |
| Queue never sees a delivery | the handoff needs a `Result:` line (delivered, partial or blocked), the pane must be idle and the file unchanged for `instances.handoff_settle_seconds` |
| Pane always busy or never busy | set `[instances] busy_pattern` to text the agent shows only while working |
| `systemd-run` missing (macOS, containers) | workers still run, without per-worker resource slices |
