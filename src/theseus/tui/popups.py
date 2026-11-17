"""Modal screens of the dashboard: the key and mouse help, row details, a yes/no confirmation and the agent popup
with its live pane mirror. A click on the dimmed background outside a popup closes it."""
import io
import queue
import threading

from rich.console import Console
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from . import theme

MIRROR_LINES = 200
ESCAPE_SECONDS = 0.5
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


class Mirror(ScrollableContainer, can_focus=True):
    """The pane mirror. In type-through mode it forwards every key to the pane instead of the dashboard's bindings."""

    def on_key(self, event):
        screen = self.screen
        if isinstance(screen, AgentScreen) and screen.typing:
            event.stop()
            event.prevent_default()
            screen.typed(event)


class AgentScreen(Dialog):
    """An agent's details over a live mirror of its tmux pane, polled while the popup is open (faster while typing
    through), with a line to send whole lines, type-through mode for single keys and buttons for common keys.
    `actions` supplies capture(pane), send(text), keys(*keys) for the buttons, type(*keys) for type-through and jump()
    (None hides Open tmux pane); every one of them runs in a worker thread, keys in order through one queue."""

    BINDINGS = [Binding("escape", "close", "Close the popup"),
                Binding("i", "type_through", "Type into the pane (Esc Esc or Ctrl-] leaves)"),
                Binding("o", "open_pane", "Open tmux pane"),
                Binding("s", "focus_send", "Type a line to send")]
    DEFAULT_CSS = """
    AgentScreen > #dialog { height: 90%; }
    AgentScreen #body { height: auto; max-height: 30%; }
    AgentScreen #note { color: $warning; text-style: bold; margin-top: 1; }
    AgentScreen #mirror { height: 1fr; min-height: 4; border: round $primary 50%; background: $background;
                          overflow-x: auto; overflow-y: auto; }
    AgentScreen #mirror-text { width: auto; }
    AgentScreen.-typing #mirror { border: heavy $warning; border-title-color: $warning; border-title-style: bold; }
    """

    def __init__(self, title, body, pane, actions, poll=1.0, fast=0.25):
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

    def content(self):
        yield VerticalScroll(Static(self.body, id="text"), id="body")
        yield Static(f"Live mirror of tmux pane {self.pane}: what you send here goes to the running agent.", id="note")
        yield Mirror(Static(Text("capturing the pane ...", style="dim"), id="mirror-text"), id="mirror")
        yield Input(placeholder="type a line and press Enter to send it to the agent", id="send")

    def buttons(self):
        yield Button("Type into pane (i)", id="type", variant="warning", compact=True)
        yield Button("Enter", id="key-enter", compact=True)
        yield Button("Escape", id="key-escape", compact=True)
        yield Button("Interrupt", id="interrupt", variant="error", compact=True)
        yield Button("/compact", id="compact", compact=True)
        if self.actions.jump is not None:
            yield Button("Open tmux pane (o)", id="open", variant="primary", compact=True)

    def on_mount(self):
        super().on_mount()
        self.query_one("#mirror").border_title = "mirror"
        self.timer = self.set_interval(self.poll_seconds, self.poll)
        self.poll()

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
        return True

    def poll(self):
        if self.busy:
            return
        self.busy = True
        self.run_worker(self.grab, thread=True, group="mirror")

    def grab(self):
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
                      else Text("the pane shows nothing", style="dim"))
        if at_bottom:
            scroller.call_after_refresh(scroller.scroll_end, animate=False, x_axis=False)

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
        mirror.border_title = (f"INSERT  keys go to pane {self.pane}  (Esc Esc or Ctrl-] leaves)" if typing else "mirror")
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

    @on(Button.Pressed, "#key-enter")
    def press_enter(self):
        self.in_thread(self.actions.keys, "Enter")

    @on(Button.Pressed, "#key-escape")
    def press_escape(self):
        self.in_thread(self.actions.keys, "Escape")

    @on(Button.Pressed, "#compact")
    def compact(self):
        self.in_thread(self.actions.send, "/compact")

    @on(Button.Pressed, "#interrupt")
    def interrupt(self):
        def answered(yes):
            if yes:
                self.press("C-c")
        self.app.push_screen(Confirm(f"Send Ctrl-C to the agent in pane {self.pane}? It interrupts its current turn."),
                             answered)

    @on(Button.Pressed, "#open")
    def open_pressed(self):
        self.action_open_pane()

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
