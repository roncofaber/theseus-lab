"""Host readiness: `theseus doctor` checks the tools the workflow uses and the host configuration, `theseus setup host`
writes a starter host configuration, and `theseus setup rea` wires REA (the reverse-engineering MCP server and its
skill) into the configured agent clients through REA's own `rea setup`.

REA owns client registration and skill installation and records this machine's absolute paths in each client's
configuration; theseus stores no paths. It resolves REA, Ghidra and the JDK through `theseus.tools` and passes them
to `rea setup` as the provider environment.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import hostconfig, tools

SKILL = "reverse-engineer-anything"
SKILL_HOMES = {"claude_code": "~/.claude/skills", "default": "~/.agents/skills"}


USER_SKILLS = {"claude_code": "~/.claude/skills", "agents": "~/.agents/skills"}


def packaged_skill(name="theseus-setup"):
    from importlib import resources
    return Path(str(resources.files("theseus") / "skills" / name))


def install_skill(client="agents", directory=None, force=False):
    """Copy the packaged theseus-setup skill into a user skill directory, so any project's agent can load it."""
    source = packaged_skill()
    base = Path(os.path.expanduser(directory or USER_SKILLS[client]))
    target = base / source.name
    if target.is_symlink() or (target.exists() and not force and (target / "SKILL.md").read_bytes() != (source / "SKILL.md").read_bytes()):
        if not force:
            return 1, [f"{target} exists and differs; pass --force to replace it"]
    if target.exists() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.is_symlink():
        target.unlink()
    shutil.copytree(source, target)
    return 0, [f"installed {target}"]


def host_template():
    from importlib import resources
    return (resources.files("theseus") / "templates" / "host" / "config.toml").read_text()


def host_check(text=None, file=None):
    """(errors, warnings) for the host config.toml (or for text that would be written to it)."""
    import tomllib
    from . import profiles
    file = file or hostconfig.path()
    try:
        data = tomllib.loads(text) if text is not None else hostconfig.parse(file)
    except (tomllib.TOMLDecodeError, hostconfig.ConfigError) as error:
        return [str(error) if text is None else f"{file}: {error}"], []
    return hostconfig.validate(data, file, profiles.problems, list(tools.CATALOG))


def host_rows():
    file = hostconfig.path()
    errors, warnings = host_check()
    rows = [("FAIL", "host config", error) for error in errors]
    rows += [("WARN", "host config", warning) for warning in warnings]
    if not errors:
        rows.append(("ok", "host config", str(file) if file.is_file() else f"none at {file} (optional; `theseus setup host`)"))
    rows += [("WARN", "deprecated file", note) for note in hostconfig.deprecation_notes()]
    return rows


TABLE = re.compile(r"^\s*(\[\[?)\s*([^\[\]#]+?)\s*\]\]?\s*(?:#.*)?$")


def tables(text):
    """(dotted name, first line, end line) of each single-bracket table, 0-based and end-exclusive. A table ends at the
    next header, leaving trailing blank and comment lines to whatever follows."""
    lines = text.splitlines()
    heads = [(i, m.group(1), re.sub(r"\s+", "", m.group(2))) for i, line in enumerate(lines) if (m := TABLE.match(line))]
    found = []
    for n, (start, brackets, name) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        if brackets == "[":
            found.append((name, start, end))
    return lines, found


def import_slices(project):
    """What --import-project moves: [(file, title, start, end, text)] and notes about what it leaves in place. Profiles
    come from theseus.toml and theseus.local.toml (a profile in both moves from theseus.toml only), tool locations from
    theseus.local.toml; each slice is checked to parse back to exactly the table it came from."""
    import tomllib
    project = Path(project).resolve()
    slices, notes, moved = [], [], set()
    for name in ("theseus.toml", "theseus.local.toml"):
        file = project / name
        if not file.is_file():
            continue
        text = file.read_text()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise hostconfig.ConfigError(f"{file}: {error}") from error
        lines, found = tables(text)
        wanted = {key: value for key, value in (data.get("workers") or {}).items() if isinstance(value, dict)}
        groups = {}
        for profile in wanted:
            if profile in moved:
                notes.append(f"[workers.{profile}] in {file} stays: it also exists in theseus.toml and now overrides the host profile")
                continue
            groups[f"[workers.{profile}]"] = ([t for t in found if t[0] == f"workers.{profile}" or t[0].startswith(f"workers.{profile}.")],
                                              ("workers", profile), wanted[profile])
        paths = (data.get("tools") or {}).get("paths")
        if name == "theseus.local.toml" and paths:
            groups["[tools.paths]"] = ([t for t in found if t[0] == "tools.paths"], ("tools", "paths"), paths)
        for title, (parts, (section, key), expected) in groups.items():
            chunk = "\n".join("\n".join(lines[start:end]) for _, start, end in parts)
            try:
                parsed = tomllib.loads(chunk).get(section, {}).get(key) if parts else None
            except tomllib.TOMLDecodeError:
                parsed = None
            if parsed != expected:
                notes.append(f"{title} in {file} is not written as its own tables (dotted keys or an inline table); copy it by hand")
                continue
            if section == "workers":
                moved.add(key)
            for _, start, end in parts:
                slices.append((file, title, start, end, "\n".join(lines[start:end])))
    return slices, notes


