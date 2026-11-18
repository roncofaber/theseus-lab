"""Modal screens of the dashboard: the key and mouse help, row details, a yes/no confirmation and the agent popup
with its live pane mirror. A click on the dimmed background outside a popup closes it."""
import io
import queue
import threading

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TabbedContent, TabPane

from . import commands, theme

MIRROR_LINES = 200
DIFF_SECONDS = 5.0
ESCAPE_SECONDS = 0.5
FIT_SECONDS = 0.2
CONVERSATION_ENTRIES = 300
TMUX_KEYS = {"enter": "Enter", "backspace": "BSpace", "delete": "DC", "up": "Up", "down": "Down", "left": "Left",
             "right": "Right", "tab": "Tab", "shift+tab": "BTab", "home": "Home", "end": "End", "pageup": "PPage",
             "pagedown": "NPage", "insert": "IC"}


def plain(renderable, width=120):
    """The text a Rich renderable shows at width, without styles."""
    console = Console(width=width, file=io.StringIO(), color_system=None, record=True)
    console.print(renderable)
    return console.export_text()


class Outside:
    """A click on the screen itself (the dimmed background), not on the popup, closes it."""

    def on_click(self, event):
        if self.get_widget_at(event.screen_x, event.screen_y)[0] is self:
            event.stop()
            self.outside()


class Dialog(Outside, ModalScreen):
    """A centered, bordered popup: a scrollable body and a row of one-line buttons; Escape, Close or a click outside
    dismisses it."""

    BINDINGS = [Binding("escape", "close", "Close the popup")]
    DEFAULT_CSS = """
    Dialog { align: center middle; }
    Dialog > #dialog { width: 90%; height: auto; max-height: 90%; border: round $primary;
                       border-title-style: bold; background: $surface; padding: 0 1; }
    Dialog #body { height: auto; max-height: 1fr; }
    Dialog #buttons { height: 1; margin-top: 1; }
    Dialog Button { margin-right: 1; min-width: 6; }
    """

    def __init__(self, title, body):
        super().__init__()
        self.heading = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield from self.content()
            with Horizontal(id="buttons"):
                yield from self.buttons()
                yield Button("Close", id="close", compact=True)

    def content(self):
        yield VerticalScroll(Static(self.body, id="text"), id="body")

    def buttons(self):
        return []

    def text(self, width=120):
        return plain(self.body, width)

    def on_mount(self):
        self.query_one("#dialog").border_title = self.heading

    def outside(self):
        self.action_close()

    def action_close(self):
        self.dismiss()

    @on(Button.Pressed, "#close")
    def close_pressed(self):
        self.dismiss()


class HelpScreen(Dialog):
    BINDINGS = [Binding("escape,question_mark,f1", "close", "Close the popup")]


