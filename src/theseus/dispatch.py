"""One-shot worker dispatch: worktree at the committed base, the rendered prompt, the assignment, and a launch.

The agent profile (theseus.profiles) decides how: a profile without a headless command (claude-subagent) prepares
everything and launches nothing, and the coordinator passes prompt.md to a subagent itself; any other profile starts
its headless command in the background, inside its own systemd user slice, with a preassigned session ID when the
CLI accepts one, the resume command recorded in launch.json for followups, and agent-run.sh retrying transient
gateway errors when the profile allows it. Coordinator-side records live in <data>/workers/NAME/; the worker writes its
logs and handoff to <worktree>/.theseus/.
"""
import datetime
import json
import shlex
import subprocess
from importlib import resources

from . import config as cfg
from . import integrate
from . import rules
from . import store
from . import tasks as t

WORKER_NOTES = (
    "Worker notes: never spawn subagents or parallel model calls; work alone from the code, the data and your own "
    "checks. Your shell may not keep the working directory between calls, so start every command with "
    "`cd {worktree} && ...` and use absolute paths inside the worktree for file edits; never edit the coordinator's "
    "checkout at {root}. Load project skills from .agents/skills/ when your agent supports skills. Stop every process "
    "you start before replying and never leave background jobs running. Never generate artificial load, and never "
    "print the environment (env, printenv, set, declare -p, /proc/*/environ): it holds credentials.\n\n"
)


def script(name):
    return str(resources.files("theseus") / "scripts" / name)


