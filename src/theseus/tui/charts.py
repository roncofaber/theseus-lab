"""Text charts for the dashboard: stacked bar charts with a y axis, a time axis and a legend, a history chart and
horizontal meters. Pure functions from data and a size to Rich text, so they are tested without widgets."""
import math

from rich.text import Text

from .. import host
from . import theme

EIGHTHS = " ▁▂▃▄▅▆▇█"
PARTIAL = " ▏▎▍▌▋▊▉█"
AXIS = "dim"


def kind_series(activity):
    """[(label, style, counts)] for one project's activity groups, in the order of host.ACTIVITY_GROUPS."""
    activity = activity or {}
    return [(theme.SERIES_LABEL[name], theme.SERIES[name], list(activity.get(name) or [0] * host.BINS))
            for name, _ in host.ACTIVITY_GROUPS]


def project_series(projects):
    """[(project, accent, counts)]: every available project's grouped events per bin, one series per project."""
    series = []
    for item in projects:
        if not item.get("available") or not item.get("activity"):
            continue
        groups = [item["activity"][name] for name, _ in host.ACTIVITY_GROUPS]
        series.append((item["name"], theme.project_color(item["name"]), [sum(column) for column in zip(*groups)]))
    return series


def legend(series, width=None):
    """Legend lines: a colored square, the label and the total of each series, wrapped to `width`."""
    lines = [Text()]
    for label, style, values in series:
        item = Text.assemble(("■ ", style), label, (f" {sum(values)}", "bold"))
        line = lines[-1]
        if line and width and len(line) + 3 + len(item) > width:
            lines.append(Text())
            line = lines[-1]
        if line:
            line.append("   ")
        line.append_text(item)
    return lines


def merge_bins(values, factor):
    """Sum groups of `factor` bins, keeping the newest bin at the end of the last group."""
    if factor <= 1:
        return list(values)
    start = len(values) % factor
    head = [sum(values[:start])] if start else []
    return head + [sum(values[i:i + factor]) for i in range(start, len(values), factor)]


def ago(seconds):
    if seconds % 3600 == 0:
        return f"-{seconds // 3600}h"
    if seconds >= 3600:
        return f"-{seconds // 3600}h{seconds % 3600 // 60:02d}"
    return f"-{seconds // 60}m"


