"""theseus command line. Every subcommand prints its usage with --help."""
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
        from . import agents, profiles
        agent = next((a for a in agents.agents(conf) if a["pane"] == pane), None)
        if agent is not None:
            env.update(profiles.instance_env(conf, agent.get("provider")))
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


def cmd_config(args):
    conf = load()
    data = resolved(conf)
    if args.action == "show":
        print(json.dumps(data, indent=2))
        return
    if args.action == "check":
        from . import profiles
        conf.instances()
        conf.consumer_patterns()
        for name in profiles.names(conf):
            profiles.resolve(conf, name)
        print(f"{cfg.CONFIG_NAME} at {conf.root} is valid")
        return
    value = data
    for part in (args.key or "").split("."):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            sys.exit(f"theseus: no configuration key {args.key}")
        value = value[part]
    print(value if isinstance(value, str) else json.dumps(value))


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
                   args.room, args.priority))
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
    elif action == "deliver":
        emit(t.deliver(queue, args.id, args.handoff))
    elif action == "note":
        emit(t.note(queue, args.id, args.text))
    elif action == "priority":
        emit(t.set_priority(queue, args.id, args.value))
    elif action in ("hold", "release-hold"):
        emit(t.hold(queue, args.id, action == "hold"))
    elif action == "reopen":
        emit(t.reopen(queue, args.id, args.reason))
    elif action == "merged":
        emit(t.mark_merged(queue, args.id, args.commit))
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
        from . import tasks as t
        task = t.Queue(conf).get(args.task)
        record = (f"Owned paths: {', '.join(task['ownedPaths'])}. Objective: {task['objective']} "
                  f"Contract: {task.get('generalContract') or 'not recorded'} Acceptance: {' | '.join(task['acceptance'])}.")
        if task.get("assignmentNote"):
            record += f" Coordinator notes: {task['assignmentNote']}"
        log_dir = (task.get("execution") or {}).get("logDir")
        if log_dir:
            record += f" Log directory (handoff.md, changed.sha256): {log_dir}."
        from . import rooms
        record += rooms.message(conf, task)
        if task.get("worktree") == str(conf.root):
            record += (" In-place assignment: there is no separate worktree; edit only your owned paths in the main checkout,"
                       " other workers share it, and never commit, stash or push.")
        sys.exit(run_script("instance-send.sh", [args.pane, args.task, args.worktree, record, args.deadline or ""], conf, args.pane))
    if args.instance_action == "watch":
        sys.exit(run_script("instance-watch.sh", args.panes, conf))
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
        print(tools.set_tool(conf, args.name, args.value))
        return
    if args.action == "env":
        for key, value in sorted(tools.environment(conf).items()):
            print(f"export {key}={shlex.quote(value)}")
        return
    for status, name, value, source, purpose in tools.table(conf):
        print(f"{status:<5} {name:<10} {value}  [{source}]")


def cmd_agents(args):
    from . import agents
    conf = load()
    action = args.action
    try:
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


