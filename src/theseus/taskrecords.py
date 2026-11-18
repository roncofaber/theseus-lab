"""The legacy task record format: conversion between task rows and the records of tasks.json, tasks-archive.jsonl and
tasks-history.jsonl, which are the export, the import format and the rollback path.

A record is rebuilt from columns in a canonical shape. Whatever an imported record holds that the columns cannot
reproduce exactly is kept beside them: values in `extra` (unknown keys, and known keys whose value does not fit their
column, like a priority of "P2"), and in `legacy` the keys the record lacked and its key order. A write through the
service normalizes only the keys it changes (`forget`), so an untouched record exports byte for byte as imported."""
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg

HISTORY = "docs/project/tasks-history.jsonl"
SCHEMA = 2
TASK_KEYS = ("id", "role", "status", "owner", "createdAt", "dependencies", "ownedPaths", "objective", "generalContract",
             "acceptance", "sharedFilesReadOnly", "result", "milestone", "priority", "room", "profile", "minCapability",
             "assignmentNote", "hold", "externalBlocker", "researchRequired", "pause", "worktree", "baseCommit",
             "execution", "ownerId", "previousAttempts", "merge", "evidence", "touches")
BLOCKERS = ("externalBlocker", "researchRequired", "pause")
ATTEMPT_KEYS = ("owner", "worktree", "baseCommit", "execution", "ownerId")
CURRENT = ("claimed", "active", "delivered", "merged")
PHASE_OF_STATUS = {"in_progress": "active", "verification": "delivered", "merged": "merged", "completed": "merged"}
CLOSED_AT = {"releasedAt": "released", "reopenedAt": "reopened", "unmergedAt": "unmerged", "supersededAt": "superseded"}


