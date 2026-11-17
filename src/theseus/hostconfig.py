"""Host configuration: ~/.config/theseus/config.toml (XDG_CONFIG_HOME is respected; THESEUS_CONFIG overrides the path).

Everything specific to one user or machine lives here rather than in the package or a project: host-wide agent profiles
([workers.<name>]), pools ([pools.<name>]), host store retention and backups ([store]), the gateway circuit breaker
([gateway]), the private tmux server of supervised agents ([supervisor]), tool locations ([tools.paths])
and host defaults for project keys a project leaves unset ([defaults]). Layers, lowest to highest: package presets and
defaults, this file, the project's theseus.toml, its theseus.local.toml, then THESEUS_* variables and command line flags.

The older pools.toml and store.toml are still read, below this file, and reported as deprecated.
"""
import copy
import os
import tomllib
from pathlib import Path

NAME = "config.toml"
SECTIONS = ("workers", "pools", "store", "tools", "defaults", "gateway", "supervisor")
DEFAULT_KEYS = ("workers.default", "workers.retries", "limits.heavy_runs", "paths.data", "paths.scratch", "paths.worktrees",
                "instances.idle_minutes", "instances.handoff_settle_seconds", "instances.send_verify_seconds", "queue.interval",
                "re.clients", "gateway.attempts", "gateway.backoff", "gateway.slow_codes", "gateway.slow_factor",
                "gateway.resend", "gateway.clear_tokens", "gateway.fresh_after_errors", "gateway.screen_lines",
                "gateway.stagger_seconds", "gateway.recovery_seconds", "agents.counts", "agents.resume", "agents.cwd")
TABLE_KEYS = ("agents.counts",)
SUPERVISOR_DEFAULTS = {"socket": "theseus", "size": "220x50", "history_limit": 50000, "detach_key": "C-q", "max_agents": 12,
                       "env_passthrough": [], "launch_shell": "exec", "ready_seconds": 90,
                       "restart": {"backoff_seconds": [5, 30, 120, 600], "max_crashes": 5, "window_minutes": 30}}
GATEWAY_DEFAULTS = {"breaker_errors": 3, "breaker_window_minutes": 10, "strong_codes": [530], "parallel_cap_step": 1,
                    "parallel_cap_minutes": 10}
POOL_KEYS = ("max", "priority")
STORE_KEYS = {"retention": ("samples_days", "events_days"), "backups": ("directory", "daily", "weekly")}


class ConfigError(Exception):
    pass


def config_base():
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "theseus"


def path():
    return Path(os.environ["THESEUS_CONFIG"]) if os.environ.get("THESEUS_CONFIG") else config_base() / NAME


def pools_path():
    return Path(os.environ["THESEUS_POOLS"]) if os.environ.get("THESEUS_POOLS") else config_base() / "pools.toml"


def store_path():
    return Path(os.environ["THESEUS_STORE_CONFIG"]) if os.environ.get("THESEUS_STORE_CONFIG") else config_base() / "store.toml"


def parse(file):
    file = Path(file)
    if not file.is_file():
        return {}
    try:
        return tomllib.loads(file.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file}: {error}") from error


def read():
    """The host file's tables; a parse error or an unknown top-level section raises ConfigError naming the file."""
    data = parse(path())
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"{path()}: unknown section(s) {', '.join(unknown)}; known: {', '.join(SECTIONS)}")
    return data


def legacy_files():
    """The deprecated host files that exist, with the section of config.toml that replaces each."""
    return [(file, section) for file, section in ((pools_path(), "[pools.<name>]"), (store_path(), "[store]")) if file.is_file()]


def deprecation_notes():
    return [f"{file} is deprecated; move its tables into {section} of {path()} (config.toml wins on conflicts)"
            for file, section in legacy_files()]


def pools():
    """Pool definitions: pools.toml (deprecated) overlaid per key by config.toml [pools]."""
    legacy = parse(pools_path()).get("pools") or {}
    host = read().get("pools") or {}
    return {name: {**(legacy.get(name) or {}), **(host.get(name) or {})} for name in [*legacy, *(n for n in host if n not in legacy)]}


def pool_sources():
    legacy = parse(pools_path()).get("pools") or {}
    host = read().get("pools") or {}
    found = {}
    for label, table in (("host (pools.toml, deprecated)", legacy), ("host", host)):
        for name, values in table.items():
            for key in values or {}:
                found[f"{name}.{key}"] = label
    return found


def gateway():
    """The host-wide gateway circuit breaker ([gateway] in config.toml over GATEWAY_DEFAULTS)."""
    return {**GATEWAY_DEFAULTS, **(read().get("gateway") or {})}


def store(defaults):
    """Host store settings: defaults < store.toml (deprecated) < config.toml [store]."""
    merged = copy.deepcopy(defaults)
    for table in (parse(store_path()), read().get("store") or {}):
        for section, values in table.items():
            if section in merged and isinstance(values, dict):
                merged[section].update(values)
    return merged


