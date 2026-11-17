"""The Textual application behind `theseus top`: a Host tab and one tab per registered project."""
import hashlib
import socket
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ItemGrid, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from .. import config as cfg
from .. import host, top
from .. import workqueue as wq
from . import charts, resources, theme
from . import rows as R

HOST_TAB = "tab-host"
HISTORY_SAMPLES = 720


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


class Pane(TabPane):
    def _on_descendant_focus(self, event):
        # Textual activates the pane of a programmatically focused widget; a deferred focus would undo a tab switch.
        event.prevent_default()


class Panel(Vertical):
    """A titled, bordered section; `accent` colors the border and title (a project's color)."""

    def __init__(self, title, *children, accent=None, **kwargs):
        super().__init__(*children, **kwargs)
        self.border_title = title
        self.accent = accent

    def on_mount(self):
        if self.accent:
            self.styles.border = ("round", self.accent)
            self.styles.border_title_color = self.accent


class BarChart(Widget):
    """A legend and a stacked bar chart of [(label, style, counts)] sized to the widget."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.series = []

    def plot(self, series):
        self.series = series
        self.refresh()

    def render(self):
        if not self.series:
            return Text("no activity data", style="dim")
        key = charts.legend(self.series, self.size.width)
        lines = [*key, *charts.stacked_bars(self.series, self.size.width, self.size.height - len(key))]
        return Text("\n").join(lines)


class HistoryChart(Widget):
    """The newest samples of a ring buffer as a filled chart."""

    def __init__(self, ring, seconds, **kwargs):
        super().__init__(**kwargs)
        self.ring = ring
        self.seconds = seconds

    def render(self):
        if not len(self.ring):
            return Text("CPU history: collecting samples", style="dim")
        title = Text("CPU history", style="bold")
        return Text("\n").join([title, *charts.history(self.ring.values(), self.size.width, self.size.height - 1,
                                                       style="level", seconds=self.seconds)])


class ProjectCard(Static, can_focus=True):
    """A project on the Host tab: its accent border, name and summary; Enter or a click opens its tab."""

    BINDINGS = [Binding("enter", "open", "Open tab", key_display="Enter")]

    class Open(Message):
        def __init__(self, root):
            super().__init__()
            self.root = root

    def __init__(self, item, **kwargs):
        super().__init__(R.card_text(item), id=f"card-{pane_id(item['root'])}", **kwargs)
        self.root = item["root"]
        self.project = item["name"]
        self.search = ""
        self.border_title = Content.styled(item["name"], "bold")
        self.accent = theme.project_color(item["name"])

    def on_mount(self):
        self.styles.border = ("round", self.accent)
        self.styles.border_title_color = self.accent

    def on_focus(self):
        self.styles.border = ("heavy", self.accent)

    def on_blur(self):
        self.styles.border = ("round", self.accent)

    def show(self, item):
        body = R.card_text(item)
        self.search = f"{item['name']} {body.plain}".lower()
        self.update(body)

    def action_open(self):
        self.post_message(self.Open(self.root))

    def on_click(self):
        self.action_open()


def resource_text(state, cpu):
    """The meters of the Host panel: CPU, load, memory, swap and every GPU."""
    line = Text()

    def meter(label, fraction, value, detail=""):
        line.append(f"{label:<5} ", style="bold")
        line.append_text(charts.meter(fraction))
        line.append(f" {value:>7}")
        if detail:
            line.append(f"  {detail}", style="dim")
        line.append("\n")

    total = cpu["total"] if cpu else None
    meter("CPU", (total or 0) / 100, "-" if total is None else f"{total:.1f}%", f"{state['cpus']} threads")
    load = state["load"]
    meter("LOAD", load[0] / state["cpus"] if state["cpus"] else 0, f"{load[0]:.2f}", f"{load[1]:.2f} {load[2]:.2f} (5, 15 min)")
    used = state["memTotal"] - state["memAvailable"]
    meter("MEM", used / state["memTotal"] if state["memTotal"] else 0, top.size(used), f"of {top.size(state['memTotal'])}")
    swap = state["swapTotal"] - state["swapFree"]
    meter("SWAP", swap / state["swapTotal"] if state["swapTotal"] else 0, top.size(swap),
          f"of {top.size(state['swapTotal'])}" if state["swapTotal"] else "no swap")
    for number, gpu in enumerate(state.get("gpus") or []):
        meter(f"GPU{number}", gpu["util"] / 100, f"{gpu['util']:.0f}%", gpu["name"])
        meter(" mem", gpu["memUsed"] / gpu["memTotal"] if gpu["memTotal"] else 0, top.size(gpu["memUsed"]),
              f"of {top.size(gpu['memTotal'])}")
        meter(" temp", gpu["temp"] / 100, f"{gpu['temp']:.0f} C")
    line.rstrip()
    return line


def core_text(cpu, width):
    """One small bar per CPU thread, as many per line as fit in `width`."""
    if not cpu or not cpu["cores"]:
        return Text("per-core CPU: collecting samples", style="dim")
    line = Text()
    per_row = max(1, width // 18)
    for index, value in enumerate(cpu["cores"]):
        line.append(f"{index:>3} ", style="dim")
        line.append_text(charts.meter(value / 100, 8))
        line.append(f" {value:>3.0f}%")
        line.append("\n" if (index + 1) % per_row == 0 else "  ")
    line.rstrip()
    return line


class HostView(VerticalScroll):
    def __init__(self, history, seconds, **kwargs):
        super().__init__(**kwargs)
        self.history = history
        self.seconds = seconds
        self.cores = False
        self.cpu = None

    def compose(self) -> ComposeResult:
        with Panel(f"HOST {socket.gethostname()}", id="resources"):
            with Horizontal(id="resource-row"):
                yield Static(id="meters")
                yield HistoryChart(self.history, self.seconds, id="cpu-history")
            yield Static(id="cores")
        with Panel("ACTIVITY all projects, events per 15 minutes over the last 12 hours", id="host-activity"):
            yield BarChart(id="host-chart", classes="chart")
        with Panel("PROJECTS Enter or click opens the tab", id="projects"):
            yield ItemGrid(id="cards", min_column_width=44, regular=False)
        with Panel("POOLS", id="pools-panel"):
            yield table("pools", R.POOL_COLUMNS)
        with Panel("EVENTS across projects, newest first", id="events-panel"):
            yield table("events", R.HOST_EVENT_COLUMNS)

    def cards(self):
        return list(self.query(ProjectCard))

    async def sync_cards(self, projects):
        if [card.root for card in self.cards()] != [item["root"] for item in projects]:
            grid = self.query_one("#cards", ItemGrid)
            await grid.remove_children()
            await grid.mount(*[ProjectCard(item) for item in projects])

    def update(self, snap, text):
        state = snap["host"]
        meters = resource_text(state, self.cpu)
        self.query_one("#meters", Static).update(meters)
        self.query_one("#resource-row").styles.height = max(6, len(meters.plain.splitlines()))
        cores = self.query_one("#cores", Static)
        cores.display = self.cores
        if self.cores:
            cores.update(core_text(self.cpu, self.size.width - 4))
        self.query_one("#cpu-history", HistoryChart).refresh()
        self.query_one("#host-chart", BarChart).plot(charts.project_series(snap["projects"]))
        needle = text.strip().lower()
        items = {item["root"]: item for item in snap["projects"]}
        for card in self.cards():
            if card.root in items:
                card.show(items[card.root])
                card.display = not needle or needle in card.search
        fill(self.query_one("#pools", DataTable), R.matches(R.pool_rows(snap["pools"]), text))
        fill(self.query_one("#events", DataTable), R.matches(R.event_rows(snap["events"], project=True), text))


class ProjectView(VerticalScroll):
    def __init__(self, root, name, **kwargs):
        super().__init__(**kwargs)
        self.root_path = root
        self.accent = theme.project_color(name)
        self.panes = {}

    def compose(self) -> ComposeResult:
        accent = self.accent
        with Panel("PROJECT", id="status-panel", accent=accent):
            yield Static(id="status")
        with Panel("STATUS", id="attention-panel", accent=accent):
            yield Static(id="attention")
        with Panel("AGENTS Enter jumps to the pane", accent=accent):
            yield table("agents", R.AGENT_COLUMNS)
        with Panel("WAITING", id="waiting-panel", accent=accent):
            yield table("waiting", R.WAITING_COLUMNS)
        with Panel("ACTIVITY events per 15 minutes over the last 12 hours", accent=accent):
            yield BarChart(id="chart", classes="chart")
        with Panel("EVENTS newest first", accent=accent):
            yield table("events", R.EVENT_COLUMNS)

    def update(self, item, text):
        status = self.query_one("#status", Static)
        agents, waiting, events = (self.query_one(f"#{name}", DataTable) for name in ("agents", "waiting", "events"))
        status_panel = self.query_one("#status-panel", Panel)
        status_panel.border_title = Content.styled(item["name"], "bold")
        if not item["available"]:
            status.update(Text(f"unavailable: {item['error']}", style="red"))
            for widget in (agents, waiting, events):
                fill(widget, [])
            return
        state = item["snapshot"]
        status_panel.border_subtitle = item["root"]
        status.update(R.project_status(state))
        found = top.issues(state)
        attention = self.query_one("#attention-panel", Panel)
        attention.border_title = f"ACTION NEEDED {len(found)}" if found else "STATUS"
        attention.styles.border_title_color = self.app.theme_variables["warning"] if found else self.accent
        self.query_one("#attention", Static).update(R.attention(state))
        queue = state["queue"]
        self.query_one("#waiting-panel", Panel).border_title = (
            f"WAITING review {len(queue['review'])} · merged {len(queue['merged'])} · ready {len(queue['ready'])}"
            f" · blocked {len(queue['blocked'])} · held {len(queue['held'])}")
        self.panes = {f"agent:{agent['name']}:{agent['pane']}": agent["pane"] for agent in state["agents"]}
        fill(agents, R.matches(R.agent_rows(state), text))
        fill(waiting, R.matches(R.waiting_rows(state), text))
        fill(events, R.matches(R.event_rows(state["events"]), text))
        self.query_one("#chart", BarChart).plot(charts.kind_series(item["activity"]))


class TopApp(App):
    TITLE = "theseus top"
    AUTO_FOCUS = None
    CSS = """
    Panel { border: round $primary 60%; border-title-color: $primary; border-title-style: bold;
            border-subtitle-color: $text-muted; height: auto; padding: 0 1; }
    DataTable { height: auto; max-height: 14; }
    VerticalScroll, DataTable { scrollbar-size-vertical: 1; }
    #filter { dock: bottom; display: none; }
    #resource-row { height: 6; }
    #meters { width: 62; }
    #cpu-history { width: 1fr; height: 100%; }
    #cores { display: none; }
    .chart { height: 13; }
    #cards { grid-gutter: 0 1; }
    ProjectCard { border: round $primary; border-title-style: bold; height: auto; padding: 0 1; }
    ProjectCard:focus { background: $boost; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "next_tab", "Tabs 1-9", priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause/resume queue"),
        Binding("c", "cores", "Per-core CPU"),
        Binding("/", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("a", "focus_table('agents')", "Agents", show=False),
        Binding("w", "focus_table('waiting')", "Waiting", show=False),
        Binding("e", "focus_table('events')", "Events", show=False),
        *[Binding(str(n), f"goto({n})", f"Tab {n}", show=False) for n in range(1, 10)],
    ]

    def __init__(self, interval=5.0, current=None, roots=(), snapshot_fn=None, jump_fn=None, loader=None, sampler=None):
        super().__init__()
        self.interval = interval
        self.current = str(Path(current).resolve()) if current else None
        self.snapshot_fn = snapshot_fn or (lambda: host.snapshot(list(roots)))
        self.jump_fn = jump_fn or top.jump_to
        self.loader = loader or (lambda root: cfg.load(root=root))
        self.sampler = sampler or resources.CpuSampler()
        self.history = resources.Ring(HISTORY_SAMPLES)
        self.cpu = None
        self.snap = None
        self.filter_text = ""
        self.busy = False
        self.pending_open = self.current
        self.greet = True
        self.views = {}
        self.items = {}

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        with TabbedContent(initial=HOST_TAB):
            with Pane("Host", id=HOST_TAB):
                yield HostView(self.history, self.interval, id="host-view")
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
        snap, error, cpu = None, None, None
        try:
            snap = self.snapshot_fn()
        except Exception as problem:
            error = f"{type(problem).__name__}: {problem}"
        try:
            cpu = self.sampler.sample()
        except Exception:
            cpu = None
        try:
            self.call_from_thread(self.finish, snap, error, cpu)
        except Exception:
            self.busy = False

    async def finish(self, snap, error, cpu=None):
        try:
            if cpu is not None:
                self.cpu = cpu
                self.history.push(cpu["total"])
            if snap is None:
                self.title = f"theseus top · refresh failed: {error}"
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
                self.views[identifier] = ProjectView(item["root"], item["name"])
                label = Content.styled(item["name"], f"bold {theme.project_color(item['name'])}")
                await tabs.add_pane(Pane(label, self.views[identifier], id=identifier))
        if self.pending_open and pane_id(self.pending_open) in self.views:
            tabs.active = pane_id(self.pending_open)
        self.pending_open = None
        await self.query_one(HostView).sync_cards(snap["projects"])
        self.title = f"theseus top · {len(snap['projects'])} project(s) · updated {top.local_clock(snap['time'])}"
        self.redraw()
        if self.greet and snap["projects"]:
            self.greet = False
            self.call_after_refresh(self.focus_first_card)

    def redraw(self):
        if self.snap is None:
            return
        view = self.query_one(HostView)
        view.cpu = self.cpu
        view.update(self.snap, self.filter_text)
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

    def action_cores(self):
        view = self.query_one(HostView)
        view.cores = not view.cores
        self.redraw()

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
        if view is not None and view.query(f"#{name}"):
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
        if isinstance(view, HostView):
            self.call_after_refresh(self.focus_first_card)
        elif view is not None:
            self.call_after_refresh(lambda: view.query_one("#agents", DataTable).focus())

    def focus_first_card(self):
        view = self.query_one(HostView)
        if self.query_one(TabbedContent).active != HOST_TAB:
            return
        cards = view.cards()
        (cards[0] if cards else view.query_one("#events", DataTable)).focus()

    @on(ProjectCard.Open)
    def open_project(self, event):
        self.query_one(TabbedContent).active = pane_id(event.root)

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


def run(interval=5.0, current=None, roots=()):
    TopApp(interval=interval, current=current, roots=roots).run()
    return 0
