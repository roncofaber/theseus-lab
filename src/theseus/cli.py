"""theseus command line. Every subcommand prints its usage with --help; `theseus` alone on a terminal opens the control
console (`theseus console`, also `theseus top`)."""
import argparse
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from . import config as cfg


def load():
    try:
        conf = cfg.load()
    except cfg.ConfigError as error:
        sys.exit(f"theseus: {error}")
    from . import registry
    registry.record(conf)
    return conf


def script(name):
    return str(resources.files("theseus") / "scripts" / name)


def run_script(name, args, conf=None, pane=None):
    conf = conf or load()
    env = {**conf.environment(), **os.environ}
    if pane:
        from . import agents, profiles, workqueue
        agent = next((a for a in agents.agents(conf) if a["pane"] == pane), None)
        if agent is not None:
            env.update(profiles.instance_env(conf, workqueue.launched(conf, agent), agent["name"]))
        else:
            env = {**conf.environment(), **profiles.instance_env(conf), **os.environ}
    return subprocess.run(["bash", script(name), *args], cwd=conf.root, env=env).returncode


def emit(lines):
    if isinstance(lines, str):
        print(lines)
    else:
        for line in lines:
            print(line)


def guarded(function):
    from . import gitutil, integrate, tasks
    def wrapper(args):
        try:
            return function(args)
        except tasks.TaskError as error:
            sys.exit(f"theseus: {error}")
        except integrate.IntegrationError as error:
            print(f"theseus: {error}", file=sys.stderr)
            sys.exit(error.code)
        except gitutil.GitError as error:
            sys.exit(f"theseus: {error}")
        except cfg.ConfigError as error:
            sys.exit(f"theseus: {error}")
    return wrapper


# scaffold, config, check

def cmd_scaffold(args):
    from . import scaffold
    emit(scaffold.scaffold(args.target, name=args.name, force=args.force, init_git=not args.no_git, hooks=not args.no_hooks,
                           adopt=args.adopt))
    if args.adopt:
        print("next: merge theseus-adopt/ by hand, configure theseus.toml, then `theseus check` and `theseus verify`")
    else:
        print("next: fill AGENTS.md, MANIFEST.md and ROADMAP.md, configure theseus.toml, then `theseus check`")


def resolved(conf):
    return {
        "root": str(conf.root), "name": conf.name, "prefix": conf.prefix,
        "paths": {"data": str(conf.data), "scratch": str(conf.scratch), "worktrees": str(conf.worktrees),
                  "original": [str(p) for p in conf.original]},
        **{key: value for key, value in conf.raw.items() if key not in ("paths", "project")},
    }


def cmd_upgrade(args):
    from . import scaffold
    emit(scaffold.upgrade(load().root, dry_run=args.dry_run))


def source_rows(conf):
    """(dotted key, value, layer) for every resolved value: the project configuration with each configured profile
    expanded over its preset, then the host pools and store settings."""
    from . import hostconfig, pools, profiles, store
    rows = []
    configured = [name for name, value in conf.raw["workers"].items() if isinstance(value, dict)]
    for key, value in cfg.leaves(conf.raw, ""):
        if key.split(".")[:2] in (["workers", name] for name in configured):
            continue
        rows.append((key, value, cfg.source(conf, key)[0] or "package"))
    for name in configured:
        try:
            profile = profiles.resolve(conf, name)
        except profiles.ProfileError as error:
            rows.append((f"workers.{name}", str(error), "error"))
            continue
        profile.pop("name", None)
        for key, value in cfg.leaves(profile, f"workers.{name}"):
            rows.append((key, value, cfg.source(conf, key)[0] or f"package (preset {profile['preset']})"))
    labels = hostconfig.pool_sources()
    for key, value in cfg.leaves(pools.definitions(), ""):
        rows.append((f"pools.{key}", value, labels.get(key, "host")))
    labels = hostconfig.store_sources(store.DEFAULTS)
    for key, value in cfg.leaves(store.settings(), ""):
        rows.append((f"store.{key}", value, labels.get(key, "package")))
    return rows


def print_sources(conf):
    files = []
    for label, file, _ in conf.layers:
        files.append(f"{label} {file}{'' if file.is_file() else ' (absent)'}" if file else label)
    print("# layers, lowest to highest: " + "; ".join(files))
    for key, value, label in source_rows(conf):
        print(f"{key} = {json.dumps(value)}    # {label}")


def cmd_config(args):
    if args.action == "check":
        return config_check()
    conf = load()
    if args.action == "show":
        if args.sources:
            print_sources(conf)
        else:
            print(json.dumps(resolved(conf), indent=2))
        return
    value = resolved(conf)
    for part in (args.key or "").split("."):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            sys.exit(f"theseus: no configuration key {args.key}")
        value = value[part]
    print(value if isinstance(value, str) else json.dumps(value))


def config_check():
    """Validate the host config.toml and, inside a project, the project configuration; exit 1 on any error."""
    from . import hostconfig, profiles, setup
    hostconfig.WARN_UNKNOWN = False
    errors, warnings = setup.host_check()
    try:
        root = cfg.find_root()
    except cfg.ConfigError as error:
        root = None
        if os.environ.get("THESEUS_ROOT"):
            errors.append(str(error))
    if root:
        errors += [f"{file}: unknown configuration sections: {', '.join(sections)}"
                   for file, sections in cfg.unknown_sections(root)]
        try:
            conf = cfg.load(root=root)
            conf.instances()
            conf.consumer_patterns()
            for name in profiles.names(conf):
                profiles.resolve(conf, name)
            from . import supervisor
            if conf.section("agents").get("managed") or conf.section("agents").get("counts"):
                found, notes = supervisor.problems(conf)
                errors += found
                warnings += notes
            for label, file, data in conf.layers:
                if label == "project" and (data.get("instances") or {}).get("panes"):
                    warnings.append(f"{file}: [instances] panes lists tmux pane ids, which belong to one machine; move them "
                                    f"to {cfg.LOCAL_NAME} (Git-ignored), or let theseus supervise the agents ([agents])")
                if label in ("project", "project-local"):
                    for name, section in (data.get("workers") or {}).items():
                        if isinstance(section, dict):
                            warnings += [f"{file}: [workers.{name}] {problem}" for problem in profiles.problems(name, section)]
        except (cfg.ConfigError, profiles.ProfileError) as error:
            if not (errors and str(error).startswith(str(hostconfig.path()))):
                errors.append(str(error))
    for line in warnings:
        print(f"warning: {line}")
    for note in hostconfig.deprecation_notes():
        print(f"note: {note}")
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    if errors:
        sys.exit(1)
    host = hostconfig.path()
    print(f"host config {host} is valid" if host.is_file() else f"no host config at {host} (optional; `theseus setup host`)")
    print(f"{cfg.CONFIG_NAME} at {root} is valid" if root else "not inside a project; checked the host config only")


def cmd_check(args):
    conf = load()
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tools/Supervisor", "-p", "test_*.py"]
    if args.verbose:
        command.append("-v")
    sys.exit(subprocess.run(command, cwd=conf.root, env={**os.environ, "THESEUS_ROOT": str(conf.root)}).returncode)


# tasks

