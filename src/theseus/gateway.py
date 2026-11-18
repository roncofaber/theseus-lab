"""Gateway errors in persistent agent panes: classification, retries with backoff, fresh-context recovery and a host-wide
circuit breaker.

A pane is in gateway_error when it is idle and its bottom lines ([gateway] screen_lines) match one of the profile's
interactive.gateway_patterns (regular expressions), for Claude Code an `API Error: <status>` line, a Cloudflare
`Error 52x` title or the `Resend your message to try again` placeholder it shows while waiting after an error. The
status, retry_after, retryable, error_name and error_category fields are read from the JSON body when it survives the
pane width; an Anthropic-format error type (rate_limit_error, overloaded_error, ...) wins over the bare status. 429 and
every 5xx (529 and 530 included) are retryable; any other status, an explicit "retryable": false and a context-too-long
message are fatal. A 429 means a shared key is over quota, so its resends are also staggered across the pool host-wide
([gateway] stagger_seconds after the latest one already scheduled).

Retryable: the queue waits max(retry_after, backoff[attempt]) ([gateway] backoff, longer by slow_factor for
slow_codes such as a 530 tunnel error), then sends the [gateway] resend text through the verified send; once a
task has hit fresh_after_errors errors, or the pane's context hint is at least clear_tokens, it sends the profile's
clear command and a restart prompt instead. After attempts resends a `stalled` event wakes the coordinator. Fatal:
one `error` event and no resend. The retry episode lives in the host store (gateway_retries), so a restarted
coordinator resumes it, and every tick only compares timestamps.

Circuit breaker ([gateway] in the host config.toml): retryable errors from breaker_errors different agents on one
gateway pool (the profile's pool, else the profile name) within breaker_window_minutes, from any project on the host, or
one error with a status in strong_codes, pause new assignments and resends on that pool until max(retry_after, the first backoff) has passed.

Classification (classify): the status comes from `Request rejected (NNN)`, `API Error: NNN`, a Cloudflare title or the
JSON body; the kind from the error kind the agent's transcript adapter reports (theseus.adapters; the session record
is read before the pane, and profiles without an adapter use the pane alone), else from the status. A 429 is split into the key's parallel cap
(max_parallel_requests) and model capacity (tokens or requests per minute, with the model name); a 5xx is named by
Cloudflare's error_name or its status; a connection or server error mid-response is retryable without a status. Every
error is stored with a short excerpt whose API keys, key hashes and email addresses are redacted.

A key-level 429 (scope "key": the shared key's parallel cap, `Rate limit exceeded for api_key`, or a per-minute rate
limit of the account) is rate, never health: besides the staggered resend, the pool is paused for the first backoff
(no resends, nudges or new tasks on it, host-wide), its working cap is lowered by [gateway] parallel_cap_step and held
for parallel_cap_minutes, then raised one slot every cap_step_up_minutes (pools.status shows the cut), and while a cut
is in force the newest working agents of the pool beyond the cap get no input at all (theseus.pools.turn_plan). It never
counts toward the breaker or a profile's health. Model capacity 429s and 529s back off per agent and feed the breaker.

Model-level errors (any 5xx, a connection lost or server error mid-response) of a profile with a fallback
([workers.<name>] fallback in the host file) go to its health instead of the breaker (theseus.health): they can mark it
down and move its agents to the fallback.
"""
import re
import time

from . import agents as ag
from . import hostconfig
from . import profiles
from . import redaction
from . import store

DEFAULT_RESEND = ("The gateway error is over. Continue the current task from the state of your worktree and finish with "
                  "the handoff and your done line.")
FATAL_TEXT = re.compile(r"prompt is too long|context (?:length|window)[^.]{0,30}(?:exceeded|too long)|too many tokens|"
                        r"invalid_request_error", re.IGNORECASE)
