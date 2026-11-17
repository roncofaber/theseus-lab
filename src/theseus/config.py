"""Per-project configuration: theseus.toml at the project root, optionally overlaid by the Git-ignored theseus.local.toml,
over the host configuration (theseus.hostconfig) and the package defaults.

Layers, lowest to highest: package (DEFAULTS and the agent presets), host (~/.config/theseus/config.toml), project
(theseus.toml), project-local (theseus.local.toml), env (THESEUS_DATA, THESEUS_SCRATCH, THESEUS_WORKTREES). Tables merge
key by key, so a project section that shares a host profile's name overrides only the keys it sets. The loader never
invents a project: without a theseus.toml it raises ConfigError instead of falling back to home or temporary directories.
"""
import copy
import os
import re
import tomllib
from pathlib import Path

from . import hostconfig
from .hostconfig import ConfigError

CONFIG_NAME = "theseus.toml"
LOCAL_NAME = "theseus.local.toml"

TASKS = "docs/project/tasks.json"
ARCHIVE = "docs/project/tasks-archive.jsonl"
STATE = "docs/project/STATE.md"
REPORTS = "docs/investigations"
TOPICS = "docs/topics"
FOLDED = "docs/project/archive/investigations"
CITATION_INDEX = "docs/re/citation-index.md"


def citation_index(conf):
    """The generated citation index path, [re] citation_index (default docs/re/citation-index.md)."""
    return str(conf.section("re").get("citation_index") or CITATION_INDEX)
SKILLS = ".agents/skills"
WORKER_LOG = ".theseus"

DEFAULT_CONSUMERS = {
    ".py": r"^\s*(?:class|def)\s+([A-Za-z_]\w*)",
    ".cs": r"\b(?:class|record|struct|interface|enum)\s+([A-Z][A-Za-z0-9_]*)",
    ".java": r"\b(?:class|interface|enum|record)\s+([A-Z][A-Za-z0-9_]*)",
    ".kt": r"\b(?:class|interface|object)\s+([A-Z][A-Za-z0-9_]*)",
    ".ts": r"\b(?:class|interface|type|enum|function)\s+([A-Za-z_]\w*)",
    ".js": r"\b(?:class|function)\s+([A-Za-z_]\w*)",
    ".c": r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)",
    ".h": r"^\s*(?:typedef\s+)?(?:struct|enum|union|class)\s+([A-Za-z_]\w*)",
    ".cpp": r"^\s*(?:class|struct|enum|union)\s+([A-Za-z_]\w*)",
    ".hpp": r"^\s*(?:class|struct|enum|union)\s+([A-Za-z_]\w*)",
    ".rs": r"\b(?:struct|enum|trait|fn)\s+([A-Za-z_]\w*)",
    ".go": r"^\s*(?:type|func)\s+([A-Za-z_]\w*)",
}

DEFAULT_CITATIONS = [
    {
        "name": "address",
        "pattern": r"(?<![0-9A-Za-z_])(?:0x|FUN_|DAT_|LAB_)([0-9A-Fa-f]{6,8})(?![0-9A-Za-z])",
        "format": "hex",
    },
    {
        "name": "capture",
        "pattern": r"(?<![0-9A-Za-z_])(cap_[0-9a-f]{12,64}(?:@[0-9]+)?)(?![0-9A-Za-z_])",
    },
]

