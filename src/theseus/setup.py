"""Host readiness: `theseus doctor` checks the tools the workflow uses, and `theseus setup rea` wires REA (the
reverse-engineering MCP server and its skill) into the configured agent clients through REA's own `rea setup`.

REA owns client registration and skill installation and records this machine's absolute paths in each client's
configuration; theseus stores no paths. It resolves REA, Ghidra and the JDK through `theseus.tools` and passes them
to `rea setup` as the provider environment.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import tools

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
