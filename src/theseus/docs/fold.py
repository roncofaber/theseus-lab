"""Fold investigation reports into living topic documents without losing a citation or evidence ID.

FOLD-MAP.tsv (stem, class, topic, successor) under docs/project/archive/investigations maps each report to its topic.
check validates the map, the archive directory and links to moved reports; a topic check requires every citation and
evidence ID of the topic's reports to appear in the topic document (or, for a citation homed elsewhere, in another
topic); apply moves folded reports with git mv and rewrites every link to them.
"""
import os
import re
import subprocess
from pathlib import Path

from .. import config as cfg
from . import citations as cit
from . import index

LOCATIONS = (cfg.REPORTS, cfg.REPORTS + "/archive", cfg.FOLDED)
SOURCES = (cfg.REPORTS, cfg.REPORTS + "/archive")
SKIP_DIRS = {".git", cfg.WORKER_LOG, "bin", "obj", "node_modules", "__pycache__", ".claude", ".vs", ".venv", "build", "dist"}
SKIP_FILES = {"tasks-archive.jsonl"}
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".jsonl", ".tsv", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini",
                 ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".kt", ".ts", ".js", ".rs", ".go"}
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
LINK_TARGET = re.compile(r"\]\(([^)\s#]+)(#[^)\s]*)?\)")
ABSOLUTE = re.compile(re.escape(cfg.REPORTS) + r"/(?:archive/)?([A-Za-z0-9_.-]+?)\.md")


def report_path(root, stem, locations=LOCATIONS):
    for location in locations:
        path = root / location / f"{stem}.md"
        if path.exists():
            return path
    return None


def live_files(root):
    for directory, names, files in os.walk(root):
        names[:] = [n for n in names if n not in SKIP_DIRS]
        for name in files:
            if name in SKIP_FILES or Path(name).suffix not in TEXT_SUFFIXES:
                continue
            yield Path(directory) / name


def read_text(path):
    try:
        return path.read_text(errors="strict")
    except (UnicodeDecodeError, OSError):
        return None


def map_problems(root, rows):
    problems = index.folded_problems(root) if (root / cfg.FOLDED).is_dir() else []
    for entry in rows:
        if report_path(root, entry["stem"]) is None:
            problems.append(f"FOLD-MAP.tsv:{entry['line']}: report {entry['stem']} exists in no known location")
    return problems


def stale_links(root, moved):
    problems = []
    for path in live_files(root):
        text = read_text(path)
        if text is None:
            continue
        relative = path.relative_to(root).as_posix()
        for match in ABSOLUTE.finditer(text):
            stem = match.group(1)
            if stem in moved and report_path(root, stem, SOURCES) is None:
                problems.append(f"{relative}: stale path {match.group(0)}")
        if path.suffix != ".md":
            continue
        for match in LINK.finditer(text):
            target = match.group(1)
            name = Path(target).name
            if "://" in target or not name.endswith(".md") or name[:-3] not in moved:
                continue
            if not (path.parent / target).resolve().exists():
                problems.append(f"{relative}: stale link {target}")
    return problems


def topic_problems(root, rows, topic, citations):
    document = root / cfg.TOPICS / f"{topic}.md"
    if not document.exists():
        return [f"topic document {document.relative_to(root)} does not exist"]
    others = "\n".join(p.read_text(errors="replace") for p in (root / cfg.TOPICS).glob("*.md")
                       if p != document and p.name != "README.md")
    text = document.read_text(errors="replace")
    have, have_evidence = citations.find(text), citations.evidence_ids(text)
    elsewhere, elsewhere_evidence = citations.find(others), citations.evidence_ids(others)
    missing, missing_evidence = {}, {}
    for entry in rows:
        if entry["topic"] != topic or entry["class"] in index.NO_TOPIC:
            continue
        path = report_path(root, entry["stem"])
        if path is None:
            continue
        report = path.read_text(errors="replace")
        for item in citations.find(report) - have - elsewhere:
            missing.setdefault(item, []).append(entry["stem"])
        for item in citations.evidence_ids(report) - have_evidence - elsewhere_evidence:
            missing_evidence.setdefault(item, []).append(entry["stem"])
    problems = [f"{kind} {value} of {', '.join(sorted(set(s)))} is missing from {document.name}"
                for (kind, value), s in sorted(missing.items())]
    problems += [f"evidence {e} of {', '.join(sorted(set(s)))} is missing from {document.name}" for e, s in sorted(missing_evidence.items())]
    return problems


def moved_stems(root, rows):
    return {e["stem"] for e in rows if (root / cfg.FOLDED / f"{e['stem']}.md").exists()}


def run(conf, topics, check, root=None):
    root = Path(root or conf.root)
    citations = cit.of(conf)
    rows = index.read_fold_map(root)
    problems = []
    if check:
        problems += map_problems(root, rows)
        problems += stale_links(root, moved_stems(root, rows))
    for topic in topics:
        problems += topic_problems(root, rows, topic, citations)
    return problems


def rewrite(text, old_path, new_path, moves, root):
    """Rewrite markdown links by resolving them from the file's old location, and path mentions by repository path."""
    def link(match):
        target, fragment = match.group(1), match.group(2) or ""
        if "://" in target or not target.endswith(".md"):
            return match.group(0)
        resolved = Path(os.path.normpath(old_path.parent / target))
        destination = moves.get(resolved)
        if destination is None and (old_path == new_path or not resolved.exists()):
            return match.group(0)
        destination = destination or resolved
        return f"]({os.path.relpath(destination, new_path.parent).replace(os.sep, '/')}{fragment})"

    def absolute(match):
        destination = moves.get(root / cfg.REPORTS / match.group(0)[len(cfg.REPORTS) + 1:])
        return destination.relative_to(root).as_posix() if destination else match.group(0)

    return ABSOLUTE.sub(absolute, LINK_TARGET.sub(link, text))


def apply(conf, topics=(), dry_run=False, verify=True, root=None):
    root = Path(root or conf.root)
    rows = index.read_fold_map(root)
    moves = {}
    for entry in rows:
        if topics and entry["topic"] not in topics:
            continue
        source = report_path(root, entry["stem"], SOURCES)
        if source is not None:
            moves[source] = root / cfg.FOLDED / source.name
    if verify:
        citations = cit.of(conf)
        names = sorted({e["topic"] for e in rows if (not topics or e["topic"] in topics) and e["class"] not in index.NO_TOPIC
                        and report_path(root, e["stem"], SOURCES)})
        problems = [p for name in names for p in topic_problems(root, rows, name, citations)]
        if problems:
            return False, problems
    edits = {}
    for path in live_files(root):
        text = read_text(path)
        if text is None:
            continue
        updated = rewrite(text, path, moves.get(path, path), moves, root)
        if updated != text:
            edits[path] = updated
    summary = [f"{len(moves)} reports to move, {len(edits)} files to rewrite"]
    if dry_run:
        return True, summary
    (root / cfg.FOLDED).mkdir(parents=True, exist_ok=True)
    for path, text in edits.items():
        path.write_text(text)
    for old, new in sorted(moves.items()):
        subprocess.run(["git", "mv", str(old.relative_to(root)), str(new.relative_to(root))], cwd=root, check=True)
    return True, summary
