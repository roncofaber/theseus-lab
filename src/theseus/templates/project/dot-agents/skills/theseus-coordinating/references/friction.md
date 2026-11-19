# Reporting theseus friction

theseus improves from what coordinators run into. Keep a log of it in `.theseus/friction.md` in the main checkout (Git-ignored, so it never reaches a commit). The theseus maintainer reads it; the project's own tasks and reports are the wrong place for it.

## What to record

Anything about theseus itself that cost you time or could mislead the next coordinator:
- a bug: a command that failed, crashed, refused something it should accept, or reported success it did not achieve;
- friction: a missing command or option, a workaround you had to invent, an error message that did not say what to do, output you had to parse by hand;
- documentation: a skill, brief or `--help` text that is wrong, stale or contradicts what the CLI does;
- an improvement you would want, with the situation that made you want it.

Problems of the project, of a worker's code, of the agent CLI or gateway (unless theseus handles them badly), and your own mistakes go here only when theseus could have prevented them; say which it is.

## How

- Write the entry when it happens, while the command and its output are at hand, not at the end of the session. It is a short append, never a reason to stop the loop.
- Append with an edit at the end of the file; never rewrite or overwrite it, and never trim old entries.
- One entry per issue, newest last:

```markdown
## <date> <one-line title>

Observed: the exact command, its exit status and the error or output that matters (shortened, never secrets).
Workaround: what you did instead, or none.
Improvement: what theseus should do.
Context: theseus version (`theseus --version`), agent profile, task ID if any.
```

- An issue already in the log: add a dated line under its entry (seen again, new detail, fixed in a newer theseus) instead of a second entry.
- Workers report theseus problems in their handoff's Coordinator actions; carry the ones about theseus into the log.
- Never put credentials, tokens, private data or original code in it.
