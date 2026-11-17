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
"""
import os
from pathlib import Path

from . import hostconfig, store

CLAIM_TTL = store.CLAIM_TTL
LEASE_TTL = store.LEASE_TTL


def config_path():
    """The deprecated pools.toml."""
    return hostconfig.pools_path()


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


def take(conf, pool, agent, task):
    with store.for_host() as conn:
        store.take(conn, pool, conf.name, agent, task)


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


def status(pool):
    with store.for_host() as conn:
        return store.status(conn, pool, spec=spec_of(pool))


def project_pools(conf):
    """The pools this project's registered agents use."""
    from . import agents as ag
    return sorted({p for p in (pool_of(conf, a.get("provider")) for a in ag.agents(conf)) if p})
