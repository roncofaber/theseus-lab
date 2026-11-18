"""Agent profiles: how theseus launches, resumes and drives a coding agent, so the workflow is not tied to one vendor.

A profile describes one agent CLI:
- headless: the argument template of a one-shot run (`theseus dispatch`); None means the coordinator launches the
  worker itself (a Claude Code subagent) and theseus only prepares the worktree and prompt;
- resume: the template that continues a run with a follow-up message (`theseus followup`, gateway retries);
- session: "preassign" when the CLI accepts a session ID chosen by theseus, "none" otherwise;
- env, model_flag, permission_mode: how the model and permissions are passed;
- retry: whether a retryable gateway error (Claude JSON output) resumes the run automatically;
- interactive: the persistent-instance behavior in a tmux pane (launch, resume, busy marker, stall patterns, gateway
  error patterns and the idle marker shown after one, the context-size hint, the input line and the key that empties it,
  extra environment, compact and clear commands, where session records live, whether the agent can message the
  coordinator) and, for agents theseus supervises itself (theseus.supervisor), the exit command, the flag that
  preassigns a session ID, a status command that lists sessions as JSON (Claude Code: `claude agents --json`), the
  screen text of a ready prompt, the startup dialogs that need a human and the placeholder the input line shows for a
  long paste; prompt_patterns (regular expressions) recognize a question on screen that needs a human, such as a
  permission prompt, which theseus never types into.

Placeholders: {command} {prompt} {message} {session} {model} {permission_mode} {keep} {agent}; an item "{model_args}" expands to
[model_flag, model] when a model is set and to nothing otherwise. Presets marked verified: false follow the CLI's
public help but were not exercised end to end; dispatch a trivial task and follow it up before relying on one.

extra_args: arguments appended to the headless, resume and persistent-instance command lines after the MCP flags, with
the same placeholders filled and a leading ~ expanded, for example a client settings file.

[workers.<name>] selects or overrides a profile: a section named after a preset adjusts it; any other name sets
`preset = "<preset>"` and overrides fields; `capacity`, `capability` and `model` apply to every profile. The section is the merge of
the host configuration and the project's files (theseus.config), so a project section that shares a host profile's
name overrides only the keys it sets.
"""
import copy
import json
import os
import re
import shlex
import shutil
from pathlib import Path

CLAUDE_INTERACTIVE = {
    "launch": "{command} --name {agent} --permission-mode {permission_mode}",
    "resume": "{launch} --resume {session}",
    "resume_missing": "No conversation found with session ID",
    "busy_pattern": "esc to interrupt",
    "stall_patterns": ["Autocompact is thrashing"],
    "gateway_patterns": [r"API Error: 5\d\d", r"\bError 52\d\b", r"API Error: 529|overloaded_error",
                         r"API Error: Request rejected \(\d{3}\)", r"API Error: (?:Connection lost|Server error) mid-response",
                         r"API Error: 429|429 Too Many Requests|rate_limit_error|[Rr]ate limit (reached|exceeded)",
                         r"API Error: 4\d\d", r"Resend your message to try again", r"[Pp]rompt is too long"],
    "gateway_ready": "Resend your message to try again",
    "context_pattern": r"/clear to save ([0-9.]+)(k?) tokens",
    "input_pattern": "^\\s*[>\u276f]\\s?(.*)$",
    "clear_input": "C-u",
    "env": {},
    "compact": "/compact Keep only: {keep}; drop finished task details.",
    "clear": "/clear",
    "sessions_dir": "~/.claude/sessions",
    "transcript": "claude-code-jsonl",
    "messaging": True,
    "exit": "/exit",
    "session_flag": "--session-id",
    "status_command": ["claude", "agents", "--json"],
    "ready_pattern": "",
    "startup_dialogs": ["Is this a project you created or one you trust", "Yes, I trust this folder",
                        "New MCP server found"],
    "paste_placeholder": r"\[Pasted text #\d+",
    "prompt_patterns": [r"[>\u276f]\s*1\.\s*Yes\b"],
}

