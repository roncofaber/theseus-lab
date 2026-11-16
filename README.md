# theseus-lab

**T**oolkit for **H**euristic **E**xploration of **S**uperseded, **E**nd-of-life, **U**nsupported **S**oftware.

A coordinator agent and a pool of worker agents recover how a legacy program behaves (decompiled binaries, its data files, the traffic it exchanges with hardware) and rebuild it on a modern architecture, one task at a time, the way the Ship of Theseus is rebuilt one plank at a time. theseus-lab is the reusable part of that workflow: a Python CLI (`theseus`) plus a project scaffold that drops the agent contract, task queue, docs layout, skills and enforcement tests into any repository. Nothing in it assumes a language, engine, platform or kind of original.

## Install

Requires Python 3.11 or newer, Git and Bash, plus a coding agent CLI for the workers: Claude Code by default, or Codex, OpenCode, Gemini CLI or anything an agent profile can describe. Optional, per feature: `jq` (headless worker status, stop, wait), `tmux` (persistent worker sessions and the queue), `shellcheck` (shell lint in the Supervisor suite), a systemd user manager (per-worker resource slices, Linux).

```sh
uv tool install git+https://github.com/roncofaber/theseus-lab      # or: pipx install git+https://github.com/roncofaber/theseus-lab
theseus --help
theseus setup skill                 # installs the theseus-setup agent skill in ~/.agents/skills
theseus setup skill --client claude_code    # or in ~/.claude/skills for Claude Code
```

Then either follow [docs/getting-started.md](docs/getting-started.md), or open your coding agent and say: "Load the theseus-setup skill and set up theseus-lab for me" (without installing the skill: "Read https://github.com/roncofaber/theseus-lab/blob/main/src/theseus/skills/theseus-setup/SKILL.md and follow it"). Agent profiles and how to add your own are in [docs/agent-profiles.md](docs/agent-profiles.md).

In a project, `theseus tools` lists the external tools theseus knows (Ghidra, a JDK, REA, rizin and rz-bin, gdb, a Python with pefile, capstone and lief, Kaitai, Wine, Xvfb, xdotool, bubblewrap, QEMU, tshark, ffmpeg) and where each was found on this machine. Nothing is hard-coded: each tool resolves from its environment variable (`GHIDRA_INSTALL_DIR`, `JAVA_HOME`, or `THESEUS_TOOL_<NAME>`), then a per-machine setting written by `theseus tools set NAME PATH` into the Git-ignored `theseus.local.toml`, then PATH, then common install locations (newest version first) and, for REA, the command its own MCP registration recorded. Resolved tools reach every script, configured command and worker as `THESEUS_TOOL_<NAME>` (plus `GHIDRA_INSTALL_DIR` and `JAVA_HOME`); `eval "$(theseus tools env)"` gives a shell the same view. A project lists what it needs in `[tools] need`, and `theseus doctor` fails when one of those is missing.

`theseus doctor` also checks the REA version and whether the REA MCP server and the `reverse-engineer-anything` skill are wired for each agent client in `re.clients`. `theseus setup rea [--update]` wires them through REA's own `rea setup`, passing the Ghidra and JDK it resolved; REA records this machine's paths in each client's configuration, so every device runs `theseus setup rea` once and nothing machine-specific enters the repository. Restart agent sessions afterwards so they load the MCP server; `theseus instance restart PANE` does that for a persistent instance, typing `/exit` and then `claude --resume <session>` into its pane so it keeps its name, history and backend. A symlinked skill directory is left alone and only the server is registered. Install REA from its release (`npm install --global rea-agents`).

## Scaffold

```sh
theseus scaffold ~/software/laser-port --name laser-port
cd ~/software/laser-port
theseus check            # the project's Supervisor suite
```

`theseus upgrade [--dry-run]` later refreshes the files theseus owns in a project (the generic skills, the hook, the display helper) from the installed version, adds template files the project lacks and lists configuration keys newer than its `theseus.toml`; project-owned files are never touched. `scaffold` runs `git init` when the directory is not a repository (it never commits), writes the files below, links `.claude/skills` to `.agents/skills`, generates the docs indexes and sets `core.hooksPath` to `tools/git-hooks`. Existing files are kept unless `--force`.

