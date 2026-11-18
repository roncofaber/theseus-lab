"""Task repository: the only module that writes SQL for the task tables of the host store.

Repo is the interface theseus.taskservice depends on; SqliteRepo implements it over one store connection. Rows come
back as plain dicts with JSON columns decoded (None for SQL NULL). The repository checks nothing about the lifecycle:
it stores what the service decided, compare-and-set on tasks.rev and the unique indexes on active attempts being the
only refusals it raises (as Conflict)."""
import contextlib
import json
import sqlite3
from typing import ContextManager, Iterable, Protocol

from . import store

TASK_JSON = ("contract", "acceptance", "blockers", "merge", "result", "evidence", "extra", "legacy")
ATTEMPT_JSON = ("execution", "record")
PROJECT_JSON = ("policy", "tasks_legacy")
TASK_COLUMNS = ("id", "status", "role", "objective", "contract", "acceptance", "milestone", "priority", "hold", "room",
                "profile", "min_capability", "note", "blockers", "merge", "result", "evidence", "extra", "legacy", "seq",
                "created")
ATTEMPT_COLUMNS = ("task_pk", "n", "agent_id", "owner", "phase", "worktree", "base", "log_dir", "execution", "handoff",
                   "handoff_sha256", "result", "source", "reason", "record", "started", "ended")
PROJECT_COLUMNS = ("id", "tasks_root", "milestone", "objective_complete", "policy", "tasks_legacy", "tasks_imported",
                   "last_export")
CURRENT = ("claimed", "active", "delivered", "merged")
ACTIVE = ("claimed", "active")
BATCH = 500


class Conflict(Exception):
    """A compare-and-set missed or a unique index refused the write: someone else changed the row first."""


