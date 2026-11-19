"""The console's actions: Textual command palette providers (Ctrl+P) whose commands run the theseus CLI itself, so
each action is the same code path as on the command line; destructive ones ask first.

Every action has a group and, for one agent, task or profile, a subject. Opened empty, the palette lists the actions
of the row highlighted when it opened, the tab's own few actions, and one entry per group ("Task actions…") that opens
the group in a second palette; typing searches every action."""
from dataclasses import dataclass
from functools import partial

from textual.command import DiscoveryHit, Hit, Provider

from .. import tasks as t

GROUPS = {"agents": "Agent actions", "scale": "Scale agents", "tasks": "Task actions", "handoffs": "Handoffs",
          "pools": "Pool slots", "profiles": "Profile actions", "new-profile": "New profile"}


@dataclass
class Action:
    title: str
    help: str
    group: str
    subject: str = None
    argv: list = None
    question: str = None
    callback: object = None


def project_actions(item):
    """The CLI actions of a project tab, from its snapshot alone: Action rows with argv (run by app.run_cli). A closed
    project offers only Reopen, an active one also Close."""
    state = item["snapshot"]
    if item.get("closed"):
        return [Action("Reopen project", "theseus reopen: bring back its agents, queue driver and coordinator", "project",
                       argv=["reopen"], question=f"Reopen {item['name']}?")]
    found = [Action("Queue tick", "run one queue tick now", "project", argv=["queue", "tick"]),
             Action("Agents up", "reconcile the supervised agents toward [agents.counts]", "project", argv=["agents", "up"]),
             Action("Close project", "theseus close --commit: export and commit the tasks, stop its agents, queue driver "
                                     "and coordinator until Reopen (refused while an agent holds a task)", "project",
                    argv=["close", "--commit"], question=f"Close {item['name']}? Everything it runs stops until Reopen.")]
    if state.get("managed"):
        found.append(Action("Stop all agents", "theseus agents down --all: supervision pauses until Agents up", "project",
                            argv=["agents", "down", "--all"], question="Stop every agent and pause supervision?"))
    for profile, count in sorted((state.get("agentCounts") or {}).items()):
        found.append(Action(f"Add a {profile} agent ({count} to {count + 1})", "theseus agents scale, in theseus.local.toml",
                            "scale", argv=["agents", "scale", profile, str(count + 1)],
                            question=f"Add a {profile} agent ({count} to {count + 1})?"))
        if count:
            found.append(Action(f"Remove a {profile} agent ({count} to {count - 1})",
                                "theseus agents scale; a busy agent stops once free", "scale",
                                argv=["agents", "scale", profile, str(count - 1)],
                                question=f"Remove a {profile} agent ({count} to {count - 1})?"))
    for agent in state["agents"]:
        shown = agent.get("display") or agent["name"]
        if agent.get("managed"):
            found += [Action(f"Restart {shown}", "theseus agents restart (resumes its conversation)", "agents",
                             agent["name"], ["agents", "restart", agent["name"]], f"Restart {shown}?"),
                      Action(f"Stop {shown}", "theseus agents down", "agents", agent["name"], ["agents", "down", agent["name"]],
                             f"Stop {shown}?"),
                      Action(f"Start {shown}", "theseus agents up NAME", "agents", agent["name"], ["agents", "up", agent["name"]])]
    queue = state["queue"]
    for task in queue.get("ready", []):
        found += [Action(f"Dispatch {task}", "theseus dispatch: a one-shot worker", "tasks", task, ["dispatch", task],
                         f"Dispatch {task}?"),
                  Action(f"Hold {task}", "theseus tasks hold", "tasks", task, ["tasks", "hold", task])]
    for task in queue.get("held", []):
        found.append(Action(f"Release the hold on {task}", "theseus tasks release-hold", "tasks", task,
                            ["tasks", "release-hold", task]))
    for task in queue.get("in_progress", []):
        found.append(Action(f"Release {task}", "theseus tasks release: back to pending", "tasks", task,
                            ["tasks", "release", task, "--reason", "released from the console"],
                            f"Release {task} back to pending?"))
    priorities = state.get("priorities") or {}
    capabilities = state.get("capabilities") or {}
    for task in [*queue.get("ready", []), *queue.get("blocked", []), *queue.get("held", [])]:
        level = int(priorities.get(task, 3))
        if level < 5:
            found.append(Action(f"Raise the priority of {task} ({level} to {level + 1})", "theseus tasks priority", "tasks",
                                task, ["tasks", "priority", task, str(level + 1)]))
        if level > 1:
            found.append(Action(f"Lower the priority of {task} ({level} to {level - 1})", "theseus tasks priority", "tasks",
                                task, ["tasks", "priority", task, str(level - 1)]))
        level = int(capabilities.get(task) or t.LOWEST_CAPABILITY)
        found.append(Action(f"Raise the cap of {task} ({level} to {level + 1})", "theseus tasks capability: the lowest "
                            "agent capability that may take it", "tasks", task, ["tasks", "capability", task, str(level + 1)]))
        if level > t.LOWEST_CAPABILITY:
            found.append(Action(f"Lower the cap of {task} ({level} to {level - 1})", "theseus tasks capability", "tasks",
                                task, ["tasks", "capability", task, str(level - 1)]))
    for task in queue.get("review", []):
        found.append(Action(f"Reopen {task}", "theseus tasks reopen: verification back to in_progress", "tasks", task,
                            ["tasks", "reopen", task, "--reason", "reopened from the console"], f"Reopen {task}?"))
    for pool in state.get("pools") or []:
        found.append(Action(f"Hold a {pool['pool']} slot for the coordinator", "theseus pool hold", "pools",
                            argv=["pool", "hold", pool["pool"], "coordinator", "--project", state["project"], "--task",
                                  "console"]))
        for lease in (pool.get("leases") or {}).values():
            if lease.get("project") == state["project"]:
                found.append(Action(f"Release the {pool['pool']} slot of {lease['agent']}", "theseus pool release", "pools",
                                    argv=["pool", "release", pool["pool"], lease["agent"], "--project", state["project"]],
                                    question=f"Release the {pool['pool']} slot of {lease['agent']}?"))
    return found


