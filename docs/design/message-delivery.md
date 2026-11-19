# Message delivery: a store inbox with per-harness adapters

Status: future expansion, not scheduled. Written 2026-11-19 against theseus 90cbd21 (store schema 7). Nothing here is implemented.

## Why

Theseus reaches a running agent by typing into its terminal: `tmux send-keys` followed by screen reading to decide whether the text arrived and whether a prompt or a person is in the way. Task messages, nudges, restart and start prompts, `theseus instance send` and the attention push all go this way. It is the one channel every agent CLI accepts, and it works, but every confirmation is inferred from pixels. On 2026-11-18 and 2026-11-19 three bugs came from that layer alone: a start prompt retyped every tick because a fast OpenCode answer hid the busy marker, a false send failure for the same reason, and automatic sends clearing a draft the user was typing. Each new harness needs its screen patterns verified by hand before theseus can trust it.

Structured data already left tmux: workers report through the task store (`theseus task done`, notes, questions) and the coordinator learns what needs it from `theseus attention`. What still travels by keystrokes is the messages themselves. The agent CLIs now offer more direct channels, and theseus should use them, keeping typing as the fallback.

## What each harness offers

| Harness | Channel | Fit |
| --- | --- | --- |
| OpenCode | `opencode serve`: a headless HTTP server with an OpenAPI spec (served at `/doc`) and a generated SDK; the TUI can attach to it. A prompt is posted to a session (`prompt_async` and its synchronous sibling; the paths differ between API generations), and `/tui` endpoints prefill or submit text in the attached TUI. | An acknowledged API: the send returns a status, no screen reading. Build against the `/doc` of the installed version. |
| Codex | `codex app-server`: JSON-RPC over stdio, a WebSocket or a Unix socket. `initialize`, then `thread/start` and `turn/start` with the input; output streams back as notifications. `codex app-server generate-json-schema` prints the schema of the installed version. | Fully structured, but an app-server agent has no TUI to attach to, which loses the live view. Suits headless workers. |
| Claude Code | No API to post into a running interactive session. Hooks come close: a Stop hook can refuse the end of a turn with `decision: block` and a `reason` that Claude then acts on (the hook must check `stop_hook_active` to avoid a loop); UserPromptSubmit can add `additionalContext` to a prompt. | Delivery at the end of a turn without typing. Limits: the block reason shows in the transcript as a blocked stop, and a hook fires only when a turn ends or a prompt is submitted, so an idle agent still needs one typed line. |
| Any CLI | The theseus CLI and MCP server: the agent reads its own inbox. | Universal, but only as reliable as the agent's habit of looking. |

## Design

1. **One inbox in the store.** A `messages` table (migration v8 or later, additive): id, project, sender (coordinator, an agent id, the queue), recipient (an agent id, the coordinator, later another project's coordinator), task (optional), kind (task, note, nudge, question, answer, push), text, created, delivered (time and channel), read (time). The task's notes and questions become messages of that table, or stay where they are and the inbox reads them; decide when building. Delivery and reading are state changes, so a message is never lost and never sent twice; the attention list gains an item for messages undelivered past a limit.
2. **Pull everywhere.** `theseus inbox [--json]` and an MCP tool `inbox` list the caller's unread messages and mark them read; workers use their credentials, the coordinator its role. The skills say to read the inbox at the start of each turn, the same habit as `theseus attention`, so a missed push loses nothing.
3. **A delivery adapter per profile,** chosen by an `interactive.delivery` key the way `interactive.transcript` chooses a transcript adapter, each answering "deliver this message now" with delivered, deferred or failed:
   - `opencode-server`: the supervisor starts the agent's own `opencode serve` (with the agent's environment, which also removes the `--standalone` workaround) and attaches the TUI to it; delivery posts to the session over HTTP and records the response.
   - `claude-code-hooks`: theseus installs a Stop hook (through the per-agent `--settings` it already writes) that asks `theseus inbox --deliver` for queued messages and blocks the stop with them as the reason; an agent already idle gets one typed pointer line.
   - `codex-app-server`: for headless Codex workers, `turn/start` on the agent's thread.
   - `tmux` (the default and the fallback): today's checked send, with its protections (nothing typed over a prompt or a draft; a text that left the input line counts as sent).
4. **Pointers, not payloads, on the typed path.** Where typing remains, it carries only "N messages for you; run `theseus inbox`", so a lost or garbled line costs nothing.
5. **Coordinator to coordinator** is a later step: a project's coordinator posts to another project's inbox through the host store; the receiving coordinator reads it with `theseus inbox`. No harness-specific session messaging is involved.

## Plan

1. The inbox table, `theseus inbox`, the MCP tool and the attention item; the skills' read-the-inbox habit. Task messages and nudges go through the inbox with the tmux adapter, so behavior is unchanged except for the record.
2. The OpenCode adapter, first because opends2 runs OpenCode workers and its coordinator: verify the installed version's `/doc`, per-agent servers, the attached TUI, delivery and its failure modes, with recorded fixtures as for the OpenCode preset.
3. The Claude Code Stop-hook adapter: check the current hook output schema and how a blocked stop reads in the transcript, then install it through the per-agent settings.
4. Codex app-server, when a project runs Codex workers.
5. Coordinator-to-coordinator messages.

## Risks and open questions

- OpenCode's server API changed between generations; pin to the installed version's spec and test against recorded responses.
- A per-agent `opencode serve` is one more process per agent to supervise and secure (bind to a Unix socket or localhost with `OPENCODE_SERVER_PASSWORD`).
- The Claude Code Stop hook's output schema has changed across releases, and whether a Stop hook may carry context without showing a blocked-stop banner is an open feature request; re-check the documentation before building.
- Codex app-server agents have no attachable TUI; the console mirror would show the event stream instead.
- Whether task notes and questions move into the messages table or the inbox reads them in place.

## Sources

- [OpenCode server](https://opencode.ai/docs/server/), [OpenCode server (v1)](https://opencode.ai/v1/docs/server.md), [OpenCode SDK](https://opencode.ai/v1/docs/sdk.md), [OpenCode v2 client](https://v2.opencode.ai/docs/build/client)
- [Codex App Server](https://developers.openai.com/codex/app-server), [Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness)
- [Claude Code hooks](https://docs.anthropic.com/en/docs/claude-code/hooks), [feature request: Stop hook context](https://claudeissues.com/issue/60993-feature-revive-24244-stop-hook-needs-additionalcontext-or-continuewith-for-clean)
