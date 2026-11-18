"""Task service: every rule about task state, over a taskrepo.Repo.

Each call is one transaction (BEGIN IMMEDIATE) on its own connection that checks the state, writes the change, the
task_events row and, for a status transition, the queue event, and commits. No call runs a subprocess: assignment is
claim, then the worktree outside any transaction, then activate or abandon (see assign). Calls take a Principal and
refuse what its role may not do. Records go in and out in the legacy tasks.json shape (theseus.taskrecords).

Who may call what: the coordinator everything; the queue claim, activate, abandon, release, show and the detected
delivery; a worker only show, note, deliver and blocked, each naming its task, which must be its agent's active
attempt. Reads (get, list, index, ready, questions) need no particular principal, so a hand-made pane without
credentials can read its task."""
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import store
from . import taskrecords as rec
from . import taskrepo
from . import tasks as t

NOTE_LIMIT = 4096
EDITABLE = {"role", "objective", "contract", "accept", "owned", "add_owned", "touches", "depends", "milestone", "room",
            "profile", "priority", "min_capability", "note", "externalBlocker", "researchRequired"}
WORKER_COMMANDS = "`theseus task show|note|done|blocked TASK`"


class TaskError(t.TaskError):
    """A refused call. code is stable for scripts: forbidden, unauthenticated, not-your-task, not-found, invalid,
    refused, conflict, not-imported, wrong-root, lossy."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Principal:
    role: str
    agent_id: str | None = None
    via: str = "cli"

    @property
    def actor(self):
        who = f"{self.role}:{self.agent_id}" if self.agent_id else self.role
        return f"{who}/{self.via}"


COORDINATOR = Principal("coordinator")
QUEUE = Principal("queue", via="queue")


def now():
    return store.iso()


def allow(p, *roles):
    if p.role in roles:
        return
    if p.role == "worker":
        raise TaskError("forbidden", f"a worker reports through {WORKER_COMMANDS} only")
    raise TaskError("forbidden", f"this call needs the {' or '.join(roles)}, not the {p.role}")


def agent_label(attempt):
    return t.agent_of({"owner": attempt["owner"], "execution": attempt["execution"]}) if attempt else None


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


class TaskService:
    def __init__(self, conf, opener=taskrepo.opened):
        self.conf = conf
        self.name = conf.name
        self.root = Path(conf.root)
        self.opener = opener
        self.checkouts = {}

    # plumbing

    @contextlib.contextmanager
    def unit(self, write=True, imported=True, root=True):
        """(repo, project) inside one transaction (write) or a plain read; Conflict surfaces as TaskError conflict."""
        try:
            with self.opener() as repo:
                project = repo.project(self.name)
                if project is None or project["id"] is None:
                    repo.ensure_project(self.name)
                    project = repo.project(self.name)
                if imported and not project["tasks_imported"]:
                    fresh = self.fresh()
                    if fresh:
                        self.import_(COORDINATOR, fresh)
                with repo.transaction() if write else contextlib.nullcontext():
                    project = repo.project(self.name)
                    if imported and not project["tasks_imported"]:
                        raise TaskError("not-imported", f"{self.name}'s tasks are not in the host store yet; run "
                                                        f"`theseus tasks import`")
                    recorded = project["tasks_root"]
                    if root and recorded and not self.same_checkout(recorded):
                        raise TaskError("wrong-root", f"{self.name}'s tasks belong to {recorded}; if the checkout "
                                                      f"moved, run `theseus tasks adopt` in the new one")
                    yield repo, project
        except taskrepo.Conflict as error:
            raise TaskError("conflict", str(error)) from error

    @contextlib.contextmanager
    def reading(self):
        """(repo, project) for a read: the store opened read-only, so a read never writes or creates anything in the
        data directory. A project with no task yet reads its files through an in-memory store."""
        with contextlib.ExitStack() as stack:
            project = None
            if store.path().exists():
                try:
                    repo = stack.enter_context(self.opener(readonly=True))
                    project = repo.project(self.name)
                except store.SchemaTooNew:
                    raise
                except (sqlite3.Error, store.StoreError) as error:
                    raise unreadable(error) from error
            if not (project and project["tasks_imported"]):
                fresh = self.fresh()
                if not fresh:
                    raise TaskError("not-imported", f"{self.name}'s tasks are not in the host store yet; run "
                                                    f"`theseus tasks import`")
                repo = stack.enter_context(taskrepo.memory())
                with repo.transaction():
                    self.load_snapshot(repo, repo.ensure_project(self.name), *parsed(fresh))
                project = repo.project(self.name)
            elif project["tasks_root"] and not self.same_checkout(project["tasks_root"]):
                raise TaskError("wrong-root", f"{self.name}'s tasks belong to {project['tasks_root']}; if the checkout "
                                              f"moved, run `theseus tasks adopt` in the new one")
            yield repo, project

    def fresh(self):
        """The task files of a project that has no task at all yet (a new one), which its first task call imports;
        None when they hold tasks, which `theseus tasks import` must load."""
        try:
            snap = rec.read(self.root)
            doc, archived, history = snap.documents()
        except FileNotFoundError:
            return rec.Snapshot(tasks='{"schema": 2, "tasks": []}\n')
        except ValueError:
            return None
        return snap if isinstance(doc, dict) and not doc.get("tasks") and not archived and not history else None

    def same_checkout(self, recorded):
        """The recorded root, or a linked worktree of it (a worker's `theseus` resolves its own worktree as root)."""
        if recorded not in self.checkouts:
            def common(path):
                found = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute",
                                        "--git-common-dir"], capture_output=True, text=True)
                return Path(found.stdout.strip()).resolve() if found.returncode == 0 and found.stdout.strip() else None
            same = Path(recorded).resolve() == self.root.resolve()
            if not same and Path(recorded).is_dir():
                mine = common(self.root)
                same = mine is not None and mine == common(recorded)
            self.checkouts[recorded] = same
        return self.checkouts[recorded]

    def load(self, repo, project, task_id):
        task = repo.task(project["id"], task_id)
        if task is None:
            raise TaskError("not-found", f"unknown task {task_id}")
        return task, repo.attempts(task["pk"])

    def records(self, repo, project, statuses=None):
        attempts = repo.project_attempts(project["id"], statuses)
        return [rec.render(task, attempts.get(task["pk"], [])) for task in repo.tasks(project["id"], statuses)]

    def write(self, repo, task, fields, touched, expected_rev=None):
        if expected_rev is not None and expected_rev != task["rev"]:
            raise TaskError("conflict", f"{task['id']} is at revision {task['rev']}, not {expected_rev}")
        return repo.update_task(task["pk"], task["rev"], {**fields, **rec.forget(task, set(touched))})

    def event(self, repo, p, task, kind, detail=None, queue=None, attempt=None):
        repo.add_event(task["pk"], p.actor, kind, detail or {}, attempt_id=attempt["id"] if attempt else None)
        if queue is not None:
            repo.queue_event(self.name, kind, task["id"], agent=agent_label(attempt), detail=queue,
                             agent_id=attempt["agent_id"] if attempt else None)

    def own(self, p, task, attempts):
        """The task's active attempt; for a worker, refuse unless it is the worker's own."""
        active = next((a for a in attempts if a["phase"] in taskrepo.ACTIVE), None)
        if p.role == "worker" and (active is None or active["agent_id"] != p.agent_id):
            raise TaskError("not-your-task", f"{task['id']} is not your active task; run `theseus task show` with the "
                                             f"task named in your latest assignment, or stop and report")
        return active

    def follow_on(self, repo, p, task, closed):
        """After closing an attempt for reopen or unmerge: the next attempt of the same agent, active, or released to
        pending when that agent already works on another task (one active task per agent). Returns that other task."""
        n = closed["n"] + 1
        execution = {k: v for k, v in (closed["execution"] or {}).items() if k != "handoff"} or None
        fields = {"task_pk": task["pk"], "n": n, "agent_id": closed["agent_id"], "owner": closed["owner"],
                  "worktree": closed["worktree"], "base": closed["base"], "log_dir": closed["log_dir"],
                  "execution": execution, "started": closed["started"]}
        busy = next((a for a in repo.active_attempts(task["project_id"]) if same_agent(a, closed)), None)
        if busy is None:
            repo.insert_attempt({**fields, "phase": "active"})
            return None
        reason = f"{agent_label(closed)} works on {busy['task_id']}"
        repo.insert_attempt({**fields, "phase": "released", "ended": now(), "reason": reason,
                             "record": rec.release_record(fields, reason, now())})
        self.event(repo, p, task, "released", {"reason": reason}, queue=reason, attempt=closed)
        return busy["task_id"]

    # reads

    def get(self, p, task_id, *, history=False):
        with self.reading() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            found = {"task": rec.render(task, attempts), "rev": task["rev"], "notes": repo.notes(task["pk"])}
            if history:
                found["history"] = repo.events(task["pk"])
            return found

    def list(self, p, *, status=None, milestone=None, include_completed=False):
        with self.reading() as (repo, project):
            statuses = None if include_completed else t.OPEN
            found = self.records(repo, project, statuses)
        return [task for task in found if (status is None or task["status"] == status)
                and (milestone is None or task.get("milestone") == milestone)]

    def index(self, p):
        return {task["id"]: task for task in self.list(p, include_completed=True)}

    def ready(self, p, *, any_milestone=False, include_paused=False, reach=None):
        """Open task ids ready to assign; reach (tasks.merge_reach) also holds a task until its dependencies' merges
        are in the base a worktree would start from."""
        with self.reading() as (repo, project):
            everything = self.records(repo, project)
            current = None if any_milestone else project["milestone"]
            holds = self.conflicts(repo, project)
        index = {task["id"]: task for task in everything}
        found = []
        for task in everything:
            candidate = {**task, "pause": None} if include_paused else task
            if task["status"] != "completed" and t.dependency_ready(candidate, index, current, reach, holds):
                found.append(task["id"])
        return found

    def holds(self, p):
        """Pending task id -> why it waits for a running task (soft ownership); computed, never stored."""
        with self.reading() as (repo, project):
            return self.conflicts(repo, project)

    def conflicts(self, repo, project):
        tasks = repo.tasks(project["id"], ("pending", "in_progress", "verification"))
        running = [task for task in tasks if task["status"] != "pending"]
        holds = {}
        for task in tasks:
            if task["status"] == "pending":
                reasons = [reason for other in running for reason in conflict(task, other)]
                if reasons:
                    holds[task["id"]] = reasons
        return holds

    def live(self, p):
        """(tasks.json document, archived records) from the store when it is readable and this checkout is the one
        the tasks belong to, else None: a guard snapshot or a worker's worktree checks the committed export."""
        try:
            with self.reading() as (repo, project):
                recorded = project["tasks_root"]
                if not recorded or Path(recorded).resolve() != self.root.resolve():
                    return None
                doc = self.header(project)
                doc["tasks"] = self.records(repo, project, t.OPEN)
                return doc, self.records(repo, project, ("completed",))
        except (TaskError, store.StoreError):
            return None

    def document(self, p):
        """The tasks.json document as an export would write it now: the header and the open tasks."""
        with self.reading() as (repo, project):
            doc = self.header(project)
            doc["tasks"] = self.records(repo, project, t.OPEN)
            return doc

    def header(self, project):
        legacy = project["tasks_legacy"] or {}
        return rec.overlay(rec.header(project), legacy.get("extra"), legacy)

    def questions(self, p, task_id=None):
        """Open worker questions, oldest first: [{task, text, author, time}]."""
        with self.reading() as (repo, project):
            tasks = [self.load(repo, project, task_id)[0]] if task_id else repo.tasks(project["id"])
            return [{"task": task["id"], "text": note["text"], "author": note["author"], "time": note["time"]}
                    for task in tasks for note in repo.notes(task["pk"])
                    if note["kind"] == "question" and not note["answered"]]

    # coordinator

    def add(self, p, spec):
        allow(p, "coordinator")
        spec = dict(spec)
        task_id = spec.get("id") or ""
        if not t.TASK_ID.fullmatch(task_id):
            raise TaskError("invalid", f"{task_id} is not a kebab-case task id")
        with self.unit() as (repo, project):
            if repo.task(project["id"], task_id):
                raise TaskError("invalid", f"{task_id} already exists")
            values = self.validated(repo, project, task_id, {
                "role": spec.get("role", ""), "objective": spec.get("objective", ""),
                "contract": spec.get("contract", ""), "accept": list(spec.get("accept") or []),
                "owned": list(spec.get("owned") or []), "depends": list(spec.get("depends") or []),
                "touches": list(spec.get("touches") or []), "room": spec.get("room"), "profile": spec.get("profile"),
                "priority": spec.get("priority") or 0, "min_capability": spec.get("min_capability") or 0},
                EDITABLE)
            milestone = spec.get("milestone") if spec.get("milestone") is not None else project["milestone"]
            pk = repo.insert_task(project["id"], {
                "id": task_id, "status": "pending", "role": values["role"], "objective": values["objective"],
                "contract": values["contract"], "acceptance": values["accept"], "milestone": milestone or None,
                "priority": values["priority"] or None, "room": values["room"], "profile": values["profile"],
                "min_capability": values["min_capability"] or None, "seq": repo.next_seq(project["id"]),
                "created": now()})
            repo.set_deps(pk, values["depends"])
            repo.set_paths(pk, "owned", values["owned"])
            repo.set_paths(pk, "touches", values["touches"])
            task, attempts = self.load(repo, project, task_id)
            record = rec.render(task, attempts)
            size = t.record_size(record)
            if size > t.TASK_RECORD_LIMIT:
                raise TaskError("invalid", f"record is {size} bytes; the limit is {t.TASK_RECORD_LIMIT}")
            self.event(repo, p, task, "added")
            return {"task": record, "size": size, "warnings": self.overlaps(repo, project, task)}

    def validated(self, repo, project, task_id, values, changed):
        """values checked as tasks.add checks them, plus no dependency cycle, for the keys in changed: an edit leaves
        the imported values it does not touch alone. Raises TaskError invalid."""
        if "owned" in changed and not any(p.startswith(t.REPORT_PREFIX) and p.endswith(".md") for p in values["owned"]):
            raise TaskError("invalid", f"ownedPaths must include a report under {t.REPORT_PREFIX} ending in .md")
        unanchored = [p for p in values["owned"] if not p.split("*")[0]] if "owned" in changed else []
        if unanchored:
            raise TaskError("invalid", f"owned pattern {unanchored[0]} starts with a wildcard, so it would own (and hold "
                                       f"the queue for) every path; start it with a directory")
        blank = [name for name in ("role", "objective", "contract")
                 if name in changed and (not isinstance(values[name], str) or not values[name].strip())]
        if "accept" in changed:
            if not values["accept"]:
                raise TaskError("invalid", "at least one acceptance item is required")
            if any(not item.strip() for item in values["accept"]):
                blank.append("accept")
            if len(values["accept"]) > t.TASK_ACCEPTANCE_LIMIT:
                raise TaskError("invalid", f"{len(values['accept'])} acceptance items; the limit is "
                                           f"{t.TASK_ACCEPTANCE_LIMIT}")
        if blank:
            raise TaskError("invalid", f"empty value for {', '.join(blank)}")
        if "depends" in changed:
            tasks = {task["id"]: task for task in repo.tasks(project["id"])}
            unknown = [dep for dep in values["depends"] if dep not in tasks]
            if unknown:
                raise TaskError("invalid", f"unknown dependencies: {', '.join(unknown)}")
            if cycle(task_id, values["depends"], tasks):
                raise TaskError("invalid", f"dependencies of {task_id} would form a cycle")
        if values["room"] not in (None, "", "clean", "dirty"):
            raise TaskError("invalid", f"room must be clean or dirty, not {values['room']}")
        if "profile" in changed and values["profile"]:
            from . import profiles
            try:
                profiles.resolve(self.conf, values["profile"])
            except profiles.ProfileError as error:
                raise TaskError("invalid", str(error)) from error
        if int(values["min_capability"] or 0) < 0:
            raise TaskError("invalid", "the minimum capability must be 0 or more")
        for key in ("externalBlocker", "researchRequired"):
            blocker = values.get(key)
            if key in changed and blocker is not None and (
                    not isinstance(blocker, dict) or not blocker.get("evidence") or not blocker.get("unblockCondition")):
                raise TaskError("invalid", f"{key} needs evidence and unblockCondition")
        if values.get("externalBlocker") and values.get("researchRequired"):
            raise TaskError("invalid", "researchRequired is assignable work, not an external blocker; keep one")
        return {**values, "room": values["room"] or None, "profile": values["profile"] or None,
                "priority": int(values["priority"] or 0), "min_capability": int(values["min_capability"] or 0)}

    def overlaps(self, repo, project, task):
        """Warnings for running tasks this task conflicts with: queuing a follow-up is legitimate, and the queue holds
        it until they are merged."""
        return [f"{reason} ({other['status']}); the queue holds it until then"
                for other in repo.tasks(project["id"], ("in_progress", "verification"))
                if other["pk"] != task["pk"] for reason in conflict(task, other)]

    def set_observed(self, p, task_id, paths):
        """The files an in-progress task's patches touch now (the tick's scan of its worktree); returns whether they
        changed. Observed touches hold conflicting tasks and are dropped when the task leaves verification."""
        allow(p, "coordinator", "queue")

        def unchanged(task, attempts):
            # A path the task also declares stays a declared touch (one row per path), so it is not observed again.
            wanted = sorted(set(paths) - set(task["touches"]))
            active = next((a for a in attempts if a["phase"] in taskrepo.ACTIVE), None)
            return active, wanted, active is None or sorted(task["observed"]) == wanted

        with self.reading() as (repo, project):
            if unchanged(*self.load(repo, project, task_id))[2]:
                return False
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            active, paths, same = unchanged(task, attempts)
            if same:
                return False
            repo.drop_observed(task["pk"])
            repo.add_observed(task["pk"], active["id"], paths)
            self.event(repo, p, task, "touches", {"observed": paths}, attempt=active)
            return True

    def edit(self, p, task_id, changes, *, expected_rev=None, force=False):
        allow(p, "coordinator")
        unknown = set(changes) - EDITABLE
        if unknown:
            raise TaskError("invalid", f"cannot edit {', '.join(sorted(unknown))}")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] == "in_progress" and not force:
                raise TaskError("refused", f"{task_id} is in progress; pass --force to change it under its worker")
            if task["status"] not in ("pending", "in_progress"):
                raise TaskError("refused", f"{task_id} is {task['status']}; reopen it before editing")
            blockers = task["blockers"] or {}
            if any(changes.get(key) for key in ("externalBlocker", "researchRequired")) and task["status"] != "pending":
                raise TaskError("invalid", f"{task_id} is {task['status']}; only a pending, unowned task can be blocked")
            current = {"externalBlocker": blockers.get("externalBlocker"),
                       "researchRequired": blockers.get("researchRequired"),
                       "role": task["role"], "objective": task["objective"], "contract": task["contract"],
                       "accept": task["acceptance"] or [], "owned": task["owned"], "depends": task["deps"],
                       "touches": task["touches"], "room": task["room"], "profile": task["profile"],
                       "priority": task["priority"] or 0, "min_capability": task["min_capability"] or 0}
            values = dict(current)
            for key, value in changes.items():
                if key == "add_owned":
                    values["owned"] = list(dict.fromkeys([*values["owned"], *value]))
                elif key in values:
                    values[key] = list(value) if key in ("accept", "owned", "depends", "touches") else value
            values = self.validated(repo, project, task_id, values,
                                    {"owned" if key == "add_owned" else key for key in changes})
            fields, touched = {}, set()
            for key, column, record_key in (("role", "role", "role"), ("objective", "objective", "objective"),
                                            ("contract", "contract", "generalContract"),
                                            ("accept", "acceptance", "acceptance"), ("room", "room", "room"),
                                            ("profile", "profile", "profile"), ("priority", "priority", "priority"),
                                            ("min_capability", "min_capability", "minCapability")):
                if values[key] != current[key] or key in changes:
                    fields[column] = values[key] or None if key in ("priority", "min_capability") else values[key]
                    touched.add(record_key)
            if "milestone" in changes:
                fields["milestone"], touched = changes["milestone"] or None, touched | {"milestone"}
            if {"externalBlocker", "researchRequired"} & set(changes):
                kept = {k: v for k, v in blockers.items() if k not in ("externalBlocker", "researchRequired")}
                kept.update({k: values[k] for k in ("externalBlocker", "researchRequired") if values[k]})
                fields["blockers"] = kept or None
                touched |= {"externalBlocker", "researchRequired"}
            if "note" in changes:
                fields["note"], touched = changes["note"] or None, touched | {"assignmentNote"}
            for key, record_key in (("owned", "ownedPaths"), ("depends", "dependencies"), ("touches", "touches")):
                if values[key] != current[key] or key in changes or (key == "owned" and "add_owned" in changes):
                    touched.add(record_key)
            repo.set_deps(task["pk"], values["depends"])
            repo.set_paths(task["pk"], "owned", values["owned"])
            repo.set_paths(task["pk"], "touches", values["touches"])
            rev = self.write(repo, task, fields, touched, expected_rev)
            repo.answer_questions(task["pk"], now())
            changed = sorted(touched)
            active = self.own(COORDINATOR, task, attempts)
            if task["status"] == "in_progress":
                repo.add_note(task["pk"], "coordinator", "edit", f"changed: {', '.join(changed)}", now(),
                              attempt_id=active["id"] if active else None)
            self.event(repo, p, task, "edited", {"fields": changed},
                       queue=f"changed: {', '.join(changed)}" if task["status"] == "in_progress" else None,
                       attempt=active)
            before = t.record_size(rec.render(task, attempts))
            task, attempts = self.load(repo, project, task_id)
            record = rec.render(task, attempts)
            size = t.record_size(record)
            if size > t.TASK_RECORD_LIMIT and size > before:
                raise TaskError("invalid", f"record would be {size} bytes; the limit is {t.TASK_RECORD_LIMIT}")
            return {"task": record, "rev": rev, "warnings": self.overlaps(repo, project, task)}

    def hold(self, p, task_id, on):
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, _ = self.load(repo, project, task_id)
            if task["status"] == "completed":
                raise TaskError("refused", f"{task_id} is completed")
            self.write(repo, task, {"hold": int(bool(on))}, {"hold"})
            self.event(repo, p, task, "held" if on else "unheld")
        return {"task": task_id, "hold": bool(on)}

    def set_milestone(self, p, name):
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            legacy = dict(project["tasks_legacy"] or {})
            legacy["extra"] = {k: v for k, v in (legacy.get("extra") or {}).items() if k != "currentMilestone"}
            legacy["absent"] = [k for k in legacy.get("absent") or [] if k != "currentMilestone"]
            repo.update_project(self.name, milestone=name or None,
                                tasks_legacy={k: v for k, v in legacy.items() if v} or None)
        return {"milestone": name or None}

    def note(self, p, task_id, text, *, question=False, on_behalf=False):
        """A worker's note or question on its own task; the coordinator's note sets the assignment note the worker
        reads, and answers open questions. on_behalf: the coordinator writes a worker's note (a subagent of its own
        session), which sets nothing."""
        allow(p, "coordinator", "worker")
        if not text.strip() or len(text) > NOTE_LIMIT:
            raise TaskError("invalid", f"a note needs 1 to {NOTE_LIMIT} characters")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            active = self.own(p, task, attempts)
            if p.role == "worker" or on_behalf:
                kind = "question" if question else "note"
                repo.add_note(task["pk"], p.agent_id or "coordinator", kind, text, now(),
                              attempt_id=active["id"] if active else None)
                self.event(repo, p, task, kind, {"text": text[:200]}, queue=text[:200] if question else None,
                           attempt=active)
                return {"task": task_id, "kind": kind}
            if task["status"] == "completed":
                raise TaskError("refused", f"{task_id} is completed")
            self.write(repo, task, {"note": text}, {"assignmentNote"})
            repo.add_note(task["pk"], "coordinator", "note", text, now(), attempt_id=active["id"] if active else None)
            answered = repo.answer_questions(task["pk"], now())
            self.event(repo, p, task, "noted", {"answered": answered} if answered else None, attempt=active)
            return {"task": task_id, "kind": "note", "answered": answered}

    def release(self, p, task_id, reason):
        allow(p, "coordinator", "queue")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] not in ("in_progress", "verification"):
                raise TaskError("refused", f"{task_id} is {task['status']}, not assigned")
            current = rec.current_attempt(attempts)
            if current:
                at = now()
                repo.update_attempt(current["id"], {"phase": "released", "ended": at, "reason": reason,
                                                    "record": rec.release_record(current, reason, at)})
            repo.drop_observed(task["pk"])
            self.write(repo, task, {"status": "pending"}, {"status", *rec.ATTEMPT_KEYS, "previousAttempts"})
            self.event(repo, p, task, "released", {"from": task["status"], "reason": reason}, queue=reason,
                       attempt=current)
            return {"task": task_id, "status": "pending", "worktree": current["worktree"] if current else None}

    def reopen(self, p, task_id, reason, *, handoff=None):
        """Verification back to in_progress for the same agent. handoff is where the caller set the delivered handoff
        aside (default: where it was recorded)."""
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] == "merged":
                raise TaskError("refused", f"{task_id} is merged; take it back with `theseus tasks unmerge {task_id}`")
            if task["status"] != "verification":
                raise TaskError("refused", f"{task_id} is {task['status']}, not verification")
            current = rec.current_attempt(attempts)
            if current is None:
                raise TaskError("refused", f"{task_id} has no recorded attempt to reopen; release it instead")
            at = now()
            repo.update_attempt(current["id"], {"phase": "reopened", "ended": at, "reason": reason, "record": {
                "reopenedAt": at, "reason": reason, "handoff": handoff or current["handoff"]}})
            repo.drop_observed(task["pk"])
            self.event(repo, p, task, "reopened", {"from": "verification", "reason": reason}, queue=reason,
                       attempt=current)
            other = self.follow_on(repo, p, task, current)
            status = "pending" if other else "in_progress"
            self.write(repo, task, {"status": status}, {"status", *rec.ATTEMPT_KEYS, "previousAttempts"})
            return {"task": task_id, "status": status, "busy_with": other}

    def unmerge(self, p, task_id, reason, revert_commit=None, *, handoff=None):
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] != "merged":
                raise TaskError("refused", f"{task_id} is {task['status']}, not merged")
            current = rec.current_attempt(attempts)
            at = now()
            entry = {"merge": task["merge"], "unmergedAt": at, "reason": reason,
                     "handoff": handoff or (current or {}).get("handoff")}
            if revert_commit:
                entry["revertCommit"] = revert_commit
            if task["evidence"]:
                entry["evidence"] = task["evidence"]
            other = None
            self.event(repo, p, task, "unmerged", {"from": "merged", "reason": reason, "revert": revert_commit},
                       queue=f"{reason} (revert {revert_commit or 'by hand'})", attempt=current)
            if current:
                repo.update_attempt(current["id"], {"phase": "unmerged", "ended": at, "reason": reason, "record": entry})
                other = self.follow_on(repo, p, task, current)
            status = "pending" if other else "in_progress"
            self.write(repo, task, {"status": status, "merge": None, "evidence": None},
                       {"status", "merge", "evidence", *rec.ATTEMPT_KEYS, "previousAttempts"})
            return {"task": task_id, "status": status, "busy_with": other,
                    "worktree": current["worktree"] if current else None}

    def rebase(self, p, task_id, base):
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] not in ("in_progress", "verification"):
                raise TaskError("refused", f"{task_id} is {task['status']}; only an assigned or delivered task can be rebased")
            current = rec.current_attempt(attempts)
            if current is None:
                raise TaskError("refused", f"{task_id} has no recorded attempt to rebase")
            repo.update_attempt(current["id"], {"base": base})
            self.write(repo, task, {}, {"baseCommit"})
            self.event(repo, p, task, "rebased", {"from": current["base"], "to": base},
                       queue=f"base {current['base']} -> {base}", attempt=current)
        return {"task": task_id, "base": base, "previous": current["base"]}

    def mark_merged(self, p, task_id, commit):
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] not in ("in_progress", "verification"):
                raise TaskError("refused", f"{task_id} is {task['status']}; only assigned or delivered tasks can be merged")
            current = rec.current_attempt(attempts)
            if current:
                repo.update_attempt(current["id"], {"phase": "merged"})
            repo.drop_observed(task["pk"])
            self.write(repo, task, {"status": "merged", "merge": {"commit": commit, "mergedAt": now()}},
                       {"status", "merge"})
            self.event(repo, p, task, "merged", {"from": task["status"], "commit": commit},
                       queue=f"in {commit}; verify, then theseus tasks complete", attempt=current)
        return {"task": task_id, "status": "merged", "commit": commit}

    def observe(self, p, task_id, how, level="observed"):
        allow(p, "coordinator")
        if level not in t.EVIDENCE_LEVELS:
            raise TaskError("invalid", f"unknown evidence level {level}")
        if not how.strip():
            raise TaskError("invalid", "say how it was observed: command, build, scenario, tester")
        with self.unit() as (repo, project):
            task, _ = self.load(repo, project, task_id)
            if task["status"] not in ("merged", "completed"):
                raise TaskError("refused", f"{task_id} is {task['status']}; observe a merged task")
            evidence = {**(task["evidence"] or {}), level: {"at": now(), "how": how}}
            self.write(repo, task, {"evidence": evidence}, {"evidence"})
            self.event(repo, p, task, "observed", {"level": level, "how": how})
        return {"task": task_id, "level": level}

    def complete(self, p, task_id, summary, commit=None, checks=(), log=None, limits=(), *, force=False):
        """The checks that need the worktree or a verify log (unintegrated files, --from-log) stay with the caller."""
        allow(p, "coordinator")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if task["status"] == "completed":
                raise TaskError("refused", f"{task_id} is already completed")
            if task["status"] not in ("verification", "merged") and not force:
                raise TaskError("refused", f"{task_id} is {task['status']}; merge it first or pass --force")
            commit = commit or (task["merge"] or {}).get("commit")
            if not commit or not log or not checks:
                raise TaskError("invalid", "complete needs a log and at least one check (the commit defaults to the "
                                           "merge commit)")
            current = rec.current_attempt(attempts)
            forced = ([f"was {task['status']}"] if task["status"] not in ("verification", "merged") else []) + \
                ([] if current else ["never attempted"])
            if current and current["phase"] != "merged":
                repo.update_attempt(current["id"], {"phase": "merged"})
            repo.drop_observed(task["pk"])
            if forced:
                self.event(repo, p, task, "forced", {"bypassed": forced, "summary": summary},
                           queue=f"completed by force: {', '.join(forced)}", attempt=current)
            result = {"integratedCommit": commit, "completedAt": now(), "summary": summary, "checks": list(checks),
                      "log": log, "limits": list(limits)}
            self.write(repo, task, {"status": "completed", "result": result, "seq": repo.next_seq(project["id"])},
                       {"status", "result"})
            self.event(repo, p, task, "completed", {"from": task["status"], "commit": commit}, queue=summary,
                       attempt=current)
        return {"task": task_id, "status": "completed", "commit": commit}

    def relocate(self, p, task_id, old, new, branch=None):
        """Point the attempts of task_id that used the worktree old (moved aside to new) at new, their log and
        handoff paths with it, and record branch (the renamed work branch) on them."""
        allow(p, "coordinator", "queue")
        old, new = str(old), str(new)
        under = lambda path: isinstance(path, str) and (path == old or path.startswith(old + os.sep))
        moved = lambda path: new + path[len(old):] if under(path) else path

        def paths_moved(execution):
            return {k: moved(v) if k in ("logDir", "handoff") else v for k, v in execution.items()}

        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            changed = 0
            for attempt in attempts:
                record = attempt["record"] if attempt["record"] is not None else {
                    "worktree": attempt["worktree"], "execution": attempt["execution"]}
                execution = record.get("execution") if isinstance(record.get("execution"), dict) else {}
                if not (under(record.get("worktree")) or under(execution.get("logDir"))):
                    continue
                fields = {"worktree": moved(attempt["worktree"]), "log_dir": moved(attempt["log_dir"]),
                          "handoff": moved(attempt["handoff"]),
                          "execution": paths_moved(attempt["execution"]) if attempt["execution"] else None}
                if attempt["record"] is not None:
                    record = {**record}
                    if "worktree" in record:
                        record["worktree"] = moved(record["worktree"])
                    if branch:
                        record["branch"] = branch
                    if execution:
                        record["execution"] = paths_moved(execution)
                    fields["record"] = record
                repo.update_attempt(attempt["id"], fields)
                changed += 1
            if changed:
                self.write(repo, task, {}, {"previousAttempts", *rec.ATTEMPT_KEYS})
                self.event(repo, p, task, "relocated", {"from": old, "to": new})
        return changed

    def adopt(self, p):
        """Record this checkout as the root of the project's tasks (a moved or renamed checkout)."""
        allow(p, "coordinator")
        with self.unit(root=False) as (repo, project):
            repo.update_project(self.name, tasks_root=str(self.root))
        return {"project": self.name, "root": str(self.root), "previous": project["tasks_root"]}

    # queue and dispatch

    def claim(self, p, task_id, agent_id, *, owner, provider, model="persistent-instance", expected_rev=None,
              capacity=None, force=False, reach=None):
        """The first step of an assignment: the task in_progress under a claimed attempt, decided in one transaction
        and by the unique indexes (one active attempt per task and per agent). No worktree exists yet. reach (see
        ready) runs git, so it is checked before the transaction opens."""
        allow(p, "coordinator", "queue")
        held = []
        if reach:
            with self.reading() as (repo, project):
                index = {record["id"]: record for record in self.records(repo, project)}
            held = t.unreached(index[task_id], index, reach) if task_id in index else []
            if held and not force:
                raise TaskError("refused", f"{task_id} is not ready: {'; '.join(held)}")
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            if expected_rev is not None and expected_rev != task["rev"]:
                raise TaskError("conflict", f"{task_id} changed since revision {expected_rev}")
            pause = (task["blockers"] or {}).get("pause")
            if pause and not force:
                raise TaskError("refused", f"{task_id} is paused with preserved work ({pause.get('state')}); pass "
                                           f"--force to start fresh")
            if task["status"] == "completed" or (task["status"] != "pending" and not force):
                raise TaskError("refused", f"{task_id} is {task['status']}; pass --force to reassign")
            bypassed = held + self.claim_problems(repo, project, task, provider, capacity)
            if bypassed and not force:
                raise TaskError("refused", f"{task_id} is not ready: {', '.join(bypassed)}")
            current = rec.current_attempt(attempts)
            at = now()
            n = max((a["n"] for a in attempts), default=0) + 1
            superseded = None
            if current or pause:
                if current:
                    repo.update_attempt(current["id"], {"phase": "superseded", "ended": at, "reason": "reassigned",
                                                        "record": rec.superseded_record(current, pause, at)})
                    superseded = {"attempt": current["id"], "phase": current["phase"], "record": current["record"]}
                else:
                    inserted = repo.insert_attempt({"task_pk": task["pk"], "n": n, "phase": "superseded", "ended": at,
                                                    "reason": "reassigned",
                                                    "record": rec.superseded_record(None, pause, at)})
                    superseded = {"attempt": inserted, "inserted": True}
                    n += 1
            # While the claim is current its record column is unused by the export: it remembers what the claim
            # superseded, so abandoning it can put that back.
            restore = {"status": task["status"], "pause": pause, "superseded": superseded} if force else None
            attempt_id = repo.insert_attempt({"task_pk": task["pk"], "n": n, "agent_id": agent_id, "owner": owner,
                                              "phase": "claimed", "execution": {"provider": provider, "model": model},
                                              "started": at, "record": restore})
            blockers = {k: v for k, v in (task["blockers"] or {}).items() if k != "pause"} or None
            rev = self.write(repo, task, {"status": "in_progress", "blockers": blockers},
                             {"status", "pause", *rec.ATTEMPT_KEYS, "previousAttempts"})
            self.event(repo, p, task, "claimed", {"from": task["status"], "agent": agent_id},
                       attempt={"id": attempt_id})
            if force and (bypassed or pause or task["status"] != "pending"):
                reasons = bypassed + ([f"paused ({pause.get('state')})"] if pause else []) + \
                    ([f"was {task['status']}"] if task["status"] != "pending" else [])
                self.event(repo, p, task, "forced", {"bypassed": reasons},
                           queue=f"forced claim despite: {', '.join(reasons)}", attempt={"id": attempt_id,
                                                                                         "owner": owner,
                                                                                         "execution": None,
                                                                                         "agent_id": agent_id})
        return {"task": task_id, "attempt": attempt_id, "rev": rev}

    def claim_problems(self, repo, project, task, provider, capacity):
        """Why task may not be claimed now: blockers, milestone, unmet dependencies, the hold, the profile's capacity."""
        index = {record["id"]: record for record in self.records(repo, project)}
        problems = t.blocking(index[task["id"]], index, project["milestone"])
        problems += [reason for other in repo.tasks(project["id"], ("in_progress", "verification"))
                     if other["pk"] != task["pk"] for reason in conflict(task, other)]
        if task["hold"]:
            problems.append("hold")
        if capacity is not None:
            running = sum(1 for a in repo.active_attempts(project["id"])
                          if (a["execution"] or {}).get("provider") == provider and a["task_pk"] != task["pk"])
            if running >= int(capacity):
                problems.append(f"provider {provider} is at its capacity of {capacity} active tasks")
        return problems

    def activate(self, p, attempt_id, *, worktree, base, log_dir, execution=None):
        allow(p, "coordinator", "queue")
        with self.unit() as (repo, project):
            attempt = repo.attempt(attempt_id)
            if attempt is None:
                raise TaskError("not-found", f"unknown attempt {attempt_id}")
            task = repo.task_by_pk(attempt["task_pk"])
            if attempt["phase"] != "claimed":
                raise TaskError("conflict", f"attempt {attempt_id} is {attempt['phase']}, not claimed")
            details = {**(attempt["execution"] or {}), "logDir": str(log_dir), "assignedAt": now(), **(execution or {})}
            repo.update_attempt(attempt_id, {"phase": "active", "worktree": str(worktree), "base": base,
                                             "log_dir": str(log_dir), "execution": details}, phases=("claimed",))
            self.write(repo, task, {}, set(rec.ATTEMPT_KEYS))
            attempt = repo.attempt(attempt_id)
            self.event(repo, p, task, "assigned", {"worktree": str(worktree), "base": base},
                       queue=f"{details.get('provider')}: {worktree}", attempt=attempt)
        return {"task": task["id"], "attempt": attempt_id, "worktree": str(worktree)}

    def abandon(self, p, attempt_id, reason):
        """Undo a claim whose worktree or task message failed: the task back to pending, recorded as a release."""
        allow(p, "coordinator", "queue")
        with self.unit() as (repo, project):
            attempt = repo.attempt(attempt_id)
            if attempt is None:
                raise TaskError("not-found", f"unknown attempt {attempt_id}")
            if attempt["phase"] not in taskrepo.ACTIVE:
                raise TaskError("conflict", f"attempt {attempt_id} is {attempt['phase']}, not claimed or active")
            task = repo.task_by_pk(attempt["task_pk"])
            at = now()
            restore = attempt["record"] if isinstance(attempt["record"], dict) and "superseded" in attempt["record"] else None
            if attempt["phase"] == "claimed":
                # Nothing reached a worker or the disk: the claim leaves no attempt in the record, only an event.
                repo.delete_attempt(attempt_id)
                self.event(repo, p, task, "unclaimed", {"reason": reason})
            else:
                repo.update_attempt(attempt_id, {"phase": "abandoned", "ended": at, "reason": reason,
                                                 "record": rec.release_record(attempt, reason, at)})
                self.event(repo, p, task, "released", {"from": task["status"], "reason": reason}, queue=reason,
                           attempt=attempt)
            status = self.restore_superseded(repo, p, task, attempt, restore) if restore else "pending"
            blockers = {**(task["blockers"] or {}), **({"pause": restore["pause"]} if restore and restore["pause"] else {})}
            self.write(repo, task, {"status": status, "blockers": blockers or None},
                       {"status", "pause", *rec.ATTEMPT_KEYS, "previousAttempts"})
        return {"task": task["id"], "status": status}

    def restore_superseded(self, repo, p, task, attempt, restore):
        """Put back the attempt a forced claim superseded; returns the task status to restore (pending when there
        was no attempt, or its agent has taken another task since)."""
        superseded = restore["superseded"]
        if not superseded:
            return restore["status"]
        if superseded.get("inserted"):
            repo.delete_attempt(superseded["attempt"])
            return restore["status"]
        try:
            repo.update_attempt(superseded["attempt"], {"phase": superseded["phase"], "ended": None, "reason": None,
                                                        "record": superseded["record"]})
        except taskrepo.Conflict:
            self.event(repo, p, task, "not-restored", {"reason": "the superseded attempt's agent holds another task"})
            return "pending"
        if repo.attempt(attempt["id"]):
            # The restored attempt is current again, so it takes the higher number (the export lists closed ones first).
            old, new = repo.attempt(superseded["attempt"])["n"], attempt["n"]
            repo.update_attempt(attempt["id"], {"n": -1})
            repo.update_attempt(superseded["attempt"], {"n": new})
            repo.update_attempt(attempt["id"], {"n": old})
        return restore["status"]

    def abandon_orphans(self, p):
        """Abandon every claimed attempt of the project: claims are made under the queue lock, so at the start of a
        tick a claimed attempt belongs to a tick or dispatch that crashed between claim and activate."""
        allow(p, "coordinator", "queue")
        with self.reading() as (repo, project):
            orphans = [a["id"] for a in repo.active_attempts(project["id"]) if a["phase"] == "claimed"]
        return [self.abandon(p, attempt_id, "orphaned claim") for attempt_id in orphans]

    def assign(self, p, task_id, agent_id, prepare, *, owner, provider, model="persistent-instance", expected_rev=None,
               capacity=None, force=False, reach=None):
        """claim, then prepare(claim) outside any transaction (worktree creation may fetch for minutes against the
        store's 30 s busy timeout), then activate with the (worktree, base, log_dir) it returns, or abandon when it
        raises."""
        claim = self.claim(p, task_id, agent_id, owner=owner, provider=provider, model=model,
                           expected_rev=expected_rev, capacity=capacity, force=force, reach=reach)
        try:
            worktree, base, log_dir = prepare(claim)
        except Exception as error:
            with contextlib.suppress(TaskError):
                self.abandon(p, claim["attempt"], f"assignment failed: {error}")
            raise
        return self.activate(p, claim["attempt"], worktree=worktree, base=base, log_dir=log_dir)

    # worker

    def show(self, p, task_id):
        allow(p, "coordinator", "worker", "queue")
        with self.reading() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            active = self.own(p, task, attempts)
            current = rec.current_attempt(attempts)
            return {"task": rec.render(task, attempts), "rev": task["rev"], "notes": repo.notes(task["pk"]),
                    "attempt": None if current is None else {key: current[key] for key in (
                        "id", "phase", "worktree", "base", "log_dir")}, "active": bool(active)}

    def deliver(self, p, task_id, *, handoff_path=None, result=None, handoff_text=None, observed_touches=(),
                reason=None, source=None):
        """The worker's report (source command), or the queue's or coordinator's when they found the handoff
        (source detected; the coordinator reporting for its own subagent passes source command). result is the
        handoff's Result: delivered, partial, blocked or None (not stated)."""
        allow(p, "coordinator", "worker", "queue")
        if result not in (None, "delivered", "partial", "blocked"):
            raise TaskError("invalid", f"unknown result {result}")
        if p.role == "worker":
            source = "outbox" if p.via == "outbox" else "command"
        source = source or "detected"
        with self.unit() as (repo, project):
            task, attempts = self.load(repo, project, task_id)
            current = rec.current_attempt(attempts)
            if task["status"] == "verification" and (p.role != "worker" or (current or {}).get("agent_id") == p.agent_id):
                return {"task": task_id, "status": "verification", "already": True}
            active = self.own(p, task, attempts)
            if task["status"] != "in_progress" or active is None or active["phase"] != "active":
                raise TaskError("refused", f"{task_id} is {task['status']} with no active attempt to deliver")
            kind = "delivered" if result in ("delivered", None) else result
            execution = dict(active["execution"] or {})
            if handoff_path is not None:
                execution["handoff"] = str(handoff_path)
            execution["deliveredAt"] = now()
            repo.update_attempt(active["id"], {
                "phase": "delivered", "execution": execution, "result": kind, "source": source, "reason": reason,
                "handoff": None if handoff_path is None else str(handoff_path),
                "handoff_sha256": None if handoff_text is None else sha256(handoff_text)}, phases=("active",))
            repo.add_observed(task["pk"], active["id"], list(dict.fromkeys(observed_touches)))
            self.write(repo, task, {"status": "verification"}, {"status", "execution"})
            detail = f"handoff result {result or 'not stated'}" + (f": {reason}" if reason else "")
            if source == "detected":
                detail += " (detected: the worker did not run `theseus task done`)"
            recorded = {"from": "in_progress", "result": kind, "source": source, "reason": reason,
                        "handoff_sha256": None if handoff_text is None else sha256(handoff_text), "attempt": active["n"]}
            self.event(repo, p, task, kind, {k: v for k, v in recorded.items() if v is not None}, queue=detail,
                       attempt=active)
            repo.delivery(self.name, task_id, agent_label(active), None if handoff_path is None else str(handoff_path),
                          kind, active["agent_id"], source)
        return {"task": task_id, "status": "verification", "result": kind, "source": source}

    def blocked(self, p, task_id, reason, *, handoff_path=None, handoff_text=None, source=None):
        if not reason.strip():
            raise TaskError("invalid", "say what blocks the task")
        return self.deliver(p, task_id, handoff_path=handoff_path, result="blocked", handoff_text=handoff_text,
                            reason=reason, source=source)

    # records

    def export(self, p, *, record=False, head=None):
        """The snapshot of the project's tasks in the legacy files. record (the caller writes it into the checkout)
        stores when, at which commit (head) and with what content it was written, as projects.last_export."""
        allow(p, "coordinator")
        if not record:
            with self.reading() as (repo, project):
                return self.snapshot(repo, project)
        with self.unit() as (repo, project):
            snap = self.snapshot(repo, project)
            repo.update_project(self.name, last_export={"time": now(), "head": head, "sha256": digests(snap)})
            return snap

    def export_state(self, p, files):
        """How the task files in the checkout (a Snapshot) stand against the store: current; stale (the store moved on
        since the export they hold, which is normal between verified merges); or edited (they hold something no
        export wrote, which theseus ignores). Returns {state, detail}."""
        with self.reading() as (repo, project):
            fresh = self.snapshot(repo, project)
            recorded = project["last_export"] or {}
            changed = sum(1 for task in repo.tasks(project["id"])
                          if task["updated"] > (recorded.get("time") or ""))
        if digests(files) == digests(fresh):
            return {"state": "current", "detail": "the committed export matches the store"}
        if digests(files) == recorded.get("sha256"):
            return {"state": "stale", "detail": f"{changed} task(s) changed since the export of {recorded.get('time')}; "
                                                f"`theseus tasks export` (and commit docs/project/) records them"}
        return {"state": "edited", "detail": "docs/project/ task files differ from every export theseus wrote: a hand "
                                             "edit, which theseus ignores. Re-export to drop it, or take it with "
                                             "`theseus tasks import --replace`"}

    def snapshot(self, repo, project):
        attempts = repo.project_attempts(project["id"])
        rendered = [(task["status"], rec.render(task, attempts.get(task["pk"], []))) for task in repo.tasks(project["id"])]
        doc = self.header(project)
        doc["tasks"] = [record for status, record in rendered if status != "completed"]
        archived = [record for status, record in rendered if status == "completed"]
        return rec.snapshot(doc, archived, [rec.history_line(row) for row in repo.history(project["id"])])

    def import_(self, p, snap, *, replace=False, new_id=False):
        """Load a snapshot (the legacy files, or an export) as the project's tasks in one transaction, export it again
        and compare: anything but the documented normalizations (schema 2 and the project header, completed tasks
        moved to the archive) rolls the import back with TaskError lossy. Records skip the add-time budgets.
        replace loads over existing tasks after writing their export to <data>/exports/; new_id gives the project a
        fresh id, for another project reusing the name once the old one's tasks are pruned."""
        allow(p, "coordinator")
        started = time.monotonic()
        doc, archived, history = parsed(snap)
        with self.unit(imported=False, root=False) as (repo, project):
            saved = None
            if repo.tasks(project["id"]):
                if new_id:
                    raise TaskError("refused", f"{self.name} has tasks under its current id; remove them first with "
                                               f"`theseus db prune --with-tasks`, which exports them")
                if not replace:
                    raise TaskError("refused", f"{self.name} already has tasks in the store; pass --replace to load "
                                               f"over them")
                saved = self.save_export(repo, project, "replaced")
                repo.delete_tasks(project["id"])
            ident, warnings, events, notes = self.load_snapshot(repo, project, doc, archived, history, new_id)
        canonical = rec.snapshot(doc, archived, history)
        return {"project": self.name, "id": ident, "open": len(doc["tasks"]), "archived": len(archived),
                "events": events, "notes": notes, "warnings": warnings, "saved": saved,
                "formatting_kept": {"tasks.json": canonical.tasks == snap.tasks,
                                    "tasks-archive.jsonl": canonical.archive == snap.archive,
                                    "tasks-history.jsonl": canonical.history == snap.history},
                "seconds": round(time.monotonic() - started, 3)}

    def load_snapshot(self, repo, project, doc, archived, history, new_id=False):
        """Load parsed export files into repo (inside the caller's transaction) and check that exporting them again
        gives the input back; returns (project id, warnings, events, notes)."""
        everything = archived + doc["tasks"]
        ident = self.import_identity(repo, project, doc, new_id)
        project = {**project, "id": ident}
        warnings = self.import_tasks(repo, project, everything)
        repo.update_project(self.name, **rec.header_columns(doc))
        project = repo.project(self.name)
        expected_doc = rec.normalized_header(doc, project)
        extra, legacy = rec.shape({**rec.header(project), "tasks": []}, {**expected_doc, "tasks": []})
        repo.update_project(self.name, tasks_root=str(self.root), tasks_imported=now(),
                            tasks_legacy={**(legacy or {}), **({"extra": extra} if extra else {})} or None)
        events, notes = self.import_history(repo, project, history)
        project = repo.project(self.name)
        exported = self.snapshot(repo, project)
        expected_doc["tasks"] = [r for r in everything if r["status"] != "completed"]
        expected = rec.snapshot(expected_doc, [r for r in everything if r["status"] == "completed"], history)
        for label, want, got in (("tasks.json", expected.tasks, exported.tasks),
                                 ("tasks-archive.jsonl", expected.archive, exported.archive),
                                 ("tasks-history.jsonl", expected.history, exported.history)):
            difference = rec.diff(want, got, label)
            if difference:
                raise TaskError("lossy", f"the export differs from the input, nothing imported:\n{difference}")
        return ident, warnings, events, notes

    def save_export(self, repo, project, label):
        """Write the current export to <data>/exports/<project>-<time>-<label>/ before tasks are deleted."""
        directory = Path(self.conf.data) / "exports" / f"{self.name}-{store.iso().replace(':', '')}-{label}"
        rec.write_flat(self.snapshot(repo, project), directory)
        return str(directory)

    def drop_tasks(self, p):
        """Export the project's tasks to <data>/exports/, then delete them (db prune --with-tasks). Returns the
        directory, or None when it had none."""
        allow(p, "coordinator")
        with self.unit(imported=False, root=False) as (repo, project):
            if not repo.tasks(project["id"]):
                return None
            saved = self.save_export(repo, project, "pruned")
            repo.delete_tasks(project["id"])
            repo.update_project(self.name, tasks_imported=None, tasks_root=None)
            return saved

    def import_identity(self, repo, project, doc, new_id):
        """The project id the import keeps: a fresh one with new_id, else the export header's (so a store on another
        host matches the committed export), else the store's."""
        wanted = store.new_id() if new_id else ((doc.get("project") or {}).get("id") or project["id"])
        if wanted != project["id"]:
            holder = repo.project_by_id(wanted)
            if holder:
                raise TaskError("refused", f"project id {wanted} belongs to {holder['name']} in this store; pass --new-id")
            repo.update_project(self.name, id=wanted)
        return wanted

    def import_tasks(self, repo, project, everything):
        warnings = []
        bound = {}
        for record in everything:
            agent = record.get("ownerId") if record["status"] == "in_progress" else None
            if isinstance(agent, str):
                assigned = str((record.get("execution") or {}).get("assignedAt") or "")
                if agent not in bound or assigned > bound[agent][0]:
                    bound[agent] = (assigned, record["id"])
        for seq, record in enumerate(everything, 1):
            fields, deps, owned, touches = rec.columns(record)
            previous = record.get("previousAttempts")
            attempts = [rec.previous_columns(entry) for entry in previous] \
                if isinstance(previous, list) and all(isinstance(e, dict) for e in previous) else []
            current = rec.current_columns(record)
            if current and current["agent_id"] and current["phase"] in taskrepo.ACTIVE \
                    and bound.get(current["agent_id"], (None, record["id"]))[1] != record["id"]:
                warnings.append(f"{record['id']}: agent {current['agent_id']} also holds "
                                f"{bound[current['agent_id']][1]}, so this older attempt is not bound to it")
                current["agent_id"] = None
            if current:
                attempts.append(current)
            for n, attempt in enumerate(attempts, 1):
                attempt["n"] = n
            row = {**fields, "deps": deps, "owned": owned, "touches": touches, "observed": [], "extra": None,
                   "legacy": None}
            extra, legacy = rec.shape(rec.build(row, attempts), record)
            pk = repo.insert_task(project["id"], {**fields, "seq": seq, "extra": extra, "legacy": legacy})
            repo.set_deps(pk, deps)
            repo.set_paths(pk, "owned", owned)
            repo.set_paths(pk, "touches", touches)
            for attempt in attempts:
                repo.insert_attempt({**attempt, "task_pk": pk})
        return warnings

    def import_history(self, repo, project, history):
        pks = {task["id"]: task["pk"] for task in repo.tasks(project["id"])}
        attempts = {(pk, attempt["n"]): attempt for pk, found in repo.project_attempts(project["id"]).items()
                    for attempt in found}
        events = notes = 0
        for line in history:
            pk = pks.get(line.get("task"))
            if pk is None:
                raise TaskError("invalid", f"history names an unknown task: {json.dumps(line)[:200]}")
            if "event" in line:
                detail = line.get("detail") or {}
                attempt = attempts.get((pk, detail.get("attempt")))
                repo.add_event(pk, line["actor"], line["event"], detail, time=line["time"],
                               attempt_id=attempt["id"] if attempt else None)
                if attempt and detail.get("source"):
                    repo.update_attempt(attempt["id"], {key: detail.get(key) for key in (
                        "result", "source", "reason", "handoff_sha256")})
                events += 1
            else:
                repo.add_note(pk, line["author"], line["kind"], line["note"], line["time"], answered=line.get("answered"))
                notes += 1
        return events, notes