def dispatch(conf, task_id, name=None, provider=None, model=None, dry_run=False, resume_paused=False, commit=True):
    from . import profiles
    try:
        profile = profiles.resolve(conf, provider)
    except profiles.ProfileError as error:
        raise integrate.IntegrationError(str(error)) from error
    provider = profile["name"]
    model = model or profile.get("model") or ""
    if profile.get("headless") is None and not int(profile.get("capacity") or 0):
        raise integrate.IntegrationError(f"profile {provider} launches subagents from the coordinator session; set "
                                         f"workers.{provider}.capacity in theseus.toml to opt in")
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if task_id not in t.ready(queue, include_paused=resume_paused):
        index = queue.index()
        reasons = t.blocking(task, index, queue.milestone(), t.merge_reach(conf), queue.holds()) or [task["status"]]
        raise integrate.IntegrationError(f"{task_id} is not ready: {', '.join(reasons)}")
    problems = rules.task_record_problems(conf)
    if problems:
        raise integrate.IntegrationError("task records break the Supervisor rules; fix them before dispatching:\n" + "\n".join(problems))
    base = integrate.base_commit(conf)
    integrate.check_reached(conf, task, base)
    name = name or task_id
    worktree = conf.worktrees / name
    record_dir = conf.workers_dir / name
    log_dir = worktree / cfg.WORKER_LOG
    if worktree.exists():
        integrate.check_previous_attempt(conf, task, worktree)
    from . import rooms
    prompt = WORKER_NOTES.format(worktree=worktree, root=conf.root) + t.render_prompt(queue, task_id, base[:12], worktree, log_dir)
    room_rules = rooms.message(conf, task) if rooms.enabled(conf) else ""
    if room_rules:
        prompt += "\n\n" + room_rules
    names, consumers = t.outside_consumers(queue, task)
    lines = []
    if consumers:
        lines.append(f"note: {len(consumers)} files outside ownedPaths use names declared inside them (`theseus tasks consumers {task_id}`)")
    session = profiles.new_session(profile) if profile.get("session") == "preassign" else ""
    launch = profiles.launch_argv(profile, prompt, session, model, conf, agent=f"worker-{name}",
                                  credentials=record_dir / "credentials") or []
    resume = profiles.resume_template(profile, session, model, conf)
    extra_env = profiles.env(profile, model)
    if launch and extra_env:
        launch = ["env", *(f"{k}={v}" for k, v in extra_env.items()), *launch]
    if dry_run:
        lines += [prompt, "--- commands ---", f"git worktree add -b work/{name} {worktree} {base[:12]}",
                  f"theseus tasks assign {task_id} --owner {provider}:{model}:{name} ..."]
        if launch:
            shown = [("<prompt>" if a == prompt else a) for a in launch]
            lines.append(f"(cd {shlex.quote(str(worktree))} && worker-scope.sh {name} -- agent-run.sh {record_dir} -- "
                         f"{shlex.join(shown)}) &")
        return lines
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "prompt.md").write_text(prompt + "\n")
    principal = t.acting_principal(None)
    # A one-shot worker registers no agent, so its attempt carries an id minted for this dispatch.
    worker_id = store.new_id()
    claim = queue.service.claim(principal, task_id, worker_id, owner=f"{provider}:{model}:{name}",
                                provider=provider, model=model, force=resume_paused,
                                capacity=None if resume_paused else t.provider_capacity(conf, provider),
                                reach=None if resume_paused else t.merge_reach(conf, base))
    created = False
    try:
        aside = integrate.move_previous_attempt(conf, queue, task_id, worktree)
        if aside:
            lines.append(f"the previous attempt's worktree moved to {aside}")
        integrate.create_worktree(conf, name, base)
        created = True
        rooms.exclude_dirty(conf, task, worktree)
        queue.service.activate(principal, claim["attempt"], worktree=worktree, base=base[:12], log_dir=log_dir)
        t.write_inbox(queue, task_id)
    except Exception as error:
        if created:
            integrate.remove_worktree(conf, worktree, name)
        integrate.abandon_quietly(queue, principal, claim["attempt"], f"dispatch failed: {error}")
        raise
    record = {"task": task_id, "name": name, "provider": provider, "profile": profile["preset"], "model": model, "base": base,
              "worktree": str(worktree), "session": session, "resume": resume, "env": extra_env,
              "started": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not launch:
        from . import taskservice
        credentials = record_dir / "credentials"
        taskservice.grant(taskservice.QUEUE, worker_id, worker_id, credentials)
        with open(record_dir / "prompt.md", "a") as handle:
            handle.write(f"\nYour task commands need your credentials: read the record with `THESEUS_CREDENTIALS="
                         f"{credentials} theseus task show {task_id}`, and when the handoff is written, report with "
                         f"`THESEUS_CREDENTIALS={credentials} theseus task done {task_id} --handoff "
                         f"{log_dir / 'handoff.md'}`. Never print the credentials file.\n")
        (record_dir / "launch.json").write_text(json.dumps(record) + "\n")
        lines += [f"prepared {task_id} in {worktree}. Launch a subagent{f' (model {model})' if model else ''} whose prompt says: "
                  f"read and follow {record_dir / 'prompt.md'}",
                  f"the handoff appears at {log_dir / 'handoff.md'}"]
        return lines
    (record_dir / "stderr.log").write_text("")
    env = integrate.worker_environment(conf, worktree)
    from . import taskservice
    env.update(taskservice.grant(taskservice.QUEUE, worker_id, worker_id, record_dir / "credentials"))
    env.update({"THESEUS_RETRY": "1" if profile.get("retry") and resume else "0",
                "THESEUS_RESUME_JSON": json.dumps(resume) if resume else ""})
    process = subprocess.Popen(
        ["env", "--default-signal=INT", "bash", script("worker-scope.sh"), name, "--",
         "bash", script("agent-run.sh"), str(record_dir), "--", *launch],
        cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    record["pid"] = process.pid
    (record_dir / "pid").write_text(f"{process.pid}\n")
    (record_dir / "launch.json").write_text(json.dumps(record) + "\n")
    lines += [f"launched {task_id} as pid {process.pid} in {worktree}; records in {record_dir}, worker logs in {log_dir}",
              "keep `theseus wait` running in the background; `theseus status` shows progress"]
    return lines
