# theseus-lab

[![Tests](https://github.com/roncofaber/theseus-lab/actions/workflows/tests.yml/badge.svg)](https://github.com/roncofaber/theseus-lab/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

Run a team of AI coding agents to reverse engineer old, unsupported software and rebuild it.

You give the project a legacy program: binaries, installers, data files, maybe the hardware it drives. One agent coordinates; several workers take small tasks in parallel, such as decompiling a library, capturing USB traffic or implementing a decoder. theseus-lab provides the command-line tool, project layout and rules that keep this reliable: every task is tracked, every change is reviewed and merged by the coordinator, and every claim is backed by evidence.

## Features

- **Task queue for agents.** Small, owned tasks with acceptance criteria, dependencies and milestones; idle agents pick up the next task automatically.
- **Isolated workers.** Each worker gets its own Git worktree and never commits; the coordinator merges deliveries with ownership checks.
- **Works with your agent.** Claude Code, Codex, OpenCode, Gemini CLI, or any CLI you describe in a few lines of config.
- **Verification gate.** Builds and tests run on a clean checkout; pushing unverified code is refused.
- **Reverse-engineering support.** Decompile each binary once, index it and query it; cite addresses and captures in reports; run the original in a VM or compatibility layer.
- **Clean-room mode.** Separate the agents that read original code from the ones that write the new implementation.
- **Live dashboard.** `theseus top` shows agents, tasks, verification state and host load.

## How it works

```
 you ──> coordinator agent ──> theseus tasks add ──> queue
                                                       │
            ┌──────────────────────────────────────────┤
            v                     v                    v
        worker 1              worker 2             worker 3      (own worktree each)
            │                     │                    │
            └──── handoff.md ─────┴────────────────────┘
                                  │
                 theseus merge ──> theseus verify ──> theseus tasks complete
```

A project is an ordinary Git repository with a `theseus.toml`, an `AGENTS.md` that every agent reads first, a task file, one report per task, and agent skills that describe the workflow. Large artifacts (original binaries, decompiled code, captures) stay outside the repository in a per-project data folder.

## Requirements

- Linux (macOS works with reduced process monitoring)
- Python 3.11 or newer, Git, Bash
- A coding agent CLI, for example [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Optional: `tmux` (long-running agents and the queue), `jq`, `shellcheck`

## Installation

```sh
uv tool install git+https://github.com/roncofaber/theseus-lab
# or
pipx install git+https://github.com/roncofaber/theseus-lab
```

Check the installation:

```sh
theseus --help
```

## Quick start

```sh
theseus scaffold ~/projects/my-port --name my-port
cd ~/projects/my-port
git add -A && git commit -m "Start project"
theseus doctor          # checks tools and the agent CLI
theseus check           # checks the project rules
```

Then open your coding agent in the project folder and tell it:

> Read AGENTS.md and act as the coordinator. Plan the first exploration tasks and show them to me before starting.

### Let an agent set it up

theseus-lab ships a setup skill that walks an agent through installation and configuration:

```sh
theseus setup skill                         # installs to ~/.agents/skills
theseus setup skill --client claude_code    # installs to ~/.claude/skills
```

Then ask your agent: "Use the theseus-setup skill to set up theseus-lab for me."

## Supported agents

| Agent | Profile | Status |
| --- | --- | --- |
| Claude Code | `claude-code` (default) | Tested |
| Claude Code subagents | `claude-subagent` | Tested |
| OpenAI Codex CLI | `codex` | Untested |
| OpenCode | `opencode` | Untested |
| Gemini CLI | `gemini-cli` | Untested |

Select one in `theseus.toml`:

```toml
[workers]
default = "codex"
```

Any other CLI can be added as a custom profile; see [Agent profiles](docs/agent-profiles.md).

## Documentation

- [Getting started](docs/getting-started.md): installation and first project, step by step
- [Workflow](docs/workflow.md): tasks, workers, merging, verification, the queue and the dashboard
- [Configuration](docs/configuration.md): `theseus.toml` reference, external tools, REA and Ghidra
- [Agent profiles](docs/agent-profiles.md): supported agents and how to add your own
- [Design notes](docs/architecture.md): how the package is built, for contributors

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and guidelines. Tested agent profiles for new CLIs are especially useful.

## License

[MIT](LICENSE)