PROGRESS_SECONDS = 120
STATUS = re.compile(r"API Error:\s*(?:Request rejected \()?(\d{3})\b")
TITLE = re.compile(r"\bError (\d{3})\b")
# Anthropic-format error types win over the bare status when both appear.
ERROR_TYPES = {"rate_limit_error": 429, "overloaded_error": 529, "authentication_error": 401, "permission_error": 403,
               "invalid_request_error": 400}


STATUS_NAMES = {400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found", 413: "too large",
                422: "unprocessable", 429: "rate limit", 500: "internal error", 502: "bad gateway", 503: "unavailable",
                504: "gateway timeout", 520: "origin error", 521: "origin down", 522: "connect timeout",
                523: "origin unreachable", 524: "origin timeout", 529: "overloaded", 530: "tunnel down"}
PARALLEL_CAP = "parallel cap"
KEY_LEVEL = re.compile(r"for\s*api[_ ]?key|exceeded your (?:[\w-]+ )?rate limit|max_parallel_requests", re.IGNORECASE)


def scrub(text):
    """text without API keys, key hashes or email addresses, its lines kept."""
    return redaction.redact(text)


def redact(text, limit=240):
    """A one-line excerpt of an error text without API keys, key hashes or email addresses."""
    return redaction.excerpt(text, limit)


def classify(text, kind=None):
    """What an API error text says: status, kind, subtype, a short label (`429 parallel cap`, `524 origin timeout`,
    `conn lost`), whether a resend may help and the redacted excerpt. kind is the transcript's error field when known."""
    text = str(text or "")
    found = STATUS.search(text) or TITLE.search(text) or re.search(r'"status"\s*:\s*(\d{3})', text)
    status = int(found.group(1)) if found else None
    error_type = field(r'"type"\s*:\s*"(\w+_error)"', text)
    if status is None and error_type in ERROR_TYPES:
        status = ERROR_TYPES[error_type]
    subtype = None
    if re.search(r"connection lost mid-response", text, re.IGNORECASE):
        kind, subtype, label = "connection_lost", "connection lost", "conn lost"
    elif re.search(r"server error mid-response", text, re.IGNORECASE):
        kind, subtype, label = kind or "server_error", "server error mid-response", "server error mid-response"
    elif status == 429 or kind == "rate_limit":
        model = field(r"[Mm]odel capacity reached for ([^\s,]+?)\.?(?:\s|$)", text)
        window = field(r"[Rr]ate limit type:\s*(\w+)", text)
        limit_type = field(r"[Ll]imit type:\s*(\w+)", text)
        if "max_parallel_requests" in text:
            subtype = PARALLEL_CAP
        elif model or re.search(r"Model [TR]PM", text):
            subtype = f"model {'RPM' if window == 'requests' else 'TPM'}" + (f" ({model})" if model else "")
        elif usage := field(r"hit your ([\w-]+) limit", text):
            subtype = f"{usage} usage limit"
        elif KEY_LEVEL.search(text):
            subtype = f"key rate ({limit_type})" if limit_type else "key rate"
        else:
            subtype = "rate limit"
        kind = kind or "rate_limit"
        label = f"{status or 429} {subtype}" if not subtype.endswith("usage limit") else subtype
    elif status is not None:
        name = field(r'"error_name"\s*:\s*"([^"]*)"', text)
        if status in STATUS_NAMES and status >= 500:
            name = None
        if status == 400 and re.search(r"context ?window|prompt is too long", text, re.IGNORECASE):
            name = "context window"
        elif status == 422 and "budget" in text.lower():
            name = "budget exceeded"
        subtype = name or STATUS_NAMES.get(status) or "error"
        subtype = subtype.replace("_", " ")
        kind = kind or ("overloaded" if status == 529 else "server_error" if status >= 500 else "client_error")
        label = f"{status} {subtype}"
    elif text.strip() and not re.search(r"Resend your message", text):
        kind, label = kind or "unknown", (kind or "unknown").replace("_", " ")
    else:
        kind, label = kind or "unknown", "unknown (resend marker)"
    retryable = not FATAL_TEXT.search(text) and (status is None or status == 429 or 500 <= status <= 599)
    return {"status": status, "kind": kind, "subtype": subtype, "label": label, "retryable": retryable,
            "scope": scope_of(status, kind, subtype), "excerpt": redact(text)}