def setup_host(force=False, print_text=False, import_project=None, dry_run=False):
    """Write the starter host config.toml (refused over an existing file without force). With import_project, append
    that project's profile tables and local tool locations; the project files are never edited."""
    target = hostconfig.path()
    exists = target.is_file()
    if exists and not force and not import_project and not print_text:
        return 1, [f"{target} exists; pass --force to replace it with the starter (or --print to see it)"]
    base = target.read_text() if exists and not force else host_template()
    if not import_project:
        if print_text:
            return 0, [host_template().rstrip("\n")]
        if dry_run:
            return 0, [f"would write the starter to {target}; nothing written"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(base)
        return 0, [f"wrote {target}; edit it, then `theseus config check`"]
    import tomllib
    slices, notes = import_slices(import_project)
    current = tomllib.loads(base)
    clashes = [f"{title} already exists in {target}" for _, title, *_ in slices
               if (title.startswith("[workers.") and title[9:-1] in (current.get("workers") or {}))
               or (title == "[tools.paths]" and "paths" in (current.get("tools") or {}))]
    if not slices:
        return 1, notes + [f"nothing to import from {import_project}"]
    blocks, last = [], None
    for file, title, start, end, chunk in slices:
        if file != last:
            blocks.append(f"# Imported from {file}")
            last = file
        blocks.append(chunk)
    text = base.rstrip("\n") + "\n\n" + "\n\n".join(blocks) + "\n"
    errors, _ = host_check(text, target)
    if clashes or errors:
        return 1, sorted(set(clashes)) + errors + ["nothing written"]
    plan = [f"{'would move' if dry_run else 'moved'} {title} from {file} lines {start + 1}-{end}" for file, title, start, end, _ in slices]
    if print_text:
        return 0, [text.rstrip("\n")]
    if dry_run:
        return 0, plan + notes + [f"target: {target} ({'append' if exists and not force else 'new file from the starter'}); nothing written"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    removable = [f"  {file}: lines {start + 1}-{end} ({title})" for file, title, start, end, _ in slices]
    return 0, plan + notes + [f"wrote {target}", "the project files are unchanged; once `theseus config check` passes you can "
                              "remove these lines from them (keep project-specific keys such as capacity there):", *removable]


def rea_environment(conf):
    """Provider variables for the REA registration: Ghidra and the JDK as resolved on this machine, and an explicit
    checkpoint directory when the environment sets one."""
    env = {}
    for name, variable in (("ghidra", "GHIDRA_INSTALL_DIR"), ("java", "JAVA_HOME")):
        value, _ = tools.resolve(conf, name)
        if value:
            env[variable] = value
    if os.environ.get("REA_GHIDRA_CHECKPOINT_DIR"):
        env["REA_GHIDRA_CHECKPOINT_DIR"] = os.environ["REA_GHIDRA_CHECKPOINT_DIR"]
    return env


def clients(conf):
    value = conf.section("re").get("clients") or ["claude_code"]
    return [value] if isinstance(value, str) else list(value)


def run_json(command, env=None, timeout=300):
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=timeout)
    try:
        return json.loads(result.stdout), result
    except ValueError:
        return None, result


def rea_client_states(conf, rea):
    """REA's own view of every agent client (detected, configured), from a dry run that changes nothing."""
    command = [*rea, "setup", "--dry-run", "--skill=false", "--format", "json"]
    for client in clients(conf):
        command += ["--client", client]
    report, _ = run_json(command, rea_environment(conf))
    if not report:
        return {}
    return {state["client"]["name"]: state for state in report.get("clientStates", []) if "client" in state}


def skill_location(client):
    home = Path(os.path.expanduser(SKILL_HOMES.get(client, SKILL_HOMES["default"])))
    for candidate in (home / SKILL, Path(os.path.expanduser(SKILL_HOMES["default"])) / SKILL):
        if (candidate / "SKILL.md").exists():
            return candidate
    return None


def doctor(conf):
    """Rows of (status, name, detail): ok, WARN (optional capability missing) or FAIL (required)."""
    rows = []
    for name in ("git", "bash"):
        found = shutil.which(name)
        rows.append(("ok" if found else "FAIL", name, found or "not on PATH; required"))
    for name, purpose in (("jq", "theseus status, stop and wait"), ("tmux", "persistent instances"),
                          ("shellcheck", "the shell lint in the Supervisor suite"), ("systemd-run", "per-worker resource slices")):
        found = shutil.which(name)
        rows.append(("ok" if found else "WARN", name, found or f"not on PATH; needed for {purpose}"))
    from . import profiles
    try:
        default = profiles.resolve(conf)
        command = default.get("command")
        found = shutil.which(command) if command else "coordinator-launched"
        rows.append(("ok" if found else "FAIL", f"profile {default['name']}",
                     f"{default['label']}; {command or 'no command'}: {found or 'not on PATH; install it or choose another workers.default'}"))
    except profiles.ProfileError as error:
        rows.append(("FAIL", "profile", str(error)))
    detected = [f"{cmd} ({name})" for name, cmd, path in profiles.detected() if path]
    rows.append(("ok" if detected else "WARN", "agent CLIs", ", ".join(detected) or "none of claude, codex, opencode, gemini on PATH"))
    for status, name, value, source, purpose in tools.table(conf):
        if status == "ok":
            rows.append(("ok", name, f"{value} ({source})"))
        elif status == "FAIL":
            rows.append(("FAIL", name, f"needed by this project ([tools] need) but not found; `theseus tools set {name} PATH`"))
        else:
            rows.append(("WARN", name, f"not found; optional, for {purpose}"))
    rea = tools.rea_command(conf)
    if rea:
        version = subprocess.run([*rea, "--version"], capture_output=True, text=True).stdout.strip()
        rows.append(("ok" if version else "FAIL", "rea version", version or f"`{' '.join(rea)} --version` failed"))
        states = rea_client_states(conf, rea)
        for client in clients(conf):
            state = states.get(client)
            if state is None:
                rows.append(("FAIL", f"rea mcp ({client})", "REA did not report this client; check `rea setup --dry-run`"))
            else:
                configured = state.get("configured")
                rows.append(("ok" if configured else "FAIL", f"rea mcp ({client})",
                             "registered" if configured else f"{state.get('status')}; run `theseus setup rea`"))
            location = skill_location(client)
            rows.append(("ok" if location else "FAIL", f"rea skill ({client})",
                         str(location) if location else "missing; run `theseus setup rea`"))
    return rows + host_rows() + store_rows()


def store_rows():
    """The host store: path, schema version and size, last backup and maintenance, and project name conflicts."""
    from . import registry, store
    try:
        with store.for_host() as conn:
            info = store.check(conn, quick=True)
            found = registry.conflicts()
    except (store.StoreError, OSError, sqlite3.Error) as error:
        return [("FAIL", "store", str(error))]
    rows = [("ok", "store", f"{info['path']} schema {info['version']}, {info['size'] / 1048576:.1f} MiB")]
    backup = info["backup"]
    age = time.time() - store.epoch_of(backup["time"]) if backup else None
    rows.append(("ok" if age is not None and age < 2 * store.DAY else "WARN", "store backup",
                 f"{backup['time']} {backup['path']}" if backup else f"none yet in {info['backups']}; `theseus db backup`"))
    rows.append(("ok" if info["maintained"] else "WARN", "store maintenance",
                 info["maintained"] or "never; `theseus db maintain` (the queue runs it daily)"))
    for job, error in info["errors"].items():
        rows.append(("WARN", f"store {job}", f"last attempt failed: {error}"))
    for conflict in found:
        rows.append(("WARN", "project name", f"{conflict['name']} is {conflict['root']}; {conflict['other']} also claims it "
                                             f"and is not recorded: give one a different [project] name"))
    if not found:
        rows.append(("ok", "project names", "no two project roots claim the same name"))
    return rows


def setup_rea(conf, update=False):
    """Optionally update REA, then register its MCP server and skill for each configured client."""
    rea = tools.rea_command(conf)
    if not rea:
        return 1, ["REA was not found; install it (npm install --global rea-agents) or `theseus tools set rea COMMAND`"]
    lines = []
    env = rea_environment(conf)
    if update:
        result = subprocess.run([*rea, "update"], capture_output=True, text=True, timeout=900)
        lines.append("rea update: " + (" ".join(result.stdout.split())[:300] or result.stderr.strip()[:300]))
        if result.returncode != 0:
            return 1, lines
    status = 0
    for client in clients(conf):
        base = [*rea, "setup", "--client", client, "--yes", "--format", "json"]
        report, result = run_json(base + ["--skill-client", client], env)
        if report and report.get("code") == "skill_install_failed" and "symbolic link" in (report.get("remediation") or ""):
            target = Path(os.path.expanduser(SKILL_HOMES.get(client, SKILL_HOMES["default"]))) / SKILL
            lines.append(f"{client}: {target} is a symbolic link that REA will not overwrite; registering the MCP server only "
                         f"(the linked skill at {os.path.realpath(target)} is used as is)")
            report, result = run_json(base + ["--skill=false"], env)
        state = next((s for s in (report or {}).get("clientStates", []) if s.get("client", {}).get("name") == client), None)
        if not (state and state.get("configured")):
            status = 1
            detail = (report or {}).get("remediation") or (result.stderr or result.stdout).strip()[:400]
            lines.append(f"{client}: NOT configured ({(report or {}).get('code') or result.returncode}): {detail}")
        else:
            lines.append(f"{client}: REA MCP server registered" + (f" with {', '.join(sorted(env))}" if env else ""))
    lines.append("restart agent sessions (coordinator and workers) so they load the MCP server")
    return status, lines
