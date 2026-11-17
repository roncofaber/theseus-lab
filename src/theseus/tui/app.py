"""The Textual application behind `theseus top`: a Host tab and one tab per registered project."""
import hashlib
import os
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

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
from . import charts, popups, resources, theme
from . import rows as R

HOST_TAB = "tab-host"
HISTORY_SAMPLES = 720
MOUSE_HELP = (("click a row", "highlight it (click elsewhere or Escape clears the highlight)"),
              ("double-click a row", "open its details (agents: with a live mirror of the tmux pane)"),
              ("click an EVENTS header", "sort by that column; click again to reverse"),
              ("click a project card", "open the project's tab"),
              ("wheel", "scroll the section under the pointer"))
TABLE_HELP = (("up, down", "highlight a row (the first press shows the highlight)"),
              ("Enter", "open the highlighted row's details"))


def pane_id(root):
    return "p" + hashlib.sha1(str(root).encode()).hexdigest()[:10]


def same(before, after):
    if before is after:
        return True
    if isinstance(before, Text) and isinstance(after, Text):
        return before.plain == after.plain and before.style == after.style and before.spans == after.spans
    return before == after


def fill(table, rows):
    """Bring table to rows [(key, cells, search)] in place: remove, update and add rows by key, then reorder; the
    highlighted row keeps its key."""
    highlighted = None
    if table.show_cursor and table.row_count:
        try:
            highlighted = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            highlighted = None
    wanted = [key for key, _, _ in rows]
    present = {key.value for key in table.rows}
    for key in present - set(wanted):
        table.remove_row(key)
    columns = list(table.columns)
    for key, cells, _ in rows:
        if key in present:
            for column, before, after in zip(columns, table.get_row(key), cells):
                if not same(before, after):
                    resized = not isinstance(before, Text) or not isinstance(after, Text) or before.cell_len != after.cell_len
                    table.update_cell(key, column, after, update_width=resized)
        else:
            table.add_row(*cells, key=key)
    if [row.key.value for row in table.ordered_rows] != wanted:
        position = {id(table.get_row(key)[0]): index for index, key in enumerate(wanted)}
        table.sort(key=lambda values: position[id(values[0])])
    if highlighted in wanted:
        table.move_cursor(row=wanted.index(highlighted), animate=False)


class Table(DataTable):
    """A row table without a highlighted row until the user asks for one (a click, a cursor key, or a/w/e); losing
    focus (a click elsewhere or Escape) hides it again. A sortable table sorts by a column (header click, or s/S)."""

    def __init__(self, columns, sort=None, **kwargs):
        super().__init__(cursor_type="row", zebra_stripes=True, show_cursor=False, **kwargs)
        self.names = columns
        self.sort_by = sort
        for name in columns:
            self.add_column(self.label(name), key=name)

    def label(self, name):
        if self.sort_by is None:
            return Text(name)
        column, descending = self.sort_by
        return Text(f"{name} {('▼' if descending else '▲') if name == column else ' '}")

    def set_sort(self, column, descending=None):
        if descending is None:
            descending = not self.sort_by[1] if column == self.sort_by[0] else column == "time"
        self.sort_by = (column, descending)
        for key, column_object in self.columns.items():
            column_object.label = self.label(key.value)
        self.refresh()

    def reveal(self):
        self.show_cursor = True

    def on_blur(self):
        self.show_cursor = False

    def on_click(self, event):
        row = event.style.meta.get("row", -1)
        if not self.show_cursor and row is not None and row >= 0:
            # The first click only shows the highlight; otherwise a click on the row under the hidden cursor counts
            # as a second click and opens the popup.
            event.prevent_default()
            self.move_cursor(row=row, animate=False)
            self.show_cursor = True

    def action_cursor_down(self):
        if self.show_cursor:
            super().action_cursor_down()
        else:
            self.show_cursor = True

    def action_cursor_up(self):
        if self.show_cursor:
            super().action_cursor_up()
        else:
            self.show_cursor = True


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
        self.paint()

    def paint(self):
        if self.accent:
            self.styles.border = ("round", theme.css(self.accent))
            self.styles.border_title_color = theme.css(self.accent)