def conflict(task, other):
    """Why task waits for the running task other: owned paths that overlap (owns:PATTERN@OTHER), or a file one of them
    touches (declared, or observed in its patches) that the other owns or touches (touches:FILE@OTHER)."""
    reasons = []
    overlapping = [b for b in other["owned"] if t.ownership_overlaps(task["owned"], [b])]
    if overlapping:
        reasons.append(f"owns:{overlapping[0]}@{other['id']}")
    mine, theirs = task["touches"] + task["observed"], other["touches"] + other["observed"]
    files = [f for f in mine if t.is_owned(f, other["owned"]) or t.is_owned(f, theirs)]
    files += [f for f in theirs if t.is_owned(f, task["owned"])]
    reasons += [f"touches:{f}@{other['id']}" for f in dict.fromkeys(files)]
    return reasons


def digests(snap):
    return {name: sha256(getattr(snap, name)) for name in ("tasks", "archive", "history")}


def parsed(snap):
    """(tasks.json document, archived records, history lines) of a snapshot, checked for ids and statuses."""
    try:
        doc, archived, history = snap.documents()
    except ValueError as error:
        raise TaskError("invalid", f"the snapshot is not valid JSON: {error}") from error
    if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
        raise TaskError("invalid", "tasks.json has no tasks list")
    everything = archived + doc["tasks"]
    problems = [f"record {n}: no id or an unknown status" for n, record in enumerate(everything)
                if not isinstance(record, dict) or not isinstance(record.get("id"), str)
                or record.get("status") not in t.STATUSES]
    seen, duplicates = set(), set()
    for record in everything:
        if isinstance(record, dict):
            (duplicates if record.get("id") in seen else seen).add(record.get("id"))
    problems += [f"{task_id}: duplicate id" for task_id in sorted(duplicates, key=str)]
    if problems:
        raise TaskError("invalid", "; ".join(problems[:10]))
    return doc, archived, history