| Path | Holds |
| --- | --- |
| `AGENTS.md` | The agent contract: objective, start-here table, team rules, built-in lessons |
| `docs/project/MANIFEST.md`, `STATE.md`, `ROADMAP.md` | The coordinator's standing mandate, the current snapshot, milestones and acceptance |
| `docs/project/tasks.json`, `tasks-archive.jsonl` | The task queue (open records plus policy) and the completed archive |
| `docs/investigations/` | One report per task from `TEMPLATE.md`; generated `README.md` index |
| `docs/topics/` | One living document per subsystem that reports are folded into |
| `docs/re/citation-index.md` | Generated index of every cited address and capture reference |
| `.agents/skills/theseus-*` | Generic skills: coordinating, worker (with prompt template and persistent-instance brief), native reverse engineering, reference runs |
| `tools/Supervisor/` | Unit tests enforcing the workflow rules (`theseus check`) |
| `tools/git-hooks/pre-push`, `tools/lib/xvfb.sh` | The push gate and a private virtual display helper |
| `theseus.toml` | The one configuration file; machine-specific values go in the Git-ignored `theseus.local.toml` |

Project skills (data formats, the device protocol, the new application's build) go next to the generic ones as `<prefix>-<topic>/SKILL.md`; the context budget test gives them a default byte budget.

## Configure

`theseus.toml` replaces every value the workflow would otherwise hard-code; `theseus config show` prints the resolved result and `theseus config get paths.data` one value. `theseus.local.toml` is merged over it, so developer paths never enter Git; `THESEUS_DATA`, `THESEUS_SCRATCH` and `THESEUS_WORKTREES` override the roots for one shell.

| Section | Keys |
| --- | --- |
| `[project]` | `name`, `prefix` (slice names, data root) |
| `[paths]` | `data` (durable evidence, logs, corpus, worker records), `scratch`, `worktrees`, `original` (read-only) |
| `[commands]` | `build`, `smoke`, `verify` (full verification on a clean worktree), `smoke_timeout`; an empty list reports NOT CONFIGURED (exit 4) |
| `[limits]` | `heavy_runs`: concurrent build, smoke, verify and `theseus heavy -- CMD` runs on this host |
| `[workers]` | `default` (agent profile, `claude-code` unless set), `retries`; `[workers.<profile>]` overrides `model`, `capacity`, `permission_mode`, `command`, `env`, `headless`, `resume`, `interactive`, and `preset` defines a variant (see `docs/agent-profiles.md`) |
| `[instances]` | `panes` (`[{ name, pane }]`), `coordinator` (session the instances report to, for agents with peer messaging), `brief`, `idle_minutes`, `handoff_settle_seconds`; `busy_pattern`, `launch` and `sessions_dir` override the profile |
| `[git]` | `base` (`HEAD` or `origin/main`), `remote`, `branch` |
| `[verification]` | `code_paths`, `doc_paths`, `max_unverified_merges`, `max_unverified_hours` |
| `[re]` | `targets` (`[{ name, path, sha256 }]`), `evidence` and `citations` patterns, `address_low`/`address_high`, `clients` (agent clients for the REA MCP server) |
| `[tools]` | `need` (tools the project requires); per-machine locations go in `theseus.local.toml` `[tools.paths]` via `theseus tools set` |
| `[consumers]` | per-extension regex for names a task's files declare (defaults for common languages) |

## The method in one page

1. **Contract and state live in the repository.** Every session, coordinator or worker, starts from `AGENTS.md`, `STATE.md` and the task record, never from memory. STATE is a snapshot that gets rewritten, not a log.
2. **Tasks are small and owned.** A record has an ID, owned paths (always including its report), one-sentence objective, a general contract, at most six observable acceptance items and a milestone; `theseus tasks add` refuses anything else and the Supervisor tests keep records within 6 KB.
3. **One milestone at a time.** `theseus tasks ready` offers only tasks of `currentMilestone` whose dependencies are merged or completed.
4. **Workers never commit.** A worker gets its own worktree on `work/<name>`, edits only its owned paths, puts anything else in `.theseus/patches/`, and ends with a handoff (result, changed files with hashes, exact checks, evidence, limits). `theseus merge` refuses a worktree whose HEAD moved. For docs-only rounds, or before a repository has its first commit, `theseus instance assign --in-place` skips the worktree and every commit; the worker writes its records to `.theseus/<task>/` and owned paths must not overlap any other active task.
5. **The coordinator integrates.** `theseus merge` checks ownership, applies the delivery three-way, applies the worker's patches with a private index, regenerates the indexes and commits exactly the touched paths; any conflict restores main and commits nothing.
6. **Merged, observed at runtime and user-tested are different claims.** `merge` marks a task merged; `theseus tasks observe` records that the coordinator saw it work; `--user` records user testing; `complete` needs a recorded check and log. STATE keeps the three lists apart.
7. **Code-only rounds owe a full pass.** The pre-push guard runs the Supervisor and docs checks on the pushed commit and refuses code that no passing `theseus verify` covers; `THESEUS_PUSH_UNVERIFIED="reason"` allows a logged code-only push only while the code commits since the last full pass stay within `max_unverified_merges` and the oldest of them is younger than `max_unverified_hours` (default 24).
8. **Recover before you build.** Every claim in a report carries its basis (Recovered, Source data, Measurement, Reconstruction, Fixture). Native lookups go cheapest first: the citation index and catalog, the pre-decompiled corpus (`theseus re show|callers|callees`), then a live decompiler. Runtime questions go to a reference run of the original in isolation: a compatibility layer with a private virtual display, or a VM with device passthrough and usbmon capture when the original's authority is its device protocol. Captures are cited as `cap_<hash>@<frame>` and indexed like addresses.
9. **Reports fold into topics.** Each report names a topic; fold sweeps merge reports into one living document per subsystem, and `theseus docs fold-apply` refuses to archive a report whose citations or evidence IDs the topic lost.
10. **Clean room, when the port will be published.** With `[rooms]` enabled, every task is `--room dirty` (reads the original's code, writes the specification in its own words) or `--room clean` (implements from the specification, captures and public documentation only). Instance assignment refuses a task from the other room, an instance that ever worked dirty is refused clean work for good (`<data>/rooms.json`), the task message carries the room rules, and the Supervisor rejects roomless tasks and clean tasks owning dirty paths.
11. **Reusable scripts live in the repository**, shell scripts pass shellcheck, tooling holds no developer paths, and every check's real output is reported. These are Supervisor tests, not advice.

**Agents, queue and dashboard.** Persistent agents are registered in `<data>/agents.json` (`theseus agents list|add NAME PANE|remove|enable|disable`, seeded from `[instances] panes`). `theseus queue tick` looks at every registered agent once: a delivered handoff marks its task for review and records a `delivered` event; an agent idle past `instances.idle_minutes` without a handoff gets one `continue` nudge, then a `stalled` event; a free agent is reset (a full `/clear` when it carries dirty context into clean work, a `/compact` after any finished task), assigned the next ready task that fits its room on a fresh worktree, and briefed. `theseus queue run` repeats the tick; `theseus queue wait` drives ticks until an event needs the coordinator (delivery, stall, error) and exits, so a coordinator runs it in the background and is woken by its exit. Tasks with `"hold": true` and a paused queue (`theseus queue pause|resume`) get no assignment; events are in `<data>/queue/events.jsonl` (`theseus queue log`). `theseus top` is the live dashboard: issues to act on, agents and their state, task and room, the queue by status, the repository and the verification state, recent events (`--once`, `--json`).

A round: read state, `theseus tasks ready`, `theseus dispatch ID` (one-shot headless worker under the default agent profile; `theseus wait` in the background) or `theseus instance assign|send` (persistent tmux sessions; `theseus instance watch` under a monitor), `theseus merge ID`, `theseus build`, `theseus smoke`, observe, `theseus tasks complete`, `archive`, `retire`, rewrite STATE, push.

Note for a fresh scaffold: the first push of code is refused until `commands.verify` is configured and `theseus verify` passed, or it is pushed with `THESEUS_PUSH_UNVERIFIED="reason"`.

## Development

```sh
uv venv .venv && uv pip install -e . --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

The tests run every command against temporary data, scratch and worktree roots with a throwaway Git identity, a fake agent CLI and a fake `tmux`, so they never touch real projects, the model gateway or live tmux panes. `test_package.py` builds a wheel, installs it into a fresh virtual environment and checks that a scaffold made from it passes its Supervisor suite.

## License

MIT, see `LICENSE`.