class BarChart(Widget):
    """A legend and a stacked bar chart of [(label, style, counts)] sized to the widget."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.series = []

    def plot(self, series):
        if series != self.series:
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
        self.body = None

    def on_mount(self):
        self.paint()

    def paint(self):
        accent = theme.css(theme.project_color(self.project))
        self.styles.border = ("heavy" if self.has_focus else "round", accent)
        self.styles.border_title_color = accent

    def on_focus(self):
        self.paint()

    def on_blur(self):
        self.paint()

    def show(self, item):
        body = R.card_text(item)
        self.search = f"{item['name']} {body.plain}".lower()
        if not same(self.body, body):
            self.body = body
            self.update(body)

    def action_open(self):
        self.post_message(self.Open(self.root))

    def on_click(self):
        self.action_open()


def resource_text(state, cpu):
    """The meters of the Host panel: CPU, load, memory and swap."""
    line = Text()

    def meter(label, fraction, value, detail=""):
        line.append(f"{label:<5} ", style="bold")
        line.append_text(charts.meter(fraction, 14))
        line.append(f" {value:>7}")
        if detail:
            line.append(f"  {detail}", style="dim")
        line.append("\n")

    total = cpu["total"] if cpu else None
    meter("CPU", (total or 0) / 100, "-" if total is None else f"{total:.1f}%", f"{state['cpus']} threads")
    load = state["load"]
    meter("LOAD", load[0] / state["cpus"] if state["cpus"] else 0, f"{load[0]:.2f}", f"{load[1]:.2f} {load[2]:.2f} 5/15m")
    used = state["memTotal"] - state["memAvailable"]
    meter("MEM", used / state["memTotal"] if state["memTotal"] else 0, top.size(used), f"of {top.size(state['memTotal'])}")
    swap = state["swapTotal"] - state["swapFree"]
    meter("SWAP", swap / state["swapTotal"] if state["swapTotal"] else 0, top.size(swap),
          f"of {top.size(state['swapTotal'])}" if state["swapTotal"] else "no swap")
    line.rstrip()
    return line


def gpu_text(gpus):
    """One compact line per GPU (utilization bar, memory used/total, temperature) under a line naming the models."""
    line = Text()
    names = sorted({gpu["name"] for gpu in gpus})
    line.append(top.cut(f"{len(gpus)} GPU: {', '.join(names)}", 44), style="bold")
    for number, gpu in enumerate(gpus):
        memory = gpu["memUsed"] / gpu["memTotal"] if gpu["memTotal"] else 0
        line.append(f"\n{number:>2} ", style="dim")
        line.append_text(charts.meter(gpu["util"] / 100, 8))
        line.append(f" {gpu['util']:>3.0f}% ")
        line.append(f"{top.size(gpu['memUsed']):>6}/{top.size(gpu['memTotal']):<6}", style=theme.level(memory))
        line.append(f" {gpu['temp']:>3.0f}C", style=theme.level(gpu["temp"] / 100))
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
        self.records = {}

    def compose(self) -> ComposeResult:
        with Panel(f"HOST {socket.gethostname()}", id="resources"):
            with Horizontal(id="resource-row"):
                yield Static(id="meters")
                yield HistoryChart(self.history, self.seconds, id="cpu-history")
                yield Static(id="gpus")
            yield Static(id="cores")
        with Panel("ACTIVITY all projects, events per 15 minutes over the last 12 hours", id="host-activity",
                   classes="grow"):
            yield BarChart(id="host-chart", classes="chart")
        with Panel("PROJECTS Enter or click opens the tab", id="projects"):
            yield ItemGrid(id="cards", min_column_width=44, regular=False)
        with Panel("POOLS", id="pools-panel"):
            yield Table(R.POOL_COLUMNS, id="pools")
        with Panel("GATEWAY health per pool, last 24 hours", id="gateway-panel"):
            yield Table(R.GATEWAY_COLUMNS, id="gateway")
        with Panel("EVENTS across projects", id="events-panel", classes="grow"):
            yield Table(R.HOST_EVENT_COLUMNS, sort=("time", True), id="events")

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
        gpus = state.get("gpus") or []
        gpu_panel = self.query_one("#gpus", Static)
        gpu_panel.display = bool(gpus)
        if gpus:
            gpu_panel.update(gpu_text(gpus))
        self.query_one("#cpu-history").styles.height = max(6, len(meters.plain.splitlines()), len(gpus) + 1)
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
        fill(self.query_one("#pools", Table), R.matches(R.pool_rows(snap["pools"]), text))
        fill(self.query_one("#gateway", Table), R.matches(R.gateway_rows(snap.get("gateway")), text))
        events = self.query_one("#events", Table)
        self.records = {R.event_key(record, index): record for index, record in enumerate(snap["events"])}
        fill(events, R.matches(R.event_rows(snap["events"], project=True, sort=events.sort_by), text))


class ProjectView(VerticalScroll):
    def __init__(self, root, name, **kwargs):
        super().__init__(**kwargs)
        self.root_path = root
        self.project = name
        self.panes = {}
        self.records = {}
        self.item = None

    @property
    def accent(self):
        return theme.project_color(self.project)

    def compose(self) -> ComposeResult:
        accent = self.accent
        with Panel("PROJECT", id="status-panel", accent=accent):
            yield Static(id="status")
        with Panel("STATUS", id="attention-panel", accent=accent):
            yield Static(id="attention")
        with Panel("AGENTS Enter or double-click: details", id="agents-panel", accent=accent, classes="grow"):
            yield Table(R.AGENT_COLUMNS, id="agents")
        with Panel("WAITING", id="waiting-panel", accent=accent, classes="grow"):
            yield Table(R.WAITING_COLUMNS, id="waiting")
        with Panel("ACTIVITY events per 15 minutes over the last 12 hours", id="activity-panel", accent=accent,
                   classes="grow"):
            yield BarChart(id="chart", classes="chart")
        with Panel("EVENTS", id="events-panel", accent=accent, classes="grow"):
            yield Table(R.EVENT_COLUMNS, sort=("time", True), id="events")

    def on_mount(self):
        self.query_one("#agents-panel", Panel).border_subtitle = R.USAGE_LEGEND

    def repaint(self):
        for panel in self.query(Panel):
            panel.accent = self.accent
            panel.paint()

    def update(self, item, text):
        self.item = item
        status = self.query_one("#status", Static)
        agents, waiting, events = (self.query_one(f"#{name}", Table) for name in ("agents", "waiting", "events"))
        status_panel = self.query_one("#status-panel", Panel)
        status_panel.border_title = Content.styled(item["name"], "bold")
        if not item["available"]:
            status.update(Text(f"unavailable: {item['error']}", style=theme.RED))
            for widget in (agents, waiting, events):
                fill(widget, [])
            return
        state = item["snapshot"]
        status_panel.border_subtitle = item["root"]
        status.update(R.project_status(state))
        found = top.issues(state)
        attention = self.query_one("#attention-panel", Panel)
        attention.border_title = f"ACTION NEEDED {len(found)}" if found else "STATUS"
        attention.styles.border_title_color = (self.app.theme_variables.get("warning", "yellow") if found
                                               else theme.css(self.accent))
        self.query_one("#attention", Static).update(R.attention(state))
        queue = state["queue"]
        self.query_one("#waiting-panel", Panel).border_title = (
            f"WAITING review {len(queue['review'])} · merged {len(queue['merged'])} · ready {len(queue['ready'])}"
            f" · blocked {len(queue['blocked'])} · held {len(queue['held'])}")
        self.panes = {f"agent:{agent['name']}:{agent['pane']}": agent for agent in state["agents"]}
        self.records = {R.event_key(record, index): record for index, record in enumerate(state["events"])}
        fill(agents, R.matches(R.agent_rows(state), text))
        fill(waiting, R.matches(R.waiting_rows(state), text))
        fill(events, R.matches(R.event_rows(state["events"], sort=events.sort_by), text))
        self.query_one("#chart", BarChart).plot(charts.kind_series(item["activity"]))


class TopApp(App):
    TITLE = "theseus top"
    AUTO_FOCUS = None
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (120, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (32, "-tall")]
    CSS = """
    TabbedContent, TabbedContent > ContentSwitcher, TabPane, HostView, ProjectView { height: 1fr; }
    TabPane { padding: 0; }
    Panel { border: round $primary 60%; border-title-color: $primary; border-title-style: bold;
            border-subtitle-color: $text-muted; height: auto; padding: 0 1; }
    Panel.grow { height: 1fr; min-height: 3; }
    Panel.grow > DataTable, Panel.grow > .chart { height: 1fr; max-height: 100%; }
    #status-panel { max-height: 25%; overflow-y: auto; }
    #attention-panel, #pools-panel, #gateway-panel, #projects { max-height: 20%; overflow-y: auto; }
    #resources { max-height: 30%; overflow-y: auto; }
    DataTable { height: auto; }
    VerticalScroll, DataTable, Panel { scrollbar-size-vertical: 1; }
    DataTable > .datatable--header { background: $panel; color: auto; }
    DataTable:ansi > .datatable--header { background: ansi_default; color: ansi_default; text-style: bold underline; }
    #filter { dock: bottom; display: none; }
    #resource-row { height: auto; }
    #meters { width: 52; }
    #cpu-history { width: 1fr; min-width: 20; }
    #gpus { width: 46; padding-left: 1; }
    Screen.-narrow #cpu-history { display: none; }
    Screen.-narrow #resource-row { layout: vertical; }
    Screen.-short #activity-panel, Screen.-short #host-activity { display: none; }
    #cores { display: none; }
    #cards { grid-gutter: 0 1; }
    ProjectCard { border: round $primary; border-title-style: bold; height: auto; padding: 0 1; }
    ProjectCard:focus { background: $boost; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark,f1", "help", "Help", key_display="?", tooltip="Keys and mouse help"),
        Binding("tab", "next_tab", "Tabs", key_display="Tab", priority=True,
                tooltip="Next tab (1-9 jump to a tab)"),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("r", "refresh", "Refresh", tooltip="Refresh now"),
        Binding("p", "pause", "Pause", tooltip="Pause or resume this project's queue"),
        Binding("c", "cores", "Cores", tooltip="Per-core CPU on the Host tab"),
        Binding("/", "filter", "Filter", key_display="/", tooltip="Filter rows"),
        Binding("escape", "clear", "Clear the filter and the row highlight", show=False),
        Binding("a", "focus_table('agents')", "Highlight a row in AGENTS", show=False),
        Binding("w", "focus_table('waiting')", "Highlight a row in WAITING", show=False),
        Binding("e", "focus_table('events')", "Highlight a row in EVENTS", show=False),
        Binding("s", "sort_next", "Sort EVENTS by the next column", show=False),
        Binding("S", "sort_reverse", "Reverse the EVENTS sort order", show=False),
        *[Binding(str(n), f"goto({n})", f"Tab {n}", show=False) for n in range(1, 10)],
    ]

    def __init__(self, interval=5.0, current=None, roots=(), snapshot_fn=None, jump_fn=None, loader=None, sampler=None,
                 capture_fn=None, send_fn=None, keys_fn=None, tmux_ok=None, mirror_seconds=1.0):
        super().__init__()
        self.interval = interval
        self.current = str(Path(current).resolve()) if current else None
        self.snapshot_fn = snapshot_fn or (lambda: host.snapshot(list(roots)))
        self.jump_fn = jump_fn or top.jump_to
        self.loader = loader or (lambda root: cfg.load(root=root))
        self.sampler = sampler or resources.CpuSampler()
        self.capture_fn = capture_fn or (lambda pane: top.capture(pane, popups.MIRROR_LINES))
        self.send_fn = send_fn or send_verified
        self.keys_fn = keys_fn or top.send_keys
        self.tmux_ok = (bool(os.environ.get("TMUX")) and shutil.which("tmux") is not None) if tmux_ok is None else tmux_ok
        self.mirror_seconds = mirror_seconds
        self.history = resources.Ring(HISTORY_SAMPLES)
        self.cpu = None
        self.snap = None
        self.filter_text = ""
        self.busy = False
        self.pending_open = self.current
        self.greet = True
        self.views = {}
        self.items = {}

    @property
    def base(self):
        """The dashboard screen, whatever popup is on top of it."""
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        with TabbedContent(initial=HOST_TAB):
            with Pane("Host", id=HOST_TAB):
                yield HostView(self.history, self.interval, id="host-view")
        yield Input(placeholder="filter: task, agent, kind, room, status (Esc clears)", id="filter")
        yield Footer()

    def on_mount(self):
        self.restyle()
        self.theme_changed_signal.subscribe(self, lambda _: self.restyle())
        self.refresh_now()
        self.set_interval(self.interval, self.refresh_now)

    def restyle(self):
        """Recompute the dashboard styles for the current theme and terminal, then repaint accents and rows."""
        current = self.current_theme
        variables = {**current.to_color_system().generate(), **current.variables}
        theme.apply(variables, current.dark, current.ansi, self.console.color_system)
        for card in self.base.query(ProjectCard):
            card.paint()
        for identifier, view in self.views.items():
            view.repaint()
            try:
                tab = self.base.query_one(TabbedContent).get_tab(identifier)
                tab.label = Content.styled(view.project, f"bold {theme.css(view.accent)}")
            except Exception:
                pass
        self.redraw()

    def check_action(self, action, parameters):
        if len(self.screen_stack) > 1 and action not in ("quit",):
            return False
        return True

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
        tabs = self.base.query_one(TabbedContent)
        self.items = {pane_id(item["root"]): item for item in snap["projects"]}
        for identifier in [i for i in self.views if i not in self.items]:
            await tabs.remove_pane(identifier)
            del self.views[identifier]
        for item in snap["projects"]:
            identifier = pane_id(item["root"])
            if identifier not in self.views:
                self.views[identifier] = ProjectView(item["root"], item["name"])
                label = Content.styled(item["name"], f"bold {theme.css(theme.project_color(item['name']))}")
                await tabs.add_pane(Pane(label, self.views[identifier], id=identifier))
        if self.pending_open and pane_id(self.pending_open) in self.views:
            tabs.active = pane_id(self.pending_open)
        self.pending_open = None
        await self.base.query_one(HostView).sync_cards(snap["projects"])
        self.title = f"theseus top · {len(snap['projects'])} project(s) · updated {top.local_clock(snap['time'])}"
        self.redraw()
        if self.greet and snap["projects"]:
            self.greet = False
            self.call_after_refresh(self.focus_first_card)

    def redraw(self):
        if self.snap is None:
            return
        view = self.base.query_one(HostView)
        view.cpu = self.cpu
        view.update(self.snap, self.filter_text)
        for identifier, view in self.views.items():
            if identifier in self.items:
                view.update(self.items[identifier], self.filter_text)

    def tab_ids(self):
        return [pane.id for pane in self.base.query_one(TabbedContent).query(TabPane)]

    def action_goto(self, number):
        ids = self.tab_ids()
        if number <= len(ids):
            self.base.query_one(TabbedContent).active = ids[number - 1]

    def action_next_tab(self):
        self.shift_tab(1)

    def action_previous_tab(self):
        self.shift_tab(-1)

    def shift_tab(self, step):
        tabs = self.base.query_one(TabbedContent)
        ids = self.tab_ids()
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    def action_refresh(self):
        self.refresh_now()

    def action_cores(self):
        view = self.base.query_one(HostView)
        view.cores = not view.cores
        self.redraw()

    def action_filter(self):
        box = self.base.query_one("#filter", Input)
        box.styles.display = "block"
        box.focus()

    def action_clear(self):
        box = self.base.query_one("#filter", Input)
        if box.value or box.display:
            box.value = ""
            box.styles.display = "none"
            self.filter_text = ""
            self.redraw()
        self.set_focus(None)

    def action_focus_table(self, name):
        view = self.active_view()
        if view is not None and view.query(f"#{name}"):
            table = view.query_one(f"#{name}", Table)
            table.focus()
            table.reveal()

    def events_table(self):
        view = self.active_view()
        return view.query_one("#events", Table) if view is not None else None

    def action_sort_next(self):
        table = self.events_table()
        if table is not None:
            names = table.names
            table.set_sort(names[(names.index(table.sort_by[0]) + 1) % len(names)])
            self.redraw()

    def action_sort_reverse(self):
        table = self.events_table()
        if table is not None:
            table.set_sort(table.sort_by[0], not table.sort_by[1])
            self.redraw()

    @on(DataTable.HeaderSelected)
    def header_selected(self, event):
        table = event.data_table
        if isinstance(table, Table) and table.sort_by is not None:
            table.set_sort(event.column_key.value)
            self.redraw()

    def active_view(self):
        active = self.base.query_one(TabbedContent).active
        return self.views.get(active) or (self.base.query_one(HostView) if active == HOST_TAB else None)

    def active_item(self):
        return self.items.get(self.base.query_one(TabbedContent).active)

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
            self.call_after_refresh(lambda: view.query_one("#agents", Table).focus())

    def focus_first_card(self):
        view = self.base.query_one(HostView)
        if self.base.query_one(TabbedContent).active != HOST_TAB:
            return
        cards = view.cards()
        if cards:
            cards[0].focus()

    @on(ProjectCard.Open)
    def open_project(self, event):
        self.base.query_one(TabbedContent).active = pane_id(event.root)

    def action_pause(self):
        item = self.active_item()
        if item is None or not item["available"]:
            self.notify("select a project tab to pause or resume its queue")
            return
        self.run_worker(lambda: self.toggle_pause(item), thread=True, group="pause")

    def toggle_pause(self, item):
        try:
            conf = self.loader(item["root"])
            message = wq.set_paused(conf, not wq.paused(conf))
            paused = wq.paused(conf)
        except Exception as problem:
            self.call_from_thread(self.notify, f"pause failed: {problem}", severity="error")
            return
        self.call_from_thread(self.paused_changed, item, message, paused)

    def paused_changed(self, item, message, paused):
        self.notify(message)
        item["snapshot"]["paused"] = paused
        self.redraw()
        self.refresh_now()

    def action_help(self):
        self.push_screen(popups.HelpScreen("theseus top: keys and mouse", self.help_body()))

    def help_body(self):
        display = self.get_key_display
        sections = [("Keys", popups.binding_rows(self.BINDINGS, lambda key: display(Binding(key, "")))),
                    ("Tables", list(TABLE_HELP)),
                    ("Mouse", list(MOUSE_HELP)),
                    ("Agent popup", popups.binding_rows(popups.AgentScreen.BINDINGS,
                                                        lambda key: display(Binding(key, "")))
                     + [("buttons", "Enter, Escape, Interrupt (asks first), /compact, Open tmux pane")])]
        legend = Text()
        legend.append("Columns\n", style="bold underline")
        legend.append(f"  {R.CPU_HELP}\n  {R.MEM_HELP}\n\n")
        legend.append("Activity colors\n", style="bold underline")
        for name, label in theme.SERIES_LABEL.items():
            legend.append("  ■ ", style=theme.SERIES[name])
            legend.append(f"{label}\n")
        legend.append("\ntmux\n", style="bold underline")
        legend.append("  tmux is only needed to open an agent's pane (and to run interactive agents). After a jump, the\n"
                      "  tmux key shown in the status line or `theseus top --back` returns to this dashboard.\n")
        return popups.help_text(sections, legend)

    @on(DataTable.RowSelected)
    def row_selected(self, event):
        if len(self.screen_stack) > 1:
            return
        key = event.row_key.value or ""
        view = self.active_view()
        name = event.data_table.id
        if name == "agents" and isinstance(view, ProjectView):
            self.open_agent(view, key)
        elif name == "waiting" and isinstance(view, ProjectView):
            self.open_task(view, key)
        elif name == "events":
            self.open_event(view, key)

    def project_tasks(self, project):
        for item in self.items.values():
            if item["name"] == project and item.get("snapshot"):
                return item["snapshot"].get("tasks") or {}
        return {}

    def open_task(self, view, key):
        _, label, task_id = key.split(":", 2)
        state = view.item["snapshot"]
        entry = next((e for e in top.waiting_entries(state) if e["label"] == label and e["task"] == task_id), None)
        task = (state.get("tasks") or {}).get(task_id)
        self.push_screen(popups.Dialog(f"task {task_id}", R.task_text(task, entry, state["project"])))

    def open_event(self, view, key):
        record = view.records.get(key)
        if record is None:
            return
        project = record.get("project") or (view.project if isinstance(view, ProjectView) else None)
        task = self.project_tasks(project).get(record.get("task")) if record.get("task") else None
        self.push_screen(popups.Dialog(f"event {record.get('kind')}", R.event_text({**record, "project": project}, task)))

    def open_agent(self, view, key):
        if key.startswith("worker:"):
            _, name, task_id = key.split(":", 2)
            task = (view.item["snapshot"].get("tasks") or {}).get(task_id)
            self.push_screen(popups.Dialog(f"worker {name}", R.task_text(task, project=view.project)))
            return
        agent = view.panes.get(key)
        if agent is None:
            return
        state = view.item["snapshot"]
        task = (state.get("tasks") or {}).get(agent.get("task"))
        body = R.agent_text(agent, task, (state.get("host") or {}).get("memTotal") or 0)
        root, pane = view.root_path, agent["pane"]
        actions = SimpleNamespace(
            capture=self.capture_fn,
            send=lambda text: self.send_fn(self.loader(root), agent, text, expect_busy=not text.startswith("/")),
            keys=lambda *keys: self.keys_fn(pane, *keys) or f"sent {' '.join(keys)} to {pane}",
            jump=(lambda: self.jump_fn(pane)) if self.tmux_ok and pane else None)
        self.push_screen(popups.AgentScreen(f"agent {agent['name']}", body, pane, actions, poll=self.mirror_seconds))


def send_verified(conf, agent, text, expect_busy=True):
    """Type text into the agent's pane through the queue's verified send; returns the message to show."""
    from .. import agents
    ok, detail = agents.send_verified(conf, agent, text, expect_busy=expect_busy)
    return f"{'sent' if ok else 'send failed'} to {agent['name']}: {detail}"


def run(interval=5.0, current=None, roots=()):
    TopApp(interval=interval, current=current, roots=roots).run()
    return 0
