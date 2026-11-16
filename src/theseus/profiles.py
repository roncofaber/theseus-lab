"""Agent profiles: how theseus launches, resumes and drives a coding agent, so the workflow is not tied to one vendor.

A profile describes one agent CLI:
- headless: the argument template of a one-shot run (`theseus dispatch`); None means the coordinator launches the
  worker itself (a Claude Code subagent) and theseus only prepares the worktree and prompt;
- resume: the template that continues a run with a follow-up message (`theseus followup`, gateway retries);
- session: "preassign" when the CLI accepts a session ID chosen by theseus, "none" otherwise;
- env, model_flag, permission_mode: how the model and permissions are passed;
- retry: whether a retryable gateway error (Claude JSON output) resumes the run automatically;
- interactive: the persistent-instance behavior in a tmux pane (launch, resume, busy marker, compact and clear
  commands, where session records live, whether the agent can message the coordinator).

Placeholders: {command} {prompt} {message} {session} {model} {permission_mode} {keep}; an item "{model_args}" expands to
[model_flag, model] when a model is set and to nothing otherwise. Presets marked verified: false follow the CLI's
public help but were not exercised end to end; dispatch a trivial task and follow it up before relying on one.

[workers.<name>] in theseus.toml selects or overrides a profile: a section named after a preset adjusts it; any other
name sets `preset = "<preset>"` and overrides fields; `capacity` and `model` apply to every profile.
"""
import copy
import json
import os
import re
import shlex
import shutil

CLAUDE_INTERACTIVE = {
    "launch": "claude",
    "resume": "{launch} --resume {session}",
    "busy_pattern": "esc to interrupt",
    "compact": "/compact Keep only: {keep}; drop finished task details.",
    "clear": "/clear",
    "sessions_dir": "~/.claude/sessions",
    "messaging": True,
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
        "permission_mode": "auto",
        "env": {},
        "retry": True,
        "capacity": 2,
        "interactive": CLAUDE_INTERACTIVE,
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
                        "sessions_dir": "", "messaging": False},
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
                        "sessions_dir": "", "messaging": False},
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
                        "sessions_dir": "", "messaging": False},
    },
}
ALIASES = {"claude": "claude-subagent"}
DEFAULT = "claude-code"
OVERRIDABLE = set(PRESETS["claude-code"]) | {"preset", "fallback"}


class ProfileError(Exception):
    pass


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


def launch_argv(profile, prompt, session, model):
    return render(profile.get("headless"), profile, prompt=prompt, session=session, model=model)


def resume_template(profile, session, model):
    """The resume command with {message} left in place, stored in launch.json for followups."""
    return render(profile.get("resume"), profile, session=session, model=model, message="{message}")


def instance_env(conf, profile_name=None):
    """Environment the instance scripts read for one agent's profile."""
    profile = resolve(conf, profile_name)
    interactive = profile.get("interactive") or {}
    instances = conf.section("instances")
    return {
        "THESEUS_BUSY_PATTERN": instances.get("busy_pattern") or interactive.get("busy_pattern") or "",
        "THESEUS_COMPACT_TEMPLATE": interactive.get("compact") or "",
        "THESEUS_CLEAR_COMMAND": interactive.get("clear") or "",
        "THESEUS_AGENT_LAUNCH": instances.get("launch") or interactive.get("launch") or profile.get("command") or "",
        "THESEUS_RESUME_TEMPLATE": interactive.get("resume") or "",
        "THESEUS_AGENT_SESSIONS": os.path.expanduser(instances.get("sessions_dir") or interactive.get("sessions_dir") or ""),
        "THESEUS_MESSAGING": "1" if interactive.get("messaging") else "0",
    }


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
