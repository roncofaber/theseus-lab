---
name: theseus-native-re
description: Use when a claim depends on how an original binary behaves - recovering a formula, call order, data layout, protocol encoder or decoder, state machine or any unrecovered native behavior - or when citing, decompiling or naming an address in an original executable or library.
---

# Native reverse engineering of the original binaries

Cheapest lookup first. A live decompiler session costs minutes and holds a lease; most questions are answered by the catalog, the corpus and the docs in seconds.

**REQUIRED SUB-SKILL:** load `reverse-engineer-anything` (shipped by REA) before the first REA tool call. It owns REA tool selection, connection recovery, deadlines and evidence semantics; this skill adds only the lookup order and project conventions. Do not copy it into the repository. The REA tools come from the `rea` MCP server: `theseus doctor` shows whether it and the skill are wired for this host's agent clients, `theseus setup rea [--update]` wires them, and a session started before that needs a restart to see the tools.

## Targets

The binaries are `[[re.targets]]` in `theseus.toml` (name, path, sha256). Verify the hash before citing; a citation is valid for that hash only. Exported and debug-symbol names are authoritative; never invent a name for an unnamed function. Describe it by address and observed role instead.

## Lookup ladder

`theseus re status` shows which targets already have a catalog, a corpus and a call graph; a target with all three is never decompiled again unless its hash changes. A new binary is inventoried first (`theseus re inventory DIR --out FILE.csv`), then registered as a target.

1. **Already recovered?** `theseus re find ADDRESS_OR_REGEX [--target NAME]` searches the target's exports, strings and `docs/re/citation-index.md` in one pass; any address form works (`0x401000`, `FUN_00401000`). Then read the cited report lines and the topic document.
2. **Read the code without a live session:** decompile each target once with REA into the corpus (`theseus re rea decompile-all <path> --provider ghidra --output <corpus> --resume`; `theseus re paths` prints the corpus directory), then `theseus re show ADDRESS` prints the function containing it in under a second, and `theseus re calls` once followed by `theseus re callers|callees ADDRESS_OR_NAME` answers call-graph questions. `rg` the corpus for constants, strings and field offsets.
3. **Bytes, structures, tables:** rizin (`rizin -q -c 'aa; pdf @ ADDR' FILE`), or Python with `pefile`, `capstone`, `lief`, `rzpipe` for tables, vtables and constant layouts the decompiler renders poorly. Managed bytecode (CLR, JVM): use its own decompilers; the same citation rules apply.
4. **Live decompiler only when the corpus cannot answer** (types, renaming, data flow, a fresh evidence export): `REA_LIVE=1 theseus re rea ...`. Export evidence into the data root `evidence/<task-id>/`.
5. **Runtime-only questions** (values, ordering, device traffic, timing): the `theseus-reference-runs` skill.

## Checkpoints and concurrency

- One live session holds a checkpoint's lease. Parallel workers copy the checkpoint into their log directory and point REA at the copy; names set in a copy are discarded, so record them in the report.
- Only the coordinator writes accepted names and comments into the canonical checkpoint, in one session, after integration. Close the binary before handing off.

## Recording results

- Every native claim cites address plus evidence ID from a same-hash bundle (or the corpus file hash and the run that produced it), in the Evidence table of the report template.
- Mark each claim Recovered, Reconstruction or Fixture. Pseudocode shape is not semantics: say what the instructions establish and what they do not.
- `docs/re/citation-index.md` and `docs/investigations/README.md` are generated and coordinator-owned; workers do not regenerate them.
- An unresolved question is a bounded research result with the next concrete probe, not a blocker.

## Common mistakes

| Mistake | Instead |
| --- | --- |
| Opening the decompiler to find or read a function | `theseus re find`, then `theseus re show` or `rg` the corpus |
| Citing an address without the binary's hash | Cite target hash, address and evidence ID |
| Retrying a timed-out MCP call with the same arguments | Read REA guidance on deadlines; use the CLI with a longer startup budget |
| Evidence left in scratch | The data root `evidence/<task-id>/` |
| Two workers on the canonical checkpoint | A private copy per worker |
