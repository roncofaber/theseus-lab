"""CPU utilisation from /proc/stat deltas and in-memory ring buffers of samples for the dashboard's history charts."""
from collections import deque
from pathlib import Path


def read_stat():
    try:
        return Path("/proc/stat").read_text()
    except OSError:
        return ""


def cpu_times(text):
    """name -> (busy ticks, total ticks) for the 'cpu' line and every 'cpuN' line."""
    found = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        name, *fields = line.split()
        try:
            values = [int(v) for v in fields[:8]]
        except ValueError:
            continue
        idle = sum(values[3:5])
        found[name] = (sum(values) - idle, sum(values))
    return found


class CpuSampler:
    """Busy percent per CPU since the previous sample; primed at construction so the first sample has a baseline."""

    def __init__(self, reader=read_stat):
        self.reader = reader
        self.previous = cpu_times(reader())

    def sample(self):
        current = cpu_times(self.reader())
        usage = {}
        for name, (busy, total) in current.items():
            before = self.previous.get(name)
            if before and total > before[1]:
                usage[name] = max(0.0, min(100.0, 100.0 * (busy - before[0]) / (total - before[1])))
        self.previous = current
        if "cpu" not in usage:
            return None
        cores = sorted((n for n in usage if n != "cpu"), key=lambda n: int(n[3:]) if n[3:].isdigit() else 0)
        return {"total": usage["cpu"], "cores": [usage[n] for n in cores]}


class Ring:
    """The newest `size` samples, oldest first."""

    def __init__(self, size):
        self.items = deque(maxlen=size)

    def push(self, value):
        self.items.append(value)

    def values(self, count=None):
        items = list(self.items)
        return items if count is None else items[-count:] if count > 0 else []

    def latest(self):
        return self.items[-1] if self.items else None

    def __len__(self):
        return len(self.items)
