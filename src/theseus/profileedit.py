"""Agent profiles as files: list, show, add, edit and remove [workers.<name>] sections (`theseus profiles`).

A profile lives in the host config.toml (every project sees it) or in a project's theseus.local.toml. Outside a project
only the package presets and the host file count (HostConfig). Every change is checked before it is kept: add and
remove restore the file when the result does not validate, and edit opens $VISUAL or $EDITOR at the profile's section
and, when the saved file does not validate, offers to edit again, revert or keep it."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import config as cfg
from . import hostconfig, profiles

NAME = re.compile(r"[A-Za-z0-9_-]+")
HEADER = re.compile(r"^\s*\[\s*workers\.([A-Za-z0-9_-]+)(\.[^\]]*)?\s*\]")
ANY_HEADER = re.compile(r"^\s*\[")


class ProfileFileError(Exception):
    pass


class HostConfig:
    """The package and host layers alone: the profiles every project sees, for use outside a project."""
    name = "host"
    root = None

    def __init__(self):
        self.layers = [("package", None, cfg.DEFAULTS),
                       ("host", hostconfig.path(), hostconfig.project_layer(hostconfig.read()))]
        raw = {}
        for _, _, data in self.layers:
            raw = cfg.merge(raw, data)
        self.raw = raw

    def section(self, name):
        return self.raw.get(name, {})


def current(host_only=False):
    """The project configuration when inside a project (unless host_only), else HostConfig."""
    if not host_only:
        try:
            return cfg.load()
        except cfg.ConfigError:
            pass
    return HostConfig()


def layers_of(conf, name):
    """[(label, file)] of the files that set [workers.<name>], lowest first (host, project, project-local)."""
    return [(label, file) for label, file, data in conf.layers
            if label != "package" and file and cfg.lookup(data, f"workers.{name}")[0]]


def defined_in(conf, name):
    """Where [workers.<name>] is set: "host", "host+project-local" and so on, or "preset"."""
    return "+".join(label for label, _ in layers_of(conf, name)) or "preset"


def rows(conf):
    """One dict per profile: name, preset, label, model, capability, capacity, pool, command, found, verified, default,
    defined (where its section is), error."""
    found = []
    default = profiles.default_name(conf)
    for name in profiles.names(conf):
        base = {"name": name, "default": name == default, "defined": defined_in(conf, name)}
        try:
            profile = profiles.resolve(conf, name)
        except profiles.ProfileError as error:
            found.append({**base, "error": str(error)})
            continue
        command = profile.get("command")
        found.append({**base, "preset": profile["preset"], "label": profile.get("label") or "",
                      "model": profile.get("model") or "", "capability": profiles.capability(conf, name),
                      "capacity": profile.get("capacity"), "pool": profile.get("pool") or "", "command": command or "",
                      "found": bool(not command or shutil.which(command)), "verified": bool(profile.get("verified")),
                      "error": None})
    return found


def detail(conf, name):
    """(dotted key, value, source) for every resolved key of one profile; source names the layer that sets it, else
    the preset."""
    profile = profiles.resolve(conf, name)
    profile.pop("name", None)
    return [(key, value, cfg.source(conf, key)[0] or f"package (preset {profile['preset']})")
            for key, value in cfg.leaves(profile, f"workers.{name}")]


def target_file(conf, project=False):
    """The file a change goes to: the project's theseus.local.toml with project, else the host config.toml."""
    if project:
        if conf.root is None:
            raise ProfileFileError("--project needs to run inside a project")
        return Path(conf.root) / cfg.LOCAL_NAME
    return hostconfig.path()


def file_of(conf, name, project=None):
    """The file that holds [workers.<name>]: the lowest file that sets it (where it is defined; a project file above
    it only overrides keys), or the target_file when project is set."""
    if project is not None:
        return target_file(conf, project)
    found = layers_of(conf, name)
    if not found:
        raise ProfileFileError(f"{name} is a package preset with no section of its own; `theseus profiles add NAME "
                               f"--preset {name}` makes a variant, or `theseus profiles edit` opens the host file")
    return Path(found[0][1])


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {toml_value(item)}" for key, item in value.items()) + " }"
    raise ProfileFileError(f"cannot write {value!r} as TOML")


def check_file(file):
    """Problems in a host or project file's profiles (and, for the host file, the rest of it), as lines."""
    file = Path(file)
    try:
        data = tomllib.loads(file.read_text()) if file.is_file() else {}
    except tomllib.TOMLDecodeError as error:
        return [f"{file}: {error}"]
    if file == hostconfig.path():
        errors, _ = hostconfig.validate(data, file, profiles.problems)
    else:
        errors = [f"{file}: [workers.{name}] {problem}" for name, section in (data.get("workers") or {}).items()
                  if isinstance(section, dict) for problem in profiles.problems(name, section)]
    if file == hostconfig.path():
        for name, section in (data.get("workers") or {}).items():
            if isinstance(section, dict) and "preset" not in section and profiles.ALIASES.get(name, name) not in profiles.PRESETS:
                errors.append(f"{file}: [workers.{name}] needs preset = \"...\" (it is not a preset's name); presets: "
                              f"{', '.join(sorted(profiles.PRESETS))}")
    return errors


