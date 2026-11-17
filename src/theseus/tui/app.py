"""The Textual application behind `theseus top`: a Host tab and one tab per registered project."""
import hashlib
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Label, Sparkline, Static, TabbedContent, TabPane

from .. import config as cfg
from .. import host, top
from .. import workqueue as wq
from . import rows as R

HOST_TAB = "tab-host"
SPARK_TITLES = {"assigned": "assigned", "delivered": "delivered/partial/blocked", "merged": "merged/completed",
                "errors": "errors/stalled"}


def pane_id(root):
    return "p" + hashlib.sha1(str(root).encode()).hexdigest()[:10]


def fill(table, rows):
    try:
        current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    except Exception:
        current = None
    table.clear()
    for key, cells, _ in rows:
        table.add_row(*cells, key=key)
    keys = [key for key, _, _ in rows]
    if current in keys:
        table.move_cursor(row=keys.index(current), animate=False)


def table(identifier, columns):
    widget = DataTable(id=identifier, cursor_type="row", zebra_stripes=True)
    for name in columns:
        widget.add_column(name, key=name)
    return widget


class HostView(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        yield Label("PROJECTS", classes="title")
        yield table("projects", R.PROJECT_COLUMNS)
        yield Label("POOLS", classes="title")
        yield table("pools", R.POOL_COLUMNS)
        yield Label("EVENTS across projects, newest first", classes="title")
        yield table("events", ("time", "project", "kind", "agent", "task", "detail"))

    def update(self, snap, text):
        host_state = snap["host"]
        used = host_state["memTotal"] - host_state["memAvailable"]
        load = host_state["load"]
        self.query_one("#summary", Static).update(
            f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f} on {host_state['cpus']} threads   "
            f"memory {top.size(used)} of {top.size(host_state['memTotal'])}   {len(snap['projects'])} project(s)")
        fill(self.query_one("#projects", DataTable), R.matches(R.project_rows(snap["projects"]), text))
        fill(self.query_one("#pools", DataTable), R.matches(R.pool_rows(snap["pools"]), text))
        fill(self.query_one("#events", DataTable), R.matches(R.event_rows(snap["events"], project=True), text))


class ProjectView(VerticalScroll):
    def __init__(self, root, **kwargs):
        super().__init__(**kwargs)
        self.root_path = root
        self.panes = {}

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Label("AGENTS (Enter jumps to the pane)", classes="title")
        yield table("agents", R.AGENT_COLUMNS)
        yield Label("WAITING", classes="title")
        yield table("waiting", R.WAITING_COLUMNS)
        yield Label("EVENTS, newest first", classes="title")
        yield table("events", R.EVENT_COLUMNS)
        yield Label("ACTIVITY, events per 15 minutes over the last 12 hours", classes="title")
        for name in SPARK_TITLES:
            with Horizontal(classes="spark"):
                yield Label(SPARK_TITLES[name], classes="spark-label", id=f"label-{name}")
                yield Sparkline([0.0] * host.BINS, id=f"spark-{name}")

    def update(self, item, text):
        status = self.query_one("#status", Static)
        agents, waiting, events = (self.query_one(f"#{name}", DataTable) for name in ("agents", "waiting", "events"))
        if not item["available"]:
            status.update(Text(f"unavailable: {item['error']}", style="red"))
            for widget in (agents, waiting, events):
                fill(widget, [])
            return
        state = item["snapshot"]
        counts = host.summary_counts(item)
        progress = counts["progress"]
        line = Text()
        line.append(f"{state['milestone'] or 'no milestone'}", style="bold")
        if progress:
            line.append(f"  {progress[0]}/{progress[1]} done")
        if state["paused"]:
            line.append("   QUEUE PAUSED", style="bold yellow")
        git = state["git"]
        line.append(f"   {git['branch']} {'dirty ' + str(git['dirty']) if git['dirty'] else 'clean'}")
        today = state.get("today") or {}
        line.append(f"   today {today.get('delivered', 0)} delivered, {today.get('completed', 0)} completed")
        status.update(line)
        agent_rows = R.agent_rows(state)
        self.panes = {}
        for agent in state["agents"]:
            self.panes[f"agent:{agent['name']}:{agent['pane']}"] = agent["pane"]
        fill(agents, R.matches(agent_rows, text))
        fill(waiting, R.matches(R.waiting_rows(state), text))
        fill(events, R.matches(R.event_rows(state["events"]), text))
        for name, data in item["activity"].items():
            self.query_one(f"#spark-{name}", Sparkline).data = [float(n) for n in data]
            self.query_one(f"#label-{name}", Label).update(f"{SPARK_TITLES[name]} {sum(data)}")