def scope_of(status, kind, subtype):
    """key for a 429 of the shared key (rate, not health), model for a model-level failure, else None."""
    if subtype == PARALLEL_CAP or str(subtype or "").startswith("key rate"):
        return "key"
    if kind == "connection_lost" or (status is None and kind == "server_error") or (status is not None and 500 <= status <= 599):
        return "model"
    return None


def settings(conf):
    section = dict(conf.section("gateway"))
    backoff = section.get("backoff") or [120]
    section["backoff"] = [float(x) for x in (backoff if isinstance(backoff, list) else [backoff])]
    section["attempts"] = int(section.get("attempts", 3))
    section["slow_codes"] = [int(x) for x in section.get("slow_codes") or []]
    section["slow_factor"] = float(section.get("slow_factor") or 1)
    section["clear_tokens"] = int(section.get("clear_tokens") or 0)
    section["fresh_after_errors"] = int(section.get("fresh_after_errors") or 0)
    section["screen_lines"] = int(section.get("screen_lines") or 15)
    section["stagger_seconds"] = float(section.get("stagger_seconds") or 0)
    section["recovery_seconds"] = float(section.get("recovery_seconds") or 900)
    return section


def field(pattern, text, convert=str):
    found = re.findall(pattern, text)
    return convert(found[-1]) if found else None


def parse(lines):
    """What the last gateway error on these screen lines says: code, retry_after, retryable, error name and category,
    fatal text and a short signature. Pane rows are joined without separators so wrapped JSON reads whole."""
    joined = "".join(line.rstrip() for line in lines)
    statuses = list(STATUS.finditer(joined))
    start = statuses[-1].start() if statuses else 0
    tail = joined[start:]
    code = int(statuses[-1].group(1)) if statuses else field(TITLE, joined, int)
    if code is None:
        code = field(r'"status"\s*:\s*(\d{3})', tail, int)
    retryable = field(r'"retryable"\s*:\s*(true|false)', tail)
    error_type = field(r'"type"\s*:\s*"(\w+_error)"', tail)
    if error_type in ERROR_TYPES:
        code = ERROR_TYPES[error_type]
    typed = classify(tail if statuses else joined)
    if code is None:
        code = typed["status"]
    return {"code": code, "error_type": error_type, "kind": typed["kind"], "subtype": typed["subtype"],
            "scope": typed["scope"] if code == typed["status"] else scope_of(code, typed["kind"], None),
            "label": typed["label"] if code == typed["status"] else f"{code} {STATUS_NAMES.get(code, 'error')}",
            "excerpt": typed["excerpt"],
            "retry_after": field(r'"retry_after"\s*:\s*(\d+(?:\.\d+)?)', tail, float),
            "retryable": None if retryable is None else retryable == "true",
            "error_name": field(r'"error_name"\s*:\s*"([^"]*)"', tail),
            "error_category": field(r'"error_category"\s*:\s*"([^"]*)"', tail),
            "fatal_text": bool(FATAL_TEXT.search(tail)),
            "signature": redact(tail, 160)}


def fatal(info):
    code = info.get("code")
    if info.get("fatal_text") or info.get("retryable") is False:
        return True
    if code is None:
        return False
    return not (code == 429 or 500 <= code <= 599)


def label(info):
    return f"gateway {info.get('label') or info.get('code') or '?'}"


