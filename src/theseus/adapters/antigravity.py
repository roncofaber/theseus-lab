"""The "antigravity-jsonl" transcript adapter (theseus.adapters): Antigravity CLI (agy) conversation records.

Each conversation lives at <sessions_dir>/<conversation-id>/.system_generated/logs/transcript.jsonl, where
<sessions_dir> defaults to ~/.gemini/antigravity-cli/brain (or $AGY_APP_DATA_DIR/brain). Each line is a JSON step
with type USER_INPUT, PLANNER_RESPONSE, GENERIC (tool result) or SYSTEM_MESSAGE. Reads start at most TAIL_BYTES
before the end of the file and continue incrementally from the previous byte offset.

agy takes no preassigned conversation id and has no status command, but a running agy holds
<app data>/presence/<conversation-id>.lock open, so live_session reads the id from the pane's processes."""
import json
import os
import re
import threading
from pathlib import Path

from .claude_code import clip, key_argument, one_line, tool_input

TAIL_BYTES = 512 * 1024
KEEP_ERRORS = 20
KEEP_ENTRIES = 500
READS = {}
LOCK = threading.Lock()
KEY_ARGUMENTS = ("Path", "CommandLine", "Query", "Url", "Target", "Prompt", "file_path", "command", "pattern", "path",
                 "url", "query", "skill", "description", "prompt", "subject")
USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
META_TAGS = re.compile(r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>", re.S)
HEADER_PREFIXES = ("Created At:", "Completed At:")
PRESENCE_LOCK = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock")


def brain_dir(conf=None, interactive=None):
    """The Antigravity brain directory holding per-conversation transcripts: the profile's sessions_dir, else
    [instances] sessions_dir, else $AGY_APP_DATA_DIR/brain, else ~/.gemini/antigravity-cli/brain."""
    configured = (interactive or {}).get("sessions_dir")
    if not configured and conf is not None:
        configured = conf.section("instances").get("sessions_dir")
    if configured:
        return Path(os.path.expanduser(str(configured)))
    variables = {**os.environ, **((interactive or {}).get("env") or {})}
    if variables.get("AGY_APP_DATA_DIR"):
        return Path(os.path.expanduser(variables["AGY_APP_DATA_DIR"])) / "brain"
    return Path(os.path.expanduser("~/.gemini/antigravity-cli/brain"))


def transcript_path(directory, session):
    return Path(os.path.expanduser(str(directory))) / str(session) / ".system_generated" / "logs" / "transcript.jsonl"


def user_text(text):
    text = str(text or "")
    match = USER_REQUEST.search(text)
    if match:
        return match.group(1).strip()
    return META_TAGS.sub("", text).strip()


def decode_args(args):
    """Unwrap JSON-encoded string values that Antigravity's transcript logger writes inside tool_calls[].args."""
    if not isinstance(args, dict):
        return args
    decoded = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                decoded[key] = parsed if isinstance(parsed, (str, int, float, bool, list, dict)) else value
                continue
            except ValueError:
                pass
        decoded[key] = value
    return decoded


def tool_summary(data, cwd=None):
    if not isinstance(data, dict):
        return ""
    value = next((data[k] for k in KEY_ARGUMENTS if isinstance(data.get(k), str) and data[k].strip()), None)
    if value is not None:
        base = str(cwd or "").rstrip("/")
        if base and value.startswith(base + "/"):
            value = value[len(base) + 1:]
        return one_line(value)
    return key_argument(data, cwd)


def result_summary(text):
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(HEADER_PREFIXES):
            return one_line(stripped)
    return ""


def entries_of(record):
    """Conversation entries of one Antigravity transcript step (see theseus.adapters). Thinking and system/meta
    steps are left out."""
    kind = record.get("type")
    source = record.get("source")
    status = record.get("status")
    time = record.get("created_at")
    if source == "SYSTEM" or kind == "SYSTEM_MESSAGE":
        return []
    if kind == "USER_INPUT":
        text = user_text(record.get("content"))
        return [{"kind": "user", "text": clip(text), "time": time}] if text else []
    if kind == "PLANNER_RESPONSE":
        found = []
        text = str(record.get("content") or "").strip()
        if text:
            found.append({"kind": "assistant", "text": clip(text), "time": time, "format": "markdown"})
        for call in record.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = decode_args(call.get("args"))
            found.append({"kind": "tool", "name": str(call.get("name") or "tool"), "time": time,
                          "summary": tool_summary(args), "text": clip(tool_input(args))})
        return found
    if kind == "GENERIC" and source == "MODEL" and status in ("DONE", "ERROR"):
        text = str(record.get("content") or "")
        return [{"kind": "result", "error": status == "ERROR", "summary": result_summary(text),
                 "text": clip(text), "time": time}]
    return []


def context_tokens(record):
    return sum(int(record.get(name) or 0) for name in ("input_tokens", "cache_read_tokens", "output_tokens"))


def consume(state, record):
    found = entries_of(record)
    if found:
        for number, entry in enumerate(found, state["total"] + 1):
            entry["n"] = number
        state["entries"] = (state["entries"] + found)[-KEEP_ENTRIES:]
        state["total"] += len(found)
    if record.get("type") != "PLANNER_RESPONSE":
        return
    if record.get("status") == "ERROR":
        text = str(record.get("content") or "error").strip()
        state["errors"] = (state["errors"] + [{"time": record.get("created_at"), "error": None,
                                               "text": clip(text)}])[-KEEP_ERRORS:]
        state["last"] = "error"
        return
    total = context_tokens(record)
    if total:
        state["tokens"] = total
    if record.get("model"):
        state["model"] = record["model"]
    state["last"] = "answer"


def read(path):
    """The transcript's state after reading what was appended since the last call (see theseus.adapters). Safe to
    call from several threads."""
    with LOCK:
        return read_locked(Path(path))


def read_locked(path):
    try:
        info = path.stat()
    except OSError:
        return None
    state = READS.get(str(path))
    if state is None or state["inode"] != info.st_ino or info.st_size < state["offset"]:
        start = max(0, info.st_size - TAIL_BYTES)
        state = {"inode": info.st_ino, "offset": start, "partial": start > 0, "tokens": None, "model": None,
                 "errors": [], "last": None, "entries": [], "total": 0, "key": f"{path}:{info.st_ino}"}
    state["updated"] = info.st_mtime
    if info.st_size > state["offset"]:
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                data = handle.read(info.st_size - state["offset"])
        except OSError:
            return None
        end = data.rfind(b"\n")
        if end >= 0:
            lines = data[:end + 1].splitlines()
            state["offset"] += end + 1
            if state.pop("partial", False):
                lines = lines[1:]
            for line in lines:
                try:
                    consume(state, json.loads(line))
                except (ValueError, AttributeError, TypeError):
                    continue
    READS[str(path)] = state
    return state


def session_file(conf, interactive, session, cwd):
    """The transcript.jsonl of a known Antigravity conversation id, or None."""
    if not session:
        return None
    path = transcript_path(brain_dir(conf, interactive), session)
    return path if path.is_file() else None


def session_state(conf, agent, interactive):
    """The transcript state of a supervised agent with a known conversation id; None otherwise."""
    if not (agent.get("managed") and agent.get("session_id")):
        return None
    path = transcript_path(brain_dir(conf, interactive), agent["session_id"])
    return read(path) if path.is_file() else None


def live_session(interactive, pids):
    """The conversation id whose presence lock one of pids holds open, or None."""
    presence = brain_dir(None, interactive).parent / "presence"
    for pid in sorted(pids):
        try:
            entries = list(os.scandir(f"/proc/{pid}/fd"))
        except OSError:
            continue
        for entry in entries:
            try:
                target = Path(os.readlink(entry))
            except OSError:
                continue
            match = PRESENCE_LOCK.fullmatch(target.name)
            if match and target.parent == presence:
                return match.group(1)
    return None
