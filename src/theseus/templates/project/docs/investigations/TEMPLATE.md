# <Subject in plain words>

Task `<task-id>` on base `<short-sha>`. Topic: `<topic>` (a document in `docs/topics/`, or `new` with a proposed name). One or two sentences: what this slice establishes and the general contract it serves.

## Result

The finding or delivered behavior first, as short declarative statements. Label every claim with its basis, once, where it is made:

- **Recovered**: original behavior established from same-hash native evidence or an isolated reference run.
- **Source data**: read from the original's own files or configuration (name the file and field).
- **Measurement**: observed or timed on a named build, device and environment.
- **Reconstruction**: provisional replacement behavior; say what would confirm or replace it.
- **Fixture**: synthetic test input; never production evidence.

## Evidence

| Claim | Basis | Reference |
| --- | --- | --- |
| <one claim per row> | Recovered / Source data / Measurement / Fixture | `0x<address>` + evidence ID, `cap_<hash>@<frame>`, file and field, or test name |

Cited addresses and captures appear in `docs/re/citation-index.md` after `theseus docs index` runs. Evidence bundles and captures live under the data root, `evidence/<task-id>/`.

## Implementation

Owned paths changed and the interface or data contract they expose. Skip for research-only reports.

## Verification

Exact commands and their real outcome (pass/fail counts, exit status). Log paths. Changed-file SHA-256 manifest path.

## Limits

What this does not establish and the unsupported cases it returns explicitly. State each limit once; do not repeat project-wide disclaimers that `AGENTS.md` already makes.

## Next

The concrete follow-up task or research question this unblocks, if any.
