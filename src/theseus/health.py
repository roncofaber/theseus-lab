"""Model health and profile fallback, shared by every project on the host.

A profile of the host configuration (~/.config/theseus/config.toml, never a project file) names its fallback and how its
health is checked:

    [workers.model-a]
    fallback = "model-b"

    [workers.model-a.health]
    probe = "probe-model model-a"   # exit 0 means healthy; leave it out for passive health
    probe_seconds = 600             # probe interval while the profile is down
    recover_after = 3               # consecutive OK probes before switching back

Health is host state in the store (meta health:<profile>), so every project sees one verdict. A profile goes down on
model-level failures only, never on a 429 (rate, not health): with a probe, a model-level error from one of its agents
(any 5xx, a connection lost or server error mid-response, theseus.gateway.scope_of) makes the next queue tick on the
host probe it at once (at most once a minute) and a failed probe marks it down; without a probe, errors from
down_errors different agents within down_window_minutes do. A probe whose output shows a 429 or a rate limit says
nothing either way.

While a profile is down and its fallback is not, it is on fallback: each project's supervisor works toward its
[agents.counts] with the profile's count added to the fallback's (supervisor.standing), so idle agents of the profile
stop at once and fallback agents start; a busy one finishes first, unless it sits on a gateway error, in which case the
queue releases its task with the worktree kept (workqueue.release_for_fallback) for a fallback agent to take. A task
whose profile is on fallback may run on the fallback, which also takes over the profile's capacity. Once on fallback it
stays there even if the fallback fails too, so a gateway-wide outage does not flip agents back and forth.

Recovery: one process on the host probes the down profile every probe_seconds and after recover_after consecutive OK
probes marks it up; without a probe it is retried after recover_after x probe_seconds. Agents then drain back the same
way. Every switch is a `fallback` event in each project that uses either profile, which wakes `theseus queue wait`
once.
"""
import copy
import json
import os
import re
import shlex
import subprocess
import time

from . import hostconfig
from . import redaction
from . import store

DEFAULTS = {"probe": None, "probe_seconds": 600, "recover_after": 3, "down_errors": 2, "down_window_minutes": 10,
            "probe_timeout": 60}
PROBE_AFTER_ERROR = 60
INCONCLUSIVE = re.compile(r"\b429\b|rate.?limit", re.IGNORECASE)
STATE = {"state": "up", "since": None, "reason": "", "oks": 0, "next_probe": None, "last_probe": None,
         "on_fallback": False, "fallback": None, "switches": 0, "switched_at": None, "switch_reason": "", "errors": []}


def key(profile):
    return f"health:{profile}"


def host_profiles():
    try:
        workers = hostconfig.read().get("workers") or {}
    except hostconfig.ConfigError:
        return {}
    return {name: table for name, table in workers.items() if isinstance(table, dict)}


def spec(profile):
    """The health settings of a profile from the host file (fallback, then DEFAULTS over its health table), or None
    when the profile names neither a fallback nor a health table."""
    table = host_profiles().get(profile) or {}
    if not table.get("fallback") and not isinstance(table.get("health"), dict):
        return None
    found = {**DEFAULTS, **(table.get("health") if isinstance(table.get("health"), dict) else {})}
    found["fallback"] = str(table["fallback"]) if table.get("fallback") else None
    for name in ("probe_seconds", "down_window_minutes", "probe_timeout"):
        found[name] = float(found[name])
    for name in ("recover_after", "down_errors"):
        found[name] = max(1, int(found[name]))
    return found


def specs():
    return {name: found for name in host_profiles() if (found := spec(name))}


def has_fallback(profile):
    found = spec(profile) if profile else None
    return bool(found and found["fallback"])


def read(conn, profile):
    raw = store.get_meta(conn, key(profile))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {**copy.deepcopy(STATE), **(data if isinstance(data, dict) else {})}


def write(conn, profile, data):
    store.set_meta(conn, key(profile), json.dumps(data, sort_keys=True))
    _cache.clear()


_cache = {}
CACHE_SECONDS = 1.0


def state(profile):
    with store.for_host() as conn:
        return read(conn, profile)


def mark_down(data, reason, now, found):
    data.update(state="down", since=now, reason=reason, oks=0,
                next_probe=now + found["probe_seconds"] if found["probe"] else None)


def mark_up(data, reason, now):
    data.update(state="up", since=now, reason=reason, oks=0, next_probe=None, errors=[])


