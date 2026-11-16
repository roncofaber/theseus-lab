# {{PROJECT}} Agent Contract

## Objective

State in two or three sentences what the port delivers, from which original, and what completion means (for example: every workflow of the original application reproduced on the new architecture, verified against recorded ground truth, on the hardware it must drive). Name deferred scope explicitly so nobody schedules it. Prototype behavior is not completion. The coordinator's standing mandate is `docs/project/MANIFEST.md`.

## Start here

| Role | Read at start | Then load skill |
| --- | --- | --- |
| Coordinator | this file, `docs/project/STATE.md`, `theseus tasks list`; `MANIFEST.md` and `ROADMAP.md` once per session | `theseus-coordinating` |
| Worker | this file, `docs/project/STATE.md`, `theseus tasks show <TASK_ID>` | `theseus-worker` |

User instructions recorded in `STATE.md` after the mandate (such as a pause) take precedence over the MANIFEST's standing loop. Do not read `docs/project/tasks-archive.jsonl` or `docs/project/archive/` whole; query them with `theseus tasks show` and `rg`. `docs/project/ROADMAP.md` holds milestones and deferred scope, `docs/topics/README.md` lists the living topic documents (read the topic first), `docs/investigations/README.md` indexes every report and `docs/re/citation-index.md` indexes every cited address and capture. All configurable values (paths, commands, worker providers, panes) are in `theseus.toml`; machine-specific values are in the Git-ignored `theseus.local.toml`.

Skills live in `.agents/skills/` (linked as `.claude/skills/`):

| Skill | Use for |
| --- | --- |
| `theseus-recipe` | the phases of a port, their standard tasks and gates; planning the next milestone |
| `theseus-coordinating` | round loop, dispatch, integration, task records, state updates |
| `theseus-worker` | working an assigned task and the handoff format |
| `theseus-native-re` | behavior of the original binaries: catalog lookup, decompiled corpus, REA |
| `theseus-reference-runs` | running the original in isolation and recording ground truth |

Project skills (data formats, the device protocol, the build of the new application) go next to them as `{{PREFIX}}-<topic>/SKILL.md` and get a row here.

## Team

The coordinator owns integration, task assignment, verification, commits, pushes and worker replenishment. Workers are coding agents described by an agent profile (`theseus agents profiles`: Claude Code, Codex, OpenCode, Gemini CLI and others): one-shot headless runs (`theseus dispatch ID`), persistent sessions in tmux panes registered with `theseus agents add` and fed by `theseus queue`, or subagents of the coordinator. Capacity per profile is `workers.<profile>.capacity` in `theseus.toml`. Workers stay inside their owned paths, report exact evidence and limits, and never commit or push.

A worker task has a task ID in `docs/project/tasks.json`, owned paths, a general contract, at most six acceptance items and a milestone. Inspect current state at task start; another slice may have changed assumptions. Only the coordinator edits shared integration files and governance files (`AGENTS.md`, `README.md`, `theseus.toml`, `docs/project/**`, `.agents/skills/**`).

The coordinator may compact or restart. Recovery starts from repository state and local runtime records, never from the assumption that earlier tool calls, agents or processes still exist.

## Rules

- Work in the main checkout or an assigned worktree. Do not create a competing checkout.
- The original (`paths.original`) is read-only. Never modify, install into or save beside it. Isolated reference runs follow the `theseus-reference-runs` skill.
- Never commit original binaries, extracted or decompiled content, captures, saves, credentials, local configuration or build output. Durable local artifacts go under the data root (`theseus config get paths.data`: `evidence/`, `logs/`, `re/`, `workers/`); scratch goes under `paths.scratch`.
- Recover behavior from the original (decompilation, data, recorded protocol traffic) before implementing it. Never silently invent behavior to fill a gap; return an explicit unsupported result and record what evidence would resolve it.
- Build general contracts tested over representative variations of the original's inputs. One device model, file or scenario is a fixture, never a subsystem boundary.
- Unrecovered original behavior is bounded reverse-engineering work, not an external blocker. Reserve external blockers for inputs the team cannot obtain itself.
- Work one milestone at a time: `currentMilestone` in `docs/project/tasks.json` gates which tasks are ready.
- Merged, observed at runtime and user-tested are three different claims. Record each (`theseus merge`, `theseus tasks observe`, `theseus tasks observe --user`) and never report one as another.
- Code-only rounds owe a periodic full verification pass (`theseus verify`); the pre-push guard refuses further unverified pushes past `verification.max_unverified_merges`.
- A script used more than once lives in this repository with a header saying what it does, never in a scratch folder.
- Never open graphical or focus-stealing windows during automated work; use a private virtual display (`tools/lib/xvfb.sh`).
- Run the relevant checks for every change and report their actual output, or the precise failure. A skipped check is reported as skipped. Keep claims proportional to verification.
- Avoid unrelated refactoring; comments only for non-obvious reasons.
- Write docs findings first, each limit once; use `docs/investigations/TEMPLATE.md`. No em dashes, no emojis, no hard-wrapped prose.
