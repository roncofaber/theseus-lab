# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `theseus top` is now an interactive Textual dashboard over every project on the host: a Host tab (projects, pools, merged events) and one tab per project (agents, waiting tasks, events, activity sparklines), with `/` filtering, `p` to pause or resume a queue, `r` to refresh and Enter on an agent to switch your tmux client to its pane. It runs outside any project too.
- Host registry filled by every command that loads a project configuration, and `theseus projects` to list it.
- `theseus top --host --once` and `--host --json` for a plain host summary, and `theseus top --interval SECONDS`.
- Host store (`theseus.store`): one SQLite database per machine (`~/.local/share/theseus/theseus.db`, `THESEUS_DB` overrides) is now the runtime source of truth for queue events, pool leases and claims, the agent registry, the project registry, deliveries and agent-state samples written by every queue tick. Task records stay in `tasks.json`.
- One-time migration: the first command in a project imports its `agents.json` and `queue/events.jsonl`, and the first on the host imports the pool state files and `projects.json`; the old files are left in place.
- `theseus db check|maintain|backup|restore FILE --yes|prune`: integrity and size report, retention trim with `ANALYZE`, optimize, incremental vacuum and WAL truncation, online backups with an integrity check and 7 daily plus 4 weekly rotation, restore that saves the current database first, and explicit removal of projects whose root is gone. The queue runs maintenance and a backup at most once a day. Retention and backups are configured in `~/.config/theseus/store.toml`.
- `theseus doctor` shows the host store (path, schema version, size, last backup, last maintenance) and project names claimed by two roots.
- `theseus top` dashboard look: boxed, titled sections in the plain dashboard's state colors; a stable accent color per project (tab label, section borders, host tables); project cards on the Host tab; one stacked activity bar chart per project tab (series per kind group, legend, time axis) and one for all projects on the Host tab; CPU, load, memory, swap and GPU meters with an in-memory CPU history and per-core view (`c`); CPU and memory bars per agent.
- Thrashing is a stall: an idle agent holding a task whose pane shows one of its profile's `interactive.stall_patterns` (Claude Code presets: "Autocompact is thrashing") is reported in state `thrashing` at once, and the queue records one `stalled` event, which wakes `theseus queue wait`, and never nudges it.
- `interactive.env` for a profile: it and the profile's `env` are put in front of the persistent instance's launch and restart as `env VAR=value ...`, for example `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS = "10000"` to make agents read large files in ranges.
- `theseus instance restart` launches the agent fresh with the same name and permission mode when the session to resume no longer exists (`interactive.resume_missing`), and reports it.

### Changed

- The Claude Code presets launch persistent instances as `{command} --name {agent} --permission-mode {permission_mode}`, and resume with the same line plus `--resume`, so a restarted agent keeps its name and permissions. `profiles.instance_env()` takes the agent name; without one, `instance restart` fills `{agent}` from the session record.
- The worker instance brief asks for short shell output and for findings to go into the report as the work goes, so they survive context compaction.
- Textual (`textual>=8.2,<9`) is now a runtime dependency. `theseus top --once` and `--json` keep their output; the old ANSI refresh loop is retired.
- The queue tick takes pool slots atomically (`store.acquire()`) and gives a slot back when the assignment fails, so concurrent queues never push a pool past its `max`.
- Event records returned by `workqueue.events()` (and in `theseus top --json`) also carry `id`, `project` and `epoch`.

### Deprecated

- `<data>/queue/events.jsonl` is still appended as a plain-text audit trail but no longer read; it will stop being written in a later release. `<data>/agents.json`, the pool state files under `~/.local/share/theseus/pools/` and `~/.local/share/theseus/projects.json` are no longer written after their one-time import.

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
