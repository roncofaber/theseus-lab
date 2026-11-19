"""Which free agent takes which ready task. Priority is a fixed 1 to 5 scale (5 first); a task that waits gains one
level per [queue] aging_hours up to the top, so old low-priority work is eventually taken. Capability decides who
takes what: a strong agent first takes the tasks only it can do and leaves easier ones to free weaker agents, unless
there are more of them than weaker agents to take them."""
import time

from . import store

PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 5, 3


def priority(task):
    """The task's priority on the 1 to 5 scale. Records from before the scale (0 or none, negative, above 5, text
    such as "P2") are read as the default or clamped to the nearest end."""
    value = task.get("priority")
    if value is None or value == "" or value == 0:
        return PRIORITY_DEFAULT
    try:
        number = int(value)
    except (TypeError, ValueError):
        return PRIORITY_DEFAULT
    return max(PRIORITY_MIN, min(PRIORITY_MAX, number))


def check_priority(value):
    """The priority as an int, or ValueError naming the scale."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"priority must be a whole number from {PRIORITY_MIN} to {PRIORITY_MAX}, not {value!r}")
    if not PRIORITY_MIN <= number <= PRIORITY_MAX:
        raise ValueError(f"priority must be from {PRIORITY_MIN} (last) to {PRIORITY_MAX} (first), not {number}")
    return number


def aging_hours(conf):
    try:
        return max(0.0, float(conf.section("queue").get("aging_hours") or 0))
    except (TypeError, ValueError):
        return 0.0


def waited(task, now=None):
    """Seconds since the task was created, 0 when unknown."""
    now = time.time() if now is None else now
    try:
        created = store.epoch_of(task.get("createdAt")) if task.get("createdAt") else None
    except (TypeError, ValueError):
        created = None
    return max(0.0, now - created) if created else 0.0


def effective(task, hours, now=None):
    """(effective priority, aging bonus): the priority plus one level per `hours` waited, capped at the top."""
    base = priority(task)
    bonus = int(waited(task, now) // (hours * 3600)) if hours else 0
    value = min(PRIORITY_MAX, base + bonus)
    return value, value - base


def need(task):
    try:
        return int(task.get("minCapability") or 0)
    except (TypeError, ValueError):
        return 0


def order_key(option):
    """Higher effective priority first, then the task that has waited longest, then task-file order."""
    return (-option["effective"], -option["waited"], option["order"])


def choose(me, options, rivals):
    """The option one agent of capability `me` takes, or None to leave the tasks to others this tick. options are
    {task, effective, waited, order, fitted} dicts of the ready tasks it fits, where fitted is the capabilities of the
    other free agents that also fit the task; rivals is the capabilities of every other free agent."""
    if not options:
        return None
    mine = [o for o in options if not o["fitted"]]
    if mine:
        return min(mine, key=order_key)
    weaker = sum(1 for c in rivals if c < me)
    open_to_weaker = [o for o in options if any(c < me for c in o["fitted"])]
    not_for_weaker = [o for o in options if not any(c < me for c in o["fitted"])]
    if not_for_weaker:
        return min(not_for_weaker, key=order_key)
    if len(open_to_weaker) > weaker:
        return min(open_to_weaker, key=lambda o: (-need(o["task"]),) + order_key(o))
    return None


def plan(free, tasks, fits, capability, hours, now=None):
    """[(agent, task, reset)] for this tick: free is [(agent, info)], tasks the ready task records in file order,
    fits(agent, info, task) -> (ok, reset), capability(agent) -> int. Strongest agents choose first; an agent that left
    its tasks to weaker ones gets a second turn for whatever they did not take."""
    now = time.time() if now is None else now
    caps = {id(agent): capability(agent) for agent, _ in free}
    fitting = {}
    for agent, info in free:
        for n, task in enumerate(tasks):
            ok, reset = fits(agent, info, task)
            if ok:
                fitting[(id(agent), task["id"])] = reset
    taken, done, chosen = set(), set(), []
    ranked = sorted(free, key=lambda pair: -caps[id(pair[0])])
    for final in (False, True):
        for agent, info in ranked:
            if id(agent) in done:
                continue
            others = [a for a, _ in free if id(a) != id(agent) and id(a) not in done]
            options = []
            for n, task in enumerate(tasks):
                if task["id"] in taken or (id(agent), task["id"]) not in fitting:
                    continue
                value, _ = effective(task, hours, now)
                fitted = [] if final else [caps[id(a)] for a in others if (id(a), task["id"]) in fitting]
                options.append({"task": task, "effective": value, "waited": waited(task, now), "order": n,
                                "fitted": fitted})
            pick = choose(caps[id(agent)], options, [] if final else [caps[id(a)] for a in others])
            if pick is None:
                continue
            task = pick["task"]
            taken.add(task["id"])
            done.add(id(agent))
            chosen.append((agent, task, fitting[(id(agent), task["id"])]))
    return chosen
