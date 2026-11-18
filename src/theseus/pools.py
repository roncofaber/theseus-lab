"""Host-wide worker pools: several projects share one provider limit, such as a model gateway that allows a fixed
number of concurrent agents.

The host configuration (~/.config/theseus/config.toml, see theseus.hostconfig) declares each pool:

    [pools.gateway]
    max = 4                               # agents working a task at once, across every project
    priority = ["laser-port", "game-port"] # earlier projects win when the pool is full

The older ~/.config/theseus/pools.toml (THESEUS_POOLS overrides the path) is still read, below config.toml, and
reported as deprecated. A profile joins a pool with `pool = "<name>"` in its [workers.<profile>] section. Only agents working a task hold a
slot; idle sessions cost nothing. The leases and claims live in the host store (theseus.store); every queue tick
refreshes its project's slots there and takes a new one with store.acquire(), which checks room and priority and takes
the slot in one transaction. A project that has ready work but finds the pool full records a claim; while a fresh claim
from a higher-priority project exists, lower-priority projects start no new tasks, so their running tasks finish and
the slots return. Slots a project stopped refreshing expire, so a stopped queue never holds the pool forever.

Turns: every tick also records each working agent of the project in the host store (meta turns:<pool>: when its task
was assigned and the context its transcript adapter last reported). turn_plan() orders the working agents of the pool,
host-wide, by assignment time and lets the oldest take turns while they fit both the working cap (only while a key-level
429 has cut it, theseus.gateway) and, when the pool sets tokens_per_minute, the token budget: each agent is estimated at
its context size times turns_per_minute (6 by default) tokens a minute. The newest working agents beyond either limit are
held: the queue types nothing into them (no resend, nudge or restart prompt) and starts no new task in the pool, until
the cap rises again one slot at a time or the estimate falls. The oldest working agent is never held.
"""
import json
import os
import time
from pathlib import Path

from . import hostconfig, store

CLAIM_TTL = store.CLAIM_TTL
LEASE_TTL = store.LEASE_TTL
TURNS_PER_MINUTE = 6.0


