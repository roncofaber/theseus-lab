"""Rich styles derived from the ANSI color maps of theseus.top, so the plain and the interactive dashboard agree, plus
the stable per-project accent colors and the activity series colors."""
import hashlib

from rich.color import Color

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
SERIES = {"assigned": "green", "delivered": "magenta", "merged": "cyan", "errors": "red"}
SERIES_LABEL = {"assigned": "assigned", "delivered": "delivered/partial/blocked", "merged": "merged/completed",
                "errors": "errors/stalled"}
STRONG_KINDS = {"delivered", "partial", "blocked", "stalled", "error", "completed"}

# xterm-256 indices, so a 256-color terminal shows exactly these and truecolor shows the same values.
PALETTE = (39, 214, 170, 78, 221, 141, 209, 44, 112, 175, 75, 179, 167, 85, 147, 216)


def project_color(name):
    """Stable accent color of a project as #rrggbb, chosen from PALETTE by a hash of its name."""
    index = int(hashlib.sha1(str(name).encode()).hexdigest(), 16) % len(PALETTE)
    return Color.from_ansi(PALETTE[index]).get_truecolor().hex


def aged(seconds, warn, alarm):
    if seconds is None:
        return ""
    return "red" if seconds >= alarm else "yellow" if seconds >= warn else ""


def level(fraction):
    return "red" if fraction > 0.9 else "yellow" if fraction > 0.7 else "green"
