"""External tools without hard-coded paths. Each machine resolves every catalogued tool in this order:

1. an environment override (the tool's conventional variable, such as GHIDRA_INSTALL_DIR, or THESEUS_TOOL_<NAME>);
2. [tools.paths] in the project's Git-ignored theseus.local.toml, written by `theseus tools set NAME VALUE`;
3. [tools.paths] in the project's theseus.toml (discouraged: locations are per machine);
4. [tools.paths] in the host configuration (~/.config/theseus/config.toml), written by `theseus tools set --host`;
5. PATH, for command tools;
6. common install locations (version-sorted, newest first), and for REA the command its own MCP registration records.

A configured location that does not work falls through to the next step. theseus.toml only lists which tools a project
needs ([tools] need = [...]); where they live is per machine. Resolved
tools are exported to every script, configured command and worker as THESEUS_TOOL_<NAME> plus the conventional
variables (GHIDRA_INSTALL_DIR, JAVA_HOME), so `eval "$(theseus tools env)"` gives an interactive shell the same view.
"""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config as cfg
from . import hostconfig

LAYER_LABELS = {"project-local": cfg.LOCAL_NAME, "project": cfg.CONFIG_NAME, "host": "host config.toml"}

CATALOG = {
    "ghidra": {"kind": "dir", "env": "GHIDRA_INSTALL_DIR", "check": "Ghidra/application.properties",
               "globs": ["~/software/ghidra_*", "~/ghidra_*", "~/opt/ghidra*", "/opt/ghidra*", "/usr/local/ghidra*", "/usr/share/ghidra"],
               "purpose": "decompiler provider for REA"},
    "java": {"kind": "dir", "env": "JAVA_HOME", "check": "bin/java",
             "globs": ["~/software/jdk-*", "~/software/jdk*", "~/.sdkman/candidates/java/*", "/usr/lib/jvm/*", "/opt/jdk*"],
             "purpose": "JDK for Ghidra (21 or newer)"},
    "rea": {"kind": "command", "names": ["rea"], "globs": ["~/.nvm/versions/node/*/bin/rea", "~/.local/bin/rea", "~/.npm-global/bin/rea"],
            "registration": True, "purpose": "reverse-engineering CLI and MCP server"},
    "rizin": {"kind": "command", "names": ["rizin"], "purpose": "disassembly and binary inspection"},
    "rz-bin": {"kind": "command", "names": ["rz-bin"], "purpose": "exports and strings for `theseus re build`"},
    "gdb": {"kind": "command", "names": ["gdb"], "purpose": "debugger observation in reference runs"},
    "python-re": {"kind": "python", "modules": ["pefile", "capstone", "lief"],
                  "globs": ["~/miniforge3/envs/*/bin/python", "~/mambaforge/envs/*/bin/python", "~/miniconda3/envs/*/bin/python",
                            "~/anaconda3/envs/*/bin/python", "~/.venvs/*/bin/python"],
                  "purpose": "Python with pefile, capstone and lief for binary scripts"},
    "kaitai": {"kind": "command", "names": ["kaitai-struct-compiler"], "globs": ["~/software/kaitai-struct-compiler-*/bin/kaitai-struct-compiler"],
               "purpose": "testable binary format descriptions"},
    "wine": {"kind": "command", "names": ["wine"], "purpose": "compatibility-layer reference runs"},
    "xvfb": {"kind": "command", "names": ["Xvfb"], "purpose": "private virtual display"},
    "xdotool": {"kind": "command", "names": ["xdotool"], "purpose": "input on the private display"},
    "bwrap": {"kind": "command", "names": ["bwrap"], "purpose": "read-only binds for reference runs"},
    "qemu": {"kind": "command", "names": ["qemu-system-x86_64"], "purpose": "VM reference runs"},
    "qemu-img": {"kind": "command", "names": ["qemu-img"], "purpose": "VM disk overlays and conversion"},
    "tshark": {"kind": "command", "names": ["tshark"], "purpose": "USB (usbmon) and network capture"},
    "ffmpeg": {"kind": "command", "names": ["ffmpeg"], "purpose": "media inspection"},
}


def variable(name):
    return "THESEUS_TOOL_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def version_key(path):
    """Newest first by the version numbers in the install directory's name (jdk-25 before java-21, ghidra_12.2 before 11.4)."""
    parts = [part for part in Path(path).parts if re.search(r"\d", part)]
    return tuple(int(n) for n in re.findall(r"\d+", parts[-1])) if parts else ()


def expanded_globs(patterns):
    found = []
    for pattern in patterns:
        found += glob.glob(os.path.expanduser(pattern))
    return sorted(set(found), key=version_key, reverse=True)


def configured(conf, name):
    """(value, label) for every layer that sets a location for the tool, highest first."""
    found = []
    for label, _, data in reversed(conf.layers):
        value = ((data.get("tools") or {}).get("paths") or {}).get(name) if label in LAYER_LABELS else None
        if value:
            found.append((cfg.expand(value, conf.values), LAYER_LABELS[label]))
    return found