PRESETS = {
    "claude-code": {
        "label": "Claude Code with the developer's own login or API key",
        "verified": True,
        "notes": "flags exercised through a Claude Code wrapper launcher; --model passing not exercised",
        "command": "claude",
        "headless": ["{command}", "-p", "{prompt}", "--session-id", "{session}", "--output-format", "json",
                     "--permission-mode", "{permission_mode}", "{model_args}"],
        "resume": ["{command}", "-p", "{message}", "--resume", "{session}", "--output-format", "json",
                   "--permission-mode", "{permission_mode}", "{model_args}"],
        "session": "preassign",
        "model_flag": "--model",
        "model": "",
        "context_limit": 200000,
        "permission_mode": "auto",
        "env": {},
        "retry": True,
        "capacity": 2,
        "interactive": CLAUDE_INTERACTIVE,
        "mcp_servers": None,
        "mcp_flags": ["--strict-mcp-config", "--mcp-config", "{mcp_config}"],
    },
    "claude-subagent": {
        "label": "Claude Code subagents launched by the coordinator session itself (Agent tool)",
        "verified": True,
        "command": "",
        "headless": None,
        "resume": None,
        "session": "none",
        "model_flag": "",
        "model": "",
        "permission_mode": "",
        "env": {},
        "retry": False,
        "capacity": 0,
        "interactive": None,
    },
    "codex": {
        "label": "OpenAI Codex CLI (codex exec)",
        "verified": False,
        "command": "codex",
        "headless": ["{command}", "exec", "--skip-git-repo-check", "-s", "workspace-write", "{model_args}", "{prompt}"],
        "resume": ["{command}", "exec", "resume", "--last", "{message}"],
        "session": "none",
        "model_flag": "-m",
        "model": "",
        "permission_mode": "",
        "env": {},
        "retry": False,
        "capacity": 2,
        "interactive": {"launch": "codex", "resume": "", "busy_pattern": "", "compact": "/compact", "clear": "/new",
                        "sessions_dir": "", "messaging": False,
                        "transcript": "pane-history"},
    },
    "opencode": {
        "label": "OpenCode (opencode run)",
        "verified": False,
        "command": "opencode",
        "headless": ["{command}", "run", "--format", "json", "--session", "{session}", "{model_args}", "{prompt}"],
        "resume": ["{command}", "run", "--format", "json", "--session", "{session}", "{model_args}", "{message}"],
        "session": "preassign",
        "model_flag": "--model",
        "model": "",
        "permission_mode": "",
        "env": {},
        "retry": False,
        "capacity": 2,
        "interactive": {"launch": "opencode", "resume": "", "busy_pattern": "", "compact": "/compact", "clear": "/new",
                        "sessions_dir": "", "messaging": False,
                        "transcript": "pane-history"},
    },
    "gemini-cli": {
        "label": "Google Gemini CLI (gemini -p)",
        "verified": False,
        "command": "gemini",
        "headless": ["{command}", "-p", "{prompt}", "{model_args}", "--yolo"],
        "resume": None,
        "session": "none",
        "model_flag": "-m",
        "model": "",
        "permission_mode": "",
        "env": {},
        "retry": False,
        "capacity": 2,
        "interactive": {"launch": "gemini", "resume": "", "busy_pattern": "", "compact": "/compress", "clear": "/clear",
                        "sessions_dir": "", "messaging": False,
                        "transcript": "pane-history"},
    },
}
ALIASES = {"claude": "claude-subagent"}
CONTEXT_LIMIT = 200000


def model_context_limit(conf, model):
    """The context window of a model id from [models] context (exact id, else the longest matching `prefix*`)."""
    table = (conf.section("models").get("context") or {}) if model else {}
    if model in table:
        return int(table[model])
    matches = [key for key in table if key.endswith("*") and model.startswith(key[:-1])]
    return int(table[max(matches, key=len)]) if matches else None
DEFAULT = "claude-code"
OVERRIDABLE = set(PRESETS["claude-code"]) | {"preset", "fallback", "pool", "mcp_servers", "mcp_flags", "extra_args",
                                             "capability", "health"}
CAPABILITY = 1
INTERACTIVE_KEYS = set(CLAUDE_INTERACTIVE) | {"launch", "resume"}
THESEUS_MCP = {"command": "theseus", "args": ["mcp"]}


class ProfileError(Exception):
    pass


