# Task records

```sh
theseus tasks add ID --role ROLE --owned PATHS... --objective "..." --contract "..." --accept ITEMS... \
  [--depends IDS] [--milestone M] [--room clean|dirty] [--priority N] [--profile P] [--min-capability N] \
  [--touches FILES] [--hold]
```

A list option takes several values (`--accept A B C`) or repeats (`--accept A --accept B`); both keep every item. `--hold` creates the record held, so the queue cannot assign it before `theseus tasks release-hold ID`. Write values with Markdown backticks as separate argv items, never inside a double-quoted shell string, where the shell runs them.

`--owned` must include the task's report, `docs/investigations/<name>.md`. The record goes into the host store as `pending`; there is nothing to commit before it is assigned (workers read it with `theseus task show`). A passing `theseus verify` and `tasks complete` write the export under `docs/project/`, which you commit; `theseus tasks export` writes it any time. Never edit those files by hand: theseus ignores the edit, and `theseus doctor` reports it.

## Rules

- Size for one worker session (30 to 60 minutes): one subsystem boundary, one report.
- Acceptance items are observable: a focused command that passes, a named variation covered, an explicit unsupported result. At most six per record, 6 KB per record.
- Never scope a task as a scenario slice; one device, file or scenario is an acceptance fixture of a general contract.
- A change to an interface owns its consumers: `theseus tasks consumers ID` lists files outside `ownedPaths` that use names declared inside.
- `researchRequired` and `externalBlocker` each need `evidence` and `unblockCondition`; `externalBlocker` is only for inputs the team cannot obtain.
- `--profile P` only when the task needs what that profile adds (an MCP server's session state); the queue relaunches a compatible agent onto it and back afterwards.
- `--min-capability N` only when the task needs a profile `capability` of at least N.
- `--touches FILES` names shared files outside `ownedPaths` the task will patch. A running or delivered task that owns or touches one holds it (and overlapping owned paths do), so name them rather than discover them at merge.
- Hardware: say in the objective or contract when a task may operate a device, and which commands are allowed (only those seen in recorded original traffic). Workers treat any device as off limits otherwise.

## Rooms

With `[rooms]` enabled every record gets `--room dirty` (reads the original's code, writes the specification in its own words) or `--room clean` (implements from the specification, captures and public documentation only; its worktree does not even contain the dirty paths). The specification is the only channel between rooms: review it for copied code before clean workers read it. An agent that worked dirty takes clean work only after `theseus instance compact NAME --clear` (the queue does it; a summarizing compact is not enough); with `taint = "permanent"` it never does.

## Changing a record

`theseus tasks edit ID --objective ... --contract ... --accept ... --owned ... --add-owned ... --depends ... --touches ...` changes a pending task, and with `--force` an in-progress one (its worker gets an edit note and an updated task copy). A delivered or merged task: reopen or unmerge first. Single fields also have their own commands:

| Change | Command |
| --- | --- |
| Note carried in the task message | `theseus tasks note ID "text"` (replaces it); `theseus tasks edit ID --append-note "text"` adds a dated line |
| Priority / hold | `theseus tasks priority ID N`, `theseus tasks hold ID`, `theseus tasks release-hold ID` |
| Profile / capability | `theseus tasks profile ID NAME`, `theseus tasks capability ID N` |
| Current milestone | `theseus tasks milestone M` |
| Back to pending (assigned or delivered) | `theseus tasks release ID --reason "..."` |
| Delivered too early, back to the worker | `theseus tasks reopen ID --reason "..."` (from the main checkout; events.md) |
| Merged but wrong | `theseus tasks unmerge ID --reason "..."` |
| Merge committed by hand | `theseus tasks merged ID --commit SHA` |

Blockers: `theseus tasks edit ID --research-required EVIDENCE --unblock CONDITION` (or `--external-blocker`, `--clear-blockers`).