def cmd_pool(args):
    from . import pools
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
        print(f"no pools defined; create {pools.config_path()} (see `theseus pool --help`)")
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
        elif action == "restore":
            result = store.restore(conn, args.file)
            print(f"saved the previous database as {result['saved']}")
            print(f"restored {result['restored']} (schema version {result['version']})")
        elif action == "prune":
            older = args.older_than_days * 86400 if args.older_than_days else None
            names = store.prune_projects(conn, older_than=older, dry_run=not args.yes)
            verb = "removed" if args.yes else "would remove"
            print(f"{verb} {', '.join(names)} and all their events, agents, deliveries and samples" if names
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

    p = sub.add_parser("config", help="show, query or validate the resolved configuration")
    p.add_argument("action", choices=("show", "get", "check"))
    p.add_argument("key", nargs="?", help="dotted key for get, e.g. paths.data or workers.default")
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
    p = ts.add_parser("release", help="return an assigned task to pending, keeping the attempt")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = ts.add_parser("deliver", help="mark a task delivered (awaiting coordinator integration)")
    p.add_argument("id")
    p.add_argument("--handoff", required=True)
    p = ts.add_parser("priority", help="set a task's priority (higher is assigned first; 0 removes it)")
    p.add_argument("id")
    p.add_argument("value", type=int)
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
    p = ins.add_parser("send", help="type the task message into an idle pane")
    p.add_argument("pane")
    p.add_argument("task")
    p.add_argument("worktree")
    p.add_argument("deadline", nargs="?")
    p = ins.add_parser("watch", help="print HANDOFF and IDLE-NO-HANDOFF events (run under a monitor)")
    p.add_argument("panes", nargs="*", metavar="INSTANCE:PANE")
    p = ins.add_parser("restart", help="restart a pane's agent and resume its session (reloads MCP servers and skills)")
    p.add_argument("pane")
    p.add_argument("--dry-run", action="store_true")
    p = ins.add_parser("compact", help="type /compact (or /clear) into an idle pane between tasks")
    p.add_argument("pane")
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
    p.set_defaults(func=guarded(cmd_tools))
    p = sub.add_parser("setup", help="setup skill (install the theseus-setup agent skill), setup rea [--update]")
    p.add_argument("component", choices=("rea", "skill"))
    p.add_argument("--update", action="store_true", help="rea: run `rea update` first")
    p.add_argument("--client", default="agents", choices=("agents", "claude_code"),
                   help="skill: ~/.agents/skills (agents, default) or ~/.claude/skills (claude_code)")
    p.add_argument("--dir", help="skill: install into this skills directory instead")
    p.add_argument("--force", action="store_true", help="skill: replace an existing, different copy")
    p.add_argument("--print-path", action="store_true", help="skill: print the packaged SKILL.md path and exit")
    p.set_defaults(func=guarded(cmd_setup))

    p = sub.add_parser("agents", help="registered persistent agents: list, add NAME PANE, remove NAME, enable NAME, disable NAME")
    p.add_argument("action", nargs="?", default="list", choices=("list", "add", "remove", "enable", "disable", "profiles"))
    p.add_argument("name", nargs="?")
    p.add_argument("pane", nargs="?")
    p.add_argument("--provider", "--profile", dest="provider", help="agent profile (theseus agents profiles)")
    p.add_argument("--rooms", help="comma-separated rooms this agent may work (clean,dirty)")
    p.set_defaults(func=guarded(cmd_agents))
    p = sub.add_parser("pool", help="host-wide worker pools shared across projects (~/.config/theseus/pools.toml)",
                       description="Show who holds the slots of each pool. A pool caps the agents working at once across "
                                   "projects: [pools.NAME] max = N, priority = [\"project\", ...] in ~/.config/theseus/pools.toml; "
                                   "a profile joins with pool = \"NAME\" in [workers.PROFILE].")
    p.add_argument("action", nargs="?", default="status", choices=("status", "hold", "release"),
                   help="hold or release a slot by hand for a worker no theseus queue tracks")
    p.add_argument("name", nargs="?", help="pool name")
    p.add_argument("agent", nargs="?", help="hold/release: the worker holding the slot")
    p.add_argument("--project", help="hold/release: project name as listed in the pool priority")
    p.add_argument("--task", help="hold: what the worker is doing")
    p.add_argument("--hours", type=float, default=4.0, help="hold: expiry if never released (default 4)")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("queue", help="work queue: status, tick, run, wait, pause, resume, log")
    p.add_argument("action", nargs="?", default="status", choices=("status", "tick", "run", "wait", "pause", "resume", "log"))
    p.add_argument("--interval", type=int)
    p.add_argument("--timeout", type=int, default=10800)
    p.add_argument("--lines", type=int, default=20)
    p.add_argument("--no-assign", action="store_true", help="tick: report deliveries and stalls but assign nothing")
    p.add_argument("--no-drive", action="store_true", help="wait: only watch events, do not run ticks")
    p.set_defaults(func=guarded(cmd_queue))
    p = sub.add_parser("top", help="interactive dashboard of every project on this host: agents, queue, pools, events")
    p.add_argument("interval", nargs="?", type=int, default=5, help="refresh seconds (default 5)")
    p.add_argument("--interval", dest="every", type=float, help="refresh seconds, fractions allowed")
    p.add_argument("--once", action="store_true", help="print one plain frame and exit")
    p.add_argument("--json", action="store_true", help="print the snapshot as JSON and exit")
    p.add_argument("--host", action="store_true", help="the host-wide view instead of the current project")
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
    if argv and argv[0] in SHELL_COMMANDS:
        sys.exit(run_script(SHELL_COMMANDS[argv[0]], argv[1:]))
    parser = build_parser()
    args = parser.parse_args(argv)
    from . import store
    try:
        args.func(args)
    except store.StoreError as error:
        sys.exit(f"theseus: {error}")


if __name__ == "__main__":
    main()
