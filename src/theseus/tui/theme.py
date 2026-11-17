"""Rich styles for the dashboard, derived from the active Textual theme: the ANSI color maps of theseus.top translated
to semantic slots, the activity series colors and the stable per-project accent colors.

`apply` recomputes every style when the theme or the terminal changes; the module attributes always hold the current
styles. Three modes:
- an ANSI theme (ansi-dark, ansi-light): native ANSI colors, so the terminal's own palette decides;
- a 16-color terminal with any other theme: the exact colors of Rich's standard palette, so downsampling keeps the hue
  (Textual renders named ANSI colors through its Monokai theme, where red and magenta are the same color);
- otherwise: red, green and yellow are the theme's $error, $success and $warning, and the other hues are computed,
  kept away from the error hue and darkened or lightened until they reach a WCAG contrast of 4.5 on $background."""
import colorsys
import hashlib

from rich.color import STANDARD_PALETTE
from textual.color import Color
from textual.theme import BUILTIN_THEMES

from .. import top

ANSI_NAMES = ("ansi_black", "ansi_red", "ansi_green", "ansi_yellow", "ansi_blue", "ansi_magenta", "ansi_cyan",
              "ansi_white", "ansi_bright_black", "ansi_bright_red", "ansi_bright_green", "ansi_bright_yellow",
              "ansi_bright_blue", "ansi_bright_magenta", "ansi_bright_cyan", "ansi_bright_white")
ANSI_SLOT = {"red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "gold": 3, "neutral": 7}
THEME_SLOT = {"red": "error", "green": "success", "yellow": "warning"}
HUES = {"blue": 220, "magenta": 285, "cyan": 185, "gold": 50, "neutral": 210}
PROJECT_HUES = (130, 285, 195, 50, 225, 165, 255, 90)
PROJECT_ANSI = {True: (10, 13, 14, 11, 12, 2, 6, 5, 3), False: (2, 5, 6, 3, 4)}
MIN_CONTRAST = 4.5
ERROR_GAP = 45

RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = GOLD = NEUTRAL = ""
STATE = KIND = QUEUE = ROOM = REASON = SERIES = {}
PROJECTS = ()
MODE = "rgb"
GENERATION = 0
SERIES_LABEL = {"assigned": "assigned", "delivered": "delivered", "partial": "partial", "blocked": "blocked",
                "merged": "merged/completed", "errors": "errors/stalled"}
STRONG_KINDS = {"delivered", "partial", "blocked", "stalled", "error", "completed"}


def luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = rgb[:3]
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first, second):
    """WCAG contrast ratio of two (r, g, b) colors, 1 to 21."""
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def hue_of(rgb):
    return colorsys.rgb_to_hls(*(v / 255 for v in rgb[:3]))[0] * 360


def away(hue, avoid):
    gap = (hue - avoid + 180) % 360 - 180
    if abs(gap) >= ERROR_GAP:
        return hue
    return (avoid + (ERROR_GAP if gap >= 0 else -ERROR_GAP)) % 360


def readable(hue, background, dark, saturation=0.85):
    """The color of `hue` closest to a mid lightness that reaches MIN_CONTRAST on `background`, as #rrggbb."""
    steps = [0.62 + i * 0.02 for i in range(18)] if dark else [0.42 - i * 0.02 for i in range(21)]
    for lightness in steps:
        rgb = tuple(round(v * 255) for v in colorsys.hls_to_rgb(hue / 360, min(lightness, 0.97), saturation))
        if contrast(rgb, background) >= MIN_CONTRAST:
            break
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def fit(value, background, dark):
    """A theme color as #rrggbb, with its hue and saturation kept and its lightness moved until it is readable."""
    rgb = Color.parse(value).rgb
    if contrast(rgb, background) >= MIN_CONTRAST:
        return "#{:02x}{:02x}{:02x}".format(*rgb)
    _, _, saturation = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    return readable(hue_of(rgb), background, dark, saturation)


def css(style):
    """The Textual spelling of a style color made here (borders and Content need ansi_* names, not color(n))."""
    if style.startswith("color("):
        return ANSI_NAMES[int(style[6:-1])]
    return style


def ansi_styles(dark, native, variables):
    def number(slot):
        if native and slot in THEME_SLOT:
            found = Color.parse(variables[THEME_SLOT[slot]]).ansi
            if found is not None and found >= 0:
                return found
        if slot == "neutral":
            return 7 if dark else 8
        return ANSI_SLOT[slot] + (8 if dark else 0)

    def spell(n):
        return f"color({n})" if native else "#{:02x}{:02x}{:02x}".format(*STANDARD_PALETTE[n])

    styles = {slot: spell(number(slot)) for slot in ANSI_SLOT}
    projects = tuple(spell(n) for n in PROJECT_ANSI[dark])
    return styles, projects


def rgb_styles(dark, variables):
    background = Color.parse(variables["background"]).rgb
    error = Color.parse(variables["error"]).rgb
    styles = {slot: fit(variables[name], background, dark) for slot, name in THEME_SLOT.items()}
    for slot, hue in HUES.items():
        styles[slot] = readable(away(hue, hue_of(error)), background, dark, 0.3 if slot == "neutral" else 0.85)
    projects = tuple(readable(away(hue, hue_of(error)), background, dark, 0.75) for hue in PROJECT_HUES)
    return styles, projects


def apply(variables=None, dark=True, ansi=False, color_system="truecolor"):
    """Recompute every style for a theme's CSS variables (App.theme_variables) and the terminal's color system."""
    global RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GOLD, NEUTRAL, STATE, KIND, QUEUE, ROOM, REASON, SERIES
    global PROJECTS, MODE, GENERATION
    GENERATION += 1
    if variables is None:
        variables = BUILTIN_THEMES["textual-dark"].to_color_system().generate()
    if ansi or color_system in ("standard", "windows"):
        MODE = "ansi" if ansi else "standard"
        styles, PROJECTS = ansi_styles(dark, ansi, variables)
    else:
        MODE = "rgb"
        styles, PROJECTS = rgb_styles(dark, variables)
    RED, GREEN, YELLOW = styles["red"], styles["green"], styles["yellow"]
    BLUE, MAGENTA, CYAN = styles["blue"], styles["magenta"], styles["cyan"]
    GOLD, NEUTRAL = styles["gold"], styles["neutral"]
    by_code = {top.R: "", top.B: "bold", top.DIM: "dim", top.RED: RED, top.GREEN: GREEN, top.YELLOW: YELLOW,
               top.BLUE: BLUE, top.MAGENTA: MAGENTA, top.CYAN: CYAN}

    def convert(colors):
        return {key: by_code[value] for key, value in colors.items()}

    SERIES = {"assigned": GREEN, "delivered": MAGENTA, "partial": GOLD, "blocked": BLUE, "merged": CYAN, "errors": RED}
    STATE, QUEUE, ROOM, REASON = (convert(m) for m in (top.STATE_COLOR, top.QUEUE_COLOR, top.ROOM_COLOR,
                                                       top.REASON_COLOR))
    KIND = {**convert(top.KIND_COLOR), "delivered": MAGENTA, "partial": GOLD, "blocked": BLUE}


def project_color(name):
    """Stable accent color of a project, chosen from the current project palette by a hash of its name."""
    return PROJECTS[int(hashlib.sha1(str(name).encode()).hexdigest(), 16) % len(PROJECTS)]


def aged(seconds, warn, alarm):
    if seconds is None:
        return ""
    return RED if seconds >= alarm else YELLOW if seconds >= warn else ""


def level(fraction):
    return RED if fraction > 0.9 else YELLOW if fraction > 0.7 else GREEN


apply()
