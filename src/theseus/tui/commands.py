"""The console's actions: Textual command palette providers (Ctrl+P) whose commands run the theseus CLI itself, so
each action is the same code path as on the command line; destructive ones ask first."""
from functools import partial

from textual.command import DiscoveryHit, Hit, Provider

from .. import tasks as t


def project_commands(item):
    """(title, help, argv, confirm question or None) for the project of a console tab, from its snapshot alone."""
    state = item["snapshot"]
    commands = [("Queue tick", "run one queue tick now", ["queue", "tick"], None),
                ("Agents up", "reconcile the supervised agents toward [agents.counts]", ["agents", "up"], None)]
    for profile, count in sorted((state.get("agentCounts") or {}).items()):
        commands.append((f"Add a {profile} agent ({count} to {count + 1})", "theseus agents scale, in theseus.local.toml",
                         ["agents", "scale", profile, str(count + 1)], f"Add a {profile} agent ({count} to {count + 1})?"))
        if count:
            commands.append((f"Remove a {profile} agent ({count} to {count - 1})",
                             "theseus agents scale; a busy agent stops once free", ["agents", "scale", profile, str(count - 1)],
                             f"Remove a {profile} agent ({count} to {count - 1})?"))
    if state.get("managed"):
        commands.append(("Stop all agents", "theseus agents down --all: supervision pauses until Agents up",
                         ["agents", "down", "--all"], "Stop every agent and pause supervision?"))
    for agent in state["agents"]:
        shown = agent.get("display") or agent["name"]
        if agent.get("managed"):
            commands += [(f"Restart {shown}", "theseus agents restart (resumes its conversation)",
                          ["agents", "restart", agent["name"]], f"Restart {shown}?"),
                         (f"Stop {shown}", "theseus agents down", ["agents", "down", agent["name"]], f"Stop {shown}?"),
                         (f"Start {shown}", "theseus agents up NAME", ["agents", "up", agent["name"]], None)]
    queue = state["queue"]
    for task in queue.get("ready", []):
        commands += [(f"Dispatch {task}", "theseus dispatch: a one-shot worker", ["dispatch", task], f"Dispatch {task}?"),
                     (f"Hold {task}", "theseus tasks hold", ["tasks", "hold", task], None)]
    for task in queue.get("held", []):
        commands.append((f"Release the hold on {task}", "theseus tasks release-hold", ["tasks", "release-hold", task], None))
    for task in queue.get("in_progress", []):
        commands.append((f"Release {task}", "theseus tasks release: back to pending",
                         ["tasks", "release", task, "--reason", "released from the console"], f"Release {task} back to pending?"))
    priorities = state.get("priorities") or {}
    for task in [*queue.get("ready", []), *queue.get("blocked", []), *queue.get("held", [])]:
        level = int(priorities.get(task, 3))
        if level < 5:
            commands.append((f"Raise the priority of {task} ({level} to {level + 1})", "theseus tasks priority",
                             ["tasks", "priority", task, str(level + 1)], None))
        if level > 1:
            commands.append((f"Lower the priority of {task} ({level} to {level - 1})", "theseus tasks priority",
                             ["tasks", "priority", task, str(level - 1)], None))
    capabilities = state.get("capabilities") or {}
    for task in [*queue.get("ready", []), *queue.get("blocked", []), *queue.get("held", [])]:
        level = int(capabilities.get(task) or t.LOWEST_CAPABILITY)
        commands.append((f"Raise the minimum capability of {task} ({level} to {level + 1})", "theseus tasks capability",
                         ["tasks", "capability", task, str(level + 1)], None))
        if level > t.LOWEST_CAPABILITY:
            commands.append((f"Lower the minimum capability of {task} ({level} to {level - 1})", "theseus tasks capability",
                             ["tasks", "capability", task, str(level - 1)], None))
    for task in queue.get("review", []):
        commands.append((f"Reopen {task}", "theseus tasks reopen: verification back to in_progress",
                         ["tasks", "reopen", task, "--reason", "reopened from the console"], f"Reopen {task}?"))
    for pool in state.get("pools") or []:
        commands.append((f"Hold a {pool['pool']} slot for the coordinator", "theseus pool hold",
                         ["pool", "hold", pool["pool"], "coordinator", "--project", state["project"], "--task", "console"], None))
        for lease in (pool.get("leases") or {}).values():
            if lease.get("project") == state["project"]:
                commands.append((f"Release the {pool['pool']} slot of {lease['agent']}", "theseus pool release",
                                 ["pool", "release", pool["pool"], lease["agent"], "--project", state["project"]],
                                 f"Release the {pool['pool']} slot of {lease['agent']}?"))
    return commands


class ConsoleCommands(Provider):
    """The actions of the active project tab, plus the coordinator popup and handoffs of delivered tasks."""

    def entries(self):
        app = self.app
        item = app.active_item()
        found = []
        if item and item.get("available"):
            name = item["name"]
            for title, help_text, argv, question in project_commands(item):
                found.append((f"{name}: {title}", help_text, partial(app.run_cli, item["root"], argv, question)))
            found.append((f"{name}: Coordinator", "the coordinator's mirror and send line", app.action_coordinator))
            if (item["snapshot"].get("coordinator") or {}).get("supervised"):
                found.append((f"{name}: Attach the coordinator", "the coordinator session here; prefix d detaches",
                              app.action_attach_coordinator))
            for task in item["snapshot"]["queue"].get("review", []):
                found.append((f"{name}: Open the handoff of {task}", "the delivered task's handoff.md",
                              partial(app.open_handoff, item, task)))
            for agent in item["snapshot"]["agents"]:
                if agent.get("managed"):
                    shown = agent.get("display") or agent["name"]
                    found.append((f"{name}: Attach {shown}", "the agent's own pane here; C-q detaches",
                                  partial(app.attach_agent, item["root"], agent["name"])))
        return found

    async def discover(self):
        for title, help_text, callback in self.entries():
            yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query):
        matcher = self.matcher(query)
        for title, help_text, callback in self.entries():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), callback, help=help_text)


class AgentCommands(Provider):
    """The agent popup's commands without a button (send Enter or Escape, compact, open in your tmux, restart, stop,
    start), from the popup the palette was opened on."""

    def entries(self):
        palette = getattr(self.screen, "palette", None)
        return palette() if palette else []

    async def discover(self):
        for title, help_text, callback in self.entries():
            yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query):
        matcher = self.matcher(query)
        for title, help_text, callback in self.entries():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), callback, help=help_text)
