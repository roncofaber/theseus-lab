"""Transcript adapters: what theseus reads from the record an agent's CLI keeps of its own session, chosen by the
profile's `interactive.transcript` key, so every harness-specific format stays in one small module.

An adapter is a module of this package, registered in ADAPTERS under the name a profile sets, with three functions:

- session_state(conf, agent, interactive): the state of the session running in the agent's pane, or None. agent is
  a registry record (name, pane, provider, managed, session_id, server); interactive is the profile's interactive
  table.
- session_file(conf, interactive, session, cwd): the record of a known session id started in cwd (the coordinator),
  or None. A path that exists; when the record holds several sessions (OpenCode's database) the path also carries
  the session id.
- read(path): the state of that record, or None. Reads are incremental and safe from several threads.

One function is optional:
- live_session(interactive, pids): the session id that one of the processes pids (an agent's pane process and its
  descendants) holds open, or None; for a CLI that takes no preassigned id and names no session in a status command
  (Antigravity), so theseus learns the id after launch and after a /clear.

A state is a dict with:
- tokens: the context in use after the last answer, or None;
- model: the model id of that answer, or None;
- errors: the newest API errors, as {"time", "error", "text"}, where error is the CLI's own error kind (a hint for
  theseus.gateway.classify) or None;
- last: "answer", "error" or None, for the newest assistant entry;
- entries: the newest conversation entries, as {"kind", "text", "time", "n"}. kind is "user", "assistant", "tool"
  (with "name") or "result" (with "error"); n numbers the entries of the record from 1, so a reader asks only for
  what is new. Optional fields: "format": "markdown" for text written in Markdown, and "summary", one line standing
  for the entry while it is folded (a tool call's key argument, such as the path it reads or the command it runs;
  a result's first line), "text" then being what unfolds (the call's input, the whole result). Thinking and system
  or meta entries are never included. The list is replaced, never changed in place;
- total: how many entries were ever read;
- key: names the record (a new key means a new session or a rewritten file, so a reader starts over);
- updated: the epoch of the record's newest write, or None; the agent counts as active while it writes, even when
  it changes no file;
- limit (optional): the context window the CLI itself uses for that model (OpenCode's configured limit.context), or
  None; it takes precedence over [models] context and the profile's context_limit.

Two names need no module: "pane-history" (no record is read; the console's Conversation tab shows the pane's tmux
scrollback, for CLIs that print inline) and "none" (no Conversation tab; context and gateway errors come from the
pane only)."""
import importlib

ADAPTERS = {"claude-code-jsonl": "claude_code", "opencode-sqlite": "opencode", "antigravity-jsonl": "antigravity"}
PANE_HISTORY = "pane-history"
NONE = "none"
NAMES = (*ADAPTERS, PANE_HISTORY, NONE)


def name_of(interactive):
    return str((interactive or {}).get("transcript") or NONE)


def load(name):
    """The adapter module registered under name, or None for "pane-history", "none" or an unknown name."""
    module = ADAPTERS.get(name)
    return importlib.import_module(f"{__name__}.{module}") if module else None


def resolve(conf, profile):
    """(name, adapter module or None, interactive table) for a profile name (None: the default profile)."""
    from .. import profiles
    try:
        interactive = profiles.interactive(conf, profile)
    except profiles.ProfileError:
        return NONE, None, {}
    name = name_of(interactive)
    return name, load(name), interactive or {}


def live_session(conf, profile, pane_pid):
    """The session id the agent process in a pane holds open, from its profile's adapter (see live_session above), or
    None when the adapter cannot tell."""
    from .. import top
    _, adapter, interactive = resolve(conf, profile)
    finder = getattr(adapter, "live_session", None)
    if finder is None or not str(pane_pid or "").isdigit():
        return None
    pid = int(pane_pid)
    try:
        return finder(interactive, set(top.descendants(top.process_table(), pid)) | {pid})
    except OSError:
        return None
