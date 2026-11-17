# Design notes

Decisions behind the package layout, for contributors. User documentation starts at the [README](../README.md).

## Shape

- Python package (`src/theseus`, Python 3.11+ for `tomllib`), one CLI entry point, unittest suite. Standard library only apart from Textual, the single runtime dependency, which drives the interactive dashboard and is imported only by `theseus top` on a terminal.
- Logic that benefits from tests (task records, merge, guard, docs, catalog, cleanup, rules) is Python. Process supervision and tmux pane polling stay Bash (`src/theseus/scripts/`), because signal handling, `exec` chains that keep a recorded PID and systemd scopes are simplest there. The CLI runs them as `bash <packaged path>` with the `THESEUS_*` environment from `Config.environment()`; a variable already set in the process environment wins, which lets tests and one-off shells override a value.
- Templates live under `src/theseus/templates/project/`; a leading `dot-` in a path segment becomes `.` at scaffold time, so package data never relies on dotfiles surviving a wheel build.

## Configuration

- `theseus.toml` is the one project configuration. `theseus.local.toml` (Git-ignored) is merged over it only so machine paths such as the original's location stay out of Git, which the portable-path rule requires.
- The loader refuses to run without a `theseus.toml`; it never falls back to home or temporary directories. The data and worktree roots must lie outside the project.

## Task lifecycle

- `pending -> in_progress -> verification -> merged -> completed`. `merged` satisfies dependencies, so code-only rounds do not stall the queue, but only `completed` tasks are archived, so merged work stays visible until verified.
- `evidence.observed` and `evidence.userTested` are recorded separately from status, because a merge, a runtime observation and a user test are different claims.
- `currentMilestone` gates `ready` and `assign`; the rule tests (`theseus check`) reject in-progress work outside it.

## Integration

- There is one integration path, `theseus merge`. It refuses: tracked changes in main other than the task file, a worker HEAD that moved, paths outside ownership, untracked files the delivery would overwrite. On a failed apply or patch it resets only the touched paths and deletes only files it created.
- The task file may be dirty at merge time because dispatch leaves the assignment uncommitted; merge commits it afterwards together with the merged status.

## Verification

- `theseus verify` runs the configured commands on a clean detached worktree of the commit and writes a log whose first line names the commit and whose last line is the final status. The guard and `tasks complete --from-log` read only that format.
- Unverified code is counted with `git rev-list SHA --not <verified commits> -- <code pathspecs>`, so a full pass of a descendant covers all its ancestors and docs-only commits never count.
- A command kind with nothing configured prints NOT CONFIGURED and exits 4: neither a silent pass nor a hard failure.

## Citations

- The citation index has configurable kinds. Native ports cite addresses; a device-protocol port cites captures (`cap_<hash>@<frame>`), and the fold check protects both the same way.
