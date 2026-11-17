---
name: theseus-worker
description: Use when working an assigned task ID of a theseus port inside a worker worktree, as a headless, persistent or subagent worker, from first read to final handoff.
---

# Theseus worker

You implement one task inside its owned paths and hand back evidence. The coordinator integrates, commits and pushes; you never do.

## Start

1. `export LOG_DIR=<log directory from your prompt>` (normally `<worktree>/.theseus`, Git-ignored) and `mkdir -p "$LOG_DIR"`.
2. Read `docs/project/STATE.md`, then `theseus tasks show <TASK_ID>`: the record is the scope. Read the topic documents and reports it names; scan `docs/investigations/README.md` and `docs/re/citation-index.md` for prior work.
3. `git status` and `git log -1`: confirm the worktree is clean at the assigned base.
4. Load the domain skills you need (`theseus-native-re`, `theseus-reference-runs`, project skills).

## While working

- Edit only owned paths. If the work needs a change elsewhere, save it as a `git diff` in `$LOG_DIR/patches/NN-name.patch` (it must pass `git apply --check` on your base) and list it in the handoff.
- Never commit, never `git stash` (the stash list is shared by every worktree), never push. The coordinator's merge refuses a worktree whose HEAD moved.
- Implement the general contract over representative variations of the original's inputs; a single device, file or scenario is a fixture.
- Recover before you implement: the decompiled original, its data and recorded traffic first. Never invent behavior to fill a gap; return an explicit unsupported result and record what evidence would resolve it.
- Never kill processes by pattern (`pkill`, `killall`); stop only PIDs you started. Never print the environment (`env`, `printenv`, `set`): it holds credentials.
- The original is read-only. Never open a window on the user's display; use a private virtual display.
- A script you need twice belongs in your owned paths in the repository, never in a scratch folder.
- Write the report at the owned `docs/investigations/<name>.md` from `TEMPLATE.md`; create it early and save after each step. Do not touch the generated indexes.
- Past the time budget, deliver what works with its limits.

## Before replying

Run the focused checks the task names and read their output, in the foreground. A plan, partial work or a running check is not a handoff; finish or report the failure. Stop every process you started.

```bash
set -o pipefail
<focused check> 2>&1 | tee "$LOG_DIR/check-<name>.log"
git status --porcelain --untracked-files=all | awk '{print $NF}' | grep -v '^\.theseus/' | xargs -r sha256sum > "$LOG_DIR/changed.sha256"
```

Use `theseus build` only if the task message names it; heavy runs share `limits.heavy_runs` slots across all workers.

## Handoff (final message; write it to `$LOG_DIR/handoff.md` once, at the very end; keep progress notes in `$LOG_DIR/notes.md`)

```
Task: <id>   Worktree: <path>   Base: <sha>
Result: delivered | partial | blocked
Changed files: <path> <sha256>   (one per line, or the manifest path)
Checks: <exact command> -> <pass/fail, counts, exit code>   (one per line)
Logs: <paths>
Evidence: <bundle paths under the data root evidence/<task-id>/, evidence IDs cited>
Limits: <what is not established; unsupported cases returned>
Coordinator actions: <registrations, outside-path patches, ownership extensions needed>
Processes left running: none
```

## Common mistakes

| Mistake | Instead |
| --- | --- |
| Committing "to save work" | Leave changes uncommitted; the merge reads the working tree |
| Claiming a flaky test fixed after N passes | Report the sample count and the mechanism exercised |
| `tee` without `pipefail` | A failing build then looks green; keep `set -o pipefail` |
| Calling something "working" after a build | Built is not observed at runtime; say which you checked |
