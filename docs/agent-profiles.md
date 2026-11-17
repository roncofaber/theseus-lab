# Agent profiles

theseus never assumes one agent vendor. An agent profile describes how to launch, resume and drive one coding agent CLI, and every worker (headless, persistent or subagent) runs under one. `theseus agents profiles` lists the profiles, which CLIs are installed and the effective settings.

## Presets

| Profile | Agent | Headless run | Resume | Persistent instances | Verified |
| --- | --- | --- | --- | --- | --- |
| `claude-code` (default) | Claude Code, own subscription or API key | `claude -p PROMPT --session-id ID --output-format json` | `claude -p MSG --resume ID` | yes, with peer messaging | yes (through a wrapper launcher; `--model` not exercised) |
| `claude-subagent` | Claude Code subagents of the coordinator session | none: theseus prepares the worktree and `prompt.md`, the coordinator launches the subagent | the coordinator's own tools | no | yes |
| `codex` | OpenAI Codex CLI | `codex exec -s workspace-write PROMPT` | `codex exec resume --last MSG` | without a busy marker until you set one | no |
| `opencode` | OpenCode | `opencode run --format json --session ID PROMPT` | `opencode run --session ID MSG` | without a busy marker until you set one | no |
| `gemini-cli` | Google Gemini CLI | `gemini -p PROMPT --yolo` | none | without a busy marker until you set one | no |

`codex exec resume --last` picks the most recent session in the current directory, and each worker has its own worktree, so follow-ups reach the right worker.

Verified means the profile has been run end to end: a dispatched worker started, wrote its handoff and was resumed. Unverified presets follow each CLI's documented flags. Before relying on one, dispatch a trivial task and check those three steps, then adjust the profile with an override if a flag differs in your installed version.

## Choosing and adjusting a profile

In `theseus.toml`:

```toml
[workers]
default = "codex"
retries = 3

[workers.codex]
model = "gpt-5-codex"
capacity = 2
```

A section named after a preset adjusts it. A section with any other name defines a variant with `preset`:

```toml
[workers.cheap-research]
preset = "opencode"
model = "provider/small-model"
capacity = 3
```

Use a variant per task with `theseus dispatch TASK --provider cheap-research`, or per persistent agent with `theseus agents add NAME PANE --profile cheap-research`. Capacity checks count active tasks per profile name.

## Profile fields

| Field | Meaning |
| --- | --- |
| `command` | The executable, substituted for `{command}` |
| `headless` | Argument list of a one-shot run; omit (none) for coordinator-launched subagents |
| `resume` | Argument list that continues a stopped run with `{message}`; stored in each worker's `launch.json` for `theseus followup` |
| `session` | `preassign` when the CLI accepts a session ID theseus chooses (`{session}`), `none` otherwise |
| `model`, `model_flag` | The model; the item `{model_args}` expands to `[model_flag, model]` only when a model is set |
| `permission_mode` | Substituted for `{permission_mode}` (Claude Code: `auto`, `acceptEdits`, `bypassPermissions`, ...) |
| `env` | Extra environment for the run; `{model}` is substituted |
| `retry` | Resume automatically after a retryable gateway error (Claude JSON output with status 429, 5xx or 529) |
| `capacity` | Concurrent active tasks under this profile |
| `pool` | A host-wide pool shared with other projects (see [Configuration](configuration.md#sharing-agents-between-projects)) |
| `interactive.launch`, `interactive.resume` | How a persistent instance is started and resumed (`{launch}`, `{session}`) by `theseus instance restart` |
| `interactive.busy_pattern` | Text the agent shows only while working; the queue treats the pane as idle otherwise |
| `interactive.compact`, `interactive.clear` | Commands typed between tasks: compact (same room, or clean to dirty) and clear (dirty to clean); `{keep}` is what compaction keeps |
| `interactive.sessions_dir` | Where the CLI records running sessions (needed by `instance restart`) |
| `interactive.messaging` | Whether the agent can message the coordinator session; without it workers reply in their pane and the queue detects the handoff file |

Placeholders: `{command}`, `{prompt}`, `{message}`, `{session}`, `{model}`, `{permission_mode}`, `{keep}`, `{launch}`, and the list item `{model_args}`.

## Wrapped CLIs and gateways

A launcher that wraps a supported CLI (a corporate gateway, a model router) is a variant of that CLI's profile:

```toml
[workers.gateway]
preset = "claude-code"
command = "claude-via-gateway"
model = "gateway/model-name"
model_flag = ""                         # the launcher reads the model from its environment instead
env = { GATEWAY_MODEL = "{model}" }
capacity = 4
```

## Adding an agent theseus does not know

Base a variant on the closest preset and override what differs:

```toml
[workers.myagent]
preset = "gemini-cli"
command = "myagent"
headless = ["{command}", "run", "--non-interactive", "{model_args}", "{prompt}"]
resume = ["{command}", "run", "--continue", "{message}"]
model_flag = "--model"
capacity = 1

[workers.myagent.interactive]
launch = "myagent"
busy_pattern = "Thinking"
compact = "/compact"
clear = "/reset"
messaging = false
```

The requirements for a worker agent are small: a non-interactive mode that takes the prompt as an argument, works in the current directory and exits when done. For persistent instances: a REPL in a tmux pane with commands to compact and clear its context, and a visible busy indicator. A profile that works well is worth contributing back as a preset.

## How delivery is detected

Workers end every task with `.theseus/handoff.md` in their worktree, and it starts with a `Result:` line (delivered, partial or blocked). A headless worker is done when its process exits. A persistent instance counts as delivered once three conditions hold:
- its pane is idle (no busy pattern);
- the handoff states a Result;
- the handoff has not changed for `instances.handoff_settle_seconds` (45 by default).

These conditions make detection independent of peer messaging, so agents without it work the same way.
