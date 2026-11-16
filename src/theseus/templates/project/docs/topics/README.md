# Topic documents

Start here for any question about how a subsystem behaves, what original facts it rests on, where the code lives and what is still open. There is one living document per subsystem; a reader should not need an investigation report to learn the current behavior. The dated reports a topic was folded from stay in `docs/project/archive/investigations/` and are pointed to, never repeated.

## How to read a topic

- Find the subsystem in the Topics table, open it and read `Current behavior` first; each claim carries its basis (**Recovered**, **Source data**, **Measurement**, **Reconstruction**, **Fixture**).
- `Recovered facts` is the citation table (address or capture, role, evidence ID, home report); `docs/re/citation-index.md` locates the same citation from the other side.
- `Open limits` lists what is not done, each limit once; `History` lists the folded reports, so a primary report is one hop away. `rg -n PATTERN docs/topics` searches all topics at once.

## Folding

- New reports name their topic in the opening paragraph, `Topic: <topic>`, using a name below, or `new` with a proposed name.
- A fold sweep merges reports into their topic, adds a row per report to `docs/project/archive/investigations/FOLD-MAP.tsv` (columns stem, class, topic, successor; classes C current, S superseded, D duplicated, H historical, A archive-only), then runs `theseus docs fold-apply --topic NAME`, which refuses to move a report whose citations or evidence IDs the topic lost.
- `theseus docs check` validates the map, the archive and every link to a moved report; it runs in the pre-push guard.

## Topics

| Topic | Scope |
| --- | --- |

## Contract

- Each topic is `docs/topics/<topic-name>.md`, named for the subsystem, never for a task or scenario. One topic owns a citation (the home topic); others link to it.
- Findings first: current behavior in present tense, each claim once, labelled with its basis.
- Every citation and evidence ID of every folded report appears in the topic, including those of superseded reports.
- Stale limits are re-verified at fold time and either restated as current or dropped with the reason in one clause.
- Numbers that age are carried only as dated measurements naming the environment.
- Prose paragraphs and list items are single unwrapped lines. No em dashes, no emojis. Aim for about 10 percent of the folded sources plus the facts table.

## Template

```markdown
# <Subsystem name>

Scope: one or two sentences, what is in and out, the owning code paths.

## Current behavior

Present-tense statements, findings first, each with its basis label.

## Recovered facts

| Citation | What | Evidence ID | Home report |
| --- | --- | --- | --- |

## Source data

Original files, fields and survey results the behavior depends on.

## Implementation map

| Component | Path | Role |
| --- | --- | --- |

## Reconstructions

Provisional behavior with no original counterpart, and what would confirm it.

## Open limits

Each limit once, re-verified at fold time.

## Verification entry points

Checks covering the topic.

## History

Folded reports with class and archive path.
```
