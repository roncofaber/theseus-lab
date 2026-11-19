"""theseus command line. Every subcommand prints its usage with --help; `theseus` alone on a terminal opens the control
console (`theseus console`, also `theseus top`)."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from . import config as cfg


def load(record=True):
    """The project's configuration; record (not for read commands, which never write) also marks it seen in the host
    store."""
    try:
        conf = cfg.load()
    except cfg.ConfigError as error:
        sys.exit(f"theseus: {error}")
    if record:
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


JSON_ENVELOPE = 'print one JSON object: {"ok": true, "data": <the output>} or {"ok": false, "error": {code, message}}'


def guarded(function):
    from . import gitutil, integrate, profiles, tasks
    def wrapper(args):
        try:
            return function(args)
        except integrate.IntegrationError as error:
            print(f"theseus: {error}", file=sys.stderr)
            sys.exit(error.code)
        except tasks.TaskError as error:
            print(f"theseus: {error}", file=sys.stderr)
            sys.exit(3 if getattr(error, "code", None) == "conflict" else 1)
        except (gitutil.GitError, cfg.ConfigError, profiles.ProfileError) as error:
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
    from . import scaffold, tasks as t
    conf = load()
    emit(scaffold.upgrade(conf.root, dry_run=args.dry_run))
    try:
        state = t.export_state(t.Queue(conf))
        print(f"tasks: in the host store ({state['state']})")
    except t.TaskError as error:
        if getattr(error, "code", "") != "not-imported":
            raise
        if not args.import_tasks:
            print("tasks: not in the host store yet; pause the queue (`theseus queue pause`, stop `queue run`), then "
                  "`theseus upgrade --import-tasks` or `theseus tasks import`, and commit docs/project/")
        elif not args.dry_run:
            from . import workqueue
            with t.acting(coordinator_principal()), workqueue.locked(conf, announce=True):
                print(t.import_(t.Queue(conf)))
                print(t.export(t.Queue(conf)))


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
            from . import coordinator, supervisor, tmuxconf
            errors += tmuxconf.problems(project=conf.section("project"))[0]
            if conf.section("coordinator").get("profile"):
                try:
                    coordinator.profile_name(conf)
                except coordinator.CoordinatorError as error:
                    errors.append(str(error))
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

# Task state is decided by the store; the queue lock only keeps ticks off a main checkout, worktree or handoff that a
# command is changing.
TASK_LOCKED = ("assign", "rebase", "unmerge", "reopen", "retire", "import")


def queue_locked(conf):
    from . import workqueue
    return workqueue.locked(conf, announce=True)


TASK_READS = ("list", "ls", "ready", "show", "prompt", "check-delivery", "consumers", "stats")


def coordinator_principal():
    """The caller, who must be the coordinator: an agent (credentials, THESEUS_AGENT_ID, a hand-made agent's pane, a
    task worktree) is refused, with the commands it may use."""
    from . import taskservice
    principal = taskservice.principal(via="cli")
    if principal.role != "coordinator":
        raise taskservice.TaskError("forbidden", "this command is the coordinator's; a worker reports through "
                                                 "`theseus task show|note|done|blocked TASK`")
    return principal


def as_coordinator(function):
    """Run a command that changes task state as the coordinator, refusing any other caller."""
    def wrapper(args):
        from . import tasks as t
        with t.acting(coordinator_principal()):
            return function(args)
    return wrapper


def json_reply(function):
    """With --json, print one object instead of text: {"ok": true, "data": ...} (a command's JSON output, else its
    lines) or {"ok": false, "error": {"code", "message"}}; exit codes keep their meaning (1 refused, 3 conflict)."""
    def wrapper(args):
        if not getattr(args, "json", False):
            return function(args)
        from . import integrate, tasks
        buffer, code, error = io.StringIO(), 0, None
        try:
            with contextlib.redirect_stdout(buffer):
                function(args)
        except SystemExit as stop:
            if isinstance(stop.code, str):
                code, error = 1, ("refused", stop.code.removeprefix("theseus: "))
            else:
                code = stop.code or 0
        except tasks.TaskError as failure:
            kind = getattr(failure, "code", "refused")
            code, error = (3 if kind == "conflict" else 1), (kind, str(failure))
        except integrate.IntegrationError as failure:
            code, error = failure.code, ("conflict" if failure.code == 3 else "refused", str(failure))
        if error:
            print(json.dumps({"ok": False, "error": {"code": error[0], "message": error[1]}}, ensure_ascii=False))
        else:
            text = buffer.getvalue()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"lines": text.splitlines()}
            print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        if code:
            sys.exit(code)
    return wrapper


def cmd_tasks(args):
    from . import tasks as t
    conf = load(record=args.task_action not in TASK_READS)
    acting = contextlib.nullcontext() if args.task_action in TASK_READS else t.acting(coordinator_principal())
    with acting, queue_locked(conf) if args.task_action in TASK_LOCKED else contextlib.nullcontext():
        tasks_action(conf, t.Queue(conf), args)


def cmd_task(args):
    """A worker's own task, named by the worker (the stale-context check); the coordinator may act for a subagent of
    its own session. When the host store cannot be read (show) or written (note, done, blocked), the worker uses its
    task copy and outbox in its log directory instead."""
    from . import tasks as t, taskservice
    action = args.task_command
    conf = load(record=action != "show")
    try:
        task_command(conf, args)
    except Exception as error:
        if not store_out_of_reach(error):
            raise
        inbox = t.find_inbox(Path.cwd(), args.id)
        if inbox is None:
            raise taskservice.TaskError("unreadable", f"{error}; and no copy of {args.id} (.theseus/task.json) is in "
                                                      f"this worktree") from error
        if action == "show":
            copy = json.loads(inbox.read_text())
            data = copy["task"].get(args.field) if args.field else {**copy, "source": str(inbox)}
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return
        entry = {"kind": action, "task": args.id, "agent_id": os.environ.get("THESEUS_AGENT_ID")}
        if action == "note":
            entry.update(text=args.text, question=args.question)
        else:
            handoff = Path(args.handoff).resolve() if args.handoff else None
            entry.update(handoff=str(handoff) if handoff else None, reason=getattr(args, "reason", None))
        print(f"the host store is out of reach; left {action} in {t.write_outbox(inbox, entry)} for the queue's next tick")


def store_out_of_reach(error):
    import sqlite3
    from . import store, taskservice
    if isinstance(error, taskservice.TaskError):
        return error.code == "unreadable"
    return isinstance(error, (sqlite3.OperationalError, store.StoreError)) and not isinstance(error, store.SchemaTooNew)


def task_command(conf, args):
    from . import tasks as t, taskservice
    action = args.task_command
    principal = taskservice.principal(via="cli", readonly=action == "show")
    queue = t.Queue(conf)
    if action == "show":
        shown = t.show_own(queue, principal, args.id)
        print(json.dumps(shown["task"].get(args.field) if args.field else shown, indent=2, ensure_ascii=False))
    elif action == "note":
        noted = t.report(queue, principal, args.id, "note", text=args.text, question=args.question)
        print(f"{args.id}: {noted['kind']} recorded")
    else:
        done = t.report(queue, principal, args.id, action, handoff=args.handoff, reason=getattr(args, "reason", None))
        print(f"{args.id} is already delivered" if done.get("already") else
              f"{args.id} -> verification ({done['result']}); the coordinator reviews it next")





def tasks_action(conf, queue, args):
    from . import tasks as t
    action = args.task_action
    if action == "add":
        emit(t.add(queue, args.id, args.role, args.owned, args.objective, args.contract, args.accept, args.depends, args.milestone,
                   args.room, args.priority, args.profile, args.min_capability, args.touches, args.hold))
    elif action in ("list", "ls"):
        emit(t.list_tasks(queue, args.status, args.all, args.evidence))
    elif action == "edit":
        changes = {key: value for key, value in (
            ("role", args.role), ("objective", args.objective), ("contract", args.contract), ("accept", args.accept),
            ("owned", args.owned), ("add_owned", args.add_owned), ("touches", args.touches), ("depends", args.depends),
            ("milestone", args.milestone), ("room", args.room), ("profile", args.profile), ("priority", args.priority),
            ("min_capability", args.min_capability), ("note", args.note), ("append_note", args.append_note))
            if value is not None}
        if args.clear_blockers:
            changes.update(externalBlocker=None, researchRequired=None)
        for flag, key in ((args.external_blocker, "externalBlocker"), (args.research_required, "researchRequired")):
            if flag is not None:
                changes[key] = {"evidence": flag, "unblockCondition": args.unblock or ""}
        if not changes:
            sys.exit("theseus: nothing to change; see theseus tasks edit --help")
        emit(t.edit(queue, args.id, changes, args.force))
    elif action == "import":
        emit(t.import_(queue, replace=args.replace, new_id=args.new_id))
    elif action in ("export", "archive"):
        emit(t.export(queue))
    elif action == "adopt":
        adopted = queue.service.adopt(t.acting_principal(None))
        emit(f"{adopted['project']}'s tasks now belong to {adopted['root']} (was {adopted['previous'] or 'unset'})")
    elif action == "ready":
        ids = t.ready(queue, args.include_paused, args.any_milestone)
        emit(ids)
        if not ids:
            sys.exit(1)
    elif action == "show":
        if args.history:
            found = queue.service.get(t.acting_principal(None), args.id, history=True)
            print(json.dumps({"task": found["task"], "notes": found["notes"], "history": found["history"]}, indent=2,
                             ensure_ascii=False))
            return
        task = queue.get(args.id)
        if not args.field:
            questions = queue.service.questions(t.acting_principal(None), args.id)
            if questions:
                task = {**task, "openQuestions": [{k: q[k] for k in ("text", "author", "time")} for q in questions]}
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
        emit(t.export(queue))
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
        from . import tasks as t
        t.ACTING.set(coordinator_principal())
        provider = args.provider or profiles.default_name(conf)
        name = f"{args.instance}-{args.suffix}"
        owner = f"{provider}:{args.instance}:{name}"
        with queue_locked(conf):
            if args.in_place:
                worktree, base, message = integrate.assign_in_place(conf, args.task, name, provider, "persistent-instance",
                                                                    owner=owner, instance=args.instance)
            else:
                worktree, base, message = integrate.assign(conf, args.task, name, provider, "persistent-instance",
                                                           owner=owner, commit=not args.no_commit, instance=args.instance)
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
        from . import profiles
        prompt = agents.pane_prompt(conf, named or args.pane, (named or {}).get("provider") or profiles.default_name(conf))
        if prompt:
            sys.exit(f"theseus: {args.pane} shows a prompt that needs a human ({prompt[:80]}); answer it in the pane "
                     f"first, since typed text would answer it")
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
    with contextlib.nullcontext() if args.dry_run else queue_locked(conf):
        lines = dispatch.dispatch(conf, args.task, name=args.name, provider=args.provider, model=args.model,
                                  dry_run=args.dry_run, resume_paused=args.resume_paused, commit=not args.no_commit)
    emit(lines)


def cmd_merge(args):
    from . import integrate
    conf = load()
    with queue_locked(conf):
        _, report = integrate.merge(conf, args.task, subject=args.subject, accept=args.accept or [],
                                    skip_patches=args.skip_patch or [], commit=not args.no_commit,
                                    regenerate=not args.no_index)
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
    if status == 0:
        export_after_verify(conf, args.revision)
    sys.exit(status)


def export_after_verify(conf, revision):
    """After a passing verification, write the task export for the coordinator to commit with docs/project/."""
    from . import gitutil, tasks as t
    try:
        with t.acting(coordinator_principal()):
            print(t.export(t.Queue(conf), head=gitutil.out(conf.root, "rev-parse", revision)))
    except t.TaskError as error:
        print(f"task export skipped: {error}")


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
            sys.exit("refusing to move reports (see above)")
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
    if args.action == "list" and args.json:
        args.action = "ls"
    if args.action in ("up", "down", "restart", "attach", "logs", "migrate", "ls", "scale"):
        return cmd_supervised(args)
    conf = load()
    action = args.action
    if action in ("remove", "enable", "disable"):
        if not args.targets:
            sys.exit(f"usage: theseus agents {action} NAME...")
        failed = []
        for name in dict.fromkeys(args.targets):
            try:
                agent_action(conf, action, name, None, args)
            except SystemExit as error:
                if error.code not in (0, None):
                    print(error.code if isinstance(error.code, str) else f"theseus: {name}: failed", file=sys.stderr)
                    failed.append(name)
        if failed:
            sys.exit(1)
        return
    agent_action(conf, action, args.name, args.pane, args)


def agent_action(conf, action, name, pane, args):
    """One agents action (add, remove, enable, disable, profiles, or the legacy listing) for one agent name."""
    from . import agents
    args.name, args.pane = name, pane
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
            stale = {a["name"] for a in agents.agents(conf) if agents.stale_pane(a)}
            for info in agents.states(conf):
                marks = ["tainted:" + info["tainted"]] if info.get("tainted") else []
                marks += ["stale pane (re-add the agent)"] if info["name"] in stale else []
                print(f"{info['name']:<20} {info['pane']:<6} {info['state']:<10} {info.get('task') or '-':<36} "
                      f"{' '.join(marks)}".rstrip())
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
            if not args.all_projects and not args.dry_run:
                from . import closure
                try:
                    closure.refuse_if_closed(confs[0], "`agents up`")
                except closure.ClosedError as error:
                    sys.exit(f"theseus: {error}")
            failed = False
            for conf in confs:
                if not supervisor.managed(conf):
                    from . import agents
                    if not args.all_projects and not names and agents.agents(conf):
                        print_actions([supervisor.start_driver(conf, dry_run=args.dry_run), *autostart(conf, args.dry_run)])
                        sys.exit(0)
                    sys.exit(f"theseus: {conf.name} does not supervise its agents; set [agents] managed = true and "
                             f"[agents.counts] in {cfg.LOCAL_NAME}")
                errors, warnings = supervisor.problems(conf)
                for line in warnings:
                    print(f"warning: {line}", file=sys.stderr)
                if errors:
                    print(f"theseus: {conf.name}: " + "; ".join(errors), file=sys.stderr)
                    failed = True
                    continue
                from . import workqueue
                with workqueue.locked(conf):
                    if not args.dry_run:
                        supervisor.set_paused(conf, False)
                    if names and not args.dry_run:
                        for name in names:
                            supervisor.update(conf, name, desired="up")
                    rows = supervisor.reconcile(conf, names or None, wait=not args.dry_run, dry_run=args.dry_run,
                                                reset=args.reset)
                if not names:
                    rows = [*(rows or []), supervisor.start_driver(conf, dry_run=args.dry_run), *autostart(conf, args.dry_run)]
                if len(confs) > 1:
                    print(f"# {conf.name}")
                print_actions(rows or [("-", "none", "no agents configured ([agents.counts])")])
            sys.exit(1 if failed else 0)
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
            if bool(names) == bool(args.all):
                sys.exit("usage: theseus agents down NAME... | --all [--force] [--now] [--replace]")
            print_actions(supervisor.down(conf, names or None, force=args.force, now=args.now))
            if args.all:
                print_actions([supervisor.stop_driver(conf)])
            if args.replace and supervisor.managed(conf):
                from . import workqueue
                with workqueue.locked(conf):
                    print_actions(supervisor.reconcile(conf, wait=True))
            return
        if action == "restart":
            if bool(names) == bool(args.drifted):
                sys.exit("usage: theseus agents restart NAME... [--fresh [--with-task [--file PROMPT]]] | --drifted [--fresh]")
            if args.drifted and (args.with_task or args.file):
                sys.exit("theseus: --drifted restarts only agents without a task; --with-task and --file do not apply")
            if (args.with_task or args.file) and not args.fresh:
                sys.exit("theseus: --with-task and --file go with --fresh (a plain restart resumes the conversation)")
            if args.file and not args.with_task:
                sys.exit("theseus: --file is the restart prompt of --with-task")
            prompt = Path(args.file).read_text().strip() if args.file else None
            if args.file and not prompt:
                sys.exit(f"theseus: {args.file} is empty")
            if args.drifted:
                changes = drifted_agents(conf)
                if not changes:
                    print("no drifted agents without a task")
                    return
            else:
                unknown = [n for n in names if not (supervisor.record(conf, n) or {}).get("managed")]
                if unknown:
                    sys.exit(f"theseus: {', '.join(unknown)}: not a supervised agent (theseus agents ls)")
                changes = {n: supervisor.drift(conf, supervisor.record(conf, n)) for n in names}
            sys.exit(restart_agents(conf, list(changes), args.fresh, args.with_task, prompt, changes))
        if action == "migrate":
            if bool(names) == bool(args.all):
                sys.exit("usage: theseus agents migrate NAME... | --all [--with-task]")
            from . import workqueue
            with workqueue.locked(conf):
                print_actions(supervisor.migrate(conf, names or None, with_task=args.with_task))
            return
        if len(names) != 1:
            sys.exit(f"usage: theseus agents {action} NAME")
        rec = supervisor.record(conf, args.name)
        if not rec or not rec.get("managed"):
            sys.exit(f"theseus: {args.name} is not a supervised agent (theseus agents ls)")
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


def autostart(conf, dry_run=False):
    """agents up: the coordinator's row when [coordinator] autostart is on."""
    from . import coordinator
    if not coordinator.autostart(conf):
        return []
    if dry_run:
        return [(coordinator.NAME, "would start", f"session {coordinator.session(conf)} unless it runs")]
    try:
        return [(coordinator.NAME, *coordinator.up(conf))]
    except (coordinator.CoordinatorError, *supervisor_errors()) as error:
        return [(coordinator.NAME, "error", str(error))]


