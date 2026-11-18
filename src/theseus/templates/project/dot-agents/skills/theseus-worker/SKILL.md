---
name: theseus-worker
description: Use when working an assigned task ID of a theseus port inside a worker worktree, as a headless, persistent or subagent worker, from first read to final handoff, including when blocked, denied or told to resolve conflicts.
---

# Theseus worker

You do one task inside its owned paths, in a Git worktree made for you, and hand back evidence. The coordinator merges and commits; you never do.

## Where you are

- **Worktree:** the path in your task message or prompt. Start every command with `cd WORKTREE && ...`; edit files by absolute path.
- **Log directory:** `WORKTREE/.theseus` (Git-ignored): `notes.md`, check logs, `patches/`, `changed.sha256`, `handoff.md`.
- **Task:** `theseus task show TASK_ID`: the record (`ownedPaths`, the only files you may change; objective, contract, acceptance, the room), notes and your prompt.

## Start

1. `cd WORKTREE && git status && git log -1 --oneline`; note the base. On a resumed task the changes are yours.
2. Read `AGENTS.md`, `docs/project/STATE.md`, the task record and the documents it names; search `docs/investigations/README.md` and `docs/re/citation-index.md` (not in a clean-room task) for prior work.
3. Create the owned report `docs/investigations/<name>.md` from `docs/investigations/TEMPLATE.md` now; save findings to it after each step, progress to `.theseus/notes.md`.
4. Load the domain skills you need: `theseus-native-re` (the original's binaries), `theseus-reference-runs` (running the original), project skills; never in a clean-room task.

## Never

- Commit, stash, push, reset, switch branches, rebase or otherwise move HEAD. The merge reads your uncommitted working tree.
- Edit outside your owned paths, in the main checkout or in another worktree; edit generated indexes or governance files (`AGENTS.md`, `theseus.toml`, `docs/project/**`, `.agents/skills/**`).
- `rm -r`, or `rm` with a glob, on a path built from a variable or a pattern. Delete only explicit paths, or leave the files.
- Touch hardware (devices, serial or USB ports, instruments) unless your task says so; never send a device commands not seen in recorded original traffic.
- Modify the original, open windows on the user's display, kill processes by name pattern (`pkill`, `killall`), print the environment or read the credentials file.
- Launch subagents or parallel model calls.
- Invent behavior to fill a gap: return an explicit unsupported result and record what evidence would resolve it.
- In a clean-room task, open anything its CLEAN ROOM line forbids, even to unblock yourself. A value you infer from captures is labelled Reconstruction.

## Changes outside your owned paths

1. Make the edit, then `git diff -- PATH > .theseus/patches/01-name.patch` (a new file: `git diff --no-index /dev/null PATH`); number further patches 02, 03.
2. Undo it in the worktree (`git restore PATH`, or delete the new file) and check `git apply --check .theseus/patches/01-name.patch`.
3. To run checks with it, `git apply` it, then `git apply -R` after. Deliver only owned-path changes; list each patch under Coordinator actions.

## Checks

Run only the checks your task names (`theseus build` or heavy suites only when named), in the foreground, and read the output:

```bash
set -o pipefail   # without it a failing check looks green
<check> 2>&1 | tee .theseus/check-<name>.log
```

A plan or a running check is not a handoff. Stop every process you started; past the time budget, deliver what works.

## Deliver

```bash
git status --porcelain --untracked-files=all | awk '{print $NF}' | grep -v '^\.theseus/' | xargs -r sha256sum > .theseus/changed.sha256
```

Write `.theseus/handoff.md` once, at the very end:

```
Task: <id>   Worktree: <path>   Base: <sha>
Result: delivered | partial | blocked
Changed files: <path> <sha256> per line, or .theseus/changed.sha256
Checks: <exact command> -> <pass/fail, counts, exit code>   (one per line)
Logs: <paths>
Evidence: <bundles under the data root evidence/<task-id>/, evidence IDs>
Limits: <what is not established; unsupported cases returned>
Coordinator actions: <patches, ownership extensions, registrations needed>
Processes left running: none
```

`delivered`: every acceptance item met with evidence. `partial`: some met, the rest under Limits. `blocked`: you cannot go on without something you cannot get; name it and what would unblock it. Then run `theseus task done TASK_ID --handoff .theseus/handoff.md` (or `theseus task blocked TASK_ID REASON`) and report as your prompt or brief says. `theseus task note TASK_ID TEXT` records progress; `--question` asks the coordinator.

## Blocked, denied or interrupted

- **Missing information** (a spec gap, an unknown, a decision): do not guess or look where you may not. Finish what you can, then deliver `partial` or `blocked` naming the gap.
- **A permission prompt was rejected or a command denied:** do not retry it in another form. Use an allowed alternative, or record what you needed under Coordinator actions (`blocked` if it stops the task).
- **Conflict markers after the coordinator rebased your worktree:** `Updated upstream` is main, `Stashed changes` is yours. Keep both intents, remove the markers, rerun the checks, rewrite the handoff.
- **Context cleared (a restart prompt):** resume from your report, notes, `git status` and `git diff`; do not start over.

## Claims

Built is not observed at runtime, and N passes of a flaky test is a sample count, not a fix: say exactly what you checked.
