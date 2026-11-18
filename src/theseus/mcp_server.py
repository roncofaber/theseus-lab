"""`theseus mcp`: the task service as MCP tools over stdio (optional extra `theseus-lab[mcp]`, the official SDK
`mcp>=2.3,<3`).

The role never comes from a tool argument. THESEUS_CREDENTIALS makes a worker server, which registers only the
worker tools and re-checks the credentials on every call (they stop working when the run ends). The coordinator's
own MCP config passes `--role coordinator`, which is accepted only when the caller is the coordinator by the
deny-by-default rule (theseus.taskservice.principal). A server with neither refuses to start."""
import functools
import json
import os
from pathlib import Path

from . import config as cfg
from . import tasks as t
from . import taskservice

WORKER_TOOLS = ("task_show", "task_note", "task_done", "task_blocked")
COORDINATOR_TOOLS = ("tasks_list", "tasks_show", "tasks_ready", "tasks_questions", "tasks_add", "tasks_edit",
                     "tasks_hold", "tasks_release", "tasks_reopen", "tasks_note")
INSTRUCTIONS = {
    "worker": "Your theseus task: task_show gives the record, notes and prompt; task_note records progress (question "
              "waits for the coordinator); finish with task_done after writing the handoff, or task_blocked.",
    "coordinator": "The project's task queue: list, show, ready and questions read it; add, edit, hold, release, "
                   "reopen and note change it. Merge, unmerge, verify and complete stay CLI commands.",
}


def resolve(role=None, env=None, cwd=None):
    """The principal a server runs as, or TaskError when it may not start."""
    env = os.environ if env is None else env
    if env.get("THESEUS_CREDENTIALS"):
        if role == "coordinator":
            raise taskservice.TaskError("forbidden", "credentials make a worker server; --role coordinator belongs only "
                                                     "in the coordinator's own MCP config")
        return taskservice.principal(env, cwd, via="mcp")
    if role != "coordinator":
        raise taskservice.TaskError("unauthenticated", "a worker MCP server needs THESEUS_CREDENTIALS (the per-agent MCP "
                                                       "config names it); the coordinator's config passes --role "
                                                       "coordinator")
    principal = taskservice.principal(env, cwd, via="mcp")
    if principal.role != "coordinator":
        raise taskservice.TaskError("forbidden", "only the coordinator may run a coordinator MCP server")
    return principal


def build(conf, principal, env=None):
    """An MCPServer with the tools of principal's role."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import CallToolResult, TextContent

    env = dict(os.environ if env is None else env)
    queue = t.Queue(conf)
    server = MCPServer("theseus", instructions=INSTRUCTIONS[principal.role])

    def caller():
        return taskservice.principal(env, via="mcp") if principal.role == "worker" else principal

    def tool(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            try:
                with t.acting(caller()):
                    result = function(*args, **kwargs)
            except t.TaskError as error:
                raise ToolError(f"{getattr(error, 'code', 'refused')}: {error}") from error
            data = structured(result)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False,
                                                                                    default=str))],
                                  structured_content=data)
        server.add_tool(call, name=function.__name__, description=(function.__doc__ or "").strip(),
                        structured_output=False)

    if principal.role == "worker":
        for function in worker_tools(queue):
            tool(function)
    else:
        for function in coordinator_tools(queue):
            tool(function)
    return server


def structured(result):
    """A tool result as one JSON object, the same in the text content and the structured content: a command's
    message as {"message"}, a list as {"items"}."""
    if isinstance(result, dict):
        return json.loads(json.dumps(result, ensure_ascii=False, default=str))
    if isinstance(result, str):
        return {"message": result}
    return {"items": json.loads(json.dumps(list(result), ensure_ascii=False, default=str))}


def worker_tools(queue):
    def task_show(task: str) -> dict:
        """Your task (name it from your assignment): record, notes, worktree and the rendered prompt."""
        return t.show_own(queue, t.acting_principal(), task)

    def task_note(task: str, text: str, question: bool = False) -> dict:
        """Record a progress note on your task; question=true also wakes the coordinator until it answers."""
        return t.report(queue, t.acting_principal(), task, "note", text=text, question=question)

    def task_done(task: str, handoff: str | None = None) -> dict:
        """Report your delivery once the handoff is written (default: handoff.md in your task's log directory)."""
        return t.report(queue, t.acting_principal(), task, "done", handoff=handoff or default_handoff(queue, task))

    def task_blocked(task: str, reason: str, handoff: str | None = None) -> dict:
        """Report that your task cannot go on, and why."""
        return t.report(queue, t.acting_principal(), task, "blocked", reason=reason, handoff=handoff)

    return task_show, task_note, task_done, task_blocked


def default_handoff(queue, task):
    attempt = queue.service.show(t.acting_principal(), task)["attempt"] or {}
    return str(Path(attempt.get("log_dir") or ".") / "handoff.md")


def coordinator_tools(queue):
    svc = queue.service

    def tasks_list(status: str | None = None, include_completed: bool = False) -> dict:
        """Task records, open ones unless include_completed; optionally only one status."""
        return svc.list(t.acting_principal(), status=status, include_completed=include_completed)

    def tasks_show(task: str, history: bool = False) -> dict:
        """One task record with its notes (and its event history with history=true)."""
        return svc.get(t.acting_principal(), task, history=history)

    def tasks_ready() -> dict:
        """Pending tasks the queue may assign now."""
        return t.ready(queue)

    def tasks_questions() -> dict:
        """Open worker questions."""
        return svc.questions(t.acting_principal())

    def tasks_add(task: str, role: str, owned: list[str], objective: str, contract: str, accept: list[str],
                  depends: list[str] | None = None, milestone: str | None = None, room: str | None = None,
                  priority: int = 0, touches: list[str] | None = None) -> dict:
        """Add a pending task (owned must include docs/investigations/TASK.md; at most six acceptance items)."""
        return t.add(queue, task, role, owned, objective, contract, accept, depends or (), milestone, room, priority,
                     touches=touches or ())

    def tasks_edit(task: str, changes: dict, force: bool = False) -> dict:
        """Change a pending task (an in-progress one with force): keys role, objective, contract, accept, owned,
        add_owned, touches, depends, milestone, room, profile, priority, min_capability, note."""
        return t.edit(queue, task, changes, force)

    def tasks_hold(task: str, on: bool = True) -> dict:
        """Keep the queue from assigning a task (on=false lets it go again)."""
        return t.hold(queue, task, on)

    def tasks_release(task: str, reason: str) -> dict:
        """Return an assigned task to pending, keeping the attempt."""
        return t.release(queue, task, reason)

    def tasks_reopen(task: str, reason: str) -> dict:
        """Undo a premature delivery: verification back to in progress."""
        return t.reopen(queue, task, reason)

    def tasks_note(task: str, text: str) -> dict:
        """Set the coordinator note the worker reads; it answers the task's open questions."""
        return t.note(queue, task, text)

    return (tasks_list, tasks_show, tasks_ready, tasks_questions, tasks_add, tasks_edit, tasks_hold, tasks_release,
            tasks_reopen, tasks_note)


def run(role=None):
    try:
        import mcp  # noqa: F401
    except ImportError as error:
        raise SystemExit("theseus: the MCP server needs the optional extra: pip install 'theseus-lab[mcp]'") from error
    principal = resolve(role)
    build(cfg.load(), principal).run()
