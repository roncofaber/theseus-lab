# Tools

Project tooling that the `theseus` CLI does not cover. Every script prints its usage with `--help` or documents it in its header.

| Path | Purpose |
| --- | --- |
| `Supervisor/` | Unit tests that enforce the workflow rules; run with `theseus check` |
| `git-hooks/` | The pre-push gate (`git config core.hooksPath tools/git-hooks`) |
| `lib/xvfb.sh` | Private virtual display for headless runs and captures; source it, then `xvfb_start` and `xvfb_stop` |
| `reference/` | Scripts that set up and drive reference runs of the original (add as needed) |

## Conventions

- Scripts never modify the original and never write outside the data root, the scratch root and the repository.
- Automated runs are headless; anything that opens a window says so in its usage text.
- A script that is used more than once belongs here, committed, with a header saying what it does and why.
- Shell scripts pass shellcheck at warning severity; no hard-coded developer paths (use `theseus config get KEY`).