def cmd_tasks(args):
    from . import tasks as t
    conf = load()
    queue = t.Queue(conf)
    action = args.task_action
    if action == "add":
        emit(t.add(queue, args.id, args.role, args.owned, args.objective, args.contract, args.accept, args.depends, args.milestone,
                   args.room, args.priority, args.profile, args.min_capability))
    elif action == "list":
        emit(t.list_tasks(queue, args.status, args.all, args.evidence))
    elif action == "ready":
        ids = t.ready(queue, args.include_paused, args.any_milestone)
        emit(ids)
        if not ids:
            sys.exit(1)
    elif action == "show":
        task = queue.get(args.id)
        print(json.dumps(task.get(args.field) if args.field else task, indent=2, ensure_ascii=False))
    elif action == "prompt":
        print(t.render_prompt(queue, args.id, args.base, args.worktree, args.log_dir), end="")
    elif action == "assign":
        emit(t.assign(queue, args.id, args.owner, args.provider, args.model, args.worktree, args.base, args.log_dir, args.force))
    elif action == "release":
        emit(t.release(queue, args.id, args.reason))
    elif action == "migrate-ids":
        emit(t.migrate_ids(queue))
    elif action == "deliver":
        emit(t.deliver(queue, args.id, args.handoff))
    elif action == "note":
        emit(t.note(queue, args.id, args.text))
    elif action == "priority":
        emit(t.set_priority(queue, args.id, args.value))
    elif action == "profile":
        emit(t.set_profile(queue, args.id, args.profile))
    elif action == "capability":
        emit(t.set_min_capability(queue, args.id, args.level))
    elif action in ("hold", "release-hold"):
        emit(t.hold(queue, args.id, action == "hold"))
    elif action == "reopen":
        emit(t.reopen(queue, args.id, args.reason))
    elif action == "merged":
        emit(t.mark_merged(queue, args.id, args.commit))
    elif action == "rebase":
        from . import integrate
        emit(integrate.rebase(conf, args.id))
    elif action == "unmerge":
        from . import integrate
        emit(integrate.unmerge(conf, args.id, args.reason, revert=not args.no_revert))
    elif action == "observe":
        emit(t.observe(queue, args.id, args.how, "userTested" if args.user else "observed"))
    elif action == "complete":
        emit(t.complete(queue, args.id, args.summary, args.commit, args.check or [], args.log, args.limit, args.from_log, args.force))
    elif action == "check-delivery":
        lines, outside = t.check_delivery(queue, args.id, args.accept or [])
        emit(lines)
        if outside:
            sys.exit(1)
    elif action == "consumers":
        names, consumers = t.outside_consumers(queue, queue.get(args.id))
        print(f"{len(names)} names declared in owned files; {len(consumers)} files outside ownedPaths reference them")
        for path in sorted(consumers, key=lambda item: (-len(consumers[item]), item))[:args.limit]:
            found = sorted(consumers[path])
            print(f"  {path}: {', '.join(found[:6])}{' ...' if len(found) > 6 else ''}")
        if consumers:
            print("A task that renames, deletes or changes these must own those files or deliver a patch for them.")
    elif action == "retire":
        emit(t.retire(queue, args.id, args.force))
    elif action == "stats":
        emit(t.stats(conf, args.since))
    elif action == "archive":
        emit(t.archive(queue))
    elif action == "milestone":
        if args.value is None:
            print(queue.milestone() or "none")
        else:
            emit(t.set_milestone(queue, args.value))


# workers

def cmd_instance(args):
    conf = load()
    if args.instance_action == "assign":
        from . import integrate
        from . import profiles
        provider = args.provider or profiles.default_name(conf)
        name = f"{args.instance}-{args.suffix}"
        if args.in_place:
            worktree, base, message = integrate.assign_in_place(conf, args.task, name, provider, "persistent-instance",
                                                                owner=f"{provider}:{args.instance}:{name}", instance=args.instance)
        else:
            worktree, base, message = integrate.assign(conf, args.task, name, provider, "persistent-instance",
                                                       owner=f"{provider}:{args.instance}:{name}", commit=not args.no_commit,
                                                       instance=args.instance)
        print(message)
        print(f"worktree {worktree} base {base[:12]}")
        return
    if args.instance_action == "send":
        sys.exit(instance_send(conf, args))
    if args.instance_action == "watch":
        sys.exit(run_script("instance-watch.sh", args.panes, conf))
    if args.instance_action in ("restart", "compact"):
        from . import agents
        named = next((a for a in agents.agents(conf) if a["name"] == args.pane), None)
        if named is not None:
            args.pane = named["pane"]
        if args.instance_action == "restart" and (named or {}).get("managed"):
            from . import supervisor, workqueue
            if args.dry_run:
                print(f"{named['name']}: would restart, resuming {named.get('session_id') or 'a fresh session'}")
                sys.exit(0)
            with workqueue.locked(conf):
                ok, detail = supervisor.restart(conf, named["name"])
            print(f"{named['name']}: {detail}", file=sys.stdout if ok else sys.stderr)
            sys.exit(0 if ok else 1)
    if args.instance_action == "restart":
        sys.exit(run_script("instance-restart.sh", [args.pane, *(["--dry-run"] if args.dry_run else [])], conf, args.pane))
    if args.instance_action == "compact":
        extra = ["--clear"] if args.clear else []
        status = run_script("instance-compact.sh", [args.pane, *extra, *([args.keep] if args.keep else [])], conf, args.pane)
        if status == 0 and args.clear:
            from . import rooms
            for entry in conf.instances():
                if entry["pane"] == args.pane:
                    print(rooms.clear_taint(conf, entry["name"]))
        sys.exit(status)