def section_span(lines, name):
    """(first, end) line indexes of [workers.<name>] and its subtables in lines, or None."""
    start = next((i for i, line in enumerate(lines) if (m := HEADER.match(line)) and m.group(1) == name
                  and not m.group(2)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        match = HEADER.match(lines[end])
        if ANY_HEADER.match(lines[end]) and not (match and match.group(1) == name):
            break
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def write_checked(file, text, before):
    """Write text, keeping it only if the file validates; otherwise restore before (None: no file) and raise."""
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    errors = check_file(file)
    if errors:
        if before is None:
            file.unlink()
        else:
            file.write_text(before)
        raise ProfileFileError("not written:\n" + "\n".join(errors))


def add(conf, name, preset, values=None, project=False):
    """Append [workers.<name>] (preset = ..., then values) to the target file. Returns (file, line)."""
    if not NAME.fullmatch(name or ""):
        raise ProfileFileError(f"{name!r} is not a profile name: use letters, digits, - and _")
    preset = profiles.ALIASES.get(preset, preset)
    if preset not in profiles.PRESETS:
        raise ProfileFileError(f"unknown preset {preset!r}; presets: {', '.join(sorted(profiles.PRESETS))}")
    file = target_file(conf, project)
    before = file.read_text() if file.is_file() else None
    lines = (before or "").splitlines()
    if section_span(lines, name):
        raise ProfileFileError(f"{file} already has [workers.{name}]; `theseus profiles edit {name}` changes it")
    section = {"preset": preset, **{k: v for k, v in (values or {}).items() if v not in (None, "", [])}}
    problems = profiles.problems(name, section)
    if problems:
        raise ProfileFileError("; ".join(problems))
    body = [f"[workers.{name}]", *(f"{key} = {toml_value(value)}" for key, value in section.items())]
    text = (before or "").rstrip("\n")
    line = len(text.splitlines()) + (2 if text else 1)
    write_checked(file, (text + "\n\n" if text else "") + "\n".join(body) + "\n", before)
    return file, line


def remove(conf, name, project=None):
    """Delete [workers.<name>] and its subtables from the file that holds it. Returns the file."""
    file = file_of(conf, name, project)
    before = file.read_text() if file.is_file() else ""
    lines = before.splitlines()
    span = section_span(lines, name)
    if span is None:
        raise ProfileFileError(f"{file} has no [workers.{name}]")
    kept = lines[:span[0]] + lines[span[1]:]
    while kept and not kept[-1].strip():
        kept.pop()
    write_checked(file, "\n".join(kept) + "\n" if kept else "", before)
    return file


def editor():
    """The editor command: $VISUAL, else $EDITOR, else vim, vi or nano."""
    chosen = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if chosen:
        return shlex.split(chosen)
    found = next((name for name in ("vim", "vi", "nano") if shutil.which(name)), None)
    if not found:
        raise ProfileFileError("no editor: set $EDITOR")
    return [found]


def edit(file, name=None, ask=None, run=None):
    """Open file in the editor at [workers.<name>] (or at the top); after each save check it, and while it does not
    validate ask (when a terminal is attached) whether to edit again, revert or keep it; without one, revert. Returns
    a summary line; raises ProfileFileError when a file with problems is kept, or after a revert without a terminal."""
    file = Path(file)
    before = file.read_text() if file.is_file() else None
    line = None
    if name:
        span = section_span((before or "").splitlines(), name)
        if span is None:
            raise ProfileFileError(f"{file} has no [workers.{name}]; `theseus profiles add {name} --preset P` makes it")
        line = span[0] + 1
    if before is None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("")
    ask = ask if ask is not None else (input if sys.stdin.isatty() else None)
    run = run or (lambda argv: subprocess.run(argv).returncode)
    while True:
        run([*editor(), *([f"+{line}"] if line else []), str(file)])
        errors = check_file(file)
        if not errors:
            after = file.read_text()
            if before is None and not after.strip():
                file.unlink()
                return f"{file}: nothing written"
            return f"{file}: {'unchanged' if after == (before or '') else 'saved and valid'}"
        print("\n".join(errors), file=sys.stderr)
        answer = (ask("e) edit again, r) revert, k) keep it anyway [e] ") if ask else "r").strip().lower()[:1]
        if answer == "r":
            if before is None:
                file.unlink()
            else:
                file.write_text(before)
            if not ask:
                raise ProfileFileError(f"{file}: reverted (no terminal to ask whether to edit again)")
            return f"{file}: reverted"
        if answer == "k":
            raise ProfileFileError(f"{file} was kept with the problems above; `theseus config check` lists them")
