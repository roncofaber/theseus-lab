"""Rich styles derived from the ANSI color maps of theseus.top, so the plain and the interactive dashboard agree."""
from .. import top

ANSI_STYLE = {top.R: "", top.B: "bold", top.DIM: "dim", top.RED: "red", top.GREEN: "green", top.YELLOW: "yellow",
              top.BLUE: "blue", top.MAGENTA: "magenta", top.CYAN: "cyan"}


def convert(colors):
    return {key: ANSI_STYLE[value] for key, value in colors.items()}


STATE = convert(top.STATE_COLOR)
KIND = convert(top.KIND_COLOR)
QUEUE = convert(top.QUEUE_COLOR)
ROOM = convert(top.ROOM_COLOR)
REASON = convert(top.REASON_COLOR)


def aged(seconds, warn, alarm):
    if seconds is None:
        return ""
    return "red" if seconds >= alarm else "yellow" if seconds >= warn else ""
