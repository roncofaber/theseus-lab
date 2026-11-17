# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `theseus top` is now an interactive Textual dashboard over every project on the host: a Host tab (projects, pools, merged events) and one tab per project (agents, waiting tasks, events, activity sparklines), with `/` filtering, `p` to pause or resume a queue, `r` to refresh and Enter on an agent to switch your tmux client to its pane. It runs outside any project too.
- Host registry (`~/.local/share/theseus/projects.json`, `THESEUS_REGISTRY` overrides) filled by every command that loads a project configuration, and `theseus projects` to list it.
- `theseus top --host --once` and `--host --json` for a plain host summary, and `theseus top --interval SECONDS`.

### Changed

- Textual (`textual>=8.2,<9`) is now a runtime dependency. `theseus top --once` and `--json` keep their output; the old ANSI refresh loop is retired.

## [0.1.0] - 2026-11-16

First public release.

### Added

- `theseus` command-line tool and project scaffold.
- Task queue with ownership, acceptance criteria, dependencies and milestones.
- Worker dispatch in isolated Git worktrees, follow-ups and automatic retries.
- Single merge path with ownership checks and clean rollback on conflict.
- Verification on a clean checkout and a pre-push gate for unverified code.
- Agent registry, automatic work queue and the `theseus top` dashboard.
- Agent profiles for Claude Code, Claude Code subagents, Codex, OpenCode and Gemini CLI, plus custom profiles.
- Reverse-engineering support: per-target decompiled corpus and catalog, citation index, artifact inventory, REA setup and a tool resolver.
- Clean-room mode with separate rooms, sparse checkouts for clean work and context clearing between rooms.
- Workflow skills for the coordinator, workers, reverse engineering, reference runs and project phases, and a setup skill for agents.