def state_dir():
    """The directory of the pre-store <pool>.json state files, read once by the store migration."""
    if os.environ.get("THESEUS_POOL_STATE"):
        return Path(os.environ["THESEUS_POOL_STATE"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "theseus" / "pools"


def definitions():
    """[pools.<name>] from config.toml over the deprecated pools.toml."""
    return hostconfig.pools()


def pool_of(conf, profile_name):
    from . import profiles
    try:
        name = profiles.resolve(conf, profile_name).get("pool")
    except profiles.ProfileError:
        return None
    return name if name and name in definitions() else None


def spec_of(pool):
    return definitions().get(pool, {})


def sync(conf, pool, held):
    """Replace this project's slots with the agents now working a task: {agent name: task id}. Manual holds stay."""
    with store.for_host() as conn:
        store.sync(conn, pool, conf.name, held)


def may_start(conf, pool):
    """(True, "") when this project may start one more task in the pool, else (False, reason); a full pool records
    this project's claim. It takes no slot; acquire() checks and takes in one step."""
    with store.for_host() as conn:
        return store.may_start(conn, pool, conf.name, spec=spec_of(pool))


def acquire(conf, pool, agent, task):
    """may_start and take atomically across processes; release() the slot if the assignment then fails."""
    with store.for_host() as conn:
        return store.acquire(conn, pool, conf.name, agent, task, spec=spec_of(pool))


def hold(project, pool, agent, task, hours=4.0):
    """Hold a slot by hand for a worker no theseus queue tracks (it expires after hours unless released). Refused when
    the pool is full or a higher-priority project claims it; returns (ok, reason)."""
    with store.for_host() as conn:
        return store.hold(conn, pool, project, agent, task, hours=hours, spec=spec_of(pool))


def release(project, pool, agent):
    with store.for_host() as conn:
        return store.release(conn, pool, project, agent)


def drop_claim(conf, pool):
    with store.for_host() as conn:
        store.drop_claim(conn, pool, conf.name)


def status(pool, now=None):
    with store.for_host() as conn:
        found = store.status(conn, pool, spec=spec_of(pool))
    plan = turn_plan(pool, now)
    found.update({"held": plan["held"], "estimate": plan["estimate"], "budget": plan["budget"]})
    return found


def turns_key(pool):
    return f"turns:{pool}"


def read_turns(conn, pool, now):
    try:
        data = json.loads(store.get_meta(conn, turns_key(pool)) or "{}")
    except ValueError:
        data = {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and now - float(v.get("seen") or 0) < LEASE_TTL}


def sync_turns(conf, pool, working, now=None):
    """Replace this project's entries in the pool's turn record: working is {agent: {"since": epoch the task was
    assigned, "tokens": context tokens or None}}; an entry with no tokens keeps the last known count."""
    now = time.time() if now is None else now
    prefix = f"{conf.name}/"
    with store.for_host() as conn, store.transaction(conn):
        data = read_turns(conn, pool, now)
        mine = {k: v for k, v in data.items() if k.startswith(prefix)}
        data = {k: v for k, v in data.items() if not k.startswith(prefix)}
        for agent, entry in working.items():
            name = prefix + agent
            tokens = entry.get("tokens") or (mine.get(name) or {}).get("tokens") or 0
            data[name] = {"since": float(entry.get("since") or now), "tokens": int(tokens), "seen": now}
        store.set_meta(conn, turns_key(pool), json.dumps(data, sort_keys=True))


def turn_plan(pool, now=None):
    """{"allowed", "held"} lists of project/agent names, oldest task first, with the token estimate of the allowed
    ones, the budget (None when unset), the working cap and the cut in force."""
    now = time.time() if now is None else now
    spec = spec_of(pool)
    with store.for_host() as conn:
        limit, _ = store.pool_spec(pool, spec, conn, now)
        cut, _ = store.cap_cut(conn, pool, now)
        data = read_turns(conn, pool, now)
    rate = float(spec.get("turns_per_minute") or TURNS_PER_MINUTE)
    budget = spec.get("tokens_per_minute")
    budget = float(budget) if budget else None
    allowed, held, total = [], [], 0.0
    for name, entry in sorted(data.items(), key=lambda item: (item[1].get("since") or 0, item[0])):
        estimate = float(entry.get("tokens") or 0) * rate
        over = (cut and limit and len(allowed) >= limit) or (budget and allowed and total + estimate > budget)
        if held or over:
            held.append(name)
        else:
            allowed.append(name)
            total += estimate
    return {"allowed": allowed, "held": held, "estimate": int(total), "budget": budget, "limit": limit, "cut": cut,
            "rate": rate}


def turn_held(project, pool, agent, now=None):
    """Whether the queue must type nothing into this agent now (turn_plan)."""
    if not pool or pool not in definitions():
        return False
    return f"{project}/{agent}" in turn_plan(pool, now)["held"]


def may_add_turn(pool, tokens, now=None):
    """(True, "") when one more agent may start working in the pool as far as turns go, else (False, reason): no agent
    is held and its estimate fits the token budget."""
    if not pool or pool not in definitions():
        return True, ""
    plan = turn_plan(pool, now)
    if plan["held"]:
        return False, f"pool {pool}: {len(plan['held'])} working agent(s) held ({', '.join(plan['held'][:3])})"
    if plan["budget"] and plan["allowed"] and plan["estimate"] + float(tokens or 0) * plan["rate"] > plan["budget"]:
        return False, (f"pool {pool} token budget: {plan['estimate']:,} of {int(plan['budget']):,} tokens/min estimated "
                       f"for {len(plan['allowed'])} working agent(s)")
    return True, ""


def wake_times(conf, now=None):
    """Epochs when the cut of one of this project's pools steps up."""
    now = time.time() if now is None else now
    found = []
    with store.for_host() as conn:
        for pool in {p for p in (pool_of(conf, a.get("provider")) for a in _agents(conf)) if p}:
            cut, until = store.cap_cut(conn, pool, now)
            if cut and until:
                found.append(until)
    return found


def _agents(conf):
    from . import agents as ag
    return ag.agents(conf)


def project_pools(conf):
    """The pools this project's registered agents use."""
    return sorted({p for p in (pool_of(conf, a.get("provider")) for a in _agents(conf)) if p})
