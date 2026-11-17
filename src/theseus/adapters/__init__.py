"""Transcript adapters: what theseus reads from the record an agent's CLI keeps of its own session, chosen by the
profile's `interactive.transcript` key, so every harness-specific format stays in one small module.

An adapter is a module of this package, registered in ADAPTERS under the name a profile sets, with three functions:

- session_state(conf, agent, interactive): the state of the session running in the agent's pane, or None. agent is
  a registry record (name, pane, provider, managed, session_id, server); interactive is the profile's interactive
  table.
- session_file(conf, interactive, session, cwd): the record of a known session id started in cwd (the coordinator),
  or None.
- read(path): the state of that record, or None. Reads are incremental and safe from several threads.

A state is a dict with:
- tokens: the context in use after the last answer, or None;
- model: the model id of that answer, or None;
- errors: the newest API errors, as {"time", "error", "text"}, where error is the CLI's own error kind (a hint for
  theseus.gateway.classify) or None;
- last: "answer", "error" or None, for the newest assistant entry;
- entries: the newest conversation entries, as {"kind", "text", "time"}. kind is "user", "assistant", "tool" (with
  "name") or "result" (with "error"); thinking is never included;
- total: how many entries were ever read, so a reader can ask only for what is new.

Two names need no module: "pane-history" (no record is read; the console's Conversation tab shows the pane's tmux
scrollback, for CLIs that print inline) and "none" (no Conversation tab; context and gateway errors come from the
pane only)."""
import importlib

ADAPTERS = {"claude-code-jsonl": "claude_code"}
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
