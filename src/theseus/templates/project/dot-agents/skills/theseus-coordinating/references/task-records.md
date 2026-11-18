# Task records

```sh
theseus tasks add ID --role ROLE --owned PATHS... --objective "..." --contract "..." --accept ITEMS... \
  [--depends IDS] [--milestone M] [--room clean|dirty] [--priority N] [--profile P] [--min-capability N]
```

`--owned` must include the task's report, `docs/investigations/<name>.md`. The record is written as `pending`; commit `docs/project/tasks.json` before the task is assigned (dispatch and the queue refuse or commit an uncommitted record). `theseus check` validates every record.

## Rules

- Size for one worker session (30 to 60 minutes): one subsystem boundary, one report.
- Acceptance items are observable: a focused command that passes, a named variation covered, an explicit unsupported result. At most six per record, 6 KB per record.
- Never scope a task as a scenario slice; one device, file or scenario is an acceptance fixture of a general contract.
- A change to an interface owns its consumers: `theseus tasks consumers ID` lists files outside `ownedPaths` that use names declared inside.
- `researchRequired` and `externalBlocker` each need `evidence` and `unblockCondition`; `externalBlocker` is only for inputs the team cannot obtain.
- `--profile P` only when the task needs what that profile adds (an MCP server's session state); the queue relaunches a compatible agent onto it and back afterwards.
- `--min-capability N` only when the task needs a profile `capability` of at least N.
- Hardware: say in the objective or contract when a task may operate a device, and which commands are allowed (only those seen in recorded original traffic). Workers treat any device as off limits otherwise.

## Rooms

With `[rooms]` enabled every record gets `--room dirty` (reads the original's code, writes the specification in its own words) or `--room clean` (implements from the specification, captures and public documentation only; its worktree does not even contain the dirty paths). The specification is the only channel between rooms: review it for copied code before clean workers read it. An agent that worked dirty takes clean work only after `theseus instance compact NAME --clear` (the queue does it; a summarizing compact is not enough); with `taint = "permanent"` it never does.

## Changing a record

There is no `tasks edit`. Use the command for the field:

| Change | Command |
| --- | --- |
| Note carried in the task message | `theseus tasks note ID "text"` |
| Priority / hold | `theseus tasks priority ID N`, `theseus tasks hold ID`, `theseus tasks release-hold ID` |
| Profile / capability | `theseus tasks profile ID NAME`, `theseus tasks capability ID N` |
| Current milestone | `theseus tasks milestone M` |
| Back to pending (assigned or delivered) | `theseus tasks release ID --reason "..."` |
| Delivered too early, back to the worker | `theseus tasks reopen ID --reason "..."` (move the handoff aside first; events.md) |
| Merged but wrong | `theseus tasks unmerge ID --reason "..."` |
| Merge committed by hand | `theseus tasks merged ID --commit SHA` |

Anything else (objective, contract, acceptance, owned paths, dependencies): edit the record in `docs/project/tasks.json`, preferably while it is `pending`, run `theseus check`, and commit before the next assignment. A worker already on the task does not see the edit; tell it with `theseus instance send`.
