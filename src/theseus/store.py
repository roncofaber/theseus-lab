"""Host store: one SQLite database per machine for host-level and runtime state shared by every project.

The database lives at ~/.local/share/theseus/theseus.db (XDG_DATA_HOME is respected; THESEUS_DB overrides the path).
It holds the projects this host runs, coordinator sessions, registered agents, queue events, pool leases and claims,
deliveries and periodic agent-state samples. Task records stay in each project's tasks.json in Git; the optional task
index here is a read-only copy for cross-project queries. There is no daemon: every short-lived process opens the file
with connect(), and each write runs in its own BEGIN IMMEDIATE transaction so concurrent processes serialize on it.
"""
import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

BUSY_TIMEOUT = 30.0
CLAIM_TTL = 15 * 60
LEASE_TTL = 30 * 60

MIGRATIONS = [
    [
        """CREATE TABLE projects (
            name TEXT PRIMARY KEY,
            root TEXT,
            data TEXT,
            coordinator TEXT,
            last_seen TEXT,
            last_seen_epoch REAL)""",
        """CREATE TABLE coordinator_sessions (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            pane TEXT,
            profile TEXT,
            started TEXT NOT NULL,
            started_epoch REAL NOT NULL,
            ended TEXT,
            ended_epoch REAL,
            detail TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX coordinator_sessions_project ON coordinator_sessions(project, started_epoch)",
        """CREATE TABLE agents (
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            name TEXT NOT NULL,
            pane TEXT,
            profile TEXT,
            rooms TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            registered TEXT,
            source TEXT,
            PRIMARY KEY (project, name))""",
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            time TEXT NOT NULL,
            epoch REAL NOT NULL,
            kind TEXT NOT NULL,
            agent TEXT,
            task TEXT,
            detail TEXT NOT NULL DEFAULT '',
            origin TEXT)""",
        "CREATE INDEX events_project_epoch ON events(project, epoch)",
        "CREATE INDEX events_epoch ON events(epoch)",
        "CREATE UNIQUE INDEX events_origin ON events(project, origin) WHERE origin IS NOT NULL",
        """CREATE TABLE pool_leases (
            pool TEXT NOT NULL,
            project TEXT NOT NULL,
            agent TEXT NOT NULL,
            task TEXT,
            seen TEXT NOT NULL,
            seen_epoch REAL NOT NULL,
            until TEXT,
            until_epoch REAL,
            PRIMARY KEY (pool, project, agent))""",
        """CREATE TABLE pool_claims (
            pool TEXT NOT NULL,
            project TEXT NOT NULL,
            claimed TEXT NOT NULL,
            claimed_epoch REAL NOT NULL,
            PRIMARY KEY (pool, project))""",
        """CREATE TABLE deliveries (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            task TEXT NOT NULL,
            agent TEXT,
            handoff TEXT,
            result TEXT,
            time TEXT NOT NULL,
            epoch REAL NOT NULL,
            origin TEXT)""",
        "CREATE INDEX deliveries_project_epoch ON deliveries(project, epoch)",
        "CREATE UNIQUE INDEX deliveries_origin ON deliveries(project, origin) WHERE origin IS NOT NULL",
        """CREATE TABLE samples (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            agent TEXT NOT NULL,
            time TEXT NOT NULL,
            epoch REAL NOT NULL,
            state TEXT NOT NULL,
            task TEXT)""",
        "CREATE INDEX samples_project_epoch ON samples(project, epoch)",
        """CREATE TABLE task_index (
            project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
            id TEXT NOT NULL,
            status TEXT,
            priority INTEGER,
            room TEXT,
            milestone TEXT,
            updated TEXT,
            indexed TEXT NOT NULL,
            PRIMARY KEY (project, id))""",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def path():
    if os.environ.get("THESEUS_DB"):
        return Path(os.environ["THESEUS_DB"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "theseus" / "theseus.db"


def iso(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_of(value):
    """Seconds since the epoch from a number, an ISO 8601 string (Z or offset) or a datetime; None stays None."""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def stamp(value=None):
    """(ISO text, epoch) for a time given as a number, ISO string or datetime; now when None."""
    epoch = time.time() if value is None else epoch_of(value)
    return iso(epoch), epoch


def connect(db=None):
    """Open the host database with WAL, a busy timeout and foreign keys, migrating it to SCHEMA_VERSION."""
    target = Path(db) if db else path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000)}")
        conn.execute("PRAGMA foreign_keys = ON")
        set_wal(conn)
        conn.execute("PRAGMA synchronous = NORMAL")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def set_wal(conn):
    # Switching a fresh file to WAL takes an exclusive lock that does not always go through the busy handler.
    deadline = time.time() + BUSY_TIMEOUT
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or time.time() > deadline:
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def opened(db=None):
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
    if version(conn) >= SCHEMA_VERSION:
        return
    with transaction(conn):
        current = version(conn)
        for number, statements in enumerate(MIGRATIONS[current:], current + 1):
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")


def touch_project(conn, name):
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (name,))


def upsert_project(conn, name, root=None, data=None, coordinator=None, seen=None):
    """Record a project and mark it seen now; a field passed as None keeps its stored value."""
    text, epoch = stamp(seen)
    with transaction(conn):
        touch_project(conn, name)
        conn.execute("""UPDATE projects SET root = COALESCE(?, root), data = COALESCE(?, data),
                        coordinator = COALESCE(?, coordinator), last_seen = ?, last_seen_epoch = ? WHERE name = ?""",
                     (None if root is None else str(root), None if data is None else str(data), coordinator,
                      text, epoch, name))


def list_projects(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY name")]


def prune_projects(conn, missing_root=True, older_than=None):
    """Remove projects whose root directory no longer exists, or (with older_than, in seconds) that nobody saw for
    that long; their agents, events, deliveries, samples, sessions and task index go with them. Returns the names."""
    cutoff = time.time() - older_than if older_than is not None else None
    with transaction(conn):
        doomed = []
        for row in conn.execute("SELECT name, root, last_seen_epoch FROM projects").fetchall():
            gone = missing_root and row["root"] and not Path(row["root"]).exists()
            stale = cutoff is not None and (row["last_seen_epoch"] or 0) < cutoff
            if gone or stale:
                doomed.append(row["name"])
        conn.executemany("DELETE FROM projects WHERE name = ?", [(name,) for name in doomed])
    return doomed


def start_session(conn, project, pane=None, profile=None, detail="", started=None):
    text, epoch = stamp(started)
    with transaction(conn):
        touch_project(conn, project)
        cursor = conn.execute("""INSERT INTO coordinator_sessions (project, pane, profile, started, started_epoch, detail)
                                 VALUES (?, ?, ?, ?, ?, ?)""", (project, pane, profile, text, epoch, detail))
        if pane:
            conn.execute("UPDATE projects SET coordinator = ? WHERE name = ?", (pane, project))
    return cursor.lastrowid


def end_session(conn, session_id, ended=None):
    text, epoch = stamp(ended)
    with transaction(conn):
        cursor = conn.execute("UPDATE coordinator_sessions SET ended = ?, ended_epoch = ? WHERE id = ? AND ended IS NULL",
                              (text, epoch, session_id))
    return cursor.rowcount == 1


def sessions(conn, project=None, active=None):
    sql, args = "SELECT * FROM coordinator_sessions WHERE 1", []
    if project is not None:
        sql += " AND project = ?"
        args.append(project)
    if active is not None:
        sql += " AND ended IS NULL" if active else " AND ended IS NOT NULL"
    return [dict(row) for row in conn.execute(sql + " ORDER BY started_epoch, id", args)]


def agent_row(row):
    record = dict(row)
    record["rooms"] = json.loads(record["rooms"]) if record["rooms"] else None
    record["enabled"] = bool(record["enabled"])
    return record


def upsert_agent(conn, project, name, pane=None, profile=None, rooms=None, enabled=True, registered=None, source=None):
    with transaction(conn):
        touch_project(conn, project)
        conn.execute("""INSERT INTO agents (project, name, pane, profile, rooms, enabled, registered, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (project, name) DO UPDATE SET pane = excluded.pane, profile = excluded.profile,
                        rooms = excluded.rooms, enabled = excluded.enabled,
                        registered = COALESCE(agents.registered, excluded.registered),
                        source = COALESCE(excluded.source, agents.source)""",
                     (project, name, pane, profile, json.dumps(list(rooms)) if rooms else None, int(bool(enabled)),
                      registered or iso(), source))


def list_agents(conn, project=None, include_disabled=True):
    sql, args = "SELECT * FROM agents WHERE 1", []
    if project is not None:
        sql += " AND project = ?"
        args.append(project)
    if not include_disabled:
        sql += " AND enabled"
    return [agent_row(row) for row in conn.execute(sql + " ORDER BY project, rowid", args)]


def remove_agent(conn, project, name):
    with transaction(conn):
        return conn.execute("DELETE FROM agents WHERE project = ? AND name = ?", (project, name)).rowcount == 1


EVENT_COLUMNS = "id, project, time, epoch, kind, agent, task, detail"


def add_event(conn, project, kind, agent=None, task=None, detail="", at=None, origin=None):
    """Append an event and return it as a dict with its id (None when origin repeats an imported one)."""
    text, epoch = stamp(at)
    with transaction(conn):
        touch_project(conn, project)
        cursor = conn.execute("""INSERT OR IGNORE INTO events (project, time, epoch, kind, agent, task, detail, origin)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                              (project, text, epoch, kind, agent, task, detail or "", origin))
    event_id = cursor.lastrowid if cursor.rowcount else None
    return {"id": event_id, "project": project, "time": text, "epoch": epoch, "kind": kind, "agent": agent,
            "task": task, "detail": detail or ""}


def filters(project=None, since=None, until=None, kinds=None, after_id=None):
    clauses, args = [], []
    if project is not None:
        clauses.append("project = ?")
        args.append(project)
    if since is not None:
        clauses.append("epoch >= ?")
        args.append(epoch_of(since))
    if until is not None:
        clauses.append("epoch < ?")
        args.append(epoch_of(until))
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        clauses.append(f"kind IN ({', '.join('?' * len(kinds))})")
        args.extend(kinds)
    if after_id is not None:
        clauses.append("id > ?")
        args.append(after_id)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def events(conn, project=None, since=None, limit=None, kinds=None, until=None, after_id=None):
    """Events oldest first; with limit, the latest limit of them (still oldest first). after_id resumes a cursor."""
    where, args = filters(project, since, until, kinds, after_id)
    sql = f"SELECT {EVENT_COLUMNS} FROM events{where} ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in reversed(conn.execute(sql, args).fetchall())]


def event_bins(conn, project, since, bin_seconds, kinds=None, until=None):
    """Event counts in consecutive bins of bin_seconds starting at since and covering up to until (now by default):
    a dense list of (bin start epoch, count), empty bins included. project None counts every project."""
    start = epoch_of(since)
    end = time.time() if until is None else epoch_of(until)
    width = float(bin_seconds)
    count = max(0, int((end - start) // width) + (1 if (end - start) % width else 0))
    where, args = filters(project, start, end, kinds)
    rows = conn.execute(f"SELECT CAST((epoch - ?) / ? AS INTEGER) AS bin, COUNT(*) FROM events{where} GROUP BY bin",
                        [start, width, *args]).fetchall()
    totals = {row[0]: row[1] for row in rows}
    return [(start + n * width, totals.get(n, 0)) for n in range(count)]


def pool_spec(pool, spec):
    if spec is None:
        from . import pools
        spec = pools.definitions().get(pool, {})
    return int(spec.get("max") or 0), list(spec.get("priority") or [])


def expire(conn, pool, now):
    conn.execute("DELETE FROM pool_leases WHERE pool = ? AND COALESCE(until_epoch, seen_epoch + ?) <= ?",
                 (pool, LEASE_TTL, now))
    conn.execute("DELETE FROM pool_claims WHERE pool = ? AND ? - claimed_epoch >= ?", (pool, now, CLAIM_TTL))


@contextlib.contextmanager
def pool_locked(conn, pool):
    with transaction(conn):
        now = time.time()
        expire(conn, pool, now)
        yield now


def active_leases(conn, pool):
    return conn.execute("SELECT COUNT(*) FROM pool_leases WHERE pool = ?", (pool,)).fetchone()[0]


def yielding_to(conn, pool, project, priority):
    ahead = priority[: priority.index(project)] if project in priority else priority
    claimed = {row[0] for row in conn.execute("SELECT project FROM pool_claims WHERE pool = ?", (pool,))}
    return next((p for p in ahead if p in claimed), None)


def put_lease(conn, pool, project, agent, task, now, until=None, replace=True):
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    conn.execute(f"""{verb} INTO pool_leases (pool, project, agent, task, seen, seen_epoch, until, until_epoch)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (pool, project, agent, task, iso(now), now, iso(until) if until else None, until))


def claim(conn, pool, project, now):
    conn.execute("INSERT OR REPLACE INTO pool_claims (pool, project, claimed, claimed_epoch) VALUES (?, ?, ?, ?)",
                 (pool, project, iso(now), now))


def admit(conn, pool, project, limit, priority, now):
    waiting = yielding_to(conn, pool, project, priority)
    if waiting:
        return False, f"yielding to {waiting} in pool {pool}"
    active = active_leases(conn, pool)
    if limit and active >= limit:
        claim(conn, pool, project, now)
        return False, f"pool {pool} full ({active}/{limit})"
    return True, ""


def sync(conn, pool, project, held):
    """Replace this project's slots with the agents now working a task: {agent name: task id}. Manual holds stay."""
    with pool_locked(conn, pool) as now:
        conn.execute("DELETE FROM pool_leases WHERE pool = ? AND project = ? AND until_epoch IS NULL", (pool, project))
        for agent, task in held.items():
            put_lease(conn, pool, project, agent, task, now, replace=False)


def may_start(conn, pool, project, spec=None):
    """(True, "") when this project may start one more task in the pool, else (False, reason); a full pool records
    this project's claim. It takes no slot: acquire() checks and takes in one step."""
    limit, priority = pool_spec(pool, spec)
    with pool_locked(conn, pool) as now:
        return admit(conn, pool, project, limit, priority, now)


def acquire(conn, pool, project, agent, task, spec=None):
    """may_start and take in one transaction, so concurrent processes never push the pool past its max."""
    limit, priority = pool_spec(pool, spec)
    with pool_locked(conn, pool) as now:
        if conn.execute("SELECT 1 FROM pool_leases WHERE pool = ? AND project = ? AND agent = ?",
                        (pool, project, agent)).fetchone() is None:
            allowed, why = admit(conn, pool, project, limit, priority, now)
            if not allowed:
                return False, why
        put_lease(conn, pool, project, agent, task, now)
        return True, ""


def take(conn, pool, project, agent, task):
    with pool_locked(conn, pool) as now:
        put_lease(conn, pool, project, agent, task, now)


def hold(conn, pool, project, agent, task, hours=4.0, spec=None):
    """Hold a slot by hand for a worker no theseus queue tracks (it expires after hours unless released). Refused when
    the pool is full or a higher-priority project claims it; returns (ok, reason)."""
    limit, priority = pool_spec(pool, spec)
    with pool_locked(conn, pool) as now:
        waiting = yielding_to(conn, pool, project, priority)
        if waiting:
            return False, f"yielding to {waiting} in pool {pool}"
        held = conn.execute("SELECT 1 FROM pool_leases WHERE pool = ? AND project = ? AND agent = ?",
                            (pool, project, agent)).fetchone()
        active = active_leases(conn, pool)
        if held is None and limit and active >= limit:
            return False, f"pool {pool} full ({active}/{limit})"
        put_lease(conn, pool, project, agent, task, now, until=now + hours * 3600)
        return True, ""


def release(conn, pool, project, agent):
    with pool_locked(conn, pool):
        return conn.execute("DELETE FROM pool_leases WHERE pool = ? AND project = ? AND agent = ?",
                            (pool, project, agent)).rowcount == 1


def drop_claim(conn, pool, project):
    with pool_locked(conn, pool):
        conn.execute("DELETE FROM pool_claims WHERE pool = ? AND project = ?", (pool, project))


def status(conn, pool, spec=None):
    """The same shape as pools.status: leases keyed project/agent with seen (and until for holds) as epochs."""
    limit, priority = pool_spec(pool, spec)
    with pool_locked(conn, pool):
        rows = conn.execute("SELECT * FROM pool_leases WHERE pool = ? ORDER BY seen_epoch", (pool,)).fetchall()
        claims = [row[0] for row in conn.execute("SELECT project FROM pool_claims WHERE pool = ?", (pool,))]
    leases = {}
    by_project = {}
    for row in rows:
        lease = {"project": row["project"], "agent": row["agent"], "task": row["task"], "seen": row["seen_epoch"]}
        if row["until_epoch"] is not None:
            lease["until"] = row["until_epoch"]
        leases[f"{row['project']}/{row['agent']}"] = lease
        by_project[row["project"]] = by_project.get(row["project"], 0) + 1
    return {"pool": pool, "max": limit, "priority": priority, "active": len(leases), "projects": by_project,
            "claims": sorted(claims), "leases": leases}


def add_delivery(conn, project, task, agent=None, handoff=None, result=None, at=None, origin=None):
    text, epoch = stamp(at)
    with transaction(conn):
        touch_project(conn, project)
        cursor = conn.execute("""INSERT OR IGNORE INTO deliveries (project, task, agent, handoff, result, time, epoch, origin)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                              (project, task, agent, None if handoff is None else str(handoff), result, text, epoch,
                               origin))
    return cursor.lastrowid if cursor.rowcount else None


def deliveries(conn, project=None, since=None, until=None, task=None, limit=None):
    where, args = filters(project, since, until)
    if task is not None:
        where += (" AND" if where else " WHERE") + " task = ?"
        args.append(task)
    sql = f"SELECT id, project, task, agent, handoff, result, time, epoch FROM deliveries{where} ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in reversed(conn.execute(sql, args).fetchall())]


def add_sample(conn, project, agent, state, task=None, at=None):
    text, epoch = stamp(at)
    with transaction(conn):
        touch_project(conn, project)
        conn.execute("INSERT INTO samples (project, agent, time, epoch, state, task) VALUES (?, ?, ?, ?, ?, ?)",
                     (project, agent, text, epoch, state, task))


def samples(conn, project=None, agent=None, since=None, until=None):
    where, args = filters(project, since, until)
    if agent is not None:
        where += (" AND" if where else " WHERE") + " agent = ?"
        args.append(agent)
    sql = f"SELECT project, agent, time, epoch, state, task FROM samples{where} ORDER BY epoch, id"
    return [dict(row) for row in conn.execute(sql, args)]


def task_updated(task):
    execution = task.get("execution") or {}
    stamps = [task.get("createdAt"), execution.get("assignedAt"), execution.get("deliveredAt"),
              (task.get("merge") or {}).get("mergedAt"), (task.get("result") or {}).get("completedAt")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def index_tasks(conn, project, tasks):
    """Replace this project's rows in the task index with the given task records."""
    indexed = iso()
    with transaction(conn):
        touch_project(conn, project)
        conn.execute("DELETE FROM task_index WHERE project = ?", (project,))
        conn.executemany("""INSERT OR REPLACE INTO task_index (project, id, status, priority, room, milestone, updated, indexed)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         [(project, task["id"], task.get("status"), task.get("priority"), task.get("room"),
                           task.get("milestone"), task_updated(task), indexed) for task in tasks])


def task_index(conn, project=None, status=None):
    sql, args = "SELECT * FROM task_index WHERE 1", []
    if project is not None:
        sql += " AND project = ?"
        args.append(project)
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    return [dict(row) for row in conn.execute(sql + " ORDER BY project, id", args)]


DELIVERY_KINDS = ("delivered", "partial", "blocked")


def import_events(conn, project, events_file):
    """Copy a project's events.jsonl into the events table, keyed by line number so a rerun adds only new lines.
    Delivery events (delivered, partial, blocked with a task) also become deliveries. Returns the events added."""
    events_file = Path(events_file)
    if not events_file.exists():
        return 0
    added = 0
    with transaction(conn):
        touch_project(conn, project)
        for number, line in enumerate(events_file.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                text, epoch = stamp(record["time"])
            except (ValueError, KeyError, TypeError):
                continue
            origin = f"{events_file.name}:{number}"
            cursor = conn.execute("""INSERT OR IGNORE INTO events (project, time, epoch, kind, agent, task, detail, origin)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                  (project, text, epoch, record.get("kind") or "unknown", record.get("agent"),
                                   record.get("task"), record.get("detail") or "", origin))
            added += cursor.rowcount
            if record.get("kind") in DELIVERY_KINDS and record.get("task"):
                conn.execute("""INSERT OR IGNORE INTO deliveries (project, task, agent, result, time, epoch, origin)
                                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                             (project, record["task"], record.get("agent"), record["kind"], text, epoch, origin))
    return added


def import_agents(conn, project, registry_file):
    """Upsert every agent in a project's agents.json (provider becomes profile); agents missing from the file are
    left alone. Returns the number of agents read."""
    registry_file = Path(registry_file)
    if not registry_file.exists():
        return 0
    entries = json.loads(registry_file.read_text()).get("agents", [])
    for entry in entries:
        upsert_agent(conn, project, entry["name"], pane=entry.get("pane"), profile=entry.get("provider"),
                     rooms=entry.get("rooms"), enabled=entry.get("enabled", True), registered=entry.get("registered"),
                     source=entry.get("source"))
    return len(entries)


def import_pool_state(conn, directory=None):
    """Copy the leases and claims of every <pool>.json under the pool state directory (pools.state_dir() by default).
    Rows already in the database win, and entries that already expired are skipped. Returns the pool names read."""
    if directory is None:
        from . import pools
        directory = pools.state_dir()
    directory = Path(directory)
    names = []
    for state_file in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            state = json.loads(state_file.read_text())
        except ValueError:
            continue
        pool = state_file.stem
        names.append(pool)
        with transaction(conn):
            now = time.time()
            for lease_key, lease in (state.get("leases") or {}).items():
                project, _, agent = lease_key.partition("/")
                seen, until = lease.get("seen", 0), lease.get("until")
                if (until or seen + LEASE_TTL) <= now:
                    continue
                put_lease(conn, pool, lease.get("project") or project, lease.get("agent") or agent, lease.get("task"),
                          seen, until=until, replace=False)
            for project, claimed in (state.get("claims") or {}).items():
                if now - claimed < CLAIM_TTL:
                    conn.execute("""INSERT OR IGNORE INTO pool_claims (pool, project, claimed, claimed_epoch)
                                    VALUES (?, ?, ?, ?)""", (pool, project, iso(claimed), claimed))
    return names


def import_registry(conn, registry_file):
    """Copy a projects.json registry ({"projects": {root: {name, data, coordinator, last_seen}}}) into projects,
    keeping the later last_seen when the project is already known. Returns the project names read."""
    registry_file = Path(registry_file)
    if not registry_file.exists():
        return []
    entries = json.loads(registry_file.read_text()).get("projects") or {}
    with transaction(conn):
        for root, entry in entries.items():
            seen = entry.get("last_seen")
            text, epoch = stamp(seen) if seen is not None else (None, None)
            conn.execute("""INSERT INTO projects (name, root, data, coordinator, last_seen, last_seen_epoch)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (name) DO UPDATE SET root = COALESCE(projects.root, excluded.root),
                            data = COALESCE(projects.data, excluded.data),
                            coordinator = COALESCE(projects.coordinator, excluded.coordinator),
                            last_seen = CASE WHEN COALESCE(projects.last_seen_epoch, 0) >= COALESCE(excluded.last_seen_epoch, 0)
                                        THEN projects.last_seen ELSE excluded.last_seen END,
                            last_seen_epoch = MAX(COALESCE(projects.last_seen_epoch, 0), COALESCE(excluded.last_seen_epoch, 0))""",
                         (entry["name"], root, entry.get("data"), entry.get("coordinator"), text, epoch))
    return [entry["name"] for entry in entries.values()]


def import_project(conn, name, root, data):
    """Register a project and import its agents.json and queue/events.jsonl from its data directory."""
    data = Path(data)
    upsert_project(conn, name, root=root, data=data)
    return {"agents": import_agents(conn, name, data / "agents.json"),
            "events": import_events(conn, name, data / "queue" / "events.jsonl")}
