"""Modal screens of the dashboard: the key and mouse help, row details, a yes/no confirmation and the agent popup
with its live pane mirror."""
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from . import theme

MIRROR_LINES = 200


class Dialog(ModalScreen):
    """A centered, bordered popup: a scrollable body and a row of buttons; Escape or Close dismisses it."""

    BINDINGS = [Binding("escape", "close", "Close the popup")]
    DEFAULT_CSS = """
    Dialog { align: center middle; }
    Dialog > #dialog { width: 90%; max-width: 150; height: auto; max-height: 90%; border: round $primary;
                       border-title-style: bold; background: $surface; padding: 0 1; }
    Dialog #body { height: auto; max-height: 1fr; }
    Dialog #buttons { height: auto; margin-top: 1; }
    Dialog Button { margin-right: 1; min-width: 8; }
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
                yield Button("Close", id="close")

    def content(self):
        yield VerticalScroll(Static(self.body, id="text"), id="body")

    def buttons(self):
        return []

    def on_mount(self):
        self.query_one("#dialog").border_title = self.heading

    def action_close(self):
        self.dismiss()

    @on(Button.Pressed, "#close")
    def close_pressed(self):
        self.dismiss()


class HelpScreen(Dialog):
    BINDINGS = [Binding("escape,question_mark,f1", "close", "Close the popup")]


class Confirm(ModalScreen[bool]):
    """Yes or no; y/Enter confirms, n/Escape declines."""

    BINDINGS = [Binding("y,enter", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 60; height: auto; border: round $error; background: $surface; padding: 0 1; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { margin-right: 1; }
    """

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question)
            with Horizontal():
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no")

    def action_answer(self, answer):
        self.dismiss(answer)

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "yes")


class AgentScreen(Dialog):
    """An agent's details over a mirror of its tmux pane, polled while the popup is open, with a line to send text
    and buttons for common keys. `actions` supplies capture(pane), send(text), keys(*keys) and jump() (None hides
    the Open tmux pane button); every one of them runs in a worker thread."""

    BINDINGS = [Binding("escape", "close", "Close the popup"),
                Binding("o", "open_pane", "Open tmux pane"),
                Binding("s", "focus_send", "Type a line to send")]
    DEFAULT_CSS = """
    AgentScreen > #dialog { height: 90%; }
    AgentScreen #body { height: auto; max-height: 35%; }
    AgentScreen #note { color: $warning; text-style: bold; margin-top: 1; }
    AgentScreen #mirror { height: 1fr; min-height: 4; border: round $primary 50%; background: $background; }
    """

    def __init__(self, title, body, pane, actions, poll=1.0):
        super().__init__(title, body)
        self.pane = pane
        self.actions = actions
        self.poll_seconds = poll
        self.timer = None
        self.busy = False
        self.captures = 0

    def content(self):
        yield VerticalScroll(Static(self.body, id="text"), id="body")
        yield Static(f"Live mirror of tmux pane {self.pane}: what you send here goes to the running agent.", id="note")
        yield VerticalScroll(Static(Text("capturing the pane ...", style="dim"), id="mirror-text"), id="mirror")
        yield Input(placeholder="type a line and press Enter to send it to the agent", id="send")

    def buttons(self):
        yield Button("Enter", id="key-enter")
        yield Button("Escape", id="key-escape")
        yield Button("Interrupt", id="interrupt", variant="error")
        yield Button("/compact", id="compact")
        if self.actions.jump is not None:
            yield Button("Open tmux pane", id="open", variant="primary")

    def on_mount(self):
        super().on_mount()
        self.timer = self.set_interval(self.poll_seconds, self.poll)
        self.poll()

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
            scroller = self.query_one("#mirror", VerticalScroll)
            mirror = self.query_one("#mirror-text", Static)
        except NoMatches:
            return
        at_bottom = scroller.scroll_y >= scroller.max_scroll_y - 1
        mirror.update(Text.from_ansi(text.rstrip("\n")) if text.strip() else Text("the pane shows nothing", style="dim"))
        if at_bottom:
            scroller.call_after_refresh(scroller.scroll_end, animate=False)

    def in_thread(self, function, *args):
        def run():
            try:
                message = function(*args)
            except Exception as problem:
                message = f"failed: {problem}"
            if message:
                self.app.call_from_thread(self.app.notify, message)
        self.run_worker(run, thread=True, group="send")

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
                self.in_thread(self.actions.keys, "C-c")
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