DEFAULTS = {
    "project": {"name": "", "prefix": ""},
    "paths": {
        "data": "~/.local/share/${THESEUS_PROJECT}",
        "scratch": "${TMPDIR:-/tmp}/${THESEUS_PROJECT}",
        "worktrees": "${THESEUS_DATA}/worktrees",
        "original": [],
    },
    "commands": {"build": [], "smoke": [], "verify": [], "smoke_timeout": 300},
    "limits": {"heavy_runs": 2},
    "workers": {"default": "claude-code"},
    "instances": {"panes": [], "busy_pattern": "", "coordinator": "", "launch": "", "sessions_dir": "",
                  "handoff_settle_seconds": 45,
                  "brief": ".agents/skills/theseus-worker/instance-brief.md", "idle_minutes": 5, "send_verify_seconds": 10},
    "git": {"base": "HEAD", "remote": "origin", "branch": "main"},
    "verification": {"code_paths": ["."],
                     "doc_paths": ["docs", "AGENTS.md", "README.md", ".agents", ".claude", "theseus.toml", "tools/git-hooks", "tools/Supervisor"],
                     "max_unverified_merges": 15, "max_unverified_hours": 24},
    "docs": {"index_command": [], "check_command": [], "generated": []},
    "upgrade": {"keep": []},
    "models": {"context": {"claude-*": 1000000, "claude-haiku-4-5*": 200000, "claude-sonnet-4-5*": 200000, "claude-opus-4-5*": 200000}},
    "re": {"targets": [], "evidence": r"ev_[0-9a-f]{64}", "citations": DEFAULT_CITATIONS,
           "address_low": 0, "address_high": 0, "clients": ["claude_code"]},
    "tools": {"need": [], "paths": {}},
    "queue": {"deadline": "", "nudge": "", "interval": 30},
    "gateway": {"attempts": 3, "backoff": [120, 240, 480], "slow_codes": [530], "slow_factor": 2, "resend": "",
                "clear_tokens": 120000, "fresh_after_errors": 2, "screen_lines": 15, "stagger_seconds": 30,
                "recovery_seconds": 900},
    "rooms": {"enabled": False, "taint": "until-clear", "clean_instances": [], "dirty_instances": [], "dirty_paths": [],
              "dirty_data": [], "spec_paths": []},
    "consumers": {},
    "agents": {"managed": False, "name": "{project}-{profile}-{n}", "cwd": "root", "resume": "task", "counts": {},
               "extra": []},
}

ENV_OVERRIDES = {"THESEUS_DATA": ("paths", "data"), "THESEUS_SCRATCH": ("paths", "scratch"),
                 "THESEUS_WORKTREES": ("paths", "worktrees")}


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand(text, extra):
    """Expand ~, $VAR, ${VAR} and ${VAR:-default}; extra wins over the process environment."""
    def replace(match):
        name = match.group(1) or match.group(3)
        value = extra.get(name, os.environ.get(name))
        if value in (None, "") and match.group(2) is not None:
            return expand(match.group(2), extra)
        if value is None:
            raise ConfigError(f"unset variable ${name} in configuration value {text!r}")
        return value
    return os.path.expanduser(VARIABLE.sub(replace, text))