class TopApp(App):
    TITLE = "theseus top"
    CSS = """
    .title { color: $accent; text-style: bold; margin-top: 1; }
    DataTable { height: auto; max-height: 14; }
    #filter { dock: bottom; display: none; }
    .spark { height: 2; }
    .spark-label { width: 30; }
    Sparkline { width: 1fr; height: 2; }
    #spark-assigned > .sparkline--max-color { color: $success; }
    #spark-delivered > .sparkline--max-color { color: $secondary; }
    #spark-merged > .sparkline--max-color { color: $primary; }
    #spark-errors > .sparkline--max-color { color: $error; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "next_tab", "Tabs 1-9", priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause/resume queue"),
        Binding("/", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("a", "focus_table('agents')", "Agents", show=False),
        Binding("w", "focus_table('waiting')", "Waiting", show=False),
        Binding("e", "focus_table('events')", "Events", show=False),
        *[Binding(str(n), f"goto({n})", f"Tab {n}", show=False) for n in range(1, 10)],
    ]

    def __init__(self, interval=5.0, current=None, roots=(), snapshot_fn=None, jump_fn=None, loader=None):
        super().__init__()
        self.interval = interval
        self.current = str(Path(current).resolve()) if current else None
        self.snapshot_fn = snapshot_fn or (lambda: host.snapshot(list(roots)))
        self.jump_fn = jump_fn or top.jump_to
        self.loader = loader or (lambda root: cfg.load(root=root))
        self.snap = None
        self.filter_text = ""
        self.busy = False
        self.pending_open = self.current
        self.views = {}
        self.items = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial=HOST_TAB):
            with TabPane("Host", id=HOST_TAB):
                yield HostView(id="host-view")
        yield Input(placeholder="filter: task, agent, kind, room, status (Esc clears)", id="filter")
        yield Footer()

    def on_mount(self):
        self.refresh_now()
        self.set_interval(self.interval, self.refresh_now)

    def refresh_now(self):
        if self.busy:
            return
        self.busy = True
        self.run_worker(self.collect, thread=True, name="collect")

    def collect(self):
        snap, error = None, None
        try:
            snap = self.snapshot_fn()
        except Exception as problem:
            error = f"{type(problem).__name__}: {problem}"
        try:
            self.call_from_thread(self.finish, snap, error)
        except Exception:
            self.busy = False

    async def finish(self, snap, error):
        try:
            if snap is None:
                self.sub_title = f"refresh failed: {error}"
                return
            await self.apply(snap)
        finally:
            self.busy = False

    async def apply(self, snap):
        self.snap = snap
        tabs = self.query_one(TabbedContent)
        self.items = {pane_id(item["root"]): item for item in snap["projects"]}
        for identifier in [i for i in self.views if i not in self.items]:
            await tabs.remove_pane(identifier)
            del self.views[identifier]
        for item in snap["projects"]:
            identifier = pane_id(item["root"])
            if identifier not in self.views:
                self.views[identifier] = ProjectView(item["root"])
                await tabs.add_pane(TabPane(item["name"], self.views[identifier], id=identifier))
        if self.pending_open and pane_id(self.pending_open) in self.views:
            tabs.active = pane_id(self.pending_open)
        self.pending_open = None
        self.sub_title = f"{len(snap['projects'])} project(s), updated {top.local_clock(snap['time'])}"
        self.redraw()

    def redraw(self):
        if self.snap is None:
            return
        self.query_one(HostView).update(self.snap, self.filter_text)
        for identifier, view in self.views.items():
            if identifier in self.items:
                view.update(self.items[identifier], self.filter_text)

    def tab_ids(self):
        return [pane.id for pane in self.query_one(TabbedContent).query(TabPane)]

    def action_goto(self, number):
        ids = self.tab_ids()
        if number <= len(ids):
            self.query_one(TabbedContent).active = ids[number - 1]

    def action_next_tab(self):
        self.shift_tab(1)

    def action_previous_tab(self):
        self.shift_tab(-1)

    def shift_tab(self, step):
        tabs = self.query_one(TabbedContent)
        ids = self.tab_ids()
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    def action_refresh(self):
        self.refresh_now()

    def action_filter(self):
        box = self.query_one("#filter", Input)
        box.styles.display = "block"
        box.focus()

    def action_clear_filter(self):
        box = self.query_one("#filter", Input)
        box.value = ""
        box.styles.display = "none"
        self.filter_text = ""
        self.redraw()
        self.set_focus(None)

    def action_focus_table(self, name):
        view = self.active_view()
        if view is not None:
            view.query_one(f"#{name}", DataTable).focus()

    def active_view(self):
        active = self.query_one(TabbedContent).active
        return self.views.get(active) or (self.query_one(HostView) if active == HOST_TAB else None)

    def active_item(self):
        return self.items.get(self.query_one(TabbedContent).active)

    @on(Input.Changed, "#filter")
    def filter_changed(self, event):
        self.filter_text = event.value
        self.redraw()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self, event):
        self.action_focus_table("agents")

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event):
        view = self.active_view()
        if view is not None:
            first = "projects" if isinstance(view, HostView) else "agents"
            self.call_after_refresh(lambda: view.query_one(f"#{first}", DataTable).focus())

    def action_pause(self):
        item = self.active_item()
        if item is None or not item["available"]:
            self.notify("select a project tab to pause or resume its queue")
            return
        try:
            conf = self.loader(item["root"])
            message = wq.set_paused(conf, not wq.paused(conf))
        except Exception as problem:
            self.notify(f"pause failed: {problem}", severity="error")
            return
        self.notify(message)
        item["snapshot"]["paused"] = wq.paused(conf)
        self.redraw()
        self.refresh_now()

    @on(DataTable.RowSelected)
    def row_selected(self, event):
        key = event.row_key.value or ""
        if event.data_table.id == "agents":
            view = self.active_view()
            pane = view.panes.get(key) if isinstance(view, ProjectView) else None
            if pane is None:
                self.notify("no tmux pane for this worker")
            else:
                self.notify(self.jump_fn(pane))
        elif event.data_table.id == "projects" and key.startswith("project:"):
            self.query_one(TabbedContent).active = pane_id(key[len("project:"):])


def run(interval=5.0, current=None, roots=()):
    TopApp(interval=interval, current=current, roots=roots).run()
    return 0