def unique_name(base, taken):
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def host_actions(app):
    """The Host tab's actions: edit or create agent profiles in $EDITOR (the host config.toml)."""
    from .. import profiles
    found = (app.snap or {}).get("profiles") or []
    taken = {p["name"] for p in found}
    actions = [Action("Edit the host config ($EDITOR)", "~/.config/theseus/config.toml, checked on save", "project",
                      callback=partial(app.edit_profiles, ["profiles", "edit", "--host"]))]
    for profile in found:
        name = profile["name"]
        if profile.get("defined") != "preset":
            actions.append(Action(f"Edit profile {name} ($EDITOR)", "its section of the host config, checked on save",
                                  "profiles", name, callback=partial(app.edit_profiles, ["profiles", "edit", name, "--host"])))
        actions.append(Action(f"Show profile {name}", "every key and where it comes from", "profiles", name,
                              callback=partial(app.show_profile, name)))
    for preset in sorted(profiles.PRESETS):
        name = unique_name(f"my-{preset}", taken)
        actions.append(Action(f"New profile from {preset} ($EDITOR)", f"adds [workers.{name}] to the host config and "
                                                                      f"opens it; rename it there", "new-profile",
                              callback=partial(app.edit_profiles, ["profiles", "add", name, "--preset", preset, "--edit"])))
    return actions


def tab_actions(app):
    """Every action of the active tab, with callbacks: the Host tab's, or a project's CLI actions plus its
    coordinator, attach and handoff actions."""
    item = app.active_item()
    if item is None:
        return host_actions(app) if app.snap is not None else []
    if not item.get("available"):
        return []
    found = project_actions(item)
    for action in found:
        action.callback = partial(app.run_cli, item["root"], action.argv, action.question)
    state = item["snapshot"]
    for task in state["queue"].get("review", []):
        found.append(Action(f"Open the handoff of {task}", "the delivered task's handoff.md", "handoffs", task,
                            callback=partial(app.open_handoff, item, task)))
    if item.get("closed"):
        return found
    found.append(Action("Coordinator", "the coordinator's mirror and send line", "project",
                        callback=app.action_coordinator))
    if (state.get("coordinator") or {}).get("supervised"):
        found.append(Action("Attach the coordinator", "the coordinator session here; prefix d detaches", "project",
                            callback=app.action_attach_coordinator))
    for agent in state["agents"]:
        if agent.get("managed"):
            shown = agent.get("display") or agent["name"]
            found.append(Action(f"Attach {shown}", "the agent's own pane here; C-q detaches", "agents", agent["name"],
                                callback=partial(app.attach_agent, item["root"], agent["name"])))
    return found


def palette_entries(app, actions, subject):
    """(title, help, callback) for the palette opened empty: the highlighted subject's actions, the tab's own, then one
    entry per group that opens the group in a second palette."""
    entries = [(f"{subject}: {a.title}", a.help, a.callback) for a in actions if subject and a.subject == subject]
    entries += [(a.title, a.help, a.callback) for a in actions if a.group == "project"]
    for group, label in GROUPS.items():
        members = [a for a in actions if a.group == group]
        if members:
            entries.append((f"{label}… ({len(members)})", f"open the {label.lower()} of this tab",
                            partial(open_group, app, members, label)))
    return entries


def open_group(app, members, label):
    """Open one group's actions in a second palette."""
    app.call_after_refresh(app.search_commands, [(a.title, a.callback, a.help) for a in members],
                           f"{label}: type to filter")


class ConsoleCommands(Provider):
    """The actions of the active tab (see the module docstring)."""

    async def discover(self):
        app = self.app
        for title, help_text, callback in palette_entries(app, tab_actions(app), app.highlighted_subject()):
            yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query):
        matcher = self.matcher(query)
        for action in tab_actions(self.app):
            score = matcher.match(action.title)
            if score > 0:
                yield Hit(score, matcher.highlight(action.title), action.callback, help=action.help)


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
