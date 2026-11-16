# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