def encode(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


def decoded(row, columns):
    if row is None:
        return None
    record = dict(row)
    for column in columns:
        if record.get(column) is not None:
            record[column] = json.loads(record[column])
    return record


class Repo(Protocol):
    def transaction(self) -> ContextManager[None]: ...
    def project(self, name: str) -> dict | None: ...
    def project_by_id(self, project_id: str) -> dict | None: ...
    def ensure_project(self, name: str) -> dict: ...
    def update_project(self, name: str, **fields) -> None: ...
    def task(self, project_id: str, task_id: str) -> dict | None: ...
    def task_by_pk(self, pk: int) -> dict | None: ...
    def tasks(self, project_id: str, statuses: Iterable[str] | None = None) -> list[dict]: ...
    def next_seq(self, project_id: str) -> int: ...
    def insert_task(self, project_id: str, fields: dict) -> int: ...
    def update_task(self, pk: int, rev: int, fields: dict) -> int: ...
    def delete_tasks(self, project_id: str) -> int: ...
    def set_deps(self, pk: int, deps: list[str]) -> None: ...
    def set_paths(self, pk: int, kind: str, paths: list[str]) -> None: ...
    def add_observed(self, pk: int, attempt_id: str, paths: list[str]) -> None: ...
    def drop_observed(self, pk: int) -> None: ...
    def attempts(self, pk: int) -> list[dict]: ...
    def attempt(self, attempt_id: str) -> dict | None: ...
    def project_attempts(self, project_id: str) -> dict[int, list[dict]]: ...
    def active_attempts(self, project_id: str) -> list[dict]: ...
    def attempts_at(self, worktree: str) -> list[dict]: ...
    def insert_attempt(self, fields: dict) -> str: ...
    def update_attempt(self, attempt_id: str, fields: dict, phases: Iterable[str] | None = None) -> None: ...
    def add_note(self, pk: int, author: str, kind: str, text: str, time: str, attempt_id: str | None = None,
                 answered: str | None = None) -> int: ...
    def notes(self, pk: int) -> list[dict]: ...
    def answer_questions(self, pk: int, time: str) -> int: ...
    def add_event(self, pk: int, actor: str, kind: str, detail: dict, time: str | None = None,
                  attempt_id: str | None = None) -> int: ...
    def events(self, pk: int) -> list[dict]: ...
    def history(self, project_id: str) -> list[dict]: ...
    def queue_event(self, project: str, kind: str, task: str, agent: str | None = None, detail: str = "",
                    agent_id: str | None = None) -> None: ...
    def delivery(self, project: str, task: str, agent: str | None, handoff: str | None, result: str,
                 agent_id: str | None, source: str) -> None: ...
    def add_credential(self, agent_id: str, run_id: str, secret_hash: str) -> None: ...
    def credential_hashes(self, agent_id: str) -> list[str]: ...
    def revoke_credential(self, agent_id: str, run_id: str) -> bool: ...


class SqliteRepo:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.contextmanager
    def transaction(self):
        with store.transaction(self.conn):
            yield

    def project(self, name):
        return decoded(self.conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone(), PROJECT_JSON)

    def project_by_id(self, project_id):
        return decoded(self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone(), PROJECT_JSON)

    def ensure_project(self, name):
        with store.transaction(self.conn):
            store.touch_project(self.conn, name)
            self.conn.execute("UPDATE projects SET id = ? WHERE name = ? AND id IS NULL", (store.new_id(), name))
        return self.project(name)

    def update_project(self, name, **fields):
        unknown = set(fields) - set(PROJECT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown project field(s): {', '.join(sorted(unknown))}")
        values = [encode(value) if key in PROJECT_JSON else value for key, value in fields.items()]
        self.conn.execute(f"UPDATE projects SET {', '.join(f'{key} = ?' for key in fields)} WHERE name = ?",
                          [*values, name])

    def task_rows(self, sql, args):
        tasks = [decoded(row, TASK_JSON) for row in self.conn.execute(sql, args)]
        if not tasks:
            return tasks
        by_pk = {task["pk"]: task for task in tasks}
        for task in tasks:
            task.update(deps=[], owned=[], touches=[], observed=[])
        pks = list(by_pk)
        for start in range(0, len(pks), BATCH):
            batch = pks[start:start + BATCH]
            marks = ",".join("?" * len(batch))
            for row in self.conn.execute(f"SELECT task_pk, depends_on FROM task_deps WHERE task_pk IN ({marks}) "
                                         "ORDER BY task_pk, n", batch):
                by_pk[row[0]]["deps"].append(row[1])
            for row in self.conn.execute(f"SELECT task_pk, kind, path, source FROM task_paths WHERE task_pk IN ({marks}) "
                                         "ORDER BY task_pk, n", batch):
                by_pk[row[0]]["observed" if row[3] == "observed" else row[1]].append(row[2])
        return tasks

    def task(self, project_id, task_id):
        found = self.task_rows("SELECT * FROM tasks WHERE project_id = ? AND id = ?", (project_id, task_id))
        return found[0] if found else None

    def task_by_pk(self, pk):
        found = self.task_rows("SELECT * FROM tasks WHERE pk = ?", (pk,))
        return found[0] if found else None

    def tasks(self, project_id, statuses=None):
        sql, args = "SELECT * FROM tasks WHERE project_id = ?", [project_id]
        if statuses:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        return self.task_rows(sql + " ORDER BY seq", args)

    def next_seq(self, project_id):
        return (self.conn.execute("SELECT MAX(seq) FROM tasks WHERE project_id = ?", (project_id,)).fetchone()[0] or 0) + 1

    def task_values(self, fields):
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task field(s): {', '.join(sorted(unknown))}")
        return {key: encode(value) if key in TASK_JSON else value for key, value in fields.items()}

    def insert_task(self, project_id, fields):
        values = self.task_values(fields)
        text, epoch = store.stamp()
        values.update(project_id=project_id, updated=text, updated_epoch=epoch)
        cursor = self.conn.execute(f"INSERT INTO tasks ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                                   list(values.values()))
        return cursor.lastrowid

    def update_task(self, pk, rev, fields):
        """Write fields when the row is still at rev; returns the new rev, raises Conflict otherwise."""
        values = self.task_values(fields)
        text, epoch = store.stamp()
        values.update(updated=text, updated_epoch=epoch)
        sets = ", ".join(f"{key} = ?" for key in values)
        cursor = self.conn.execute(f"UPDATE tasks SET {sets}, rev = rev + 1 WHERE pk = ? AND rev = ?",
                                   [*values.values(), pk, rev])
        if cursor.rowcount != 1:
            raise Conflict(f"task row {pk} changed since revision {rev}")
        return rev + 1

    def delete_tasks(self, project_id):
        return self.conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,)).rowcount

    def set_deps(self, pk, deps):
        self.conn.execute("DELETE FROM task_deps WHERE task_pk = ?", (pk,))
        self.conn.executemany("INSERT INTO task_deps (task_pk, depends_on, n) VALUES (?, ?, ?)",
                              [(pk, dep, n) for n, dep in enumerate(deps)])

    def set_paths(self, pk, kind, paths):
        self.conn.execute("DELETE FROM task_paths WHERE task_pk = ? AND kind = ? AND source = 'declared'", (pk, kind))
        self.conn.executemany("INSERT INTO task_paths (task_pk, kind, path, n) VALUES (?, ?, ?, ?)",
                              [(pk, kind, path, n) for n, path in enumerate(paths)])

    def add_observed(self, pk, attempt_id, paths):
        start = self.conn.execute("SELECT COALESCE(MAX(n), -1) + 1 FROM task_paths WHERE task_pk = ?", (pk,)).fetchone()[0]
        self.conn.executemany("""INSERT OR IGNORE INTO task_paths (task_pk, kind, path, n, source, attempt_id)
                                 VALUES (?, 'touches', ?, ?, 'observed', ?)""",
                              [(pk, path, start + n, attempt_id) for n, path in enumerate(paths)])

    def drop_observed(self, pk):
        self.conn.execute("DELETE FROM task_paths WHERE task_pk = ? AND source = 'observed'", (pk,))

    def attempts(self, pk):
        return [decoded(row, ATTEMPT_JSON) for row in
                self.conn.execute("SELECT * FROM task_attempts WHERE task_pk = ? ORDER BY n", (pk,))]

    def attempt(self, attempt_id):
        return decoded(self.conn.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone(), ATTEMPT_JSON)

    def project_attempts(self, project_id):
        """Every attempt of the project's tasks, by task pk, each list ordered by n."""
        grouped = {}
        for row in self.conn.execute("""SELECT a.* FROM task_attempts a JOIN tasks t ON t.pk = a.task_pk
                                        WHERE t.project_id = ? ORDER BY a.task_pk, a.n""", (project_id,)):
            grouped.setdefault(row["task_pk"], []).append(decoded(row, ATTEMPT_JSON))
        return grouped

    def active_attempts(self, project_id):
        rows = self.conn.execute("""SELECT a.*, t.id AS task_id FROM task_attempts a JOIN tasks t ON t.pk = a.task_pk
                                    WHERE t.project_id = ? AND a.phase IN ('claimed', 'active') ORDER BY a.started""",
                                 (project_id,))
        return [decoded(row, ATTEMPT_JSON) for row in rows]

    def attempts_at(self, worktree):
        """Attempts of unfinished tasks (any project) whose worktree is the given directory, with the project's root."""
        rows = self.conn.execute("""SELECT a.*, t.id AS task_id, p.root AS project_root, p.tasks_root
                                    FROM task_attempts a JOIN tasks t ON t.pk = a.task_pk JOIN projects p ON p.id = t.project_id
                                    WHERE a.worktree = ? AND a.phase IN ('claimed', 'active', 'delivered', 'merged')
                                    AND t.status != 'completed'""", (worktree,))
        return [decoded(row, ATTEMPT_JSON) for row in rows]

    def insert_attempt(self, fields):
        unknown = set(fields) - set(ATTEMPT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown attempt field(s): {', '.join(sorted(unknown))}")
        values = {"id": store.new_id(), **{k: encode(v) if k in ATTEMPT_JSON else v for k, v in fields.items()}}
        try:
            self.conn.execute(f"INSERT INTO task_attempts ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                              list(values.values()))
        except sqlite3.IntegrityError as error:
            if "task_attempts" not in str(error):
                raise
            raise Conflict(f"an active attempt already exists for this task or agent ({error})") from error
        return values["id"]

    def update_attempt(self, attempt_id, fields, phases=None):
        unknown = set(fields) - set(ATTEMPT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown attempt field(s): {', '.join(sorted(unknown))}")
        sets = ", ".join(f"{key} = ?" for key in fields)
        sql, args = f"UPDATE task_attempts SET {sets} WHERE id = ?", [
            *(encode(v) if k in ATTEMPT_JSON else v for k, v in fields.items()), attempt_id]
        if phases:
            phases = list(phases)
            sql += f" AND phase IN ({','.join('?' * len(phases))})"
            args += phases
        try:
            cursor = self.conn.execute(sql, args)
        except sqlite3.IntegrityError as error:
            raise Conflict(f"an active attempt already exists for this task or agent ({error})") from error
        if cursor.rowcount != 1:
            raise Conflict(f"attempt {attempt_id} changed under this call (expected phase {', '.join(phases or ('any',))})")

    def add_note(self, pk, author, kind, text, time, attempt_id=None, answered=None):
        return self.conn.execute("""INSERT INTO task_notes (task_pk, attempt_id, author, kind, text, time, answered)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                 (pk, attempt_id, author, kind, text, time, answered)).lastrowid

    def notes(self, pk):
        return [dict(row) for row in self.conn.execute("SELECT * FROM task_notes WHERE task_pk = ? ORDER BY id", (pk,))]

    def answer_questions(self, pk, time):
        return self.conn.execute("""UPDATE task_notes SET answered = ? WHERE task_pk = ? AND kind = 'question'
                                    AND answered IS NULL""", (time, pk)).rowcount

    def add_event(self, pk, actor, kind, detail, time=None, attempt_id=None):
        text, epoch = store.stamp(time)
        return self.conn.execute("""INSERT INTO task_events (task_pk, attempt_id, time, epoch, actor, kind, detail)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                 (pk, attempt_id, text, epoch, actor, kind, encode(detail or {}))).lastrowid

    def events(self, pk):
        rows = self.conn.execute("SELECT * FROM task_events WHERE task_pk = ? ORDER BY id", (pk,))
        return [decoded(row, ("detail",)) for row in rows]

    def history(self, project_id):
        """Every task event and note of the project with its task id, ordered by (time, events before notes, id)."""
        events = self.conn.execute("""SELECT 'event' AS type, e.id, e.time, t.id AS task, e.kind, e.actor, e.detail
                                      FROM task_events e JOIN tasks t ON t.pk = e.task_pk WHERE t.project_id = ?""",
                                   (project_id,))
        notes = self.conn.execute("""SELECT 'note' AS type, n.id, n.time, t.id AS task, n.kind, n.author, n.text,
                                     n.answered FROM task_notes n JOIN tasks t ON t.pk = n.task_pk WHERE t.project_id = ?""",
                                  (project_id,))
        rows = [decoded(row, ("detail",)) for row in events] + [dict(row) for row in notes]
        return sorted(rows, key=lambda row: (row["time"], row["type"] != "event", row["id"]))

    def queue_event(self, project, kind, task, agent=None, detail="", agent_id=None):
        store.add_event(self.conn, project, kind, agent=agent, task=task, detail=detail, agent_id=agent_id)

    def delivery(self, project, task, agent, handoff, result, agent_id, source):
        store.add_delivery(self.conn, project, task, agent=agent, handoff=handoff, result=result, agent_id=agent_id,
                           source=source)

    def add_credential(self, agent_id, run_id, secret_hash):
        self.conn.execute("""INSERT OR REPLACE INTO agent_credentials (agent_id, run_id, secret_hash, issued)
                             VALUES (?, ?, ?, ?)""", (agent_id, run_id, secret_hash, store.iso()))

    def credential_hashes(self, agent_id):
        return [row[0] for row in self.conn.execute(
            "SELECT secret_hash FROM agent_credentials WHERE agent_id = ? AND revoked IS NULL", (agent_id,))]

    def revoke_credential(self, agent_id, run_id):
        return self.conn.execute("""UPDATE agent_credentials SET revoked = ? WHERE agent_id = ? AND run_id = ?
                                    AND revoked IS NULL""", (store.iso(), agent_id, run_id)).rowcount == 1


@contextlib.contextmanager
def opened(db=None):
    """A SqliteRepo over a fresh store connection, closed on exit."""
    with store.opened(db) as conn:
        yield SqliteRepo(conn)