def instance_send(conf, args):
    """theseus instance send AGENT (TEXT | --file PATH), or the older PANE TASK WORKTREE [DEADLINE] task message."""
    import time
    from . import agents, workqueue
    registered = agents.agents(conf)
    agent = next((a for a in registered if args.target in (a["name"], a["pane"])), None)
    if agent is None:
        if not args.target.startswith("%") and ":" not in args.target:
            print(f"theseus: {args.target} is neither a registered agent nor a tmux pane (%N or session:window.pane)",
                  file=sys.stderr)
            return 2
        agent = {"name": args.target, "pane": args.target}
    if args.file and args.rest or not args.file and len(args.rest) not in (1, 2, 3):
        print("usage: theseus instance send AGENT (TEXT | --file PATH), or PANE TASK WORKTREE [DEADLINE]", file=sys.stderr)
        return 2
    task_id = None
    if args.file:
        text = Path(args.file).read_text().strip()
    elif len(args.rest) == 1:
        text = args.rest[0]
    else:
        from . import rooms
        from . import tasks as t
        task_id, worktree = args.rest[0], args.rest[1]
        task = t.Queue(conf).get(task_id)
        record = (f"Owned paths: {', '.join(task['ownedPaths'])}. Objective: {task['objective']} "
                  f"Contract: {task.get('generalContract') or 'not recorded'} Acceptance: {' | '.join(task['acceptance'])}.")
        if task.get("assignmentNote"):
            record += f" Coordinator notes: {task['assignmentNote']}"
        log_dir = (task.get("execution") or {}).get("logDir")
        if log_dir:
            record += f" Log directory (handoff.md, changed.sha256): {log_dir}."
        record += rooms.message(conf, task)
        if task.get("worktree") == str(conf.root):
            record += (" In-place assignment: there is no separate worktree; edit only your owned paths in the main checkout,"
                       " other workers share it, and never commit, stash or push.")
        deadline = args.rest[2] if len(args.rest) == 3 else ""
        text = workqueue.task_message(conf, task_id, worktree, record, deadline, workqueue.launched(conf, agent))
    if not text:
        print("theseus: nothing to send", file=sys.stderr)
        return 2
    poll = float(os.environ.get("THESEUS_TYPE_POLL") or 5)
    deadline_at = time.monotonic() + args.wait
    server = agents.where(agent)[0]
    while agents.pane_busy(conf, agent["pane"], workqueue.launched(conf, agent), server=server) and time.monotonic() < deadline_at:
        time.sleep(poll)
    if agents.pane_busy(conf, agent["pane"], workqueue.launched(conf, agent), server=server):
        print(f"{agent['pane']} still busy after {args.wait:g}s; not sending", file=sys.stderr)
        return 1
    ok, detail = agents.send_verified(conf, agent, text, task_id)
    print(f"{agent['pane']}: {detail}" + (f" ({task_id})" if task_id and ok else ""), file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_dispatch(args):
    from . import dispatch
    conf = load()
    emit(dispatch.dispatch(conf, args.task, name=args.name, provider=args.provider, model=args.model,
                           dry_run=args.dry_run, resume_paused=args.resume_paused, commit=not args.no_commit))


def cmd_merge(args):
    from . import integrate
    conf = load()
    sha, report = integrate.merge(conf, args.task, subject=args.subject, accept=args.accept or [],
                                  skip_patches=args.skip_patch or [], commit=not args.no_commit, regenerate=not args.no_index)
    emit(report)


def cmd_shell(name):
    def handler(args):
        sys.exit(run_script(name, args.rest))
    return handler


# verification

def cmd_build(args):
    from . import verify
    sys.exit(verify.run_kind(load(), "build"))


def cmd_smoke(args):
    from . import verify
    sys.exit(verify.smoke(load(), push=args.push))


def cmd_verify(args):
    from . import verify
    conf = load()
    if args.debt:
        unverified, limit = verify.debt(conf, args.revision)
        print(f"{len(unverified)} code commits since the last passing full verification (limit {limit or 'none'})")
        for commit in unverified[:10]:
            print("  " + subprocess.run(["git", "-C", str(conf.root), "log", "-1", "--format=%h %s", commit],
                                        capture_output=True, text=True).stdout.strip())
        sys.exit(1 if limit and len(unverified) > limit else 0)
    status, _ = verify.full(conf, args.revision)
    sys.exit(status)


def cmd_heavy(args):
    from . import verify
    conf = load()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        sys.exit("usage: theseus heavy -- COMMAND...")
    with verify.heavy_slot(conf):
        sys.exit(subprocess.run(command, env={**os.environ, **conf.environment()}).returncode)


def cmd_guard(args):
    from . import verify
    conf = load()
    status, messages = verify.guard(conf, args.remote or "", sys.stdin.read().splitlines())
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(status)


# docs, re, cleanup

def cmd_docs(args):
    from . import docs
    from .docs import fold
    conf = load()
    if args.docs_action == "index":
        emit([f"wrote {p}" for p in docs.regenerate(conf)])
        return
    if args.docs_action == "check":
        problems = docs.check(conf)
        emit(problems)
        if problems:
            sys.exit(1)
        print("docs indexes and fold map are current")
        return
    if args.docs_action == "fold-check":
        problems = fold.run(conf, args.topic, True)
    else:
        ok, problems = fold.apply(conf, args.topic, args.dry_run, not args.no_verify)
        emit(problems)
        if not ok:
            sys.exit("refusing to move reports: topic check failed")
        if not args.dry_run:
            emit([f"wrote {p}" for p in docs.regenerate(conf)])
        return
    emit(problems)
    if problems:
        sys.exit(1)
    print("docs fold check passed")


def cmd_re(args):
    from .re import catalog
    conf = load()
    sys.exit(catalog.main(conf, args) or 0)


def cmd_doctor(args):
    from . import setup
    rows = setup.doctor(load())
    for status, name, detail in rows:
        print(f"{status:<5} {name:<22} {detail}")
    failed = [name for status, name, _ in rows if status == "FAIL"]
    print(f"{len(failed)} required check(s) failed" if failed else "all required checks passed")
    sys.exit(1 if failed else 0)


def cmd_setup(args):
    from . import setup
    if args.component == "host":
        status, lines = setup.setup_host(force=args.force, print_text=args.print_text, import_project=args.import_project,
                                         dry_run=args.dry_run)
        emit(lines)
        sys.exit(status)
    if args.component == "skill":
        if args.print_path:
            print(setup.packaged_skill() / "SKILL.md")
            return
        status, lines = setup.install_skill(args.client, args.dir, args.force)
        emit(lines)
        sys.exit(status)
    status, lines = setup.setup_rea(load(), update=args.update)
    emit(lines)
    sys.exit(status)


def cmd_tools(args):
    import shlex
    from . import tools
    conf = load()
    if args.action == "set":
        if not args.name or not args.value:
            sys.exit("usage: theseus tools set NAME VALUE")
        print(tools.set_tool(conf, args.name, args.value, host=args.host))
        return
    if args.action == "env":
        for key, value in sorted(tools.environment(conf).items()):
            print(f"export {key}={shlex.quote(value)}")
        return
    for status, name, value, source, purpose in tools.table(conf):
        print(f"{status:<5} {name:<10} {value}  [{source}]")


AGENT_ACTIONS = ("list", "ls", "add", "remove", "enable", "disable", "profiles", "up", "down", "restart", "attach", "logs",
                 "migrate", "scale", "_exec", "_died")


def cmd_agents(args):
    from . import agents
    if args.action == "_died":
        from . import supervisor
        sys.exit(supervisor.died(*(args.targets + ["", "", "", "", ""])[:5]))
    if args.action == "_exec":
        from . import supervisor
        if len(args.targets) != 3:
            sys.exit("usage: theseus agents _exec PROJECT AGENT GEN")
        sys.exit(supervisor.exec_agent(*args.targets))
    args.name, args.pane = (args.targets + [None, None])[:2]
    if args.action in ("up", "down", "restart", "attach", "logs", "migrate", "ls", "scale"):
        return cmd_supervised(args)
    conf = load()
    action = args.action
    try:
        if action in ("add", "remove"):
            from . import supervisor
            rec = supervisor.record(conf, args.name) or {}
            supervised = supervisor.managed(conf) and (args.name in {w["name"] for w in supervisor.desired(conf)}
                                                        or rec.get("managed"))
            if supervised and action == "remove" and rec:
                if agents.current_task(conf, args.name):
                    sys.exit(f"theseus: {args.name} holds a task; release it first (theseus tasks release ID)")
                info = supervisor.pane_info(supervisor.server_of(rec), rec["target"]) if rec.get("target") else None
                if info and info.get("pane_dead") == "0":
                    sys.exit(f"theseus: {args.name} is running; stop it first (theseus agents down {args.name})")
                if rec.get("target") and supervisor.server_of(rec).has_session(rec["target"]):
                    supervisor.server_of(rec).kill_session(rec["target"])
            elif supervised:
                sys.exit(f"theseus: {args.name} is a supervised agent; edit [agents] in {cfg.LOCAL_NAME} and run "
                         f"`theseus agents up`")
        if action == "add":
            if not args.name or not args.pane:
                sys.exit("usage: theseus agents add NAME PANE [--provider P] [--rooms clean,dirty]")
            from . import profiles
            profile = profiles.resolve(conf, args.provider)
            print(agents.add(conf, args.name, args.pane, args.provider, args.rooms.split(",") if args.rooms else None))
            if not profile.get("interactive"):
                print(f"warning: profile {profile['name']} has no persistent-instance settings; use it with theseus dispatch",
                      file=sys.stderr)
            elif not agents.busy_detectable(conf, profile["name"]):
                print(f"warning: profile {profile['name']} has no busy marker, so a working agent and an idle one look the "
                      f"same; deliveries are still detected from the handoff, but stalls are only reported as quiet. Set "
                      f"[workers.{profile['name']}.interactive] busy_pattern to text the agent shows only while working.",
                      file=sys.stderr)
        elif action == "remove":
            print(agents.remove(conf, args.name))
        elif action in ("enable", "disable"):
            print(agents.set_enabled(conf, args.name, action == "enable"))
        elif action == "profiles":
            from . import profiles
            for name, mark, text in profiles.describe(conf):
                print(f"{name:<16} {mark:<8} {text}")
        else:
            for info in agents.states(conf):
                print(f"{info['name']:<20} {info['pane']:<6} {info['state']:<10} {info.get('task') or '-':<36} "
                      f"{'tainted:' + info['tainted'] if info.get('tainted') else ''}")
    except agents.AgentError as error:
        sys.exit(f"theseus: {error}")


def print_actions(rows):
    for name, action, detail in rows:
        print(f"{name:<24} {action:<14} {detail}")


def cmd_supervised(args):
    from . import supervisor
    action = args.action
    names = args.targets
    try:
        if action == "up":
            confs = all_managed() if args.all_projects else [load()]
            for conf in confs:
                if not supervisor.managed(conf):
                    sys.exit(f"theseus: {conf.name} does not supervise its agents; set [agents] managed = true and "
                             f"[agents.counts] in {cfg.LOCAL_NAME}")
                errors, warnings = supervisor.problems(conf)
                for line in warnings:
                    print(f"warning: {line}", file=sys.stderr)
                if errors:
                    sys.exit("theseus: " + "; ".join(errors))
                from . import workqueue
                with workqueue.locked(conf):
                    if not args.dry_run:
                        supervisor.set_paused(conf, False)
                    if names and not args.dry_run:
                        for name in names:
                            supervisor.update(conf, name, desired="up")
                    rows = supervisor.reconcile(conf, names or None, wait=not args.dry_run, dry_run=args.dry_run,
                                                reset=args.reset)
                if len(confs) > 1:
                    print(f"# {conf.name}")
                print_actions(rows or [("-", "none", "no agents configured ([agents.counts])")])
            return
        conf = load()
        if action == "ls":
            return list_supervised(conf, args.json)
        if action == "scale":
            if len(names) != 2 or not names[1].isdigit():
                sys.exit("usage: theseus agents scale PROFILE COUNT")
            if not supervisor.managed(conf):
                sys.exit(f"theseus: {conf.name} does not supervise its agents; set [agents] managed = true in {cfg.LOCAL_NAME}")
            from . import workqueue
            print(supervisor.set_count(conf, names[0], int(names[1])))
            conf = load()
            with workqueue.locked(conf):
                print_actions(supervisor.reconcile(conf, wait=True))
            return
        if action == "down":
            if not names and not args.all:
                sys.exit("usage: theseus agents down NAME... | --all [--force] [--now] [--replace]")
            print_actions(supervisor.down(conf, names or None, force=args.force, now=args.now))
            if args.replace and supervisor.managed(conf):
                from . import workqueue
                with workqueue.locked(conf):
                    print_actions(supervisor.reconcile(conf, wait=True))
            return
        if action == "restart":
            if args.drifted:
                names = [n for n, r in supervisor.records(conf).items()
                         if r.get("managed") and supervisor.drifted(conf, r) and not supervisor.current_task(conf, n)]
            if not names:
                sys.exit("usage: theseus agents restart NAME... [--fresh [--with-task [--file PROMPT]]] | --drifted")
            if (args.with_task or args.file) and not args.fresh:
                sys.exit("theseus: --with-task and --file go with --fresh (a plain restart resumes the conversation)")
            if args.file and not args.with_task:
                sys.exit("theseus: --file is the restart prompt of --with-task")
            prompt = Path(args.file).read_text().strip() if args.file else None
            if args.file and not prompt:
                sys.exit(f"theseus: {args.file} is empty")
            sys.exit(restart_agents(conf, names, args.fresh, args.with_task, prompt))
        if action == "migrate":
            if not names and not args.all:
                sys.exit("usage: theseus agents migrate NAME... | --all [--with-task]")
            from . import workqueue
            with workqueue.locked(conf):
                print_actions(supervisor.migrate(conf, names or None, with_task=args.with_task))
            return
        rec = supervisor.record(conf, args.name) if args.name else None
        if not rec or not rec.get("managed"):
            sys.exit(f"usage: theseus agents {action} NAME (a supervised agent; theseus agents ls)")
        if action == "attach":
            if args.window:
                ok, message = supervisor.open_window(rec, read_only=args.read_only)
                print(message, file=sys.stdout if ok else sys.stderr)
                sys.exit(0 if ok else 1)
            argv = supervisor.attach_argv(rec, read_only=args.read_only)
            os.execvp(argv[0], argv)
        if action == "logs":
            if args.transcript:
                path = supervisor.transcript(conf, rec)
                if path is None:
                    sys.exit(f"theseus: no transcript found for session {rec.get('session_id')}")
                print(path)
                return
            text = supervisor.capture(rec, history=args.lines, ansi=False)
            print(text.rstrip("\n"))
            if not text.strip():
                print(f"(the pane shows nothing; its history is empty under a fullscreen renderer: "
                      f"`theseus agents logs {args.name} --transcript` names the conversation file)", file=sys.stderr)
    except supervisor.SupervisorError as error:
        sys.exit(f"theseus: {error}")


def restart_agents(conf, names, fresh, with_task, prompt=None):
    """agents restart: an agent that holds a task restarts fresh only with --with-task, and then gets exactly one
    task-aware restart prompt (prompt, or one built from the task) before anything else is typed into it."""
    from . import agents, supervisor, workqueue
    failed = False
    for name in names:
        with workqueue.locked(conf):
            task = supervisor.current_task(conf, name)
            if fresh and task and not with_task:
                print_actions([(name, "refused", f"holds task {task['id']}; --with-task restarts it fresh and sends one "
                                                 f"restart prompt for the task")])
                failed = True
                continue
            supervisor.update(conf, name, desired="up")
            if (supervisor.record(conf, name) or {}).get("phase") == "failed":
                supervisor.update(conf, name, phase=None)
            before = (supervisor.last_run(conf, name) or {}).get("gen")
            ok, detail = supervisor.restart(conf, name, fresh=fresh)
            if fresh and task and (supervisor.last_run(conf, name) or {}).get("gen") != before:
                workqueue.owe_prompt(conf, name, task["id"], "a fresh restart", prompt)
            if ok and fresh and task:
                status, sent = workqueue.deliver_owed(conf, agents.entry(supervisor.record(conf, name)))
                detail += (f"; restart prompt for {task['id']} {sent}" if status == "sent" else
                           f"; restart prompt for {task['id']} not confirmed ({sent}); the queue retries it")
        failed |= not ok
        print_actions([(name, "restarted" if ok else "failed", detail)])
    return 1 if failed else 0


def all_managed():
    from . import registry, supervisor
    found = []
    for entry in registry.entries():
        try:
            conf = cfg.load(root=entry["root"])
        except (cfg.ConfigError, KeyError, TypeError):
            continue
        if supervisor.managed(conf):
            found.append(conf)
    return found


def list_supervised(conf, as_json=False):
    import time
    from . import agents, supervisor
    states = {info["name"]: info for info in agents.states(conf)}
    rows = []
    for item in supervisor.snapshot(conf):
        info = states.get(item["name"], {})
        rec = supervisor.record(conf, item["name"])
        row = {**item, "state": info.get("state"), "task": info.get("task"),
               "drift": supervisor.drifted(conf, rec) if rec else False,
               "uptime": int(time.time() - item["started"]) if item.get("started") and item.get("running") else None}
        rows.append(row)
    legacy = [info for name, info in states.items() if not info.get("managed")]
    paused = supervisor.paused(conf)
    if as_json:
        print(json.dumps({"paused": paused, "supervised": rows, "unmanaged": legacy}, indent=2, default=str))
        return
    if paused:
        print("supervision paused (`theseus agents down --all`); `theseus agents up` resumes it")
    for row in rows:
        uptime = f"{row['uptime'] // 60}m" if row["uptime"] is not None else "-"
        print(f"{row['name']:<24} {row['state'] or '-':<10} {row['profile'] or '-':<10} gen {row['gen'] or '-':<4} "
              f"pid {row['pid'] or '-':<8} {row['task'] or '-':<28} restarts {row['restarts']:<2} "
              f"{'drift ' if row['drift'] else ''}up {uptime} session {row['sessionId'] or '-'}")
    for info in legacy:
        print(f"{info['name']:<24} {info['state']:<10} unmanaged pane {info['pane']}")


def cmd_pool(args):
    from . import hostconfig, pools
    if args.action in ("hold", "release"):
        if not (args.name and args.agent and args.project):
            sys.exit("usage: theseus pool hold|release POOL AGENT --project NAME [--task ID] [--hours H]")
        if args.action == "hold":
            ok, why = pools.hold(args.project, args.name, args.agent, args.task or "-", args.hours)
            print(f"held {args.name} slot for {args.project}/{args.agent}" if ok else f"refused: {why}")
            sys.exit(0 if ok else 3)
        print("released" if pools.release(args.project, args.name, args.agent) else "no such slot")
        return
    names = [args.name] if args.name else sorted(pools.definitions())
    if not names:
        print(f"no pools defined; add [pools.NAME] to {hostconfig.path()} (see `theseus pool --help`)")
        return
    for name in names:
        info = pools.status(name)
        projects = ", ".join(f"{p} {n}" for p, n in sorted(info["projects"].items())) or "none"
        print(f"{name}: {info['active']}/{info['max'] or '-'} working ({projects}); priority {' > '.join(info['priority']) or '-'}; "
              f"claims {', '.join(info['claims']) or 'none'}")
        for lease in sorted(info["leases"].values(), key=lambda v: (v["project"], v["agent"])):
            print(f"  {lease['project']:<16} {lease['agent']:<20} {lease['task']}")


def cmd_queue(args):
    from . import workqueue
    conf = load()
    action = args.action
    if action == "tick":
        for record in workqueue.tick(conf, assign=not args.no_assign):
            print(f"{record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
    elif action == "run":
        workqueue.run(conf, interval=args.interval or int(conf.section("queue").get("interval") or 30))
    elif action == "wait":
        sys.exit(workqueue.wait(conf, timeout=args.timeout, interval=args.interval or int(conf.section("queue").get("interval") or 30),
                                drive=not args.no_drive))
    elif action in ("pause", "resume"):
        print(workqueue.set_paused(conf, action == "pause"))
    elif action == "log":
        for record in workqueue.events(conf, args.lines):
            print(f"{record['time']} {record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
    else:
        for key, items in workqueue.summary(conf).items():
            print(f"{key:<12} {len(items):>3}  {', '.join(x['id'] for x in items[:8])}")
        print("paused" if workqueue.paused(conf) else "running")


def cmd_top(args):
    from . import top
    if args.back:
        ok, message = top.go_back()
        print(message, file=sys.stdout if ok else sys.stderr)
        sys.exit(0 if ok else 1)
    interval = args.every or args.interval
    interactive = sys.stdout.isatty() and not args.once and not args.json
    try:
        conf = cfg.load()
    except cfg.ConfigError:
        conf = None
    if conf is not None:
        from . import registry
        registry.record(conf)
    if interactive:
        from .tui.app import run
        sys.exit(run(interval=interval, current=conf.root if conf and not args.host else None, roots=[conf.root] if conf else []))
    if args.host or conf is None:
        from . import host
        state = host.snapshot([conf.root] if conf else [])
        print(json.dumps(state, indent=2, default=str) if args.json else host.render_host(state))
        sys.exit(0)
    sys.exit(top.main(conf, interval=interval, once=args.once, as_json=args.json))


def cmd_projects(args):
    from . import registry
    found = registry.entries()
    if args.json:
        print(json.dumps(found, indent=2))
        return
    for entry in found:
        print(f"{entry.get('name') or '-':<24} {entry['root']}")
    if not found:
        print("no projects registered yet: run any theseus command inside a project")


def size_text(count):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024


def cmd_db(args):
    from . import store
    action = args.action
    if action == "restore" and not args.yes:
        sys.exit("theseus: restore replaces the host database for every project on this machine; "
                 "rerun with --yes (the current database is backed up first)")
    if action == "restore" and not args.file:
        sys.exit("usage: theseus db restore FILE --yes")
    if action == "restore":
        result = store.restore_file(args.file)
        print(f"saved the previous database as {result['saved']}")
        print(f"restored {result['restored']} (schema version {result['version']})")
        return
    with store.opened() as conn:
        if action == "backup":
            result = store.backup(conn)
            print(f"backup {result['path']} ({size_text(result['size'])}), integrity_check {result['integrity']}")
            for name in result["removed"]:
                print(f"  rotated out {name}")
        elif action == "maintain":
            result = store.maintain(conn)
            removed = ", ".join(f"{n} {table}" for table, n in result["removed"].items())
            print(f"trimmed {removed}; ANALYZE, optimize, {result['freed_pages']} free page(s) returned"
                  f"{' after converting to incremental vacuum' if result['vacuumed'] else ''}, WAL truncated; "
                  f"{size_text(result['before'])} -> {size_text(result['after'])}")
        elif action == "prune":
            older = args.older_than_days * 86400 if args.older_than_days else None
            names = store.prune_projects(conn, older_than=older, dry_run=not args.yes)
            verb = "removed" if args.yes else "would remove"
            print(f"{verb} {', '.join(names)} and all their rows (events, agents, runs, deliveries, samples, gateway "
                  f"errors and retries, pool slots)" if names
                  else "nothing to prune")
            if names and not args.yes:
                print("rerun with --yes to delete them")
        else:
            info = store.check(conn)
            ok = info["integrity"] == ["ok"]
            print(f"database      {info['path']}")
            print(f"integrity     {'ok' if ok else '; '.join(info['integrity'][:5])}")
            print(f"schema        version {info['version']} (this theseus knows {info['known']})")
            print(f"size          {size_text(info['size'])} (WAL {size_text(info['wal'])}, {info['pages']} pages of "
                  f"{info['page_size']} B, {info['free_pages']} free)")
            print("rows          " + ", ".join(f"{table} {count}" for table, count in info["rows"].items()))
            backup = info["backup"]
            print(f"last backup   {backup['time'] + ' ' + backup['path'] if backup else 'none'} (directory {info['backups']})")
            print(f"maintenance   {info['maintained'] or 'never'}")
            for job, error in info["errors"].items():
                print(f"last {job} error: {error}")
            if not ok:
                sys.exit(1)


def cmd_cleanup(args):
    from . import cleanup
    conf = load()
    emit(cleanup.run(conf, apply=args.apply, max_age_hours=args.max_age_hours,
                     skip_worktrees=args.skip_worktrees, skip_scratch=args.skip_scratch))


def build_parser():
    parser = argparse.ArgumentParser(prog="theseus", description=__doc__)
    parser.add_argument("--version", action="version", version=f"theseus {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scaffold", help="create the workflow layout in a target repository")
    p.add_argument("target")
    p.add_argument("--name", help="project name (default: the directory name)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument("--no-git", action="store_true", help="do not run git init in a directory that is not a repository")
    p.add_argument("--no-hooks", action="store_true", help="do not set core.hooksPath to tools/git-hooks")
    p.add_argument("--adopt", action="store_true",
                   help="bring theseus into an existing repository: overwrite nothing, put differing template files in "
                        "theseus-adopt/, leave the push hook and generated indexes alone")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("upgrade", help="refresh theseus-owned files (generic skills, hook, helpers) from this version's templates")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=guarded(cmd_upgrade))

    p = sub.add_parser("config", help="show, query or validate the resolved configuration",
                       description="Configuration layers, lowest to highest: package defaults and presets, the host "
                                   "~/.config/theseus/config.toml (THESEUS_CONFIG overrides the path), the project's "
                                   "theseus.toml, its theseus.local.toml, then THESEUS_* variables and command line flags.")
    p.add_argument("action", choices=("show", "get", "check"))
    p.add_argument("key", nargs="?", help="dotted key for get, e.g. paths.data or workers.default")
    p.add_argument("--sources", action="store_true", help="show: print each resolved value with the layer it came from")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("check", help="run the project's Supervisor tests (tools/Supervisor) with this interpreter")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_check)

    tasks = sub.add_parser("tasks", help="query and maintain the task queue")
    ts = tasks.add_subparsers(dest="task_action", required=True)
    p = ts.add_parser("add", help="write a new pending task record")
    p.add_argument("id", help="kebab-case ID, unique across the queue and the archive")
    p.add_argument("--role", required=True, help="tooling, implementation, research, fix, verification, docs ...")
    p.add_argument("--owned", nargs="+", required=True, metavar="PATH", help="owned paths; must include docs/investigations/NAME.md")
    p.add_argument("--objective", required=True, help="one sentence stating what the task achieves")
    p.add_argument("--contract", required=True, help="generalContract: the general behavior and its representative variations")
    p.add_argument("--accept", nargs="+", required=True, metavar="TEXT", help="acceptance items, at most six")
    p.add_argument("--depends", nargs="+", default=[], metavar="ID")
    p.add_argument("--milestone", help="milestone (default: the current one)")
    p.add_argument("--room", choices=("clean", "dirty"), help="clean-room separation (see [rooms] in theseus.toml)")
    p.add_argument("--priority", type=int, default=0, help="higher is assigned first among ready tasks (default 0)")
    p.add_argument("--profile", help="agent profile the task needs (default: any); see theseus tasks profile")
    p.add_argument("--min-capability", type=int, default=0, metavar="N",
                   help="lowest profile capability that may take the task (default 0: any); see theseus tasks capability")
    p = ts.add_parser("list", help="one line per task with readiness or blocking reasons")
    p.add_argument("--status", choices=("pending", "in_progress", "verification", "merged", "completed"))
    p.add_argument("--all", action="store_true", help="include archived tasks")
    p.add_argument("--evidence", action="store_true", help="show merged / observed / user-tested")
    p = ts.add_parser("ready", help="pending tasks of the current milestone whose dependencies are merged or completed")
    p.add_argument("--include-paused", action="store_true")
    p.add_argument("--any-milestone", action="store_true")
    p = ts.add_parser("show", help="print one task record, open or archived")
    p.add_argument("id")
    p.add_argument("--field")
    p = ts.add_parser("prompt", help="render the worker prompt for a task")
    p.add_argument("id")
    p.add_argument("--base", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--log-dir", required=True)
    p = ts.add_parser("assign", help="mark a ready task in_progress with its execution record (no worktree; see dispatch)")
    p.add_argument("id")
    for flag in ("--owner", "--provider", "--model", "--worktree", "--base", "--log-dir"):
        p.add_argument(flag, required=True)
    p.add_argument("--force", action="store_true")
    ts.add_parser("migrate-ids", help="record the owning agent's ID (ownerId, agentId on attempts) on task records "
                                      "written before agents had IDs; safe to rerun")
    p = ts.add_parser("release", help="return an assigned task to pending, keeping the attempt")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = ts.add_parser("deliver", help="mark a task delivered (awaiting coordinator integration)")
    p.add_argument("id")
    p.add_argument("--handoff", required=True)
    p = ts.add_parser("priority", help="set a task's priority (higher is assigned first; 0 removes it)")
    p.add_argument("id")
    p.add_argument("value", type=int)
    p = ts.add_parser("profile", help="set the agent profile a task needs: agents registered with it take it, and agents whose "
                                      "profile differs only in MCP servers or env are relaunched onto it first; no NAME: any")
    p.add_argument("id")
    p.add_argument("profile", nargs="?", default="")
    p = ts.add_parser("capability", help="set the lowest profile capability ([workers.NAME] capability, default 1) an "
                                         "agent needs to take a task; 0: any")
    p.add_argument("id")
    p.add_argument("level", type=int)
    p = ts.add_parser("note", help="set the coordinator note carried in the task message")
    p.add_argument("id")
    p.add_argument("text")
    p = ts.add_parser("hold", help="keep the queue from assigning a task")
    p.add_argument("id")
    p = ts.add_parser("release-hold", help="let the queue assign a held task")
    p.add_argument("id")
    p = ts.add_parser("reopen", help="undo a premature delivery (verification back to in_progress)")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = ts.add_parser("unmerge", help="take a merged task back to in_progress: revert its merge commit (or check it is "
                                      "already reverted with --no-revert), keeping the worktree for a redelivery")
    p.add_argument("id")
    p.add_argument("--reason", default="merge reverted")
    p.add_argument("--no-revert", action="store_true", help="the merge is already reverted; only update the record")
    p = ts.add_parser("rebase", help="record the worktree HEAD as the task base after the worker rebased onto the base "
                                     "branch; refuses when the worker committed")
    p.add_argument("id")
    p = ts.add_parser("merged", help="record a merge the coordinator committed by hand")
    p.add_argument("id")
    p.add_argument("--commit", required=True)
    p = ts.add_parser("observe", help="record runtime observation (or --user testing) of a merged task")
    p.add_argument("id")
    p.add_argument("--how", required=True, help="the command, build, scenario or tester that showed it working")
    p.add_argument("--user", action="store_true", help="record user testing instead of coordinator observation")
    p = ts.add_parser("complete", help="record a merged task as verified against its acceptance")
    p.add_argument("id")
    p.add_argument("--summary", required=True)
    p.add_argument("--from-log", help="a passing `theseus verify` log; fills commit, check and log")
    p.add_argument("--commit", help="default: the merge commit")
    p.add_argument("--check", action="append", help="exact command -> outcome; repeatable")
    p.add_argument("--log", help="log path (and SHA-256)")
    p.add_argument("--limit", action="append", default=[], help="remaining limitation; repeatable")
    p.add_argument("--force", action="store_true")
    p = ts.add_parser("check-delivery", help="compare a worker worktree's changes with ownedPaths and its manifest")
    p.add_argument("id")
    p.add_argument("--accept", action="append", help="a reviewed out-of-ownership path to accept; repeatable")
    p = ts.add_parser("consumers", help="files outside ownedPaths that reference names declared inside them")
    p.add_argument("id")
    p.add_argument("--limit", type=int, default=15)
    p = ts.add_parser("retire", help="archive a worker's .theseus records and remove its worktree")
    p.add_argument("id")
    p.add_argument("--force", action="store_true")
    p = ts.add_parser("stats", help="per provider and model: runs, results, minutes to handoff, follow-ups")
    p.add_argument("--since")
    ts.add_parser("archive", help="move completed tasks to tasks-archive.jsonl")
    p = ts.add_parser("milestone", help="print or set currentMilestone ('' clears it)")
    p.add_argument("value", nargs="?")
    tasks.set_defaults(func=guarded(cmd_tasks))

    inst = sub.add_parser("instance", help="persistent worker sessions in tmux panes")
    ins = inst.add_subparsers(dest="instance_action", required=True)
    p = ins.add_parser("assign", help="create the worktree and branch, record and commit the assignment")
    p.add_argument("task")
    p.add_argument("instance")
    p.add_argument("suffix")
    p.add_argument("--provider")
    p.add_argument("--no-commit", action="store_true")
    p.add_argument("--in-place", action="store_true",
                   help="no worktree and no commit: edit owned paths in the main checkout (docs-only rounds, or before a first commit)")
    p = ins.add_parser("send", help="type text into an agent's pane once idle and confirm it was submitted (send_failed "
                                    "otherwise); PANE TASK WORKTREE [DEADLINE] types a task message")
    p.add_argument("target", metavar="AGENT", help="registered agent name or tmux pane")
    p.add_argument("rest", nargs="*", metavar="TEXT", help="the text, or TASK WORKTREE [DEADLINE]")
    p.add_argument("--file", help="send the contents of this file instead of TEXT")
    p.add_argument("--wait", type=float, default=180, help="seconds to wait for an idle pane first (default 180)")
    p = ins.add_parser("watch", help="print HANDOFF and IDLE-NO-HANDOFF events (run under a monitor)")
    p.add_argument("panes", nargs="*", metavar="INSTANCE:PANE")
    p = ins.add_parser("restart", help="restart a pane's agent and resume its session (reloads MCP servers and skills)")
    p.add_argument("pane", metavar="AGENT", help="registered agent name or tmux pane")
    p.add_argument("--dry-run", action="store_true")
    p = ins.add_parser("compact", help="type the profile's compact command (--clear: its clear command) into an idle pane "
                                       "between tasks")
    p.add_argument("pane", metavar="AGENT", help="registered agent name or tmux pane")
    p.add_argument("--clear", action="store_true")
    p.add_argument("keep", nargs="?")
    inst.set_defaults(func=guarded(cmd_instance))

    p = sub.add_parser("dispatch", help="create a worker worktree, render the prompt, assign and launch a one-shot worker")
    p.add_argument("task")
    p.add_argument("--name")
    p.add_argument("--provider", "--profile", dest="provider", help="agent profile (default: workers.default; theseus agents profiles)")
    p.add_argument("--model")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume-paused", action="store_true")
    p.add_argument("--no-commit", action="store_true", help="refuse instead of committing an uncommitted task record")
    p.set_defaults(func=guarded(cmd_dispatch))

    p = sub.add_parser("merge", help="integrate a delivered task: ownership check, three-way apply, patches, indexes, commit")
    p.add_argument("task")
    p.add_argument("--subject", help="commit subject (default: the task objective)")
    p.add_argument("--accept", action="append", help="a reviewed out-of-ownership path to accept; repeatable")
    p.add_argument("--skip-patch", action="append", help="a worker patch file name to skip; repeatable")
    p.add_argument("--no-commit", action="store_true", help="apply without committing")
    p.add_argument("--no-index", action="store_true", help="do not regenerate the docs indexes")
    p.set_defaults(func=guarded(cmd_merge))

    for name, script_name, text in (("status", "status.sh", "dispatched workers and every other worktree"),
                                    ("stop", "stop.sh", "stop a dispatched shell worker and its slice: stop NAME"),
                                    ("followup", "followup.sh", "resume a stopped shell worker: followup NAME MESSAGE"),
                                    ("wait", "wait.sh", "block until a dispatched shell worker exits")):
        p = sub.add_parser(name, help=text, add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(func=cmd_shell(script_name))

    sub.add_parser("build", help="run commands.build").set_defaults(func=cmd_build)
    p = sub.add_parser("smoke", help="run commands.smoke; --push pushes the branch after a pass")
    p.add_argument("--push", action="store_true")
    p.set_defaults(func=cmd_smoke)
    p = sub.add_parser("verify", help="full verification of a commit on a clean worktree, or --debt")
    p.add_argument("revision", nargs="?", default="HEAD")
    p.add_argument("--debt", action="store_true", help="code commits since the last passing full verification")
    p.set_defaults(func=guarded(cmd_verify))
    p = sub.add_parser("heavy", help="run a command in one of limits.heavy_runs slots: heavy -- CMD")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_heavy)
    p = sub.add_parser("guard", help="pre-push gate (reads git's pre-push stdin)")
    p.add_argument("remote", nargs="?")
    p.add_argument("url", nargs="?")
    p.set_defaults(func=guarded(cmd_guard))

    docs = sub.add_parser("docs", help="report index, citation index and topic folding")
    ds = docs.add_subparsers(dest="docs_action", required=True)
    ds.add_parser("index", help="regenerate docs/investigations/README.md, the folded listing and the citation index")
    ds.add_parser("check", help="indexes current and fold map valid")
    p = ds.add_parser("fold-check", help="validate the fold map; --topic also checks a topic kept every citation")
    p.add_argument("--topic", action="append", default=[])
    p = ds.add_parser("fold-apply", help="move folded reports to the archive and rewrite links")
    p.add_argument("--topic", action="append", default=[])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    docs.set_defaults(func=guarded(cmd_docs))

    from .re import catalog
    catalog.add_parser(sub).set_defaults(func=guarded(cmd_re))

    sub.add_parser("doctor", help="check the host tools and the REA MCP and skill wiring").set_defaults(func=guarded(cmd_doctor))
    p = sub.add_parser("tools", help="external tools resolved on this machine: list, set NAME VALUE, env")
    p.add_argument("action", nargs="?", default="list", choices=("list", "set", "env"))
    p.add_argument("name", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--host", action="store_true", help="set: record the location in the host config.toml instead of theseus.local.toml")
    p.set_defaults(func=guarded(cmd_tools))
    p = sub.add_parser("setup", help="setup host (write a starter host config.toml), setup skill (install the theseus-setup "
                                     "agent skill), setup rea [--update]")
    p.add_argument("component", choices=("host", "rea", "skill"))
    p.add_argument("--update", action="store_true", help="rea: run `rea update` first")
    p.add_argument("--client", default="agents", choices=("agents", "claude_code"),
                   help="skill: ~/.agents/skills (agents, default) or ~/.claude/skills (claude_code)")
    p.add_argument("--dir", help="skill: install into this skills directory instead")
    p.add_argument("--force", action="store_true", help="skill: replace an existing, different copy; host: replace an existing file")
    p.add_argument("--print-path", action="store_true", help="skill: print the packaged SKILL.md path and exit")
    p.add_argument("--print", dest="print_text", action="store_true", help="host: print the file instead of writing it")
    p.add_argument("--import-project", metavar="PATH",
                   help="host: also move this project's [workers.*] profile tables and theseus.local.toml [tools.paths] into "
                        "the host file (the project files are not edited; the lines you can remove are printed)")
    p.add_argument("--dry-run", action="store_true", help="host: print what would be written or moved, write nothing")
    p.set_defaults(func=guarded(cmd_setup))

    p = sub.add_parser("agents", help="persistent agents: list, add NAME PANE, remove, enable, disable; supervised agents: "
                                      "up, down, ls, restart, attach, logs, migrate, scale",
                       description="Registered persistent agents. With [agents] managed = true theseus starts and supervises "
                                   "them itself on a private tmux server: `up` reconciles toward [agents.counts] live agents per "
                                   "profile, `down` stops one and takes it out of the count, `restart` restarts (resuming the conversation unless --fresh), `attach` opens the "
                                   "pane (C-q detaches), `logs` prints its screen, `migrate` moves a hand-made pane's agent "
                                   "onto the private server, `scale PROFILE COUNT` sets [agents.counts] in "
                                   "theseus.local.toml and reconciles.")
    p.add_argument("action", nargs="?", default="list", choices=AGENT_ACTIONS, metavar="ACTION",
                   help=", ".join(a for a in AGENT_ACTIONS if not a.startswith("_")))
    p.add_argument("targets", nargs="*", metavar="NAME", help="agent names (add: NAME PANE)")
    p.add_argument("--provider", "--profile", dest="provider", help="agent profile (theseus agents profiles)")
    p.add_argument("--rooms", help="comma-separated rooms this agent may work (clean,dirty)")
    p.add_argument("--dry-run", action="store_true", help="up: print the plan, change nothing")
    p.add_argument("--all-projects", action="store_true", help="up: every supervised project on this host")
    p.add_argument("--reset", action="store_true", help="up: clear the restart budget of failed agents")
    p.add_argument("--all", action="store_true", help="down, migrate: every agent of the project")
    p.add_argument("--force", action="store_true", help="down: stop an agent that holds a task")
    p.add_argument("--now", action="store_true", help="down: skip the graceful exit")
    p.add_argument("--replace", action="store_true", help="down: reconcile at once, starting replacements under free names")
    p.add_argument("--fresh", action="store_true", help="restart: start a new conversation")
    p.add_argument("--drifted", action="store_true", help="restart: every free agent whose launch configuration changed")
    p.add_argument("--read-only", action="store_true", help="attach: look without typing")
    p.add_argument("--window", action="store_true", help="attach: open in a new window of your own tmux")
    p.add_argument("--lines", type=int, default=200, help="logs: history lines (default 200)")
    p.add_argument("--transcript", action="store_true", help="logs: print the conversation transcript path instead")
    p.add_argument("--with-task", action="store_true", help="migrate: also move agents that hold a task; restart --fresh: "
                                                            "restart an agent that holds a task and send it one restart "
                                                            "prompt for the task")
    p.add_argument("--file", metavar="PROMPT", help="restart --fresh --with-task: the restart prompt to send instead of the "
                                                    "one built from the task")
    p.add_argument("--json", action="store_true", help="ls: JSON")
    p.set_defaults(func=guarded(cmd_agents))
    p = sub.add_parser("pool", help="host-wide worker pools shared across projects ([pools.NAME] in ~/.config/theseus/config.toml)",
                       description="Show who holds the slots of each pool. A pool caps the agents working at once across "
                                   "projects: [pools.NAME] max = N, priority = [\"project\", ...] in ~/.config/theseus/config.toml; "
                                   "a profile joins with pool = \"NAME\" in [workers.PROFILE].")
    p.add_argument("action", nargs="?", default="status", choices=("status", "hold", "release"),
                   help="hold or release a slot by hand for a worker no theseus queue tracks")
    p.add_argument("name", nargs="?", help="pool name")
    p.add_argument("agent", nargs="?", help="hold/release: the worker holding the slot")
    p.add_argument("--project", help="hold/release: project name as listed in the pool priority")
    p.add_argument("--task", help="hold: what the worker is doing")
    p.add_argument("--hours", type=float, default=4.0, help="hold: expiry if never released (default 4)")
    p.set_defaults(func=guarded(cmd_pool))

    p = sub.add_parser("queue", help="work queue: status, tick, run, wait, pause, resume, log")
    p.add_argument("action", nargs="?", default="status", choices=("status", "tick", "run", "wait", "pause", "resume", "log"))
    p.add_argument("--interval", type=int)
    p.add_argument("--timeout", type=int, default=10800)
    p.add_argument("--lines", type=int, default=20)
    p.add_argument("--no-assign", action="store_true", help="tick: report deliveries and stalls but assign nothing")
    p.add_argument("--no-drive", action="store_true", help="wait: only watch events, do not run ticks")
    p.set_defaults(func=guarded(cmd_queue))
    p = sub.add_parser("console", aliases=["top"], help="the control console of every project on this host (also `theseus` "
                                                        "alone on a terminal): agents, coordinator, queue, pools, events "
                                                        "and actions; --once and --json print one frame for scripts")
    p.add_argument("interval", nargs="?", type=int, default=5, help="refresh seconds (default 5)")
    p.add_argument("--interval", dest="every", type=float, help="refresh seconds, fractions allowed")
    p.add_argument("--once", action="store_true", help="print one plain frame and exit")
    p.add_argument("--json", action="store_true", help="print the snapshot as JSON and exit")
    p.add_argument("--host", action="store_true", help="the host-wide view instead of the current project")
    p.add_argument("--back", action="store_true",
                   help="switch this tmux client back to the dashboard pane it left when it jumped to an agent")
    p.set_defaults(func=cmd_top)
    p = sub.add_parser("projects", help="list the projects registered on this host")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("db", help="host store: check, maintain, backup, restore FILE --yes, prune",
                       description="The host database (~/.local/share/theseus/theseus.db, THESEUS_DB overrides) holds "
                                   "projects, agents, events, pool slots, deliveries and samples for every project on "
                                   "this machine. The queue runs maintain and backup at most once a day by itself.")
    p.add_argument("action", nargs="?", default="check", choices=("check", "maintain", "backup", "restore", "prune"),
                   help="check: integrity, version, sizes, rows, last backup and maintenance; maintain: trim by "
                        "retention, ANALYZE, optimize, checkpoint, vacuum; backup: online copy, integrity check and "
                        "rotation; restore FILE --yes: back up the current database, then load FILE; prune: list (with "
                        "--yes delete) projects whose root is gone")
    p.add_argument("file", nargs="?", help="restore: the backup file to load")
    p.add_argument("--yes", action="store_true", help="restore, prune: really do it")
    p.add_argument("--older-than-days", type=float, help="prune: also projects nobody used for this many days")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("cleanup", help="reclaim disk from retired worktrees and old scratch (dry run by default)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--max-age-hours", type=float, default=24)
    p.add_argument("--skip-worktrees", action="store_true")
    p.add_argument("--skip-scratch", action="store_true")
    p.set_defaults(func=guarded(cmd_cleanup))
    return parser


SHELL_COMMANDS = {"status": "status.sh", "stop": "stop.sh", "followup": "followup.sh", "wait": "wait.sh"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["console"]
    if argv == ["--schema-version"]:
        from . import store
        print(store.SCHEMA_VERSION)
        return
    if argv and argv[0] in SHELL_COMMANDS:
        sys.exit(run_script(SHELL_COMMANDS[argv[0]], argv[1:]))
    parser = build_parser()
    args = parser.parse_args(argv)
    from . import store, upgrade
    while True:
        try:
            args.func(args)
            return
        except store.SchemaTooNew as error:
            if not (args.command == "queue" and args.action in ("run", "wait")):
                sys.exit(f"theseus: {error}")
            from . import upgrade
            upgrade.newer_store(error, argv, interval=args.interval or 30)
        except upgrade.CodeChanged:
            upgrade.restart(argv)
            return
        except store.StoreError as error:
            sys.exit(f"theseus: {error}")


if __name__ == "__main__":
    main()