def drifted_agents(conf):
    """{name: changes} of the supervised agents whose launch configuration changed and that hold no task; those that
    hold one are printed as skipped."""
    from . import supervisor
    found = {}
    for name, rec in sorted(supervisor.records(conf).items()):
        changes = supervisor.drift(conf, rec) if rec.get("managed") else []
        if not changes:
            continue
        task = supervisor.current_task(conf, name)
        if task:
            print_actions([(name, "skipped", f"{'; '.join(changes)}; holds task {task['id']}, restart it once free")])
        else:
            found[name] = changes
    return found


def restart_agents(conf, names, fresh, with_task, prompt=None, changes=None):
    """agents restart: an agent that holds a task restarts fresh only with --with-task, and then gets exactly one
    task-aware restart prompt (prompt, or one built from the task) before anything else is typed into it. changes
    names what each restart applies ({name: [change, ...]})."""
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
        applied = (changes or {}).get(name)
        if ok and applied:
            detail = f"applied {', '.join(applied)}; {detail}"
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
    from . import profiles
    import time
    from . import agents, supervisor
    states = {info["name"]: info for info in agents.states(conf)}
    rows = []
    for item in supervisor.snapshot(conf):
        info = states.get(item["name"], {})
        rec = supervisor.record(conf, item["name"])
        changes = supervisor.drift(conf, rec) if rec else []
        row = {**item, "state": info.get("state"), "task": info.get("task"), "drift": bool(changes), "driftChanges": changes,
               "capability": profiles.capability(conf, item.get("profile")) if item.get("profile") else None,
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
        print(f"{row['name']:<24} {row['state'] or '-':<10} {row['profile'] or '-':<10} "
              f"cap {profiles.capability(conf, row['profile']) if row['profile'] else '-'} gen {row['gen'] or '-':<4} "
              f"pid {row['pid'] or '-':<8} {row['task'] or '-':<28} restarts {row['restarts']:<2} "
              f"up {uptime} session {row['sessionId'] or '-'}" + (f" drift: {'; '.join(row['driftChanges'])}" if row["driftChanges"] else ""))
    stale = {a["name"] for a in agents.agents(conf) if agents.stale_pane(a)}
    for info in legacy:
        marks = ["tainted:" + info["tainted"]] if info.get("tainted") else []
        marks += ["stale pane (re-add the agent)"] if info["name"] in stale else []
        print(f"{info['name']:<24} {info['state']:<10} unmanaged pane {info['pane']:<6} {info.get('task') or '-':<28} "
              f"{' '.join(marks)}".rstrip())


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
        cut = f" (cap cut -{info['capCut']})" if info.get("capCut") else ""
        budget = f"; tokens {info['estimate']:,}/{int(info['budget']):,} per min (estimate)" if info.get("budget") else ""
        print(f"{name}: {info['active']}/{info['max'] or '-'} working{cut} ({projects}); priority {' > '.join(info['priority']) or '-'}; "
              f"claims {', '.join(info['claims']) or 'none'}{budget}")
        for lease in sorted(info["leases"].values(), key=lambda v: (v["project"], v["agent"])):
            held = "  (turns held)" if f"{lease['project']}/{lease['agent']}" in info.get("held", []) else ""
            print(f"  {lease['project']:<16} {lease['agent']:<20} {lease['task']}{held}")
    from . import health, top
    for entry in health.snapshot():
        state = top.fallback_text(entry) if entry["onFallback"] or entry["state"] == "down" else \
            f"profile {entry['profile']} healthy (fallback {entry['fallback'] or 'none'})"
        print(state)


def cmd_queue(args):
    from . import workqueue
    conf = load()
    action = args.action
    if action in ("tick", "run") or (action == "wait" and not args.no_drive):
        coordinator_principal()
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
        groups = workqueue.summary(conf)
        for key, items in groups.items():
            print(f"{key:<12} {len(items):>3}  {', '.join(x['id'] for x in items[:8])}")
        print("paused" if workqueue.paused(conf) else "running")
        for task in groups["blocked"][:8]:
            print(f"  {task['id']}: {'; '.join(task['reasons'])}")
        from . import tasks as t
        for question in t.Queue(conf).service.questions(t.acting_principal()):
            print(f"question {question['task']}: {question['text']} (answer with `theseus tasks note "
                  f"{question['task']} TEXT`)")


def cmd_attention(args):
    from . import attention
    conf = load(record=False)
    if args.follow:
        attention.follow(conf, interval=args.interval)
        return
    found = attention.items(conf)
    if args.json:
        s = attention.settings(conf)
        print(json.dumps({"project": conf.name, "summary": attention.summary(found), "items": found,
                          "push": {"notify": s["notify"], "coordinator": attention.coordinator(conf)}}, indent=2))
        return
    print(attention.render(conf, found))


def cmd_coordinator(args):
    from . import attention, coordinator
    if args.action == "_exec":
        if len(args.args) != 2:
            sys.exit("usage: theseus coordinator _exec PROJECT GEN")
        sys.exit(coordinator.exec_coordinator(*args.args))
    conf = load(record=args.action not in ("status", "attach"))
    if args.action in ("register", "unregister", "up", "down", "restart"):
        coordinator_principal()
    if args.action in ("down", "restart") and coordinator.inside(conf):
        sys.exit(f"theseus: this runs inside the coordinator session, which `coordinator {args.action}` would end; run it "
                 f"from another terminal")
    try:
        if args.action == "register":
            print(attention.register(conf, (args.args or [None])[0], args.profile))
        elif args.action == "unregister":
            print(attention.unregister(conf))
        elif args.action == "up":
            from . import closure
            try:
                closure.refuse_if_closed(conf, "`coordinator up`")
            except closure.ClosedError as error:
                sys.exit(f"theseus: {error}")
            print_actions([(coordinator.NAME, *coordinator.up(conf, args.profile, wait=not args.no_wait))])
            if attention.settings(conf)["notify"] != "pane":
                print("note: push is off; set [attention] notify = \"pane\" in theseus.local.toml for the queue driver to "
                      "type attention lines into the session")
        elif args.action == "down":
            print_actions([(coordinator.NAME, *coordinator.down(conf))])
        elif args.action == "restart":
            ok, detail = coordinator.restart(conf, fresh=args.fresh, profile=args.profile, wait=not args.no_wait)
            print_actions([(coordinator.NAME, "restarted" if ok else "failed", detail)])
            sys.exit(0 if ok else 1)
        elif args.action == "attach":
            if not coordinator.server().has_session(coordinator.session(conf)):
                sys.exit(f"theseus: no coordinator session for {conf.name} (theseus coordinator up)")
            argv = coordinator.attach_argv(conf, read_only=args.read_only)
            os.execvp(argv[0], argv)
        else:
            for line in coordinator.status_lines(conf):
                print(line)
            record = attention.coordinator(conf)
            if record is not None and record.get("pane") != coordinator.address(conf):
                problem = attention.pane_problem(record)
                print(f"hand-registered pane {record['pane']} (pid {record.get('pane_pid')}, profile "
                      f"{record.get('profile')}), registered {record.get('registered')}: {problem or 'live'}")
    except attention.AttentionError as error:
        sys.exit(f"theseus: {error}")
    except coordinator.CoordinatorError as error:
        sys.exit(f"theseus: {error}")
    except supervisor_errors() as error:
        sys.exit(f"theseus: {error}")


def supervisor_errors():
    from . import supervisor, tmux, tmuxconf
    return (supervisor.SupervisorError, tmux.TmuxError, tmuxconf.TmuxConfError)


def cmd_tmux(args):
    from . import supervisor, tmux as tm, tmuxconf
    if args.socket and Path(args.socket).resolve() != tm.socket_path(*tm.configured()).resolve():
        sys.exit(f"theseus: {args.socket} is not theseus's private tmux server; nothing here runs on another server")
    try:
        if args.action == "status":
            if args.file is not None:
                path = args.file
            else:
                path = tmuxconf.status_path(args.project or load(record=False).name)
            if args.json:
                try:
                    print(Path(path).read_text().strip())
                except OSError:
                    print("{}")
            elif args.left or args.right:
                print(tmuxconf.render(path, "left" if args.left else "right", args.agent, kind=args.kind))
            else:
                print(tmuxconf.describe(path))
        elif args.action == "apply":
            srv = tm.Server("-S", args.socket) if args.socket else supervisor.server()
            if not srv.alive():
                path, _ = tmuxconf.write_conf()
                print(f"the private tmux server is not running; {path} applies when it starts")
                return
            path, sessions = tmuxconf.apply(srv)
            message = f"theseus: applied {path} to {len(sessions)} session(s)"
            if args.client:
                srv.run("display-message", "-c", args.client, message)
            else:
                print(message)
        elif args.action == "conf":
            path, _ = tmuxconf.write_conf()
            print(path.read_text() if args.print_text else path)
        elif args.action == "_popup":
            what = args.what or "attention"
            command = [*supervisor.theseus_argv(), "console" if what == "console" else "attention"]
            if what == "console":
                os.execvp(command[0], command)
            subprocess.run(command)
            try:
                input("\n(Enter closes) ")
            except EOFError:
                pass
        elif args.action == "_goto":
            message = tmuxconf.goto(args.socket, args.client, args.session, args.what or "coordinator")
            if message:
                tm.Server("-S", args.socket).run("display-message", "-c", args.client, message)
    except supervisor_errors() as error:
        sys.exit(f"theseus: {error}")


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
    found = registry.entries(include_closed=args.all)
    if args.json:
        print(json.dumps(found, indent=2))
        return
    for entry in found:
        closed = entry.get("closed")
        print(f"{entry.get('name') or '-':<24} {entry['root']}"
              + (f"  (closed {closed.get('closed') or ''}{': ' + closed['reason'] if closed.get('reason') else ''})"
                 if closed else ""))
    if not found:
        print("no projects registered yet: run any theseus command inside a project")


def cmd_close(args):
    from . import closure, coordinator
    conf = load()
    coordinator_principal()
    inside = coordinator.inside(conf)
    try:
        rows, notes = closure.close(conf, reason=args.reason or "", force=args.force, commit=args.commit)
    except closure.ClosedError as error:
        sys.exit(f"theseus: {error}")
    print_actions(rows)
    emit(notes)
    if inside:
        print("this runs inside the coordinator session, which stops now", flush=True)
    sys.stdout.flush()
    stopped = closure.stop_coordinator(conf)
    if stopped:
        print_actions([stopped])


def cmd_reopen(args):
    from . import closure
    conf = load()
    coordinator_principal()
    was = closure.closed(conf)
    if not was:
        print(f"{conf.name} is not closed; bringing it up anyway")
    print_actions(closure.reopen(conf, wait=not args.no_wait))


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
    if action == "downgrade":
        downgrade(args)
        return
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
            if args.with_tasks and args.yes:
                for row in store.prune_projects(conn, older_than=older, dry_run=True, with_tasks=True, rows=True):
                    saved = drop_project_tasks(row)
                    if saved:
                        print(f"exported {row['name']}'s tasks to {saved} and deleted them from the store")
            names = store.prune_projects(conn, older_than=older, dry_run=not args.yes, with_tasks=args.with_tasks)
            if not args.with_tasks:
                for name in sorted(set(store.prune_projects(conn, older_than=older, dry_run=True, with_tasks=True))
                                   - set(names)):
                    print(f"kept {name}: it has tasks in the store (--with-tasks exports them, then removes it)")
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


def downgrade(args):
    """`theseus db downgrade 6`: refuse unless every imported project's committed export is current."""
    from types import SimpleNamespace
    from . import store, taskrecords, taskservice
    if args.file != "6":
        sys.exit("usage: theseus db downgrade 6 [--yes]")
    problems = []
    with store.opened() as conn:
        rows = [row for row in store.list_projects(conn) if row.get("tasks_imported")]
    for row in rows:
        root = Path(row["tasks_root"] or "")
        if not root.is_dir():
            problems.append(f"{row['name']}: its checkout {root} is gone, so its export cannot be checked")
            continue
        conf = SimpleNamespace(name=row["name"], root=root, data=Path(row["data"] or root))
        state = taskservice.open(conf).export_state(taskservice.COORDINATOR, taskrecords.read(root))
        if state["state"] != "current":
            problems.append(f"{row['name']}: {state['detail']} (in {root})")
    if problems:
        sys.exit("theseus: not downgrading; export (and commit) first:\n  " + "\n  ".join(problems))
    if not args.yes:
        print(f"would remove the task store from {store.path()} ({len(rows)} project(s), all exported); rerun with --yes")
        return
    with store.opened() as conn:
        saved = store.downgrade_to_6(conn)
    print(f"schema 6 again; the previous database is saved as {saved}. Install the older theseus now: any command of "
          f"this one migrates the store to 7 again.")


def drop_project_tasks(row):
    """Export a pruned project's tasks to its data directory's exports/ and delete them from the store."""
    from types import SimpleNamespace
    from . import store, taskservice
    data = row["data"] or str(store.path().parent / "projects" / row["name"])
    conf = SimpleNamespace(name=row["name"], root=Path(row["root"] or data), data=Path(data))
    return taskservice.open(conf).drop_tasks(taskservice.COORDINATOR)


def cmd_mcp(args):
    from . import mcp_server
    mcp_server.run(args.role)


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
    p.add_argument("--import-tasks", action="store_true", help="also import the project's task files into the host "
                                                               "store (pause the queue first) and export them")
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
    p.add_argument("--owned", nargs="+", action="extend", required=True, metavar="PATH", help="owned paths; must include docs/investigations/NAME.md")
    p.add_argument("--objective", required=True, help="one sentence stating what the task achieves")
    p.add_argument("--contract", required=True, help="generalContract: the general behavior and its representative variations")
    p.add_argument("--accept", nargs="+", action="extend", required=True, metavar="TEXT", help="acceptance items, at most six; repeatable")
    p.add_argument("--depends", nargs="+", action="extend", default=[], metavar="ID")
    p.add_argument("--milestone", help="milestone (default: the current one)")
    p.add_argument("--room", choices=("clean", "dirty"), help="clean-room separation (see [rooms] in theseus.toml)")
    p.add_argument("--priority", type=int, default=0, help="1 (last) to 5 (first), default 3; an old task gains a level "
                                                          "per [queue] aging_hours")
    p.add_argument("--profile", help="agent profile the task needs (default: any); see theseus tasks profile")
    p.add_argument("--min-capability", type=int, default=0, metavar="N",
                   help="lowest profile capability that may take the task (default 0: any); see theseus tasks capability")
    p.add_argument("--touches", nargs="+", action="extend", default=[], metavar="PATH",
                   help="shared files outside ownedPaths the task will patch; a running task that owns or touches them "
                        "holds this one, and this one holds them while it runs")
    p.add_argument("--hold", action="store_true", help="create the task held (theseus tasks release-hold ID lets it run)")
    for name in ("list", "ls"):
        p = ts.add_parser(name, help="one line per task with readiness or blocking reasons")
        p.add_argument("--status", choices=("pending", "in_progress", "verification", "merged", "completed"))
        p.add_argument("--all", action="store_true", help="include archived tasks")
        p.add_argument("--evidence", action="store_true", help="show merged / observed / user-tested")
    p = ts.add_parser("edit", help="change a pending task (an in-progress one with --force, which leaves its worker a "
                                   "note); verification and later: reopen first")
    p.add_argument("id")
    p.add_argument("--role")
    p.add_argument("--objective")
    p.add_argument("--contract")
    p.add_argument("--accept", nargs="+", action="extend", metavar="TEXT", help="replaces the acceptance items; repeatable")
    p.add_argument("--owned", nargs="+", action="extend", metavar="PATH", help="replaces the owned paths")
    p.add_argument("--add-owned", nargs="+", action="extend", metavar="PATH", help="adds owned paths")
    p.add_argument("--touches", nargs="+", action="extend", metavar="PATH", help="replaces the shared files the task will patch")
    p.add_argument("--depends", nargs="*", action="extend", metavar="ID", help="replaces the dependencies (none: clear them)")
    p.add_argument("--milestone")
    p.add_argument("--room", choices=("clean", "dirty", ""))
    p.add_argument("--profile")
    p.add_argument("--priority", type=int)
    p.add_argument("--min-capability", type=int, metavar="N")
    p.add_argument("--note", help="the coordinator note carried in the task message (replaces it)")
    p.add_argument("--append-note", metavar="TEXT", help="add a dated line to the coordinator note")
    p.add_argument("--external-blocker", metavar="EVIDENCE", help="an input the team cannot obtain itself (with --unblock)")
    p.add_argument("--research-required", metavar="EVIDENCE",
                   help="unrecovered behavior that needs a bounded research task first (with --unblock)")
    p.add_argument("--unblock", metavar="CONDITION", help="what lifts the blocker")
    p.add_argument("--clear-blockers", action="store_true", help="drop externalBlocker and researchRequired")
    p.add_argument("--force", action="store_true", help="edit an in-progress task")
    p = ts.add_parser("import", help="load docs/project/tasks.json and tasks-archive.jsonl into the host store (once "
                                     "per project; the store is backed up first and the import checked lossless)")
    p.add_argument("--replace", action="store_true", help="load over tasks already in the store (they are exported "
                                                          "to <data>/exports/ first)")
    p.add_argument("--new-id", action="store_true", help="give the project a fresh id (another project reusing the name)")
    ts.add_parser("export", help="write the store's tasks to docs/project/ (tasks.json, tasks-archive.jsonl, "
                                 "tasks-history.jsonl); commit them with the next change")
    ts.add_parser("adopt", help="record this checkout as the root of the project's tasks (a moved checkout)")
    p = ts.add_parser("ready", help="pending tasks of the current milestone whose dependencies are merged or completed")
    p.add_argument("--include-paused", action="store_true")
    p.add_argument("--any-milestone", action="store_true")
    p = ts.add_parser("show", help="print one task record, open or archived")
    p.add_argument("id")
    p.add_argument("--field")
    p.add_argument("--history", action="store_true", help="with its notes and event history")
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
    ts.add_parser("migrate-ids", help="deprecated: agent ids live on the attempts in the host store")
    p = ts.add_parser("release", help="return an assigned task to pending, keeping the attempt")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = ts.add_parser("deliver", help="mark a task delivered (awaiting coordinator integration)")
    p.add_argument("id")
    p.add_argument("--handoff", required=True)
    p = ts.add_parser("priority", help="set a task's priority, 1 (last) to 5 (first)")
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
    p = ts.add_parser("rebase", help="bring the worktree up to the base ref: record a rebase the worker did, or move HEAD "
                                     "and replay its uncommitted edits (conflicts stay as markers); refuses when the "
                                     "worker committed")
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
    ts.add_parser("archive", help="move completed tasks to tasks-archive.jsonl and commit the two files")
    p = ts.add_parser("milestone", help="print or set currentMilestone ('' clears it)")
    p.add_argument("value", nargs="?")
    tasks.set_defaults(func=guarded(json_reply(cmd_tasks)))
    for subparser in ts.choices.values():
        subparser.add_argument("--json", action="store_true", help=JSON_ENVELOPE)
    task = sub.add_parser("task", help="a worker's own task (name it from your assignment): show, note, done, blocked")
    tk = task.add_subparsers(dest="task_command", required=True)
    p = tk.add_parser("show", help="your task: the record, notes and worktree")
    p.add_argument("id")
    p.add_argument("--field")
    p = tk.add_parser("note", help="a progress note the coordinator reads (--question also wakes it)")
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--question", action="store_true", help="a question that waits for the coordinator's answer")
    p = tk.add_parser("done", help="report the delivery: reads the handoff's Result and your patch targets")
    p.add_argument("id")
    p.add_argument("--handoff", default=f"{cfg.WORKER_LOG}/handoff.md")
    p = tk.add_parser("blocked", help="report that the task cannot go on, and why")
    p.add_argument("id")
    p.add_argument("reason")
    p.add_argument("--handoff")
    for subparser in tk.choices.values():
        subparser.add_argument("--json", action="store_true", help=JSON_ENVELOPE)
    task.set_defaults(func=guarded(json_reply(cmd_task)))
    p = sub.add_parser("mcp", help="the task tools as an MCP server over stdio (needs theseus-lab[mcp]): a worker's "
                                   "tools from THESEUS_CREDENTIALS, or the coordinator's with --role coordinator")
    p.add_argument("--role", choices=("coordinator",), help="only in the coordinator's own MCP config")
    p.set_defaults(func=guarded(cmd_mcp))

    inst = sub.add_parser("instance", help="persistent worker sessions in tmux panes")
    ins = inst.add_subparsers(dest="instance_action", required=True)
    p = ins.add_parser("assign", help="create the worktree and branch, record and commit the assignment")
    p.add_argument("task")
    p.add_argument("instance")
    p.add_argument("suffix")
    p.add_argument("--provider")
    p.add_argument("--no-commit", action="store_true", help="no effect (kept for scripts): an assignment commits nothing")
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
    p.add_argument("--no-commit", action="store_true", help="no effect (kept for scripts): an assignment commits nothing")
    p.set_defaults(func=guarded(as_coordinator(cmd_dispatch)))

    p = sub.add_parser("merge", help="integrate a delivered task: ownership check, three-way apply, patches, indexes, commit")
    p.add_argument("task")
    p.add_argument("--subject", help="commit subject (default: the task objective)")
    p.add_argument("--accept", action="append", help="a reviewed out-of-ownership path to accept; repeatable")
    p.add_argument("--skip-patch", action="append", help="a worker patch file name to skip; repeatable")
    p.add_argument("--no-commit", action="store_true", help="apply without committing")
    p.add_argument("--no-index", action="store_true", help="do not regenerate the docs indexes")
    p.set_defaults(func=guarded(as_coordinator(cmd_merge)))

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
    p.add_argument("--client", default="agents", choices=("agents", "claude_code", "antigravity"),
                   help="skill: ~/.agents/skills (agents, default), ~/.claude/skills (claude_code) or ~/.gemini/config/skills (antigravity)")
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
    p.add_argument("--drifted", action="store_true", help="restart: every agent without a task whose launch configuration "
                                                          "(command, arguments, model, env, MCP config, extra_args) changed")
    p.add_argument("--read-only", action="store_true", help="attach: look without typing")
    p.add_argument("--window", action="store_true", help="attach: open in a new window of your own tmux")
    p.add_argument("--lines", type=int, default=200, help="logs: history lines (default 200)")
    p.add_argument("--transcript", action="store_true", help="logs: print the conversation transcript path instead")
    p.add_argument("--with-task", action="store_true", help="migrate: also move agents that hold a task; restart --fresh: "
                                                            "restart an agent that holds a task and send it one restart "
                                                            "prompt for the task")
    p.add_argument("--file", metavar="PROMPT", help="restart --fresh --with-task: the restart prompt to send instead of the "
                                                    "one built from the task")
    p.add_argument("--json", action="store_true", help="ls and list: JSON")
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
    p = sub.add_parser("attention", help="what needs the coordinator now (deliveries, merges to verify, questions, stalled "
                                         "agents, gateway pauses, assignment errors, a stopped driver, the export, "
                                         "verification debt), each with a suggested command",
                       description="The attention list is computed from the task store and live state on every call; an "
                                   "item disappears only when its cause is resolved. Run it at the start of every turn and "
                                   "after every action. --follow prints one line whenever the list changes and never exits, "
                                   "for a harness that streams a command's output.")
    p.add_argument("--json", action="store_true", help="one JSON object: summary, items (key, kind, project, since, "
                                                       "summary, command) and the push settings")
    p.add_argument("--follow", action="store_true", help="print one line per change; never exits")
    p.add_argument("--interval", type=float, default=10.0, help="--follow: seconds between checks (default 10)")
    p.set_defaults(func=guarded(cmd_attention))
    p = sub.add_parser("coordinator", help="the project's coordinator: up, down, restart, attach and status of its "
                                           "supervised tmux session, or register a pane of your own for the attention "
                                           "push",
                       description="up starts the agent of [coordinator] profile (no default: you choose it) in session "
                                   "PROJECT--coordinator of the private tmux server, in the project root, supervised "
                                   "like an agent (a crash resumes its conversation) and registered for the attention "
                                   "push; a new conversation gets the project's start prompt. attach shows it in this "
                                   "terminal, nested in your own tmux too (detach with the prefix and d, or C-q). "
                                   "register records a pane you run yourself ($TMUX_PANE by default) instead.")
    p.add_argument("action", nargs="?", default="status",
                   choices=("status", "up", "down", "restart", "attach", "register", "unregister", "_exec"),
                   metavar="ACTION", help="status, up, down, restart, attach, register [PANE], unregister")
    p.add_argument("args", nargs="*", metavar="PANE", help="register: the pane id (default $TMUX_PANE)")
    p.add_argument("--profile", help="up, restart: the coordinator's agent profile instead of [coordinator] profile; "
                                     "register: the profile of the pane's agent (default [workers] default)")
    p.add_argument("--fresh", action="store_true", help="restart: a new conversation")
    p.add_argument("--read-only", action="store_true", help="attach: look without typing")
    p.add_argument("--no-wait", action="store_true", help="up, restart: return once started, not ready")
    p.set_defaults(func=guarded(cmd_coordinator))
    p = sub.add_parser("tmux", help="theseus's private tmux server: status (the status line's cache), apply (the "
                                    "generated configuration to the running server), conf",
                       description="The private server (tmux -L theseus) runs with a configuration theseus generates "
                                   "(never your ~/.tmux.conf); [tmux] prefix and extra_conf in the host config adjust "
                                   "it. status prints the cache the queue driver writes after each tick (--left and "
                                   "--right are the status line's parts).")
    p.add_argument("action", choices=("status", "apply", "conf", "_popup", "_goto"), metavar="ACTION",
                   help="status, apply, conf")
    p.add_argument("what", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("--file", help="status: the cache file (default: the current project's)")
    p.add_argument("--project", help="status: the project whose cache to read")
    p.add_argument("--left", action="store_true", help="status: the left part (the agent's task)")
    p.add_argument("--right", action="store_true", help="status: the right part (attention, working/max, gateway)")
    p.add_argument("--agent", help="status --left: the agent")
    p.add_argument("--kind", help="status --right: the session's kind (coordinator, agent, queue)")
    p.add_argument("--json", action="store_true", help="status: the cache as JSON")
    p.add_argument("--print", dest="print_text", action="store_true", help="conf: print the file instead of its path")
    p.add_argument("--socket", help=argparse.SUPPRESS)
    p.add_argument("--client", help=argparse.SUPPRESS)
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tmux)
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
    p.set_defaults(func=guarded(cmd_top))
    p = sub.add_parser("projects", help="list the projects registered on this host")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include closed projects")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("close", help="close this project: export its tasks, stop its agents, queue driver and coordinator, "
                                     "and keep them stopped (hidden from the console) until `theseus reopen`")
    p.add_argument("--reason", help="recorded with the close and shown by `theseus projects --all`")
    p.add_argument("--force", action="store_true", help="also stop agents that hold a task (it stays assigned and "
                                                        "resumes on reopen)")
    p.add_argument("--commit", action="store_true", help="commit the task export")
    p.set_defaults(func=guarded(cmd_close))

    p = sub.add_parser("reopen", help="reopen a closed project: its agents, queue driver and (if it ran) coordinator")
    p.add_argument("--no-wait", action="store_true", help="return once started, not ready")
    p.set_defaults(func=guarded(cmd_reopen))

    p = sub.add_parser("db", help="host store: check, maintain, backup, restore FILE --yes, prune",
                       description="The host database (~/.local/share/theseus/theseus.db, THESEUS_DB overrides) holds "
                                   "projects, agents, events, pool slots, deliveries and samples for every project on "
                                   "this machine. The queue runs maintain and backup at most once a day by itself.")
    p.add_argument("action", nargs="?", default="check",
                   choices=("check", "maintain", "backup", "restore", "prune", "downgrade"),
                   help="check: integrity, version, sizes, rows, last backup and maintenance; maintain: trim by "
                        "retention, ANALYZE, optimize, checkpoint, vacuum; backup: online copy, integrity check and "
                        "rotation; restore FILE --yes: back up the current database, then load FILE; prune: list (with "
                        "--yes delete) projects whose root is gone; downgrade 6 --yes: remove the task store (schema 7) "
                        "once every project's export is current, for an older theseus")
    p.add_argument("file", nargs="?", help="restore: the backup file to load; downgrade: the version (6)")
    p.add_argument("--yes", action="store_true", help="restore, prune: really do it")
    p.add_argument("--older-than-days", type=float, help="prune: also projects nobody used for this many days")
    p.add_argument("--with-tasks", action="store_true", help="prune: also projects with tasks in the store, after "
                                                              "exporting them to the project's data directory")
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
    if tuple(argv[:2]) == ("tmux", "status") and "--file" in argv and ("--left" in argv or "--right" in argv):
        # The status line runs this every few seconds: skip the parser and the imports it needs.
        from . import tmuxconf
        value = dict(zip(argv, argv[1:]))
        print(tmuxconf.render(value.get("--file"), "left" if "--left" in argv else "right", value.get("--agent"),
                              kind=value.get("--kind")))
        return
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
            code = args.func(args)
            if isinstance(code, int) and not isinstance(code, bool) and code:
                sys.exit(code)
            return
        except store.SchemaTooNew as error:
            if not (args.command == "queue" and args.action in ("run", "wait")):
                sys.exit(f"theseus: {error}")
            upgrade.newer_store(error, argv, interval=args.interval or 30)
        except upgrade.CodeChanged:
            upgrade.restart(argv)
            return
        except store.StoreError as error:
            sys.exit(f"theseus: {error}")


if __name__ == "__main__":
    main()