def valid(entry, value):
    if not value:
        return False
    kind = entry["kind"]
    path = Path(os.path.expanduser(value))
    if kind == "dir":
        return (path / entry["check"]).exists()
    if kind == "python":
        return path.exists() and has_modules(str(path), entry["modules"])
    return path.exists() and os.access(path, os.X_OK)


def has_modules(python, modules):
    code = "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in sys.argv[1:]) else 1)"
    try:
        return subprocess.run([python, "-c", code, *modules], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def registration_command(client_config="~/.claude.json"):
    """The REA command line its own `rea setup` recorded for Claude Code on this machine."""
    try:
        server = json.loads(Path(os.path.expanduser(client_config)).read_text()).get("mcpServers", {}).get("rea")
    except (OSError, ValueError):
        return None
    if not server or not server.get("command"):
        return None
    args = [a for a in server.get("args", []) if a != "mcp"]
    return [server["command"], *args]


def resolve(conf, name):
    """(value, source) for one tool; value is a path, a command list (rea), or None."""
    entry = CATALOG[name]
    candidates = []
    for env_name in filter(None, (entry.get("env"), variable(name))):
        if os.environ.get(env_name):
            candidates.append((os.environ[env_name], f"environment {env_name}"))
    candidates += configured(conf, name)
    for value, source in candidates:
        if name == "rea":
            words = shlex.split(value)
            if words and shutil.which(words[0]):
                return words, source
        elif valid(entry, value):
            return str(Path(os.path.expanduser(value))), source
    if entry["kind"] in ("command", "python"):
        names = entry.get("names") or (["python3"] if entry["kind"] == "python" else [])
        for executable in names:
            found = shutil.which(executable)
            if found and (entry["kind"] != "python" or has_modules(found, entry["modules"])):
                return (with_node(found) if name == "rea" else found), "PATH"
    if entry.get("registration"):
        command = registration_command()
        if command and all(Path(part).exists() for part in command):
            return command, "REA MCP registration"
    for path in expanded_globs(entry.get("globs", [])):
        if valid(entry, path):
            if name == "rea":
                return with_node(path), "install location"
            return path, "install location"
    return None, "not found"


def with_node(script):
    """Run a Node launcher with the interpreter installed beside it, so it works without that Node on PATH."""
    node = Path(script).parent / "node"
    return [str(node), script] if node.exists() else [script]


def rea_command(conf):
    value, _ = resolve(conf, "rea")
    return value


def needed(conf):
    return list((conf.raw.get("tools") or {}).get("need") or [])


def table(conf):
    rows = []
    wanted = set(needed(conf))
    for name, entry in CATALOG.items():
        value, source = resolve(conf, name)
        shown = shlex.join(value) if isinstance(value, list) else (value or "-")
        status = "ok" if value else ("FAIL" if name in wanted else "-")
        rows.append((status, name, shown, source, entry["purpose"]))
    return rows


def environment(conf):
    """Variables for scripts, commands and workers: THESEUS_TOOL_<NAME> for every resolved tool, plus GHIDRA_INSTALL_DIR
    and JAVA_HOME when they are not already set."""
    env = {}
    for name, entry in CATALOG.items():
        if entry["kind"] == "python":
            continue
        value, _ = resolve(conf, name)
        if not value:
            continue
        env[variable(name)] = shlex.join(value) if isinstance(value, list) else value
        if entry.get("env") and not os.environ.get(entry["env"]):
            env[entry["env"]] = value
    return env


def set_tool(conf, name, value, host=False):
    """Record a machine-specific location in [tools.paths] of theseus.local.toml, or of the host config.toml with host;
    refuses a value that does not work."""
    if name not in CATALOG:
        raise cfg.ConfigError(f"unknown tool {name}; known: {', '.join(CATALOG)}")
    entry = CATALOG[name]
    if name == "rea":
        words = shlex.split(value)
        if not words or not shutil.which(os.path.expanduser(words[0])):
            raise cfg.ConfigError(f"{value!r} is not a runnable command")
    elif not valid(entry, value):
        detail = f"no {entry['check']} inside it" if entry["kind"] == "dir" else "not usable"
        raise cfg.ConfigError(f"{value} is {detail}")
    path = hostconfig.path() if host else conf.root / cfg.LOCAL_NAME
    text = path.read_text() if path.exists() else ""
    line = f'{name} = "{value}"' if '"' not in value else f"{name} = '{value}'"
    pattern = re.compile(rf'^{re.escape(name)}\s*=.*$', re.MULTILINE)
    header = "[tools.paths]"
    found = re.search(r"^\[tools\.paths\][ \t]*$", text, re.MULTILINE)
    if found:
        start = found.end()
        end = text.find("\n[", start)
        end = len(text) if end == -1 else end
        section = text[start:end]
        section = pattern.sub(line, section) if pattern.search(section) else section.rstrip("\n") + "\n" + line + "\n"
        text = text[:start] + section + text[end:]
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + header + "\n" + line + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"{name} -> {value} ({path if host else cfg.LOCAL_NAME})"