def store_sources(defaults):
    found = {f"{s}.{k}": "package" for s, values in defaults.items() for k in values}
    for label, table in (("host (store.toml, deprecated)", parse(store_path())), ("host", read().get("store") or {})):
        for section, values in table.items():
            if section in defaults and isinstance(values, dict):
                for key in values:
                    found[f"{section}.{key}"] = label
    return found


def supervisor():
    """The host's [supervisor] table over SUPERVISOR_DEFAULTS."""
    merged = copy.deepcopy(SUPERVISOR_DEFAULTS)
    for key, value in (read().get("supervisor") or {}).items():
        if key == "restart" and isinstance(value, dict):
            merged["restart"].update(value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def flatten(table, prefix=""):
    """Dotted keys of a nested table; a table named in TABLE_KEYS stays one value."""
    items = {}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and name not in TABLE_KEYS:
            items.update(flatten(value, name + "."))
        else:
            items[name] = value
    return items


def project_layer(data):
    """The host file as a layer of project configuration: [defaults] keys theseus knows, [workers.<name>] profiles and
    [tools.paths]."""
    layer = {}
    for key, value in flatten(data.get("defaults") or {}).items():
        if key in DEFAULT_KEYS:
            section, name = key.split(".", 1)
            layer.setdefault(section, {})[name] = copy.deepcopy(value)
    profiles = {name: copy.deepcopy(value) for name, value in (data.get("workers") or {}).items() if isinstance(value, dict)}
    if profiles:
        layer.setdefault("workers", {}).update(profiles)
    paths = (data.get("tools") or {}).get("paths")
    if isinstance(paths, dict) and paths:
        layer["tools"] = {"paths": copy.deepcopy(paths)}
    return layer


def validate(data, file, profile_problems, tool_names=None):
    """(errors, warnings) for the host file's content; profile_problems(name, section) lists a profile's bad keys."""
    errors, warnings = [], []
    for section in sorted(set(data) - set(SECTIONS)):
        errors.append(f"{file}: unknown section [{section}]; known: {', '.join(SECTIONS)}")
    for name, value in (data.get("workers") or {}).items():
        if not isinstance(value, dict):
            hint = f"; set it as [defaults] workers.{name}" if f"workers.{name}" in DEFAULT_KEYS else ""
            errors.append(f"{file}: [workers] {name} is not a profile table; the host [workers] holds only [workers.<name>] profiles{hint}")
            continue
        errors += [f"{file}: [workers.{name}] {problem}" for problem in profile_problems(name, value)]
    for name, value in (data.get("pools") or {}).items():
        if not isinstance(value, dict):
            errors.append(f"{file}: [pools] {name} must be a table [pools.{name}] with {', '.join(POOL_KEYS)}")
            continue
        errors += [f"{file}: [pools.{name}] unknown key {key}; known: {', '.join(POOL_KEYS)}" for key in value if key not in POOL_KEYS]
    for section, value in (data.get("store") or {}).items():
        if section not in STORE_KEYS or not isinstance(value, dict):
            errors.append(f"{file}: [store] unknown key {section}; known tables: {', '.join(f'[store.{s}]' for s in STORE_KEYS)}")
            continue
        errors += [f"{file}: [store.{section}] unknown key {key}; known: {', '.join(STORE_KEYS[section])}"
                   for key in value if key not in STORE_KEYS[section]]
    for key in data.get("gateway") or {}:
        if key not in GATEWAY_DEFAULTS:
            errors.append(f"{file}: [gateway] unknown key {key}; known: {', '.join(GATEWAY_DEFAULTS)} (per-project gateway "
                          f"keys go under [defaults] gateway.<key>)")
    for key, value in (data.get("tools") or {}).items():
        if key != "paths" or not isinstance(value, dict):
            errors.append(f"{file}: [tools] unknown key {key}; the host file holds only [tools.paths] (a project lists the tools it "
                          f"needs in its own theseus.toml)")
            continue
        if tool_names is not None:
            errors += [f"{file}: [tools.paths] unknown tool {tool}; known: {', '.join(tool_names)}" for tool in value if tool not in tool_names]
    for key, value in (data.get("supervisor") or {}).items():
        if key not in SUPERVISOR_DEFAULTS:
            errors.append(f"{file}: [supervisor] unknown key {key}; known: {', '.join(SUPERVISOR_DEFAULTS)}")
        elif key == "restart":
            errors += [f"{file}: [supervisor.restart] unknown key {name}; known: {', '.join(SUPERVISOR_DEFAULTS['restart'])}"
                       for name in (value if isinstance(value, dict) else {}) if name not in SUPERVISOR_DEFAULTS["restart"]]
        elif key == "launch_shell" and value not in ("exec", "login"):
            errors.append(f"{file}: [supervisor] launch_shell must be \"exec\" or \"login\"")
    for key in flatten(data.get("defaults") or {}):
        if key not in DEFAULT_KEYS:
            warnings.append(f"{file}: [defaults] {key} is not a host default and is ignored; known: {', '.join(DEFAULT_KEYS)}")
    return errors, warnings