def same_agent(a, b):
    """Two attempts held by one agent: the same agent id, or, for agents without one, the same name in the owner
    label (unless both carry ids that differ)."""
    if a["agent_id"] and b["agent_id"]:
        return a["agent_id"] == b["agent_id"]
    return bool(agent_label(a)) and agent_label(a) == agent_label(b)


def cycle(task_id, depends, tasks):
    """True when task_id is reachable from its new dependencies through the stored ones."""
    stack, seen = list(depends), set()
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in seen or current not in tasks:
            continue
        seen.add(current)
        stack.extend(tasks[current]["deps"])
    return False


def open(conf, opener=taskrepo.opened):
    return TaskService(conf, opener)


# identity

def issue_credentials(p, agent_id, run_id, opener=taskrepo.opened):
    """A fresh run secret for one supervised run or dispatch of agent_id; only its hash is stored. Returns the secret."""
    allow(p, "coordinator", "queue")
    secret = secrets.token_urlsafe(32)
    with opener() as repo, repo.transaction():
        repo.add_credential(agent_id, run_id, sha256(secret))
    return secret


def write_credentials(path, agent_id, run_id, secret):
    """The credentials file an agent's theseus reads (THESEUS_CREDENTIALS): mode 0600, replaced atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"agent_id": agent_id, "run_id": run_id, "run_secret": secret}, handle)
    os.replace(temporary, path)
    return path


def grant(p, agent_id, run_id, path, opener=taskrepo.opened):
    """Issue a run secret for agent_id and write its credentials file; returns the environment that names them."""
    write_credentials(path, agent_id, run_id, issue_credentials(p, agent_id, run_id, opener))
    return {"THESEUS_AGENT_ID": agent_id, "THESEUS_CREDENTIALS": str(path)}


def revoke_credentials(p, agent_id, run_id, opener=taskrepo.opened):
    allow(p, "coordinator", "queue")
    with opener() as repo, repo.transaction():
        return repo.revoke_credential(agent_id, run_id)


def unreadable(error):
    return TaskError("unreadable", f"cannot read the host store {store.path()} read-only ({error}). A reader that may "
                                   f"not write its directory can read only while its -wal and -shm files exist "
                                   f"(another theseus process has it open); ask the coordinator for `theseus tasks "
                                   f"show ID`")


def facts(opener, readonly):
    """A repo for identity checks: read-only for a read command, and None before any store exists (then no agent,
    credentials or worktree exist either)."""
    if not store.path().exists():
        return contextlib.nullcontext()
    return opener(readonly=True) if readonly else opener()


def authenticate(credentials, *, via="cli", opener=taskrepo.opened, readonly=False):
    """The worker principal of credentials {agent_id, run_secret} when the secret matches an unrevoked run of that
    agent. The role never comes from the caller: credentials only ever make a worker."""
    agent_id, secret = (credentials or {}).get("agent_id"), (credentials or {}).get("run_secret")
    if isinstance(agent_id, str) and isinstance(secret, str):
        with facts(opener, readonly) as repo:
            hashes = repo.credential_hashes(agent_id) if repo else []
        if any(hmac.compare_digest(sha256(secret), stored) for stored in hashes):
            return Principal("worker", agent_id, via)
    raise TaskError("unauthenticated", "these credentials match no running agent (a restarted or ended run, or a "
                                       "copied file)")


def principal(env=None, cwd=None, *, via="cli", opener=taskrepo.opened, readonly=False):
    """Who is calling, deny by default: credentials (THESEUS_CREDENTIALS) make a worker; the coordinator is a caller
    with neither THESEUS_AGENT_ID nor THESEUS_CREDENTIALS, not in a hand-made agent's tmux pane (TMUX_PANE, which only
    ever denies), whose working directory is not a task worktree. Anything else is refused, never promoted. Reads do
    not need this (see the module docstring)."""
    try:
        return identify(os.environ if env is None else env, Path(cwd or os.getcwd()), via, opener, readonly)
    except store.SchemaTooNew:
        raise
    except (sqlite3.Error, store.StoreError) as error:
        raise unreadable(error) from error


def identify(env, cwd, via, opener, readonly):
    path = env.get("THESEUS_CREDENTIALS")
    if path:
        try:
            credentials = json.loads(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise TaskError("unauthenticated", f"cannot read the credentials file {path}: {error}") from error
        return authenticate(credentials, via=via, opener=opener, readonly=readonly)
    if env.get("THESEUS_AGENT_ID") or env.get("THESEUS_AGENT"):
        # THESEUS_AGENT is set by every supervised launch, also by theseus versions that wrote no credentials yet.
        raise TaskError("unauthenticated", "this process runs as a theseus agent without valid credentials; an agent is "
                                           "never the coordinator (an agent started before credentials existed gets "
                                           "them at its next restart)")
    if env.get("TMUX_PANE"):
        with facts(opener, readonly) as repo:
            agent = repo.hand_made_agent(env["TMUX_PANE"]) if repo else None
        if agent and (not agent["pane_pid"] or agent["pane_pid"] in ancestors()):
            raise TaskError("unauthenticated", f"this pane ({env['TMUX_PANE']}) is the registered agent "
                                               f"{agent['project']}/{agent['name']}; an agent is never the coordinator "
                                               f"(if that registration is stale, remove it with `theseus agents remove "
                                               f"{agent['name']}`)")
    worktree = task_worktree(cwd, opener, readonly)
    if worktree:
        raise TaskError("unauthenticated", f"{cwd} is inside the task worktree {worktree}; the coordinator works from "
                                           f"the main checkout")
    return Principal("coordinator", via=via)


def ancestors(pid=None):
    """This process and its ancestors' pids (from /proc): a registered pane's process is among them only while the
    caller really runs in that pane, not in a later one that tmux gave the same id."""
    pid, found = pid or os.getpid(), set()
    while pid and pid not in found:
        found.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat[stat.rfind(")") + 2:].split()[1])
    return found


def task_worktree(cwd, opener=taskrepo.opened, readonly=False):
    """The worktree of an unfinished task (any project) that holds cwd, or None. A task assigned in place, in its
    project's own checkout, does not make that checkout a worktree."""
    candidates = {cwd, cwd.resolve()}
    with facts(opener, readonly) as repo:
        if repo is None:
            return None
        for directory in [*candidates, *(parent for c in candidates for parent in c.parents)]:
            for attempt in repo.attempts_at(str(directory)):
                roots = {Path(root).resolve() for root in (attempt["project_root"], attempt["tasks_root"]) if root}
                if Path(attempt["worktree"]).resolve() not in roots:
                    return str(directory)
    return None