def time_axis(starts, span, slot_seconds, offset, total):
    """The label line under a chart: whole-hour labels at their slot's left edge and 'now' at the right edge, plus the
    plot columns that get a tick on the baseline."""
    slots = len(starts)
    narrowest = min(b - a for a, b in zip(starts, starts[1:] + [span])) if slots else 1
    line = [" "] * (offset + span + 4)
    step_seconds = next((n for n in (900, 1800, 3600, 7200, 10800, 14400, 21600, 43200)
                         if n % slot_seconds == 0 and n // slot_seconds * narrowest >= 9), None)
    ticks = []
    for index in range(slots):
        seconds = (slots - index) * slot_seconds
        if step_seconds and seconds % step_seconds or not step_seconds and index % max(1, 9 // narrowest + 1):
            continue
        label = ago(seconds)
        position = offset + starts[index]
        if position + len(label) <= offset + span - 4 and all(starts[index] >= t + 9 for t in ticks[-1:]):
            line[position:position + len(label)] = label
            ticks.append(starts[index])
    end = offset + span - 3
    line[end:end + 3] = "now"
    return Text("".join(line[:total]).rstrip(), style=AXIS), ticks


def stacked_bars(series, width, height, bin_seconds=host.BIN_SECONDS):
    """Lines of a stacked bar chart of `series` [(label, style, counts)] in `width` x `height` cells: a y axis with
    integer ticks, one bar per bin (adjacent bins merged when narrow) spread over the full width, a baseline and a
    time axis. Each row is a whole number of events: one when the tallest bar fits, a larger integer otherwise."""
    bins = len(series[0][2]) if series else host.BINS
    totals = [sum(values[i] for _, _, values in series) for i in range(bins)]
    rows = max(1, height - 2)
    label_width = max(3, len(str(sum(totals) + rows)) + 1)
    plot_width = max(1, width - label_width - 1)
    factor = max(1, math.ceil(bins / plot_width))
    merged = [(label, style, merge_bins(values, factor)) for label, style, values in series]
    slots = len(merged[0][2]) if merged else math.ceil(bins / factor)
    slot_totals = [sum(values[i] for _, _, values in merged) for i in range(slots)]
    starts = [i * plot_width // slots for i in range(slots)]
    widths = [b - a for a, b in zip(starts, starts[1:] + [plot_width])]
    bar_width = min(widths) - 1 if min(widths) >= 2 else 1
    peak = max(slot_totals or [0])
    unit = max(1, math.ceil(peak / rows))
    lines = []
    label_rows = set(range(rows - 1, -1, -max(1, math.ceil(rows / 3))))
    for row in range(rows - 1, -1, -1):
        low, high = row * unit, (row + 1) * unit
        line = Text()
        tick = row in label_rows
        line.append(f"{high if tick else '':>{label_width - 1}} ", style=AXIS)
        line.append("┤" if tick else "│", style=AXIS)
        for slot in range(slots):
            total = slot_totals[slot]
            cell, style = " ", ""
            if total > low:
                fill = min(total, high) - low
                cell = "█" if fill >= unit else EIGHTHS[max(1, round(8 * fill / unit))]
                middle = low + fill / 2
                running = 0
                for _, series_style, values in merged:
                    running += values[slot]
                    if running > middle:
                        style = series_style
                        break
            line.append(cell * bar_width, style=style)
            line.append(" " * (widths[slot] - bar_width))
        lines.append(line)
    axis_text, ticks = time_axis(starts, plot_width, bin_seconds * factor, label_width + 1, width)
    base = ["─"] * plot_width
    for position in ticks:
        base[position] = "┴"
    lines.append(Text(f"{0:>{label_width - 1}} └" + "".join(base), style=AXIS))
    lines.append(axis_text)
    return lines


def history(values, width, height, style="green", maximum=100.0, seconds=None, suffix="%"):
    """A filled history chart of the newest samples (oldest left), one column per sample, `maximum` at the top."""
    rows = max(1, height - 1)
    label_width = len(f"{maximum:.0f}{suffix}") + 1
    plot_width = max(1, width - label_width - 1)
    shown = list(values)[-plot_width:]
    padding = plot_width - len(shown)
    lines = []
    for row in range(rows - 1, -1, -1):
        line = Text()
        label = f"{maximum:.0f}{suffix}" if row == rows - 1 else (f"0{suffix}" if row == 0 else "")
        line.append(f"{label:>{label_width - 1}} ", style=AXIS)
        line.append("┤" if label else "│", style=AXIS)
        line.append(" " * padding)
        for value in shown:
            fraction = min(max(value / maximum, 0.0), 1.0) * rows - row
            cell = "█" if fraction >= 1 else EIGHTHS[round(max(fraction, 0.0) * 8)]
            line.append(cell, style=theme.level(value / maximum) if style == "level" else style)
        lines.append(line)
    footer = Text(" " * label_width + "└", style=AXIS)
    covered = int(seconds * len(shown)) if seconds else 0
    span = "" if not covered else f"-{covered}s" if covered < 60 else f"-{covered // 60}m"
    fits = span and plot_width >= len(span) + 4
    footer.append(f"{span:<{plot_width - 3}}now" if fits else f"{'now':>{plot_width}}"[:plot_width], style=AXIS)
    lines.append(footer)
    return lines


def meter(fraction, width=18, style=None):
    """A horizontal bar: full blocks, an eighth-block partial cell and a dim remainder."""
    fraction = min(max(fraction or 0.0, 0.0), 1.0)
    cells = fraction * width
    full = int(cells)
    partial = PARTIAL[int((cells - full) * 8)] if full < width else ""
    bar = Text("█" * full + partial.strip(), style=style or theme.level(fraction))
    bar.append("░" * (width - len(bar.plain)), style="dim")
    return bar