def detect(conf, profile, screen, transcript_error=None):
    """The gateway error on the bottom of the screen, or None: parse() plus the pattern that matched, whether the
    profile's gateway_ready marker is showing and whether the error is fatal. transcript_error, the newest API error
    the agent's transcript adapter read after its last answer ({"text", "error", "time"}), classifies the error
    when the screen shows one: it holds the whole message even when the pane shows only the resend marker."""
    interactive = profiles.interactive(conf, profile)
    patterns = interactive.get("gateway_patterns") or []
    if not patterns:
        return None
    lines = screen[-settings(conf)["screen_lines"]:]
    text = "\n".join(lines)
    matched = next((pattern for pattern in patterns if re.search(pattern, text)), None)
    if matched is None:
        return None
    ready = interactive.get("gateway_ready")
    ready = bool(ready and ready in text)
    # With the ready marker showing the newest error is current, so a long error body that starts above the bottom
    # window can be read from the whole screen; the last error on it wins.
    info = parse(screen if ready else lines)
    if transcript_error and transcript_error.get("text"):
        typed = classify(transcript_error["text"], transcript_error.get("error"))
        info.update({"code": typed["status"], "kind": typed["kind"], "subtype": typed["subtype"], "label": typed["label"],
                     "scope": typed["scope"],
                     "excerpt": typed["excerpt"], "source": "transcript",
                     "fatal_text": bool(FATAL_TEXT.search(transcript_error["text"]))})
        info["signature"] = info["signature"] or redact(typed["excerpt"], 160)
    info.update({"pattern": matched, "ready": ready, "fatal": fatal(info)})
    return info


def context_tokens(conf, profile, screen):
    """The token count of the profile's context hint (Claude Code: `/clear to save 142.4k tokens`), or None."""
    pattern = profiles.interactive(conf, profile).get("context_pattern")
    if not pattern:
        return None
    found = re.findall(pattern, "\n".join(screen))
    if not found:
        return None
    value = found[-1]
    number, unit = (value[0], value[1] if len(value) > 1 else "") if isinstance(value, tuple) else (value, "")
    try:
        return int(float(number) * (1000 if unit.lower() == "k" else 1))
    except ValueError:
        return None


def pool_key(conf, profile):
    try:
        return profiles.resolve(conf, profile).get("pool") or profile or profiles.default_name(conf)
    except profiles.ProfileError:
        return profile or "default"


def delay(conf, attempt, code=None, retry_after=None):
    """Seconds to wait before resend number attempt + 1."""
    section = settings(conf)
    schedule = section["backoff"]
    base = schedule[min(attempt, len(schedule) - 1)]
    if code in section["slow_codes"]:
        base *= section["slow_factor"]
    return max(base, float(retry_after or 0))


def stagger(conn, pool, now, spacing):
    """The earliest epoch a new rate-limited resend on this pool may take: spacing seconds after the latest resend
    already scheduled on it by any project, so a key over quota is not hit by every pane at once."""
    latest = max([row["next_epoch"] or 0 for row in store.gateway_waiting(conn, pool)] or [0])
    return max(now, latest + spacing) if latest else now


def paused_until(pool, now=None):
    now = time.time() if now is None else now
    with store.for_host() as conn:
        until = store.gateway_pool(conn, pool)["paused_until"]
    return until if until and until > now else None


def stale(conf, agent_name, info):
    """An error text still on screen from an episode the agent already recovered from (it was seen working after the
    resend) and no ready marker: not a new error."""
    if info.get("ready"):
        return False
    with store.for_project(conf) as conn:
        row = store.gateway_retry(conn, conf.name, agent_name)
    return bool(row and row["recovered"] and row["signature"] == info.get("signature"))


def breaker(conf, conn, pool, code, retry_after, now):
    """Pause the pool when the host saw enough retryable errors on it; returns (until, started, count) or None. Key-level
    429s and the model-level errors a profile's health took (theseus.health) do not count."""
    from . import health
    host = hostconfig.gateway()
    window = float(host["breaker_window_minutes"]) * 60
    taken = health.error_agents(conn, since=now - window)
    count = len({(e["project"], e["agent"]) for e in store.gateway_errors(conn, pool, since=now - window, retryable=True)
                 if scope_of(e.get("code"), e.get("kind"), e.get("subtype")) != "key"
                 and (e["project"], e["agent"]) not in taken})
    strong = code is not None and code in [int(x) for x in host.get("strong_codes") or []]
    if count < int(host["breaker_errors"]) and not strong:
        return None
    until = now + delay(conf, 0, code, retry_after)
    why = f"{count} agents hit retryable gateway errors in {host['breaker_window_minutes']} min" + (f", latest {code}" if code else "")
    if strong and count < int(host["breaker_errors"]):
        why = f"gateway {code}"
    return until, store.pause_gateway(conn, pool, until, why, now=now), why