def find_root(start=None):
    explicit = os.environ.get("THESEUS_ROOT")
    if explicit:
        root = Path(explicit).resolve()
        if not (root / CONFIG_NAME).is_file():
            raise ConfigError(f"THESEUS_ROOT={explicit} has no {CONFIG_NAME}")
        return root
    here = Path(start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} in {here} or any parent; run `theseus scaffold` or set THESEUS_ROOT")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Config:
    def __init__(self, root, raw, layers=None):
        self.root = Path(root).resolve()
        self.raw = raw
        self.layers = layers or [("package", None, DEFAULTS)]
        name = raw["project"].get("name") or self.root.name
        self.name = name
        self.prefix = slug(raw["project"].get("prefix") or name) or "theseus"
        values = {"THESEUS_PROJECT": self.prefix, "THESEUS_ROOT": str(self.root)}
        paths = raw["paths"]
        self.data = Path(expand(paths["data"], values)).resolve()
        values["THESEUS_DATA"] = str(self.data)
        self.scratch = Path(expand(paths["scratch"], values)).resolve()
        values["THESEUS_SCRATCH"] = str(self.scratch)
        self.worktrees = Path(expand(paths["worktrees"], values)).resolve()
        original = paths.get("original") or []
        if isinstance(original, str):
            original = [original]
        self.original = [Path(expand(item, values)) for item in original]
        for label, path in (("paths.data", self.data), ("paths.worktrees", self.worktrees)):
            if path == self.root or self.root in path.parents:
                raise ConfigError(f"{label} ({path}) must lie outside the project root")
        self.values = values

    def section(self, name):
        return self.raw.get(name, {})

    def path(self, relative):
        return self.root / relative

    @property
    def tasks_file(self):
        return self.root / TASKS

    @property
    def archive_file(self):
        return self.root / ARCHIVE

    @property
    def build_dir(self):
        """A build directory under the scratch root unique to this checkout, so worktrees never share build state."""
        import hashlib
        return self.scratch / "build" / f"{self.root.name}-{hashlib.sha1(str(self.root).encode()).hexdigest()[:8]}"

    @property
    def logs(self):
        return self.data / "logs"

    @property
    def evidence(self):
        return self.data / "evidence"

    @property
    def workers_dir(self):
        return self.data / "workers"

    def commands(self, kind):
        value = self.raw["commands"].get(kind) or []
        return [value] if isinstance(value, str) else list(value)

    def worker(self, provider):
        workers = self.raw["workers"]
        if provider not in workers or not isinstance(workers[provider], dict):
            raise ConfigError(f"unknown worker provider {provider!r}; configure [workers.{provider}]")
        return workers[provider]

    def providers(self):
        return {name: value for name, value in self.raw["workers"].items() if isinstance(value, dict)}

    def instances(self):
        from . import agents
        if agents.registered(self):
            return agents.instance_entries(self)
        panes = self.raw["instances"].get("panes") or []
        result = []
        for entry in panes:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("pane"):
                raise ConfigError("each [[instances.panes]] entry needs name and pane")
            result.append({"name": entry["name"], "pane": entry["pane"]})
        return result

    def consumer_patterns(self):
        patterns = dict(DEFAULT_CONSUMERS)
        patterns.update(self.raw.get("consumers") or {})
        return {ext: re.compile(rx, re.MULTILINE) for ext, rx in patterns.items() if rx}

    def environment(self):
        """Variables exported to every packaged shell script and configured command."""
        env = {
            "THESEUS_ROOT": str(self.root),
            "THESEUS_PROJECT": self.prefix,
            "THESEUS_DATA": str(self.data),
            "THESEUS_SCRATCH": str(self.scratch),
            "THESEUS_WORKTREES": str(self.worktrees),
            "THESEUS_BUILD_DIR": str(self.build_dir),
            "THESEUS_LOGS": str(self.logs),
            "THESEUS_EVIDENCE": str(self.evidence),
            "THESEUS_WORKER_LOG": WORKER_LOG,

            "THESEUS_BRIEF": str(self.root / self.raw["instances"].get("brief", "")),
            "THESEUS_IDLE_MINUTES": str(self.raw["instances"].get("idle_minutes", 5)),
            "THESEUS_COORDINATOR": str(self.raw["instances"].get("coordinator") or ""),
            "THESEUS_INSTANCES": " ".join(f"{i['name']}:{i['pane']}" for i in self.instances()),
            "THESEUS_RETRIES": str(self.raw["workers"].get("retries", 3)),
        }
        if self.original:
            env["THESEUS_ORIGINAL"] = ":".join(str(p) for p in self.original)
        from . import profiles, tmux, tools
        env["THESEUS_TMUX_FLAG"], env["THESEUS_TMUX_SOCKET"] = tmux.configured()
        try:
            env.update(profiles.instance_env(self))
        except profiles.ProfileError:
            pass
        env.update(tools.environment(self))
        return env


def load(start=None, root=None):
    root = Path(root).resolve() if root else find_root(start)
    if not (root / CONFIG_NAME).is_file():
        raise ConfigError(f"{root} has no {CONFIG_NAME}")
    host = hostconfig.path()
    layers = [("package", None, DEFAULTS), ("host", host, hostconfig.project_layer(hostconfig.read()))]
    for label, name in (("project", CONFIG_NAME), ("project-local", LOCAL_NAME)):
        path = root / name
        data = {}
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text())
            except tomllib.TOMLDecodeError as error:
                raise ConfigError(f"{path}: {error}") from error
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            hostconfig.warn_once(f"{path}: ignoring unknown configuration sections: {', '.join(unknown)}")
            data = {key: value for key, value in data.items() if key not in unknown}
        layers.append((label, path, data))
    env = {}
    for variable, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(variable):
            env.setdefault(section, {})[key] = os.environ[variable]
    layers.append(("env", None, env))
    raw = {}
    for _, _, data in layers:
        raw = merge(raw, data)
    return Config(root, raw, layers)


def unknown_sections(root):
    """(file, sections) for each project file of root with top-level sections theseus does not know."""
    found = []
    for name in (CONFIG_NAME, LOCAL_NAME):
        path = Path(root) / name
        if path.is_file():
            try:
                unknown = sorted(set(tomllib.loads(path.read_text())) - set(DEFAULTS))
            except tomllib.TOMLDecodeError:
                continue
            if unknown:
                found.append((path, unknown))
    return found


def lookup(table, dotted):
    """(True, value) when the dotted key is set in a nested table, else (False, None)."""
    value = table
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def source(conf, dotted):
    """The highest layer that sets a dotted key: (label, file), or (None, None) when no layer does."""
    for label, file, data in reversed(conf.layers):
        if lookup(data, dotted)[0]:
            return label, file
    return None, None


def leaves(value, prefix):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from leaves(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value