def record_error(project, profile, agent, label, now=None):
    """A model-level error of an agent running profile. Returns the reason when it marks the profile down."""
    now = time.time() if now is None else now
    found = spec(profile)
    if found is None:
        return None
    window = found["down_window_minutes"] * 60
    reason = None
    with store.for_host() as conn, store.transaction(conn):
        data = read(conn, profile)
        data["errors"] = [e for e in data["errors"] if e.get("epoch", 0) > now - window][-50:]
        data["errors"].append({"project": project, "agent": agent, "epoch": now, "label": label})
        if data["state"] == "up":
            if found["probe"]:
                last = (data.get("last_probe") or {}).get("epoch") or 0
                if data["next_probe"] is None and now - last >= PROBE_AFTER_ERROR:
                    data["next_probe"] = now
            else:
                agents = {(e["project"], e["agent"]) for e in data["errors"]}
                if len(agents) >= found["down_errors"]:
                    reason = f"{label} from {len(agents)} agents within {found['down_window_minutes']:g} min"
                    mark_down(data, reason, now, found)
        write(conn, profile, data)
    return reason


def error_agents(conn, since):
    """(project, agent) pairs whose model-level errors since an epoch went to a profile with a fallback, which the
    gateway breaker leaves out."""
    found = set()
    for profile, settings in specs().items():
        if settings["fallback"]:
            found |= {(e["project"], e["agent"]) for e in read(conn, profile)["errors"] if e.get("epoch", 0) >= since}
    return found