def same(a, b):
    """Equal as JSON text, so 1, 1.0 and True differ and so does key order inside objects."""
    return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def text(value):
    return value if isinstance(value, str) else None


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def texts(value):
    """The distinct strings of a list in order; what does not fit shows up as a difference in shape()."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def current_attempt(attempts):
    found = [attempt for attempt in attempts if attempt["phase"] in CURRENT]
    return found[-1] if found else None


def build(task, attempts):
    """The canonical record of a task row and its attempts, before the legacy overlay."""
    current = current_attempt(attempts)
    record = {"id": task["id"], "role": task["role"], "status": task["status"],
              "owner": current["owner"] if current else None}
    if task["created"] is not None:
        record["createdAt"] = task["created"]
    record.update(dependencies=list(task["deps"]), ownedPaths=list(task["owned"]), objective=task["objective"])
    if task["contract"] is not None:
        record["generalContract"] = task["contract"]
    record.update(acceptance=list(task["acceptance"] or []), sharedFilesReadOnly=True, result=task["result"])
    for key, column in (("milestone", "milestone"), ("priority", "priority"), ("room", "room"), ("profile", "profile"),
                        ("minCapability", "min_capability"), ("assignmentNote", "note")):
        if task[column] is not None:
            record[key] = task[column]
    if task["hold"]:
        record["hold"] = True
    for key in BLOCKERS:
        if (task["blockers"] or {}).get(key) is not None:
            record[key] = task["blockers"][key]
    if current:
        for key, column in (("worktree", "worktree"), ("baseCommit", "base"), ("execution", "execution"),
                            ("ownerId", "agent_id")):
            if current[column] is not None:
                record[key] = current[column]
    previous = [attempt["record"] for attempt in attempts if attempt["phase"] not in CURRENT]
    if previous:
        record["previousAttempts"] = previous
    for key in ("merge", "evidence"):
        if task[key] is not None:
            record[key] = task[key]
    if task["touches"]:
        record["touches"] = list(task["touches"])
    return record


def overlay(built, extra, legacy):
    record = {**built, **(extra or {})}
    for key in (legacy or {}).get("absent", ()):
        record.pop(key, None)
    order = (legacy or {}).get("order")
    if order:
        first = {key: record[key] for key in order if key in record}
        record = {**first, **{key: value for key, value in record.items() if key not in first}}
    return record


def render(task, attempts):
    return overlay(build(task, attempts), task["extra"], task["legacy"])


def shape(built, record):
    """(extra, legacy) that make overlay(built, extra, legacy) equal record, key order included."""
    extra = {key: value for key, value in record.items() if key not in built or not same(built[key], value)}
    legacy = {}
    absent = [key for key in built if key not in record]
    if absent:
        legacy["absent"] = absent
    if list(overlay(built, extra, legacy)) != list(record):
        legacy["order"] = list(record)
    return extra or None, legacy or None


def forget(task, keys):
    """extra and legacy with the given record keys normalized, for a write that sets them from columns."""
    extra = {key: value for key, value in (task["extra"] or {}).items() if key not in keys}
    legacy = dict(task["legacy"] or {})
    if legacy.get("absent"):
        legacy["absent"] = [key for key in legacy["absent"] if key not in keys]
        if not legacy["absent"]:
            del legacy["absent"]
    return {"extra": extra or None, "legacy": legacy or None}


def columns(record):
    """(task columns, dependencies, owned paths, touches) of a legacy record; values that do not fit stay out."""
    get = record.get
    blockers = {key: record[key] for key in BLOCKERS if get(key) is not None}
    fields = {"id": record["id"], "status": record["status"], "role": text(get("role")), "objective": text(get("objective")),
              "contract": get("generalContract"), "acceptance": get("acceptance") if isinstance(get("acceptance"), list) else None,
              "milestone": text(get("milestone")), "priority": integer(get("priority")) or None,
              "hold": 1 if get("hold") is True else 0, "room": text(get("room")), "profile": text(get("profile")),
              "min_capability": integer(get("minCapability")) or None, "note": text(get("assignmentNote")),
              "blockers": blockers or None, "merge": get("merge"), "result": get("result"), "evidence": get("evidence"),
              "created": text(get("createdAt"))}
    return fields, texts(get("dependencies")), texts(get("ownedPaths")), texts(get("touches"))


def attempt_columns(entry, phase):
    execution = entry.get("execution") if isinstance(entry.get("execution"), dict) else None
    return {"phase": phase, "owner": text(entry.get("owner")), "worktree": text(entry.get("worktree")),
            "base": text(entry.get("baseCommit")), "execution": execution,
            "log_dir": text((execution or {}).get("logDir")), "handoff": text((execution or {}).get("handoff")),
            "started": text((execution or {}).get("assignedAt"))}


def current_columns(record):
    """The current attempt of an imported record, or None when it has none (pending, or nothing recorded)."""
    if record["status"] == "pending" or not any(record.get(key) is not None for key in ATTEMPT_KEYS):
        return None
    fields = attempt_columns(record, PHASE_OF_STATUS[record["status"]])
    fields["agent_id"] = text(record.get("ownerId"))
    return fields


def previous_columns(entry):
    """An imported previousAttempts entry as a closed attempt; the entry itself is kept verbatim as its record."""
    kind = next((key for key in CLOSED_AT if key in entry), None)
    fields = attempt_columns(entry, CLOSED_AT.get(kind, "released"))
    fields["handoff"] = text(entry.get("handoff")) or fields["handoff"]
    fields.update(agent_id=text(entry.get("agentId")), reason=text(entry.get("reason")), record=entry,
                  ended=text(entry.get(kind)) if kind else None)
    return fields


def header(project):
    """The canonical tasks.json header of a project row, tasks left empty."""
    doc = {"schema": SCHEMA, "project": {"id": project["id"], "name": project["name"]},
           "currentMilestone": project["milestone"], "objectiveComplete": bool(project["objective_complete"])}
    if project["policy"] is not None:
        doc["policy"] = project["policy"]
    doc["tasks"] = []
    return doc


def header_columns(doc):
    complete = doc.get("objectiveComplete")
    return {"milestone": text(doc.get("currentMilestone")),
            "objective_complete": int(complete) if isinstance(complete, bool) else 0, "policy": doc.get("policy")}


def normalized_header(doc, project):
    """The header an export of doc must have: schema 2 and the project's id and name after it; the rest unchanged."""
    ident = {"id": project["id"], "name": project["name"]}
    out = {} if "schema" in doc else {"schema": SCHEMA, "project": ident}
    for key, value in doc.items():
        if key == "project":
            continue
        out[key] = SCHEMA if key == "schema" else value
        if key == "schema":
            out["project"] = ident
    return out