class Confirm(Outside, ModalScreen[bool]):
    """Yes or no; y/Enter confirms, n/Escape or a click outside declines."""

    BINDINGS = [Binding("y,enter", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 64; height: auto; border: round $error; background: $surface; padding: 0 1; }
    Confirm Horizontal { height: 1; margin-top: 1; }
    Confirm Button { margin-right: 1; }
    """

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question)
            with Horizontal():
                yield Button("Yes", id="yes", variant="error", compact=True)
                yield Button("No", id="no", compact=True)

    def outside(self):
        self.dismiss(False)

    def action_answer(self, answer):
        self.dismiss(answer)

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "yes")


class Contained:
    """The mouse wheel over this widget scrolls only it: at its ends, or with nothing to scroll, the wheel event stops
    here instead of moving the popup or the screen behind it."""

    def _on_mouse_scroll_down(self, event):
        super()._on_mouse_scroll_down(event)
        event.stop()

    def _on_mouse_scroll_up(self, event):
        super()._on_mouse_scroll_up(event)
        event.stop()

    def _on_mouse_scroll_left(self, event):
        super()._on_mouse_scroll_left(event)
        event.stop()

    def _on_mouse_scroll_right(self, event):
        super()._on_mouse_scroll_right(event)
        event.stop()


class Conversation(Contained, VerticalScroll):
    """The agent's conversation from its session transcript, oldest first, following new entries at the bottom."""


class Fold(Static, can_focus=True):
    """One line that a click, Enter or Space expands to the full text and folds again."""

    BINDINGS = [Binding("enter,space", "toggle", "Expand or fold", show=False)]

    def __init__(self, summary, detail, **kwargs):
        super().__init__(summary, **kwargs)
        self.summary, self.detail, self.expanded = summary, detail, False

    def action_toggle(self):
        self.expanded = not self.expanded
        self.set_class(self.expanded, "-open")
        self.update(self.detail if self.expanded else self.summary)

    def on_click(self, event):
        event.stop()
        self.action_toggle()


def conversation_widget(entry):
    """The widget of one conversation entry: user and assistant text in full (Markdown when the entry says so), a tool
    call (`Tool: summary`) or result (its summary line) as a Fold that unfolds to the entry's text."""
    text = entry.get("text") or ""
    if entry["kind"] == "history":
        return Static(Text.from_ansi(text.rstrip("\n")), classes="turn history")
    if entry["kind"] in ("user", "assistant"):
        body = Markdown(text) if entry.get("format") == "markdown" else Text(text)
        return Static(body, classes=f"turn {entry['kind']}")
    lines = text.splitlines() or [""]
    summary = " ".join(str(entry.get("summary") or "").split())
    if entry["kind"] == "tool":
        head = f"▸ {entry.get('name') or 'tool'}: {summary or ' '.join(text.split())}"
        style = "dim"
    else:
        head = (f"  ↳ {'error: ' if entry.get('error') else ''}{summary or lines[0].strip()}"
                + (f"  ({len(lines)} lines)" if len(lines) > 1 else ""))
        style = "red" if entry.get("error") else "dim"
    return Fold(Text(head, style=style, no_wrap=True, overflow="ellipsis"), Text(text, style=style),
                classes=f"fold {entry['kind']}")


class Mirror(Contained, ScrollableContainer, can_focus=True):
    """The pane mirror. In type-through mode it forwards every key to the pane instead of the dashboard's bindings."""

    def on_key(self, event):
        screen = self.screen
        if isinstance(screen, AgentScreen) and screen.typing:
            event.stop()
            event.prevent_default()
            screen.typed(event)

    def on_resize(self, event):
        if isinstance(self.screen, AgentScreen):
            self.screen.mirror_resized()

    def visible_size(self):
        """Columns and rows of text the mirror shows: inside its border, less the vertical scrollbar but not the
        horizontal one, which goes away once the pane fits."""
        region = self.region.shrink(self.styles.gutter)
        return region.width - self.styles.scrollbar_size_vertical, region.height


class AgentScreen(Dialog):
    """An agent's details over tabs: Live (a mirror of its tmux pane, polled while shown, faster while typing through,
    with a line to send whole lines, type-through mode for single keys and buttons for common keys), Conversation (its
    session transcript, read while shown), Diff (its task worktree against the task's base, refreshed while shown),
    Task and Events. `actions` supplies capture(pane), send(text), keys(*keys), type(*keys), jump() (None hides Open
    tmux pane) and optionally conversation(after) ((total, entries after the first `after`, reset) or None, entries
    already redacted, in the form of theseus.adapters plus a "history" kind for plain scrollback; None hides the
    tab), diff(), task(), events(),
    attach() (a callable run while the app is suspended, None for panes it cannot attach), compact(), restart(), stop(),
    start() (None hides them; each asks first) and fit(columns, rows) (None for panes it must not resize), which sizes
    the agent's window to the mirror while it is shown, follows the mirror's size fit_seconds after it changes and on
    every poll, and fit(None, None) restores the pinned size on close. Everything but attach and that restore runs in
    a worker thread. The buttons are Attach, Type, Interrupt and Close; the rest are keys and palette commands."""

    COMMANDS = {commands.AgentCommands}

    BINDINGS = [Binding("escape", "close", "Close the popup"),
                Binding("i", "type_through", "Type into the pane (Esc Esc or Ctrl-] leaves)"),
                Binding("enter", "attach", "Attach to the pane here (C-q detaches)"),
                Binding("o", "open_pane", "Open the pane in a new window of your own tmux"),
                Binding("s", "focus_send", "Type a line to send"),
                Binding("E", "send_key('Escape')", "Send Escape to the pane"),
                Binding("K", "compact", "Compact the agent's context (theseus instance compact)"),
                Binding("R", "lifecycle('restart')", "Restart the agent (asks first)"),
                Binding("X", "lifecycle('stop')", "Stop the agent (asks first)"),
                Binding("U", "lifecycle('start')", "Start the agent (asks first)"),
                Binding("l", "show_tab('live')", "Live tab"),
                Binding("c", "show_tab('conversation')", "Conversation tab"), Binding("d", "show_tab('diff')", "Diff tab"),
                Binding("t", "show_tab('task')", "Task tab"), Binding("v", "show_tab('events')", "Events tab")]
    DEFAULT_CSS = """
    AgentScreen > #dialog { height: 90%; }
    AgentScreen #body { height: auto; max-height: 30%; }
    AgentScreen #tabs { height: 1fr; }
    AgentScreen TabPane { padding: 0; }
    AgentScreen #note { color: $warning; text-style: bold; }
    AgentScreen #mirror { height: 1fr; min-height: 4; border: round $primary 50%; background: $background;
                          overflow-x: auto; overflow-y: auto; }
    AgentScreen .sheet { height: 1fr; overflow-x: auto; overflow-y: auto; }
    AgentScreen .sheet > Static { width: auto; }
    AgentScreen #mirror-text { width: auto; }
    AgentScreen.-typing #mirror { border: heavy $warning; border-title-color: $warning; border-title-style: bold; }
    AgentScreen #conversation { height: 1fr; }
    AgentScreen #conversation .turn { margin-top: 1; padding: 0 1; }
    AgentScreen #conversation .user { border-left: thick $accent; }
    AgentScreen #conversation .fold { padding: 0 1; }
    AgentScreen #conversation .fold:focus { background: $boost; }
    """

    def __init__(self, title, body, pane, actions, poll=1.0, fast=0.25, fit_seconds=FIT_SECONDS):
        super().__init__(title, body)
        self.pane = pane
        self.actions = actions
        self.poll_seconds = poll
        self.fast_seconds = min(fast, poll)
        self.timer = None
        self.busy = False
        self.captures = 0
        self.typing = False
        self.escape_timer = None
        self.outbox = queue.Queue()
        self.pumping = False
        self.lock = threading.Lock()
        self.diff_timer = None
        self.fit_seconds = fit_seconds
        self.fit_timer = None
        self.fit_size = None
        self.fitted = False
        self.fit_lock = threading.Lock()
        self.conversation_timer = None
        self.conversation_total = 0
        self.conversation_busy = False

    def live_title(self):
        return f"{self.heading.split(' ', 1)[-1]} · live"

    def action_of(self, name):
        return getattr(self.actions, name, None)

    def content(self):
        yield VerticalScroll(Static(self.body, id="text"), id="body")
        with TabbedContent(id="tabs", initial="live"):
            with TabPane("Live", id="live"):
                yield Mirror(Static(Text("…", style="dim"), id="mirror-text"), id="mirror")
                yield Input(placeholder="send", id="send")
            if self.action_of("conversation") is not None:
                with TabPane("Conversation", id="conversation-tab"):
                    yield Conversation(Static(Text("…", style="dim"), id="conversation-note"), id="conversation")
            for name, label in (("diff", "Diff"), ("task", "Task"), ("events", "Events")):
                with TabPane(label, id=name):
                    yield ScrollableContainer(Static(Text("…", style="dim"), id=f"{name}-text"), classes="sheet")

    def buttons(self):
        if self.action_of("attach") is not None:
            yield Button("Attach (Enter)", id="attach", variant="primary", compact=True)
        yield Button("Type (i)", id="type", variant="warning", compact=True)
        yield Button("Interrupt", id="interrupt", variant="error", compact=True)

    def palette(self):
        """(title, help, callback) of the popup's commands that have no button, for the command palette."""
        found = [("Send Enter", "press Enter in the pane", lambda: self.action_send_key("Enter")),
                 ("Send Escape (E)", "press Escape in the pane", lambda: self.action_send_key("Escape"))]
        if self.action_of("compact") is not None:
            found.append(("Compact (K)", "theseus instance compact: the profile's compact command", self.action_compact))
        if self.actions.jump is not None:
            found.append(("Open in my tmux (o)", "the pane in a new window of your own tmux", self.action_open_pane))
        for name, key in (("restart", "R"), ("stop", "X"), ("start", "U")):
            if self.action_of(name) is not None:
                found.append((f"{name.capitalize()} ({key})", f"theseus agents {name}; asks first",
                              lambda name=name: self.action_lifecycle(name)))
        shown = self.heading.split(" ", 1)[-1]
        return [(f"{shown}: {title}", help_text, callback) for title, help_text, callback in found]

    def on_mount(self):
        super().on_mount()
        self.query_one("#mirror").border_title = self.live_title()
        self.timer = self.set_interval(self.poll_seconds, self.poll)
        self.poll()
        for name in ("task", "events"):
            self.load_sheet(name)

    def load_sheet(self, name):
        source = self.action_of(name)
        if source is None:
            self.fill_sheet(name, Text("-", style="dim"))
            return

        def run():
            try:
                content = source()
            except Exception as problem:
                content = Text(f"failed: {problem}", style="red")
            try:
                self.app.call_from_thread(self.fill_sheet, name, content)
            except Exception:
                pass
        self.run_worker(run, thread=True, group=f"sheet-{name}", exclusive=True)

    def fill_sheet(self, name, content):
        try:
            self.query_one(f"#{name}-text", Static).update(content if content else Text("-", style="dim"))
        except NoMatches:
            pass

    @on(TabbedContent.TabActivated, "#tabs")
    def tab_activated(self, event):
        active = event.pane.id
        for timer in (self.diff_timer, self.conversation_timer):
            if timer is not None:
                timer.stop()
        self.diff_timer = self.conversation_timer = None
        if active == "conversation-tab":
            self.load_conversation()
            self.conversation_timer = self.set_interval(self.poll_seconds, self.load_conversation)
        elif active == "diff":
            self.load_sheet("diff")
            self.diff_timer = self.set_interval(DIFF_SECONDS, lambda: self.load_sheet("diff"))
        elif active in ("task", "events"):
            self.load_sheet(active)

    def action_show_tab(self, name):
        if name == "conversation" and self.action_of("conversation") is None:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        if self.focused is not None and tabs in self.focused.ancestors:
            self.set_focus(None)
        tabs.active = "conversation-tab" if name == "conversation" else name

    def load_conversation(self):
        """Read what the transcript gained since the last read in a worker, then append it to the Conversation tab."""
        source = self.action_of("conversation")
        if source is None:
            self.show_conversation(None, [], False, "-")
            return
        if self.conversation_busy:
            return
        self.conversation_busy = True
        after = self.conversation_total

        def run():
            try:
                found, note = source(after), None
            except Exception as problem:
                found, note = None, f"failed: {problem}"
            total, entries, reset = found if found else (None, [], False)
            if found is None and note is None:
                note = "no transcript for this agent's session"
            try:
                self.app.call_from_thread(self.show_conversation, total, entries, reset, note)
            except Exception:
                self.conversation_busy = False
        self.run_worker(run, thread=True, group="conversation", exclusive=True)

    def show_conversation(self, total, entries, reset, note=None):
        self.conversation_busy = False
        try:
            log = self.query_one("#conversation", Conversation)
            placeholder = self.query_one("#conversation-note", Static)
        except NoMatches:
            return
        if total is None:
            placeholder.update(Text(note or "-", style="dim"))
            return
        at_bottom = log.scroll_y >= log.max_scroll_y - 1
        if reset:
            log.remove_children(".turn, .fold")
        if entries:
            log.mount_all([conversation_widget(entry) for entry in entries[-CONVERSATION_ENTRIES:]])
        shown = list(log.query(".turn, .fold"))
        for widget in shown[:max(0, len(shown) - CONVERSATION_ENTRIES)]:
            widget.remove()
        placeholder.display = not (total or entries)
        placeholder.update(Text("no conversation yet", style="dim"))
        self.conversation_total = total
        if at_bottom and entries:
            log.call_after_refresh(log.scroll_end, animate=False)

    def action_attach(self):
        attach = self.action_of("attach")
        if attach is None:
            self.app.notify("not supervised: o opens the pane", severity="warning")
            return
        if self.fit_timer is not None:
            self.fit_timer.stop()
            self.fit_timer = None
        with self.app.suspend():
            message = attach()
        self.app.refresh(layout=True)
        self.mirror_resized()
        if message:
            self.app.notify(message)

    @on(Button.Pressed, "#attach")
    def attach_pressed(self):
        self.action_attach()

    QUESTIONS = {"restart": "Restart {name}?", "stop": "Stop {name}?", "start": "Start {name}?"}

    def action_lifecycle(self, name):
        function = self.action_of(name)
        if function is None:
            self.app.notify("not supervised", severity="warning")
            return

        def answered(yes):
            if yes:
                self.in_thread(function)
        self.app.push_screen(Confirm(self.QUESTIONS[name].format(name=self.heading.split(" ", 1)[-1])), answered)

    def press(self, *keys):
        """Queue tmux keys for the pane; one worker sends them in order and ends when the queue is empty."""
        self.outbox.put(keys)
        with self.lock:
            if self.pumping:
                return
            self.pumping = True
        self.run_worker(self.pump, thread=True, group="keys")

    def check_action(self, action, parameters):
        if action == "open_pane" and self.actions.jump is None:
            return False
        if action == "compact" and self.action_of("compact") is None:
            return False
        if self.typing and action in ("attach", "lifecycle", "show_tab", "focus_send", "open_pane", "send_key", "compact"):
            return False
        return True

    def poll(self):
        if self.busy:
            return
        self.busy = True
        self.run_worker(self.grab, thread=True, group="mirror")

    def grab(self):
        self.refit()
        try:
            text = self.actions.capture(self.pane)
        except Exception as problem:
            text = f"capture failed: {problem}"
        try:
            self.app.call_from_thread(self.show, text)
        except Exception:
            self.busy = False

    def show(self, text):
        self.busy = False
        self.captures += 1
        try:
            scroller = self.query_one("#mirror", Mirror)
            mirror = self.query_one("#mirror-text", Static)
        except NoMatches:
            return
        at_bottom = scroller.scroll_y >= scroller.max_scroll_y - 1
        mirror.update(Text.from_ansi(text.rstrip("\n"), no_wrap=True) if text.strip()
                      else Text("-", style="dim"))
        if at_bottom:
            scroller.call_after_refresh(scroller.scroll_end, animate=False, x_axis=False)

    def mirror_resized(self):
        """Follow the mirror's size once it has stopped changing for fit_seconds."""
        if self.action_of("fit") is None:
            return
        if self.fit_timer is not None:
            self.fit_timer.stop()
        self.fit_timer = self.set_timer(self.fit_seconds, self.fit_now)

    def fit_now(self):
        self.fit_timer = None
        try:
            columns, rows = self.query_one("#mirror", Mirror).visible_size()
        except NoMatches:
            return
        if columns < 1 or rows < 1 or not self.is_attached:
            return
        self.fit_size = (columns, rows)
        self.run_worker(self.refit, thread=True, group="fit", exclusive=True)

    def refit(self):
        """Size the pane to the mirror (a no-op when it already fits or a client is attached to it)."""
        fit = self.action_of("fit")
        if fit is None:
            return
        with self.fit_lock:
            if self.fit_size is None:
                return
            try:
                fit(*self.fit_size)
                self.fitted = True
            except Exception:
                pass

    def on_unmount(self):
        if self.fit_timer is not None:
            self.fit_timer.stop()
        fit = self.action_of("fit")
        if fit is None:
            return
        with self.fit_lock:
            self.fit_size = None
            if self.fitted:
                try:
                    fit(None, None)
                except Exception:
                    pass

    def pump(self):
        """Send queued keys in order; consecutive literal text goes out as one send-keys -l."""
        while True:
            with self.lock:
                if self.outbox.empty():
                    self.pumping = False
                    return
                item = self.outbox.get()
            batch = [item]
            while not self.outbox.empty():
                if self.outbox.queue[0][0] != "-l" or batch[-1][0] != "-l":
                    break
                batch.append(self.outbox.get())
            keys = ("-l", "".join(part[1] for part in batch)) if batch[0][0] == "-l" else batch[0]
            try:
                message = self.actions.type(*keys)
            except Exception as problem:
                message = f"failed: {problem}"
            if message:
                self.app.call_from_thread(self.app.notify, message, severity="error")

    def set_typing(self, typing):
        self.typing = typing
        self.set_class(typing, "-typing")
        mirror = self.query_one("#mirror", Mirror)
        mirror.border_title = "INSERT" if typing else self.live_title()
        if self.timer is not None:
            self.timer.stop()
        self.timer = self.set_interval(self.fast_seconds if typing else self.poll_seconds, self.poll)
        if typing:
            mirror.focus()

    def typed(self, event):
        key = event.key
        if key == "escape":
            if self.escape_timer is not None:
                self.escape_timer.stop()
                self.escape_timer = None
                self.set_typing(False)
            else:
                self.escape_timer = self.set_timer(ESCAPE_SECONDS, self.lone_escape)
            return
        if self.escape_timer is not None:
            self.escape_timer.stop()
            self.lone_escape()
        if key in ("ctrl+right_square_bracket", "ctrl+]"):
            self.set_typing(False)
        elif key == "ctrl+c":
            self.interrupt()
        elif key in TMUX_KEYS:
            self.press(TMUX_KEYS[key])
        elif key.startswith("ctrl+") and len(key) == 6:
            self.press(f"C-{key[-1]}")
        elif event.character and event.is_printable:
            self.press("-l", event.character)

    def lone_escape(self):
        self.escape_timer = None
        self.press("Escape")

    def in_thread(self, function, *args):
        def run():
            try:
                message = function(*args)
            except Exception as problem:
                message = f"failed: {problem}"
            if message:
                self.app.call_from_thread(self.app.notify, message)
        self.run_worker(run, thread=True, group="send")

    def action_type_through(self):
        self.set_typing(not self.typing)

    @on(Button.Pressed, "#type")
    def type_pressed(self):
        self.set_typing(not self.typing)

    @on(Input.Submitted, "#send")
    def send_line(self, event):
        text = event.value.strip()
        if text:
            event.input.value = ""
            self.in_thread(self.actions.send, text)

    def action_send_key(self, key):
        self.in_thread(self.actions.keys, key)

    def action_compact(self):
        if self.action_of("compact") is not None:
            self.in_thread(self.actions.compact)

    @on(Button.Pressed, "#interrupt")
    def interrupt(self):
        def answered(yes):
            if yes:
                self.press("C-c")
        self.app.push_screen(Confirm(f"Interrupt {self.heading.split(' ', 1)[-1]} (Ctrl-C)?"),
                             answered)

    def action_open_pane(self):
        if self.actions.jump is not None:
            self.in_thread(self.actions.jump)

    def action_focus_send(self):
        self.query_one("#send", Input).focus()


def binding_rows(bindings, display):
    """(keys, description) for every binding with a description, keys as the app shows them."""
    found = []
    for binding in bindings:
        if binding.description:
            keys = binding.key_display or ", ".join(display(key) for key in binding.key.split(","))
            found.append((keys, binding.tooltip or binding.description))
    return found


def help_text(sections, legend):
    """The help popup body: titled sections of (keys, description) rows, then the legend lines."""
    line = Text()
    for title, entries in sections:
        line.append(f"{title}\n", style="bold underline")
        width = max([len(keys) for keys, _ in entries] + [8])
        for keys, description in entries:
            line.append(f"  {keys:<{width}}  ", style=f"bold {theme.CYAN}")
            line.append(f"{description}\n")
        line.append("\n")
    line.append_text(legend)
    line.rstrip()
    return line