def run_probe(command, timeout):
    """(result, detail) of one probe run: ok (exit 0), fail, or inconclusive (rate limited, or the command cannot run).
    A string command is split like a shell would and a leading ~ of each word expanded."""
    argv = [str(a) for a in command] if isinstance(command, list) else shlex.split(str(command))
    argv = [os.path.expanduser(a) for a in argv]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "fail", f"no answer within {timeout:g}s"
    except OSError as error:
        return "inconclusive", f"cannot run the probe: {error}"
    lines = [line for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    detail = redaction.excerpt(lines[-1] if lines else f"exit {result.returncode}", 160)
    if result.returncode == 0:
        return "ok", detail
    if INCONCLUSIVE.search(result.stdout + result.stderr):
        return "inconclusive", detail
    return "fail", detail


def apply_probe(data, found, result, detail, now):
    data["last_probe"] = {"epoch": now, "result": result, "detail": detail}
    if data["state"] == "up":
        if result == "fail":
            mark_down(data, f"probe failed: {detail}", now, found)
        return
    if result == "ok":
        data["oks"] = int(data.get("oks") or 0) + 1
        if data["oks"] >= found["recover_after"]:
            mark_up(data, f"{data['oks']} consecutive OK probes", now)
            return
    elif result == "fail":
        data["oks"] = 0
    data["next_probe"] = now + found["probe_seconds"]


def probe_due(now=None, runner=None):
    """Run the probes that are due on this host. Each is claimed in one store transaction, so concurrent queues of
    other projects never run it twice, and run outside it. Returns [(profile, result, detail)]."""
    now = time.time() if now is None else now
    runner = runner or run_probe
    done = []
    for profile, found in specs().items():
        if not found["probe"]:
            continue
        with store.for_host() as conn, store.transaction(conn):
            data = read(conn, profile)
            due = data["next_probe"] is not None and data["next_probe"] <= now
            if due:
                data["next_probe"] = now + found["probe_seconds"] if data["state"] == "down" else None
                data["last_probe"] = {**(data.get("last_probe") or {}), "epoch": now}
                write(conn, profile, data)
        if not due:
            continue
        result, detail = runner(found["probe"], found["probe_timeout"])
        with store.for_host() as conn, store.transaction(conn):
            data = read(conn, profile)
            apply_probe(data, found, result, detail, now)
            write(conn, profile, data)
        done.append((profile, result, detail))
    return done


def evaluate(now=None):
    """Retry passive profiles that waited long enough and switch profiles onto or back from their fallback. Returns
    [(profile, fallback, "on" | "off", reason)]."""
    now = time.time() if now is None else now
    switched = []
    for profile, found in specs().items():
        with store.for_host() as conn, store.transaction(conn):
            data = read(conn, profile)
            before = json.dumps(data, sort_keys=True)
            if data["state"] == "down" and not found["probe"] and \
                    now >= (data["since"] or now) + found["probe_seconds"] * found["recover_after"]:
                mark_up(data, f"retried after {found['probe_seconds'] * found['recover_after'] / 60:g} min without a probe", now)
            if found["fallback"] and data["state"] == "down" and not data["on_fallback"]:
                if read(conn, found["fallback"])["state"] != "down" and found["fallback"] != profile:
                    data.update(on_fallback=True, fallback=found["fallback"], switches=int(data["switches"]) + 1,
                                switched_at=now, switch_reason=data["reason"])
                    switched.append((profile, found["fallback"], "on", data["reason"]))
            elif data["state"] == "up" and data["on_fallback"]:
                data.update(on_fallback=False, switches=int(data["switches"]) + 1, switched_at=now,
                            switch_reason=data["reason"])
                switched.append((profile, data["fallback"], "off", data["reason"]))
            if json.dumps(data, sort_keys=True) != before:
                write(conn, profile, data)
    return switched


def host_tick(now=None, runner=None):
    """Probes due and switches, once per queue tick of any project, before anything is typed."""
    now = time.time() if now is None else now
    probed = probe_due(now, runner)
    return probed, evaluate(now)


def fallbacks():
    """{profile: fallback} of the profiles on fallback now (read again at most once a second: the queue asks for every
    agent and task of a tick)."""
    cached = _cache.get("fallbacks")
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return dict(cached[1])
    found = {}
    configured = specs()
    if configured:
        with store.for_host() as conn:
            for profile in configured:
                data = read(conn, profile)
                if data["on_fallback"] and data["fallback"]:
                    found[profile] = data["fallback"]
    _cache["fallbacks"] = (time.monotonic(), found)
    return dict(found)


def substitute(profile, active=None):
    """The profile to run instead of profile: its fallback while on fallback, else itself (None stays None)."""
    if not profile:
        return profile
    active = fallbacks() if active is None else active
    return active.get(profile, profile)


def effective_counts(counts, active=None):
    """[agents.counts] with each profile on fallback moved onto its fallback, so a project keeps its number of agents."""
    active = fallbacks() if active is None else active
    found = dict(counts)
    for profile, fallback in active.items():
        count = found.get(profile, 0)
        if count:
            found[profile] = 0
            found[fallback] = found.get(fallback, 0) + count
    return found


def extra_capacity(conf, profile, active=None):
    """Capacity a fallback takes over from the profiles on fallback onto it (None when one of them is unlimited)."""
    from . import profiles
    active = fallbacks() if active is None else active
    extra = 0
    for primary, fallback in active.items():
        if fallback != profile or primary == profile:
            continue
        try:
            value = profiles.resolve(conf, primary).get("capacity")
        except profiles.ProfileError:
            continue
        if value is None:
            return None
        extra += int(value)
    return extra


def uses(conf, *names):
    from . import agents as ag
    from . import supervisor
    from . import workqueue
    wanted = set(names)
    try:
        if supervisor.managed(conf) and any(supervisor.counts(conf).get(name) for name in wanted):
            return True
        return any(workqueue.registered_profile(conf, a) in wanted for a in ag.agents(conf))
    except Exception:
        return False


def describe(profile, data, found=None):
    found = found or spec(profile) or DEFAULTS
    fallback = data.get("fallback")
    if data["on_fallback"]:
        until = (f"{int(found['recover_after'])} OK probes in a row" if found.get("probe")
                 else f"a retry {found['probe_seconds'] * found['recover_after'] / 60:g} min after it went down")
        return (f"{profile} is down ({data['switch_reason'] or data['reason']}); its agents move to {fallback} with each "
                f"project's counts kept, until {until}")
    return f"{profile} is healthy again ({data['switch_reason'] or data['reason']}); agents move back from {fallback}"


def notify(conf):
    """One `fallback` event in this project for each switch of a profile it uses that it has not seen yet."""
    from . import workqueue
    recorded = []
    for profile, found in specs().items():
        if not found["fallback"]:
            continue
        with store.for_host() as conn:
            data = read(conn, profile)
            marker = f"health.seen:{conf.name}:{profile}"
            seen = store.get_meta(conn, marker)
        if seen == str(data["switches"]) or not uses(conf, profile, data["fallback"] or found["fallback"]):
            continue
        with store.for_host() as conn:
            store.set_meta(conn, marker, str(data["switches"]))
        if seen is None and not data["on_fallback"]:
            continue
        recorded.append(workqueue.event(conf, "fallback", None, None, describe(profile, data, found)))
    return recorded


def wake_times(now=None):
    """Epochs when a probe falls due or a passive profile is retried."""
    now = time.time() if now is None else now
    found = []
    configured = specs()
    if not configured:
        return found
    with store.for_host() as conn:
        for profile, settings in configured.items():
            data = read(conn, profile)
            if data["next_probe"]:
                found.append(max(now, data["next_probe"]))
            elif data["state"] == "down" and not settings["probe"] and data["since"]:
                found.append(data["since"] + settings["probe_seconds"] * settings["recover_after"])
    return found


def snapshot(now=None):
    """Every profile with health settings: name, fallback, state, since, reason, onFallback, nextProbe, lastProbe."""
    now = time.time() if now is None else now
    found = []
    configured = specs()
    if not configured:
        return found
    with store.for_host() as conn:
        for profile, settings in sorted(configured.items()):
            data = read(conn, profile)
            found.append({"profile": profile, "fallback": data["fallback"] or settings["fallback"], "state": data["state"],
                          "since": data["since"], "reason": data["switch_reason"] if data["on_fallback"] else data["reason"],
                          "onFallback": bool(data["on_fallback"]), "nextProbe": data["next_probe"],
                          "lastProbe": data["last_probe"], "oks": data["oks"], "recoverAfter": settings["recover_after"],
                          "probe": bool(settings["probe"])})
    return found