def problems(name, section):
    """What is wrong with one [workers.<name>] table: unknown keys and badly typed extra_args."""
    found = [f"unknown key {key}; known: {', '.join(sorted(OVERRIDABLE))}" for key in section if key not in OVERRIDABLE]
    interactive = section.get("interactive")
    if isinstance(interactive, dict):
        found += [f"unknown key interactive.{key}; known: {', '.join(sorted(INTERACTIVE_KEYS))}"
                  for key in interactive if key not in INTERACTIVE_KEYS]
    extra = section.get("extra_args")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(item, str) for item in extra)):
        found.append("extra_args must be a list of strings")
    if isinstance(interactive, dict) and "transcript" in interactive:
        from . import adapters
        if interactive["transcript"] not in adapters.NAMES:
            found.append(f"interactive.transcript {interactive['transcript']!r} is not an adapter; known: "
                         f"{', '.join(adapters.NAMES)}")
    capability = section.get("capability")
    if capability is not None and (isinstance(capability, bool) or not isinstance(capability, int) or capability < 0):
        found.append("capability must be a non-negative integer")
    preset = section.get("preset")
    if preset is not None and preset not in PRESETS:
        found.append(f"preset {preset!r} is not a preset; presets: {', '.join(sorted(PRESETS))}")
    fallback = section.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not fallback or fallback == name):
        found.append("fallback must name another profile")
    health = section.get("health")
    if health is not None:
        from . import health as hl
        if not isinstance(health, dict):
            found.append(f"health must be a table [workers.{name}.health] with {', '.join(hl.DEFAULTS)}")
        else:
            found += [f"unknown key health.{key}; known: {', '.join(hl.DEFAULTS)}" for key in health if key not in hl.DEFAULTS]
            probe = health.get("probe")
            if probe is not None and not (isinstance(probe, str) and probe.strip()) and \
                    not (isinstance(probe, list) and probe and all(isinstance(item, str) for item in probe)):
                found.append("health.probe must be a command string or a list of strings")
            for key in ("probe_seconds", "recover_after", "down_errors", "down_window_minutes", "probe_timeout"):
                value = health.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                    found.append(f"health.{key} must be a positive number")
    return found


def names(conf):
    workers = conf.section("workers")
    configured = [k for k, v in workers.items() if isinstance(v, dict)]
    return sorted(set(PRESETS) | set(configured))


def default_name(conf):
    return conf.section("workers").get("default") or DEFAULT


def resolve(conf, name=None):
    """The effective profile: a preset merged with its [workers.<name>] section."""
    name = name or default_name(conf)
    section = conf.section("workers").get(name)
    section = section if isinstance(section, dict) else {}
    base_name = section.get("preset") or ALIASES.get(name, name)
    if base_name not in PRESETS:
        raise ProfileError(f"unknown agent profile {name!r}; presets: {', '.join(sorted(PRESETS))}, or set preset = ... "
                           f"in [workers.{name}]")
    profile = copy.deepcopy(PRESETS[base_name])
    for key, value in section.items():
        if key == "interactive" and isinstance(value, dict) and isinstance(profile.get("interactive"), dict):
            profile["interactive"].update(value)
        elif key in OVERRIDABLE:
            profile[key] = value
    profile["name"] = name
    profile["preset"] = base_name
    return profile


def capability(conf, name=None):
    """The profile's capability ([workers.<name>] capability, default 1): tasks with a higher min_capability never go to
    its agents. An unknown profile counts as 0."""
    try:
        value = resolve(conf, name).get("capability")
    except ProfileError:
        return 0
    try:
        return CAPABILITY if value is None else int(value)
    except (TypeError, ValueError):
        return 0


PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill(template, values):
    """Substitute {name} placeholders in one pass, so placeholder-like text inside a value (a prompt) is left alone."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def render(template, profile, **values):
    if template is None:
        return None
    model = values.get("model", profile.get("model") or "")
    data = {"command": profile.get("command") or "", "model": model, "permission_mode": profile.get("permission_mode") or "",
            **{k: v for k, v in values.items() if v is not None}}
    argv = []
    for item in template:
        if item == "{model_args}":
            if model and profile.get("model_flag"):
                argv += [profile["model_flag"], model]
            continue
        argv.append(fill(item, data))
    return argv


def env(profile, model=None):
    model = model if model is not None else profile.get("model") or ""
    return {k: fill(v, {"model": model}) for k, v in (profile.get("env") or {}).items()}


def mcp_config(conf, profile, agent=None, credentials=None, role=None):
    """Path of the MCP config a profile's agents start with (only the servers in mcp_servers), written under the data
    root, or None when the profile keeps the client's own MCP servers. Each server definition comes from the user's
    Claude Code configuration; "theseus" is the theseus MCP server. With theseus listed the file is per agent, and its
    theseus entry names the agent's credentials file in `env`: some clients (Codex, the MCP SDK's own) pass a stdio
    server only an allow list of the environment plus that entry. role "coordinator" writes the supervised
    coordinator's own file instead, whose theseus entry passes --role coordinator and names no credentials."""
    names = profile.get("mcp_servers")
    if names is None:
        return None
    known = {}
    client = Path(os.path.expanduser("~/.claude.json"))
    if client.exists():
        try:
            known = json.loads(client.read_text()).get("mcpServers", {})
        except ValueError:
            known = {}
    servers = {}
    for name in names:
        if name == "theseus" and role == "coordinator":
            servers[name] = {**THESEUS_MCP, "args": [*THESEUS_MCP["args"], "--role", "coordinator"]}
        elif name == "theseus":
            servers[name] = {**THESEUS_MCP, **({"env": {"THESEUS_CREDENTIALS": str(credentials)}} if credentials else {})}
        elif name in known:
            servers[name] = known[name]
        else:
            raise ProfileError(f"profile {profile['name']}: MCP server {name!r} is not defined in {client}")
    if role == "coordinator":
        path = conf.data / "mcp" / profile["name"] / "coordinator.json"
    else:
        path = conf.data / "mcp" / (f"{profile['name']}/{agent}.json" if "theseus" in names and agent else
                                    f"{profile['name']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"mcpServers": servers}, indent=2) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path


def mcp_args(conf, profile, agent=None, credentials=None, role=None):
    path = mcp_config(conf, profile, agent, credentials, role) if conf is not None else None
    if path is None:
        return []
    if not profile.get("mcp_flags"):
        raise ProfileError(f"profile {profile['name']} sets mcp_servers but its CLI has no mcp_flags")
    return [fill(flag, {"mcp_config": str(path)}) for flag in profile["mcp_flags"]]


def agent_credentials(conf, agent):
    """Where a supervised agent's credentials file is (written by `agents _exec`)."""
    return conf.data / "agents" / agent / "credentials" if conf is not None and agent else None


def extra_args(profile, **values):
    """The profile's extra_args with placeholders filled and a leading ~ expanded."""
    extra = profile.get("extra_args") or []
    if not (isinstance(extra, list) and all(isinstance(item, str) for item in extra)):
        raise ProfileError(f"profile {profile.get('name')}: extra_args must be a list of strings")
    data = {"command": profile.get("command") or "", "model": profile.get("model") or "",
            "permission_mode": profile.get("permission_mode") or "", **{k: v for k, v in values.items() if v is not None}}
    return [os.path.expanduser(fill(item, data)) for item in extra]


def launch_argv(profile, prompt, session, model, conf=None, agent=None, credentials=None):
    argv = render(profile.get("headless"), profile, prompt=prompt, session=session, model=model)
    if not argv:
        return argv
    return argv + mcp_args(conf, profile, agent, credentials) + extra_args(profile, prompt=prompt, session=session,
                                                                           model=model)


def resume_template(profile, session, model, conf=None):
    """The resume command with {message} left in place, stored in launch.json for followups."""
    argv = render(profile.get("resume"), profile, session=session, model=model, message="{message}")
    if not argv:
        return argv
    return argv + mcp_args(conf, profile) + extra_args(profile, session=session, model=model, message="{message}")


EMPTY_OPTION = re.compile(r" --?[\w-]+ \{(\w+)\}")


def launch_template(conf, profile, agent=None):
    """The persistent-instance launch template ([instances] launch, interactive.launch or the bare command) with
    {command}, {permission_mode}, {model} and {agent} filled; an option whose placeholder value is empty is dropped.
    Without an agent name {agent} stays for instance-restart.sh to fill from the session record."""
    interactive = profile.get("interactive") or {}
    template = conf.section("instances").get("launch") or interactive.get("launch") or profile.get("command") or ""
    values = {"command": profile.get("command") or "", "permission_mode": profile.get("permission_mode") or "",
              "model": profile.get("model") or ""}
    if agent:
        values["agent"] = shlex.quote(agent)
    template = EMPTY_OPTION.sub(lambda m: "" if m.group(1) in values and not values[m.group(1)] else m.group(0), template)
    return fill(template, values)


def interactive_env(profile):
    """The interactive.env variables of a persistent instance, which come after (and override) the profile's env."""
    return {k: str(v) for k, v in ((profile.get("interactive") or {}).get("env") or {}).items()}


def interactive_launch(conf, profile, agent=None):
    """The command line that starts a persistent instance: `env VAR=value ...` for the profile's env and
    interactive.env, the launch template, the MCP flags, then extra_args."""
    variables = {**env(profile), **interactive_env(profile)}
    prefix = "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in variables.items()) if variables else ""
    extra = shlex.join(extra_args(profile, agent=agent))
    return " ".join(filter(None, [prefix, launch_template(conf, profile, agent), shlex.join(mcp_args(conf, profile)), extra]))


def interactive_parts(conf, profile, agent):
    """The parts of a supervised instance's launch, before any session arguments: launch (the filled template as an
    argument list), mcp (the MCP flags), extra (extra_args), env (the profile's env) and interactive_env."""
    parts = {"launch": shlex.split(launch_template(conf, profile, agent)),
             "mcp": mcp_args(conf, profile, agent, agent_credentials(conf, agent)),
             "extra": extra_args(profile, agent=agent), "env": env(profile), "interactive_env": interactive_env(profile)}
    if not parts["launch"]:
        raise ProfileError(f"profile {profile.get('name')} has no launch command")
    return parts


def interactive_argv(conf, profile, agent, session=None, resume=False, parts=None):
    """(argv, env) that start a supervised instance: the launch command of interactive_launch as an argument list
    without the env prefix, then the session arguments: the resume template with session when resume is set,
    otherwise interactive.session_flag and session when the profile preassigns one. env is the profile's env and
    interactive.env, which the caller applies to its own process, never to a command line."""
    interactive = profile.get("interactive") or {}
    parts = parts or interactive_parts(conf, profile, agent)
    argv = parts["launch"] + parts["mcp"] + parts["extra"]
    if session and resume:
        template = interactive.get("resume") or ""
        if not template:
            raise ProfileError(f"profile {profile.get('name')} cannot resume a session (no interactive.resume)")
        argv = shlex.split(fill(template, {"launch": shlex.join(argv), "session": shlex.quote(session)}))
    elif session and interactive.get("session_flag") and profile.get("session") == "preassign":
        argv += [interactive["session_flag"], session]
    return argv, {**parts["env"], **parts["interactive_env"]}


def stall_patterns(conf, profile_name=None):
    """Screen text that means the agent stopped and needs the coordinator (interactive.stall_patterns)."""
    return list((resolve(conf, profile_name).get("interactive") or {}).get("stall_patterns") or [])


def interactive(conf, profile_name=None):
    """The persistent-instance settings of a profile ({} when it has none or does not resolve)."""
    try:
        return dict(resolve(conf, profile_name).get("interactive") or {})
    except ProfileError:
        return {}


def instance_env(conf, profile_name=None, agent=None):
    """Environment the instance scripts read for one agent's profile."""
    profile = resolve(conf, profile_name)
    interactive = profile.get("interactive") or {}
    instances = conf.section("instances")
    return {
        "THESEUS_BUSY_PATTERN": instances.get("busy_pattern") or interactive.get("busy_pattern") or "",
        "THESEUS_COMPACT_TEMPLATE": interactive.get("compact") or "",
        "THESEUS_CLEAR_COMMAND": interactive.get("clear") or "",
        "THESEUS_AGENT_LAUNCH": interactive_launch(conf, profile, agent),
        "THESEUS_RESUME_TEMPLATE": interactive.get("resume") or "",
        "THESEUS_RESUME_MISSING": interactive.get("resume_missing") or "",
        "THESEUS_AGENT_SESSIONS": os.path.expanduser(instances.get("sessions_dir") or interactive.get("sessions_dir") or ""),
        "THESEUS_MESSAGING": "1" if interactive.get("messaging") else "0",
        "THESEUS_EXIT_COMMAND": interactive.get("exit") or "",
    }


RELAUNCH_FREE = ("name", "label", "notes", "verified", "capacity", "capability", "mcp_servers", "env", "fallback", "health")


def relaunch_compatible(conf, current, target):
    """Whether an agent running profile current can switch to profile target with a relaunch in the same pane: the two
    may differ only in MCP servers and environment (env, interactive.env); command, pool, model and every template
    must match."""
    def launch_key(name):
        profile = resolve(conf, name)
        for key in RELAUNCH_FREE:
            profile.pop(key, None)
        if isinstance(profile.get("interactive"), dict):
            profile["interactive"].pop("env", None)
        return profile
    try:
        return current == target or launch_key(current) == launch_key(target)
    except ProfileError:
        return False


def detected():
    """Agent CLIs found on PATH, with the presets that use them."""
    found = []
    for name, preset in PRESETS.items():
        command = preset.get("command")
        if command:
            found.append((name, command, shutil.which(command)))
    return found


def describe(conf):
    rows = []
    default = default_name(conf)
    for name in names(conf):
        try:
            profile = resolve(conf, name)
        except ProfileError as error:
            rows.append((name, "-", str(error)))
            continue
        command = profile.get("command") or "(coordinator-launched)"
        found = "found" if not profile.get("command") or shutil.which(profile["command"]) else "missing"
        mark = "default" if name == default else ""
        rows.append((name, mark, f"{profile['label']}; command {command} ({found}); capacity {profile.get('capacity')}; "
                                 f"model {profile.get('model') or 'CLI default'}; {'verified' if profile.get('verified') else 'unverified'}"))
    return rows


def as_json(value):
    return json.dumps(value)


def shell(argv):
    return shlex.join(argv)
