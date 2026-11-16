# Contributing

Thanks for your interest in theseus-lab. Bug reports, fixes, documentation improvements and new agent profiles are all welcome.

## Development setup

```sh
git clone https://github.com/roncofaber/theseus-lab
cd theseus-lab
uv venv .venv && uv pip install -e . --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

The test suite needs `git`, `bash`, `jq` and `shellcheck`. It runs every command against temporary folders with a throwaway Git identity, a fake agent CLI and a fake `tmux`, so it never touches your projects, your agents or your tmux sessions.

To try your changes on a real project, install your checkout as the CLI:

```sh
uv tool install --editable .
```

## Guidelines

- The package uses only the Python standard library. Please don't add runtime dependencies.
- Process supervision and tmux handling live in `src/theseus/scripts/` as Bash, and must pass `shellcheck`.
- Templates for new projects are in `src/theseus/templates/project/`. A path segment starting with `dot-` becomes `.` when a project is scaffolded.
- Keep the package neutral. No company, site or vendor-specific settings in the package, the templates or the docs; those belong in a project's `theseus.toml` as a profile variant. The test suite checks this.
- Add or update tests with every behavior change, and run the full suite before opening a pull request.
- Write comments only when the reason for the code is not obvious from the code itself.

## Agent profiles

Profiles for Codex, OpenCode and Gemini CLI follow each tool's documentation but have not been tested end to end. If you use one of them, please report what works.

A profile is tested when:
- a dispatched worker started;
- the worker wrote its handoff;
- `theseus followup` resumed it.

Fixes go in `src/theseus/profiles.py`, along with a note in `docs/agent-profiles.md`.

## Reporting bugs

Open an issue with:
- the `theseus` command you ran and its full output;
- your OS, Python version and agent CLI version;
- the relevant part of `theseus.toml`.

Remove any private paths or project names first.
