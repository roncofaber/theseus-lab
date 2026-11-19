# Agent profiles

theseus never assumes one agent vendor. An agent profile describes how to launch, resume and drive one coding agent CLI, and every worker (headless, persistent or subagent) runs under one. `theseus agents profiles` lists the profiles, which CLIs are installed and the effective settings.

## Presets

| Profile | Agent | Headless run | Resume | Persistent instances | Verified |
| --- | --- | --- | --- | --- | --- |
| `claude-code` (default) | Claude Code, own subscription or API key | `claude -p PROMPT --session-id ID --output-format json` | `claude -p MSG --resume ID` | yes, with peer messaging | yes (through a wrapper launcher; `--model` not exercised) |
| `claude-subagent` | Claude Code subagents of the coordinator session | none: theseus prepares the worktree and `prompt.md`, the coordinator launches the subagent | the coordinator's own tools | no | yes |
| `codex` | OpenAI Codex CLI | `codex exec -s workspace-write PROMPT` | `codex exec resume --last MSG` | without a busy marker until you set one | no |
| `opencode` | OpenCode, each agent on its own private OpenCode server | `opencode run --standalone --format json --session ID PROMPT` | `opencode run --standalone --session ID MSG` | yes, supervised, with its SQLite transcript; no peer messaging (see [OpenCode](configuration.md#opencode)) | persistent instances yes (OpenCode 2.3.0, supervised in tmux); the headless run by hand, not through `theseus dispatch` |
| `gemini-cli` | Google Gemini CLI | `gemini -p PROMPT --yolo` | none | without a busy marker until you set one | no |
| `antigravity` (alias `agy`) | Google Antigravity CLI | `agy -p PROMPT --output-format json --mode auto` | `agy -p MSG --continue --output-format json --mode auto` | without a busy marker until you set one; `antigravity-jsonl` transcript adapter (see [Antigravity](configuration.md#antigravity)) | no |

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

## Host-wide profiles

How you reach an agent (your launcher, gateway model, environment, MCP servers, client settings) is about you and your machine, not about a project. Define such a profile once in the host configuration, `~/.config/theseus/config.toml` (see [Configuration layers](configuration.md#configuration-layers)), with the same fields as in `theseus.toml`:

```toml
# ~/.config/theseus/config.toml
[workers.gateway]
preset = "claude-code"
command = "agent-launcher"
model = "provider/model-name"
model_flag = ""
env = { GATEWAY_MODEL = "{model}" }
mcp_servers = ["theseus"]
extra_args = ["--settings", "~/.config/theseus/worker-settings.json"]
pool = "gateway"

[workers.gateway.interactive]
env = { CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS = "10000" }
```

A project then only refers to it, and sets what is about the project:

```toml
# theseus.toml
[workers]
default = "gateway"

[workers.gateway]
capacity = 4
```

A project section with the same name as a host profile overrides only the keys it sets; `env` and `interactive` (including `interactive.env`) merge key by key, so `[workers.gateway.interactive] env = { EXTRA = "1" }` adds a variable and keeps the host's. A host section named after a preset adjusts that preset for every project, under whatever the project sets. `theseus config show --sources` prints every field of every configured profile with the layer it came from, and `theseus setup host --import-project PATH` moves an existing project's profiles into the host file as a starting point.

## Profile fields

| Field | Meaning |
| --- | --- |
| `command` | The executable, substituted for `{command}` |
| `headless` | Argument list of a one-shot run; omit (none) for coordinator-launched subagents |
| `resume` | Argument list that continues a stopped run with `{message}`; stored in each worker's `launch.json` for `theseus followup` |
| `session` | `preassign` when the CLI accepts a session ID theseus chooses (`{session}`), `none` otherwise |
| `session_prefix` | What a preassigned session ID starts with, before a random UUID (OpenCode: `ses_`, the only ids it accepts) |
| `model`, `model_flag` | The model; the item `{model_args}` expands to `[model_flag, model]` only when a model is set |
| `context_limit` | The context window in tokens that the console measures an agent's context use against when its model is not in `[models] context` (200000) |
| `permission_mode` | Substituted for `{permission_mode}` (Claude Code: `auto`, `acceptEdits`, `bypassPermissions`, ...) |
| `env` | Extra environment for the run; `{model}` is substituted |
| `model_env` | Environment added to `env` only when a model is set, for a CLI whose interactive mode has no model flag (OpenCode: `OPENCODE_CONFIG_CONTENT = '{"model": "{model}"}'`) |
| `retry` | Resume automatically after a retryable gateway error (Claude JSON output with status 429, 5xx or 529) |
| `capacity` | Concurrent active tasks under this profile |
| `capability` | An integer rank of what the profile's agents can handle (default 1); a task with a higher minimum capability never goes to them (see [Capability](#capability)) |
| `extra_args` | Arguments appended to the headless run, the resume and the persistent-instance launch, after the MCP flags. Placeholders are filled as in the templates and a leading `~` is expanded, for example `["--settings", "~/.config/theseus/worker-settings.json"]` to give workers their own client settings file |
| `mcp_servers` | MCP servers the agent starts with, and only those (`--strict-mcp-config --mcp-config <data>/mcp/<profile>.json`, from `mcp_flags`); each is copied from your Claude Code configuration, and `theseus` is the theseus server. Unset: the client's own servers. OpenCode has no MCP flag and merges the file over its own configuration, so for it the file also disables each server of your global OpenCode configuration the list leaves out (a project's own `opencode.json` still wins) |
| `mcp_format`, `mcp_env` | How the MCP config file is written (`claude`, the default: `mcpServers`; `opencode`: `mcp.servers` with local `command` lists and `environment`) and, for a CLI that reads it from its environment instead of a flag, the variables that name it (`{mcp_config}`; OpenCode: `OPENCODE_CONFIG`). The variables join `env` in the headless run, the resume and the persistent instance |
| `pool` | A host-wide pool shared with other projects (see [Configuration](configuration.md#sharing-agents-between-projects)) |
| `fallback`, `health` | Host file only: the profile that takes over this profile's agents while it is down, and how its health is probed (see [Profile fallback](configuration.md#profile-fallback)) |
| `interactive.launch`, `interactive.resume` | How a persistent instance is started and resumed (`{launch}`, `{session}`) by `theseus instance restart`; the Claude Code presets launch `{command} --name {agent} --permission-mode {permission_mode}`, so a restarted agent keeps its name and permissions, and an option whose value is empty is left out |
| `interactive.resume_missing` | Text the CLI prints when the session to resume no longer exists; `instance restart` then launches the agent fresh with the same launch line and says so |
| `interactive.env` | Extra environment for the persistent instance. It and `env` are put in front of the launch line as `env VAR=value ...` (shell-quoted) whenever theseus launches the agent: `theseus instance restart` and queue relaunches. You start each agent the first time yourself, so start it the same way or restart it once after registering |
| `interactive.busy_pattern` | Text the agent shows only while working; the queue treats the pane as idle otherwise |
| `interactive.stall_patterns` | Screen text that means the agent has stopped and needs the coordinator. A pane holding a task that shows one is `thrashing` at once, busy or not: the queue records one `stalled` event and never nudges it. Claude Code presets: `["Autocompact is thrashing"]` |
| `interactive.gateway_patterns` | Regular expressions that mark a gateway error on the bottom of an idle pane holding a task (state `gateway_error`, see [Workflow](workflow.md)). Claude Code presets: `API Error: <status>`, `Error 52x`, 529 or `overloaded_error`, 429 or `rate_limit_error`, `Resend your message to try again`, `Prompt is too long` |
| `interactive.gateway_ready` | Text the CLI shows while it waits after an error (Claude Code: `Resend your message to try again`); with it on screen the error is always taken as current |
| `interactive.context_pattern` | A regular expression for the CLI's context-size hint; group 1 is the number and group 2 an optional `k`. Claude Code: `/clear to save ([0-9.]+)(k?) tokens` |
| `interactive.input_pattern`, `interactive.clear_input` | The input line (group 1 is its text) and the tmux key that empties it, used by the verified send to spot text that was typed but never submitted. Claude Code: a line starting with `>` or the prompt glyph, and `C-u` (one press clears one visual line of a wrapped input, so the send repeats it for supervised agents until the line is empty) |
| `interactive.paste_placeholder` | A regular expression for what the input line shows instead of a long text: Claude Code collapses a long typed or pasted text into `[Pasted text #N]`, which then counts as text still held in the input line |
| `interactive.exit` | The command that ends the agent at an idle prompt, used to stop and restart supervised agents and by `theseus agents migrate` (Claude Code: `/exit`) |
| `interactive.session_flag` | The flag that preassigns a conversation id when a supervised agent starts fresh, for profiles with `session = "preassign"` (Claude Code: `--session-id`); a restart that resumes uses `interactive.resume` |
| `interactive.status_command` | A command that prints the CLI's sessions as a JSON list with `pid`, `sessionId`, `status` (`busy`, `idle`, `waiting`) and `waitingFor`, matched to a supervised agent by its pane's process tree (Claude Code: `["claude", "agents", "--json"]`). Without one, readiness and busy come from the screen |
| `interactive.ready_pattern` | Optional screen text of a ready prompt, for CLIs without a status command |
| `interactive.startup_dialogs` | Screen texts of startup questions only a human may answer (Claude Code: the folder trust question and a new MCP server); a supervised agent showing one is `blocked` and theseus never answers it |
| `interactive.prompt_patterns` | Regular expressions for a question on screen that needs a human, matched in the pane's last 15 lines (Claude Code: the `1. Yes` choice line of a permission prompt; OpenCode: the footer rows of its permission prompt and question dialog; codex and gemini-cli set none). Any agent, supervised or hand-made, showing one or a startup dialog is `waiting`, and theseus types nothing into it (no task message, nudge, compact, clear or exit command, no `theseus instance send`) until a human answers, for example from the console's send line |
| `interactive.compact`, `interactive.clear` | Commands typed between tasks: compact (same room, or clean to dirty) and clear (dirty to clean); `{keep}` is what compaction keeps (OpenCode's `/compact` takes no text: with any, it is sent as an ordinary message, so its preset leaves `{keep}` out) |
| `interactive.sessions_dir` | Where the CLI records running sessions (needed by `instance restart`) |
| `interactive.transcript` | The transcript adapter that reads the CLI's own record of a session (see [Transcript adapters](#transcript-adapters)): `claude-code-jsonl` (the Claude Code presets), `opencode-sqlite` (the opencode preset), `pane-history` (the codex and gemini-cli presets: the console shows the pane's tmux scrollback) or `none` |
| `interactive.messaging` | Whether the agent can message the coordinator session; without it workers reply in their pane and the queue detects the handoff file |

Placeholders: `{command}`, `{prompt}`, `{message}`, `{session}`, `{model}`, `{permission_mode}`, `{keep}`, `{launch}`, `{agent}` (the registered agent name, shell-quoted), and the list item `{model_args}`.

## Wrapped CLIs and gateways

A launcher that wraps a supported CLI (a corporate gateway, a model router) is a variant of that CLI's profile, best defined in the host configuration:

```toml
[workers.gateway]
preset = "claude-code"
command = "claude-via-gateway"
model = "gateway/model-name"
model_flag = ""                         # the launcher reads the model from its environment instead
env = { GATEWAY_MODEL = "{model}" }
capacity = 4
```

### Persistent instance environment

`env` and `interactive.env` reach a persistent instance through its launch line, since the agent starts in the pane's own shell. A model with a small context window, or a gateway that sends every tool schema on every request, fills its context quickly when the agent reads whole files, and Claude Code compacts over and over ("Autocompact is thrashing"). Lowering the file read limit makes the agent read large files in line ranges:

```toml
[workers.gateway.interactive]
env = { CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS = "10000" }
```

`theseus instance restart PANE` then relaunches the agent as `env CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS=10000 claude-via-gateway --name worker-1 --permission-mode auto --resume SESSION`; restart running agents to apply a change.

## Per-task profiles

A task can require a profile: `theseus tasks add ID ... --profile NAME`, or `theseus tasks profile ID NAME` later (`theseus tasks profile ID` with no name clears it; no profile means any agent). The queue gives such a task only to:
- an agent registered with that profile;
- an agent whose registered profile differs from it only in `mcp_servers`, `env` or `interactive.env` (same command, pool, model, templates and `extra_args`; `capacity` and `capability` may differ). Before the task the queue restarts that agent's pane with the task profile's launch line, resuming its session, then compacts or clears it as usual, and records a `relaunched` event.

The profile an agent currently runs is kept in the queue's runtime state, and the agent is switched back to its registered profile (another `relaunched` event) before its next task that does not require the special one. `theseus instance restart PANE` keeps whichever profile the agent currently runs. The task's assignment still records the agent's registered profile, so capacity counts it there. When no registered agent can take a task, the dashboard's waiting list says `needs profile NAME`.

## Capability

`capability` ranks profiles by what their agents can handle, for example a stronger model above a cheaper one. It is an integer set in the host config (default 1; the package sets no other value, and the numbers mean only what you decide):

```toml
[workers.strong]
preset = "claude-code"
model = "provider/large-model"
capability = 3
```

A task can ask for a minimum: `theseus tasks add ID ... --min-capability N`, `theseus tasks capability ID N` later (0 removes it), or the console's actions (Ctrl+P, "Raise/Lower the minimum capability"). The queue then gives the task only to an agent whose registered profile has at least that capability. Exact profile routing wins: an agent registered with the task's `--profile` takes it whatever the numbers say. An agent relaunched onto a compatible task profile (above) must meet the minimum with both its registered profile and the task profile; `capability` itself never decides whether two profiles are relaunch compatible. When the minimum is the only thing that keeps every agent from a task, the console's WAITING reason says `needs capability >= N`. Capability also steers which free agent takes a task when several qualify: stronger agents take the tasks only they can do and leave easier ones to free weaker agents (see [Workflow](workflow.md)). The console shows an agent's capability and a task's minimum as filled pips (`●●` and `≥●●●`, red when no agent meets it). Capability works the same for every preset.

### Example: an MCP server only for the tasks that need it

Every MCP tool schema an agent loads costs context on each request, and on a gateway without tool search a large server can take a third of the window before the agent reads anything. Keep the default profile lean and add a variant with the heavy server:

```toml
[workers.gateway]
preset = "claude-code"
command = "claude-via-gateway"
mcp_servers = []                        # no MCP servers: the lean default
pool = "gateway"

[workers.gateway-rea]
preset = "claude-code"
command = "claude-via-gateway"
mcp_servers = ["rea"]
pool = "gateway"
```

Register agents with the lean profile and mark only the tasks that need REA's session state, such as long interactive decompilation work, with `--profile gateway-rea`. Everything else uses REA through its skill and command line (`rea <command>`, `theseus re rea ...`) and the indexed corpus (`theseus re show|callers|callees|find`), which cost no context until used.

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

## Transcript adapters

What theseus reads from a CLI's record of its own session goes through one adapter, named by the profile's `interactive.transcript`. The adapter supplies the context in use and the model for the console's context meter, the newest API errors for the gateway classification, and the conversation the agent popup's Conversation tab shows. Without one, the context meter falls back to `interactive.context_pattern`, gateway errors are read from the pane only, and the Conversation tab shows the pane's tmux scrollback (`pane-history`, for CLIs that print inline) or is hidden (`none`).

| Adapter | Reads |
| --- | --- |
| `claude-code-jsonl` | Claude Code's JSONL transcript: the session of a supervised agent from its stored session id, of another pane from the client's `interactive.sessions_dir`; user prompts and slash commands as typed, assistant text as Markdown, each tool call as one `Tool: key argument` line and each result as a one-line preview (both unfold), never thinking, system reminders, command output or compaction summaries |
| `opencode-sqlite` | OpenCode's session database (`OPENCODE_DB`, else `$XDG_DATA_HOME/opencode/opencode.db`, also when set in `interactive.env`), read-only: the session of a supervised agent from its stored session id (the database names no pane or process, so a hand-made pane gets none); user text, assistant text as Markdown, tool calls and their results, the error that ended a failed turn (with OpenCode's error kind mapped to `rate_limit`, `server_error` or `connection_lost`), never reasoning or compaction summaries; the context in use is the last answer's input, output, reasoning and cached tokens, the figure OpenCode's own meter shows |
| `antigravity-jsonl` | Antigravity CLI's JSONL transcript (`<sessions_dir>/<conversation-id>/.system_generated/logs/transcript.jsonl`, defaulting to `~/.gemini/antigravity-cli/brain` or `$AGY_APP_DATA_DIR/brain`): user prompts, assistant text as Markdown, tool calls and their finished results, model, context tokens (`input_tokens + cache_read_tokens + output_tokens`) and error steps; thinking and system messages are left out |
| `pane-history` | Nothing; the console shows the pane's scrollback (`capture-pane -S`) |
| `none` | Nothing; no Conversation tab |

An adapter is one module in `src/theseus/adapters/`, registered in `ADAPTERS` in `src/theseus/adapters/__init__.py`, with three functions:
- `session_state(conf, agent, interactive)`: the state of the session running in the agent's pane, or None;
- `session_file(conf, interactive, session, cwd)`: the record of a known session id started in `cwd` (the coordinator), or None;
- `read(path)`: the state of that record, read incrementally and safe to call from several threads.

A state is a dict with:
- `tokens` and `model`: the context in use after the last answer and its model, or None;
- `errors`: the newest API errors as `{"time", "error", "text"}`, where `error` is the CLI's own error kind or None;
- `last`: `answer`, `error` or None, for the newest assistant entry;
- `entries`: the newest conversation entries as `{"kind", "text", "time", "n"}`, with `kind` one of `user`, `assistant`, `tool` (plus `name`) and `result` (plus `error`), and `n` numbering the entries of the record from 1 so the console asks only for new ones; optionally `"format": "markdown"` for text the console renders as Markdown and `summary`, the one line a folded entry shows (a tool call's key argument, shown as `Tool: argument`; a result's first line) while `text` is what unfolds; thinking and system or meta entries are left out; the list is replaced, never changed in place;
- `total`: how many entries were ever read;
- `key`: names the record; a new key (a new session, a rewritten file) makes the console start over.

Adding a CLI means writing that module in its own record format, adding its name to `ADAPTERS`, and setting `transcript = "<name>"` in the preset's `interactive` table. The console redacts API keys, key hashes and email addresses in everything it shows from a transcript or the scrollback.
## What depends on the preset

Every theseus feature is driven by profile keys, never by a hard-coded agent. The `claude-code` preset sets them all; `opencode` sets all but `status_command`, `sessions_dir` and `messaging` (see [OpenCode](configuration.md#opencode)); the `antigravity`, `codex` and `gemini-cli` presets leave `busy_pattern`, `gateway_patterns`, `input_pattern` and `prompt_patterns` unset (see [Antigravity](configuration.md#antigravity)) and degrade as follows until you add the missing keys to their `[workers.<name>.interactive]`:

| Feature | Needs | Without it |
| --- | --- | --- |
| Verified send (task messages, reminders, resends, restart prompts) | `busy_pattern`; `input_pattern` and `clear_input` for the second Enter | the text is typed and counted as sent, unverified |
| Stall reminders | `busy_pattern` | a silent agent is shown as `quiet` and never nudged |
| Thrashing detection | `stall_patterns` | none |
| Gateway error recovery and the circuit breaker | `gateway_patterns`, optionally `gateway_ready` and `context_pattern` | gateway errors look like an idle stall |
| Fresh-context recovery and the restart prompt | `clear` (the restart prompt itself needs nothing) | the restart prompt is sent without clearing the context |
| Context reset between tasks | `compact`, `clear` | none |
| Supervised agents: session ids, busy and waiting from the CLI | `session_flag`, `status_command`, `sessions_dir` | readiness and busy come from the screen (`ready_pattern`); a restart resumes only with `resume` |
| Supervised stop and migrate | `exit` | the agent is stopped with a signal; `theseus agents migrate` skips it |
| Context use, gateway error classification and the Conversation tab | `transcript` (an adapter, above) with `sessions_dir`, else `context_pattern` | context from the pane hint or none; errors classified from the screen; the Conversation tab shows the pane scrollback (`pane-history`) |
| Capability, task profiles, pools, queue, merge and verification | nothing | |

## How delivery is detected

Workers end every task with `.theseus/handoff.md` in their worktree, and it starts with a `Result:` line (delivered, partial or blocked). A headless worker is done when its process exits. A persistent instance counts as delivered once three conditions hold:
- its pane is idle (no busy pattern);
- the handoff states a Result;
- the handoff has not changed for `instances.handoff_settle_seconds` (45 by default).

These conditions make detection independent of peer messaging, so agents without it work the same way.
