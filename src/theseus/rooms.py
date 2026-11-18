"""Clean-room separation for ports that will be published: dirty-room workers read the original's code and write a
specification in their own words; clean-room workers implement from that specification, recorded captures and
public documentation only, and never see the original's code.

[rooms] in theseus.toml names the persistent instances of each room, the repository paths only the dirty room may
read (dirty_paths, globs), the data-root folders only the dirty room may read (dirty_data, such as the decompiled
corpus and extracted files) and the handover paths both rooms read (spec_paths). A task carries "room": "clean" or
"dirty". An instance that works a dirty task is recorded as tainted under <data>/rooms.json and is refused clean
work until its context is cleared: `theseus instance compact PANE --clear` (a full /clear, not a summarizing
/compact) lifts the taint when rooms.taint is "until-clear"; with "permanent" it never lifts. The instance lists are
optional: when both are empty any instance may take either room, subject to the taint. One-shot dispatched workers
start with an empty context and may take either room.
"""
import json

from . import config as cfg
from . import tasks as t

ROOMS = ("clean", "dirty")


class RoomError(Exception):
    pass


def section(conf):
    return conf.section("rooms") or {}


def enabled(conf):
    return bool(section(conf).get("enabled"))


def ledger_path(conf):
    return conf.data / "rooms.json"


def ledger(conf):
    path = ledger_path(conf)
    return json.loads(path.read_text()) if path.exists() else {"tainted": {}}


def configured_room(conf, instance):
    rooms = section(conf)
    if instance in (rooms.get("dirty_instances") or []):
        return "dirty"
    if instance in (rooms.get("clean_instances") or []):
        return "clean"
    return None


def check_assignment(conf, task, instance):
    """Refuse an instance assignment that would break the separation."""
    if not enabled(conf):
        return
    room = task.get("room")
    if room not in ROOMS:
        raise RoomError(f"{task['id']} has no room; with [rooms] enabled every task is clean or dirty (theseus tasks add --room)")
    tainted = ledger(conf)["tainted"].get(instance)
    configured = configured_room(conf, instance)
    if room == "clean" and tainted:
        if taint_mode(conf) == "permanent":
            raise RoomError(f"{instance} worked dirty task {tainted} and may never take clean work")
        raise RoomError(f"{instance} worked dirty task {tainted}; clear its context first (theseus instance compact PANE --clear)")
    lists = section(conf).get("clean_instances") or section(conf).get("dirty_instances")
    if not lists:
        return
    if configured is None:
        raise RoomError(f"{instance} is in neither rooms.clean_instances nor rooms.dirty_instances")
    if configured != room:
        raise RoomError(f"{task['id']} is a {room}-room task but {instance} is a {configured}-room instance")


def taint_mode(conf):
    return section(conf).get("taint") or "until-clear"


def clear_taint(conf, instance):
    """After a full context clear: lift the taint unless rooms.taint is permanent. Returns a message."""
    if not enabled(conf):
        return ""
    data = ledger(conf)
    task = data["tainted"].get(instance)
    if not task:
        return f"{instance}: not tainted"
    if taint_mode(conf) == "permanent":
        return f"{instance}: still tainted by {task} (rooms.taint is permanent)"
    data["tainted"].pop(instance)
    data.setdefault("cleared", []).append({"instance": instance, "task": task})
    cfg.write_atomic(ledger_path(conf), json.dumps(data, indent=2) + "\n")
    return f"{instance}: context cleared, taint from {task} lifted"


def record_assignment(conf, task, instance):
    if enabled(conf) and task.get("room") == "dirty":
        data = ledger(conf)
        data["tainted"].setdefault(instance, task["id"])
        ledger_path(conf).parent.mkdir(parents=True, exist_ok=True)
        cfg.write_atomic(ledger_path(conf), json.dumps(data, indent=2) + "\n")


def sparse_patterns(conf):
    """Sparse-checkout patterns that keep every dirty path out of a clean-room worktree."""
    patterns = ["/*"]
    for path in section(conf).get("dirty_paths") or []:
        stem = path.rstrip("*")
        patterns.append("!/" + stem + ("" if stem.endswith("/") or "." in stem.rsplit("/", 1)[-1] else "*"))
    return patterns


def exclude_dirty(conf, task, worktree):
    """Remove dirty paths from a clean-room worktree with a non-cone sparse checkout; returns the patterns applied."""
    from . import gitutil as g
    if not enabled(conf) or task.get("room") != "clean" or not section(conf).get("dirty_paths"):
        return []
    patterns = sparse_patterns(conf)
    g.git(worktree, "sparse-checkout", "set", "--no-cone", *patterns)
    return patterns


def message(conf, task):
    """The room rules appended to a task message."""
    if not enabled(conf) or task.get("room") not in ROOMS:
        return ""
    rooms = section(conf)
    dirty = ", ".join(rooms.get("dirty_paths") or []) or "none listed"
    data = ", ".join(f"{conf.data}/{d}" for d in rooms.get("dirty_data") or []) or "none listed"
    spec = ", ".join(rooms.get("spec_paths") or []) or "the specification"
    if task["room"] == "clean":
        return (f" CLEAN ROOM: never open, search or summarize {dirty}, the data-root folders {data}, or the original's "
                f"binaries; never use REA, a decompiler or `theseus re` on them. Work only from {spec}, recorded captures and "
                "public documentation. If the specification is missing something, stop and report the gap instead of looking for it.")
    return (f" DIRTY ROOM: record what the original does in your own words; never copy decompiled code, disassembly or vendor "
            f"text into any file, and never write implementation code for the new project. Keep the two outputs apart: {spec} "
            "describes only externally observable behavior an implementer needs (wire formats, requests, payload layouts, "
            "constants on the wire, ordering, timing, error behavior), labelled Recovered or Reconstruction and pointing to report "
            "sections for evidence; internal addresses, offsets, function names and call structure stay in your report.")


def rule_problems(conf):
    """Supervisor rule: with rooms enabled every open task has a room, and clean tasks own nothing dirty."""
    if not enabled(conf):
        return []
    dirty = section(conf).get("dirty_paths") or []
    problems = []
    for task in t.Queue(conf).load()["tasks"]:
        room = task.get("room")
        if room not in ROOMS:
            problems.append(f"{task['id']}: no room (clean or dirty) while [rooms] is enabled")
        elif room == "clean" and t.ownership_overlaps(task.get("ownedPaths", []), dirty):
            problems.append(f"{task['id']}: clean-room task owns dirty paths")
    return problems
