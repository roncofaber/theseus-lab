# Supervisor rules

Unit tests that enforce the workflow rules on this repository. Run them with `theseus check` (it uses the interpreter theseus is installed in, so the `theseus` package is importable); the pre-push guard runs them on every pushed commit.

| Test | Enforces |
| --- | --- |
| `test_context_budget.py` | Byte budgets for the governance files, every skill (project skills get a default) and the startup read sets |
| `test_project_tasks.py` | Task identities, statuses and evidence, a report per open task, dependencies, blocker records, provider capacity, record budgets, one milestone at a time |
| `test_portable_paths.py` | No hard-coded developer paths in `tools/` |
| `test_shell_scripts.py` | Bash syntax and shellcheck at warning severity for every script in `tools/` |
| `test_repository_scripts.py` | Governance docs, skills and task records cite no scripts under scratch or temporary directories |
| `test_generality_guard.py` | No production file named for a single fixture (enable by filling `SCENARIO_WORDS`) |

Budgets, allowlists and scanned directories are the project's to tune in these files; the rule logic is in `theseus.rules`.