def typed(error):
    return {"kind": error.get("kind"), "subtype": error.get("subtype"), "label": error.get("label"),
            "excerpt": error.get("excerpt")}


def cap_pool(conn, pool, now, why="a 429 parallel cap"):
    """Lower the pool's working cap host-wide after a key-level 429 ([gateway] parallel_cap_step slots, held for
    parallel_cap_minutes, then raised a slot every cap_step_up_minutes); returns the event detail, or '' for a pool
    without a configured cap."""
    from . import pools
    limit = int(pools.spec_of(pool).get("max") or 0)
    if not limit:
        return ""
    host = hostconfig.gateway()
    until = now + float(host["parallel_cap_minutes"]) * 60
    cut = store.cut_cap(conn, pool, int(host["parallel_cap_step"]), until - now, limit, now=now)
    step = store.cap_step_seconds()
    raise_text = f", then one slot every {step / 60:g} min" if step else ""
    return f"pool {pool} working cap {limit - cut}/{limit} held until {store.iso(until)}{raise_text} after {why}" if cut else ""


def prompt_file(conf, task):
    """A rendered task prompt in the task's log directory for a restart after /clear, or None when it cannot be made."""
    from pathlib import Path
    from . import tasks as t
    log_dir = (task.get("execution") or {}).get("logDir")
    if not log_dir:
        return None
    target = Path(log_dir) / "prompt.md"
    if target.exists():
        return target
    try:
        text = t.render_prompt(t.Queue(conf), task["id"], task.get("baseCommit") or "", task.get("worktree") or "", log_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n")
    except (OSError, KeyError, t.TaskError):
        return None
    return target


def restart_prompt(conf, task, reason):
    """The one message that restarts a task in a fresh context: the brief, the task and its record summary, the worktree,
    the task prompt, the partial report and notes that exist, and how to resume cheaply."""
    from pathlib import Path
    brief = conf.root / (conf.section("instances").get("brief") or "")
    log_dir = (task.get("execution") or {}).get("logDir")
    worktree = task.get("worktree")
    where = prompt_file(conf, task)
    text = (f"Read and follow {brief} first. Your context was cleared after {reason}; resume task {task['id']} in worktree "
            f"{worktree}. Objective: {' '.join(str(task.get('objective') or '').split())} Owned paths: "
            f"{', '.join(task.get('ownedPaths') or []) or '-'}. ")
    text += f"The task prompt is {where}. " if where else f"Read the task with `theseus tasks show {task['id']}`. "
    reports = [str(Path(worktree) / path) for path in task.get("ownedPaths") or []
               if worktree and path.endswith(".md") and (Path(worktree) / path).is_file()]
    if reports:
        text += f"Your partial report: {', '.join(reports)}. "
    notes = Path(log_dir) / "notes.md" if log_dir else None
    if notes and notes.is_file():
        text += f"Your notes: {notes}. "
    if log_dir:
        text += f"Log directory (handoff.md, changed.sha256, notes.md): {log_dir}. "
    return text + ("Continue the partial report or changes in place from the state of the worktree (check git status and "
                   "git diff first); do not start over or re-read what the report already records. Read large files in "
                   "line ranges, not whole, work in small steps and save progress to the report or notes as you go. "
                   "Finish with the handoff and your done line.")


def recover(conf, agent, task, fresh, reason, text=None):
    """Get an idle agent going again: a plain resend of text, or (fresh) the profile's clear command followed by the one
    restart prompt the agent is then owed (workqueue.owe_prompt). Every keystroke goes through the verified send; inside
    a queue tick the restart prompt follows once the clear command is confirmed. Returns (ok, detail)."""
    from . import workqueue
    profile = workqueue.launched(conf, agent)
    task_id = task["id"] if task else None
    if fresh and task:
        clear = profiles.interactive(conf, profile).get("clear")
        if clear:
            ok, detail = ag.send_verified(conf, agent, clear, task_id, expect_busy=False, profile=profile, kind="clear")
            if not ok:
                return False, f"{clear}: {detail}"
        workqueue.owe_prompt(conf, agent["name"], task_id, reason)
        if clear and ag.batch() is not None:
            return True, f"fresh context ({clear}, then the restart prompt once it is confirmed)"
        if clear:
            timeout, poll = ag.send_settings(conf)
            deadline = ag.clock.monotonic() + timeout
            while ag.pane_busy(conf, agent["pane"], profile, server=ag.where(agent)[0]) and ag.clock.monotonic() < deadline:
                ag.clock.sleep(poll)
        status, detail = workqueue.deliver_owed(conf, agent)
        return status in ("sent", "typed"), f"fresh context ({clear or 'no clear command'} and a restart prompt): {detail}"
    kind = "nudge" if text else "resend"
    ok, detail = ag.send_verified(conf, agent, text or settings(conf).get("resend") or DEFAULT_RESEND, task_id,
                                  profile=profile, kind=kind)
    return ok, detail


def handle(conf, agent, info, now=None):
    """Advance one agent's gateway episode for this tick. Returns the events it recorded."""
    from . import health
    from . import pools
    from . import tasks as t
    from . import workqueue
    now = time.time() if now is None else now
    name, task_id, error = agent["name"], info["task"], info["gateway"]
    section = settings(conf)
    attempts = section["attempts"]
    profile = workqueue.launched(conf, agent)
    pool = pool_key(conf, profile)
    code = error.get("code")
    tag = label(error)
    with store.for_project(conf) as conn:
        row = store.gateway_retry(conn, conf.name, name)
        if row and row["task"] != task_id:
            store.drop_gateway_retry(conn, conf.name, name)
            row = None
    phase = row["phase"] if row else "idle"
    if error["fatal"]:
        if phase == "fatal" and row["code"] == code:
            return []
        with store.for_project(conf) as conn:
            store.add_gateway_error(conn, pool, conf.name, code, False, agent=name, task=task_id, at=now, **typed(error))
            store.put_gateway_retry(conn, conf.name, name, now=now, task=task_id, pool=pool, code=code, phase="fatal",
                                    signature=error["signature"], recovered=0)
        what = error.get("error_name") or ("context too long" if error.get("fatal_text") else "not retryable")
        return [workqueue.event(conf, "error", name, task_id, f"{tag} fatal ({what}): not resent")]
    if phase == "exhausted":
        return []
    if phase == "waiting":
        if now < (row["next_epoch"] or 0) or paused_until(pool, now) or pools.turn_held(conf.name, pool, name, now):
            return []
        attempt, errors = row["attempt"], row["errors"]
        tokens = info.get("contextTokens") or 0
        fresh = ((section["fresh_after_errors"] and errors >= section["fresh_after_errors"])
                 or (section["clear_tokens"] and tokens >= section["clear_tokens"]))
        task = t.Queue(conf).get(task_id)
        ok, detail = recover(conf, agent, task, fresh, tag)
        with store.for_project(conf) as conn:
            store.put_gateway_retry(conn, conf.name, name, now=now, phase="resent", attempt=attempt + 1, resent_epoch=now)
        if not ok:
            return [workqueue.events(conf, 1)[-1]]
        how = "fresh context" if fresh else "continue"
        return [workqueue.event(conf, "resent", name, task_id, f"{how} after {tag}, attempt {attempt + 1}/{attempts}: {detail}")]
    attempt = row["attempt"] if row and not row["recovered"] else 0
    errors = (row["errors"] if row else 0) + 1
    if attempt >= attempts:
        with store.for_project(conf) as conn:
            store.put_gateway_retry(conn, conf.name, name, now=now, phase="exhausted", code=code, errors=errors)
        return [workqueue.event(conf, "stalled", name, task_id, f"{tag} x{attempt}: still failing after {attempt} resends")]
    wait = delay(conf, attempt, code, error.get("retry_after"))
    recorded = []
    scope = error.get("scope") or scope_of(code, error.get("kind"), error.get("subtype"))
    key_level = scope == "key"
    to_health = scope == "model" and health.has_fallback(profile)
    strong = code is not None and code in [int(x) for x in hostconfig.gateway().get("strong_codes") or []]
    with store.for_project(conf) as conn:
        if code == 429 and section["stagger_seconds"]:
            wait = max(wait, stagger(conn, pool, now, section["stagger_seconds"]) - now)
        store.add_gateway_error(conn, pool, conf.name, code, True, agent=name, task=task_id,
                                retry_after=error.get("retry_after"), at=now, **typed(error))
        cut = cap_pool(conn, pool, now, f"a key-level {error.get('label') or '429'}") if key_level else ""
        paused = None
        if key_level:
            until = now + delay(conf, 0, code, error.get("retry_after"))
            if store.pause_gateway(conn, pool, until, f"key-level {error.get('label') or '429'}: turns paused", now=now):
                paused = (until, True, f"key-level {error.get('label') or '429'}; resends resume oldest task first")
        store.put_gateway_retry(conn, conf.name, name, now=now, task=task_id, pool=pool, code=code, phase="waiting",
                                attempt=attempt, errors=errors, retry_after=error.get("retry_after"),
                                next_epoch=now + wait, signature=error["signature"], recovered=0)
    down = health.record_error(conf.name, profile, name, error.get("label") or str(code or "?"), now=now) if to_health else None
    with store.for_project(conf) as conn:
        tripped = paused if key_level else (None if to_health and not strong else
                                            breaker(conf, conn, pool, code, error.get("retry_after"), now))
    after = f", retry_after {error['retry_after']:g}s" if error.get("retry_after") else ""
    recorded.append(workqueue.event(conf, "gateway_error", name, task_id,
                                    f"code {code or '?'} attempt {attempt + 1}/{attempts}, next retry at "
                                    f"{store.iso(now + wait)} (in {wait:g}s{after}) · {error.get('label') or code or '?'}"
                                    + (f"; profile {profile} marked down: {down}" if down else "")))
    if cut:
        recorded.append(workqueue.event(conf, "gateway_capped", name, task_id, cut))
    if tripped and tripped[1]:
        until, _, why = tripped
        recorded.append(workqueue.event(conf, "gateway_paused", name, task_id,
                                        f"pool {pool} paused until {store.iso(until)}: {why}"))
    return recorded


def observe(conf, agent, info, now=None):
    """Bookkeeping for an agent seen busy. Busy alone is no success: a request that ends in a 524 shows the busy marker
    for minutes first. Progress is a change in the worktree or log directory after the resend, or recovery_seconds of
    work since it; only then does the episode count as recovered (its attempts start over) and the pool record a
    success. A waiting episode whose pane got going without the queue's resend keeps its attempts and gets no resend."""
    from . import workqueue
    now = time.time() if now is None else now
    pool = pool_key(conf, workqueue.launched(conf, agent))
    changed = info.get("lastChange")
    with store.for_project(conf) as conn:
        row = store.gateway_retry(conn, conf.name, agent["name"])
        if row and row["task"] == info.get("task"):
            since = row["resent_epoch"] or 0
            progress = (changed and changed > since) or now - since >= settings(conf)["recovery_seconds"]
            if row["phase"] == "waiting":
                store.put_gateway_retry(conn, conf.name, agent["name"], now=now, phase="resent", resent_epoch=now)
            elif row["phase"] == "resent" and progress:
                store.put_gateway_retry(conn, conf.name, agent["name"], now=now, phase="recovered", recovered=1)
        if changed and now - changed <= PROGRESS_SECONDS:
            store.gateway_success(conn, pool, at=now)


def health(now=None, window=24 * 3600):
    """Per gateway pool on this host: recent error codes, last error, last success and the pause in force."""
    now = time.time() if now is None else now
    with store.for_host() as conn:
        pools = {row["pool"]: row for row in store.gateway_pools(conn)}
        errors = store.gateway_errors(conn, since=now - window)
        cuts = {name: store.cap_cut(conn, name, now) for name in pools}
    names = sorted(set(pools) | {e["pool"] for e in errors})
    found = []
    for name in names:
        mine = [e for e in errors if e["pool"] == name]
        row = pools.get(name) or {}
        until = row.get("paused_until")
        cut, cut_until = cuts.get(name, (0, None))
        found.append({"pool": name, "codes": [e["code"] for e in mine[-8:]], "errors": len(mine),
                      "labels": [e.get("label") or (str(e["code"]) if e["code"] else "unknown (resend marker)")
                                 for e in mine[-8:]],
                      "lastError": mine[-1]["epoch"] if mine else None, "lastSuccess": row.get("last_success"),
                      "pausedUntil": until if until and until > now else None, "reason": row.get("reason"),
                      "capCut": cut or 0, "capUntil": cut_until if cut else None})
    return [entry for entry in found if entry["errors"] or entry["pausedUntil"] or entry["capCut"]]


OUTCOMES = {"waiting": "retry", "resent": "resent", "recovered": "recovered", "exhausted": "stalled", "fatal": "fatal"}


def recent(now=None, window=24 * 3600, limit=40, attempts=None):
    """The newest gateway errors on the host, newest first: the stored error (typed label and redacted excerpt) with
    its attempt number for the task, the outcome (retry at next_epoch, resent, recovered, stalled, fatal) and the
    state of its pool (ok, paused until, cap cut until)."""
    now = time.time() if now is None else now
    attempts = attempts or {}
    with store.for_host() as conn:
        errors = store.gateway_errors(conn, since=now - window)
        retries = {(row["project"], row["agent"]): row for row in store.gateway_retries(conn)}
        pools = {row["pool"]: row for row in store.gateway_pools(conn)}
        cuts = {name: store.cap_cut(conn, name, now) for name in pools}
    counted = {}
    for error in errors:
        key = (error["project"], error["agent"], error["task"])
        counted[key] = counted.get(key, 0) + 1
        error["attempt"] = counted[key]
    found, newer = [], set()
    for error in reversed(errors[-limit * 4:]):
        key = (error["project"], error["agent"], error["task"])
        retry = retries.get(key[:2])
        next_epoch = None
        if not error["retryable"]:
            outcome = "fatal"
        elif key in newer:
            outcome = "resent"
        elif retry and retry["task"] == error["task"]:
            outcome = OUTCOMES.get(retry["phase"], retry["phase"])
            next_epoch = retry["next_epoch"] if retry["phase"] == "waiting" else None
        else:
            outcome = "-"
        newer.add(key)
        pool = pools.get(error["pool"]) or {}
        limit_attempts = int(attempts.get(error["project"]) or 3)
        found.append({**error, "attempt": min(error["attempt"], limit_attempts), "attempts": limit_attempts,
                      "outcome": outcome, "next": next_epoch,
                      "label": error.get("label") or (f"{error['code']} {STATUS_NAMES.get(error['code'], 'error')}"
                                                      if error["code"] else "unknown (resend marker)"),
                      "pausedUntil": pool.get("paused_until") if (pool.get("paused_until") or 0) > now else None,
                      "capCut": cuts.get(error["pool"], (0, None))[0],
                      "capUntil": cuts.get(error["pool"], (0, None))[1]})
        if len(found) >= limit:
            break
    return found