def release_record(attempt, reason, at):
    """The previousAttempts entry tasks.release wrote: the attempt's owner, worktree, base and execution, its agent."""
    entry = {"owner": attempt["owner"], "worktree": attempt["worktree"], "baseCommit": attempt["base"],
             "execution": attempt["execution"]}
    if attempt["agent_id"]:
        entry["agentId"] = attempt["agent_id"]
    entry.update(releasedAt=at, reason=reason)
    return entry


def superseded_record(attempt, pause, at):
    entry = {} if attempt is None else {"owner": attempt["owner"], "worktree": attempt["worktree"],
                                        "baseCommit": attempt["base"], "execution": attempt["execution"]}
    entry["pause"] = pause
    entry = {key: value for key, value in entry.items() if value is not None}
    if attempt is not None and attempt["agent_id"]:
        entry["agentId"] = attempt["agent_id"]
    entry.update(supersededAt=at, reason="reassigned")
    return entry


@dataclass(frozen=True)
class Snapshot:
    """The three export files as text."""
    tasks: str
    archive: str = ""
    history: str = ""

    def documents(self):
        """(tasks.json document, archived records, history entries)."""
        lines = lambda body: [json.loads(line) for line in body.splitlines() if line.strip()]
        return json.loads(self.tasks), lines(self.archive), lines(self.history)


def dump_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def snapshot(doc, archived, history):
    return Snapshot(tasks=json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    archive="".join(map(dump_line, archived)), history="".join(map(dump_line, history)))


def read(root):
    """The export files under a project root; a missing archive or history reads as empty."""
    root = Path(root)
    body = lambda name: (root / name).read_text() if (root / name).exists() else ""
    return Snapshot(tasks=(root / cfg.TASKS).read_text(), archive=body(cfg.ARCHIVE), history=body(HISTORY))


def write(snap, root):
    root = Path(root)
    for name, body in ((cfg.TASKS, snap.tasks), (cfg.ARCHIVE, snap.archive), (HISTORY, snap.history)):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if body or target.exists():
            cfg.write_atomic(target, body)


def write_flat(snap, directory):
    """The three export files side by side in directory, for a copy kept outside the checkout."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in (("tasks.json", snap.tasks), ("tasks-archive.jsonl", snap.archive),
                       ("tasks-history.jsonl", snap.history)):
        cfg.write_atomic(directory / name, body)


def history_line(row):
    if row["type"] == "event":
        line = {"time": row["time"], "task": row["task"], "event": row["kind"], "actor": row["actor"]}
        if row["detail"]:
            line["detail"] = row["detail"]
        return line
    line = {"time": row["time"], "task": row["task"], "note": row["text"], "kind": row["kind"], "author": row["author"]}
    if row["answered"]:
        line["answered"] = row["answered"]
    return line


def diff(expected, actual, label):
    """A short description of the first lines where two texts differ, or None when they are equal."""
    if expected == actual:
        return None
    lines = difflib.unified_diff(expected.splitlines(), actual.splitlines(), f"{label} (input)", f"{label} (export)",
                                 n=0, lineterm="")
    return "\n".join(line[:300] for line, _ in zip(lines, range(12)))
