"""Local, Git-excluded lookup catalog for the original binaries ([[re.targets]] in theseus.toml).

build:   exports.tsv and strings.tsv under <data>/re/<sha256 prefix>/ from rz-bin, plus manifest.json.
find:    regex or address search over the catalog and the committed citation index in one pass.
show:    the decompiled function containing an address, from the corpus REA wrote to <data>/re/<prefix>/decompiled.
calls:   parse the corpus once into calls.tsv (caller, callee, callee name).
callers, callees: call-graph lookups from calls.tsv.
rea:     run REA; single-address decompiles are answered from the corpus unless REA_LIVE=1.
paths:   print the catalog and corpus directories of a target.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config as cfg

STRING_TYPES = {"ascii", "utf8", "utf16le"}
ADDRESS = re.compile(r"(?:0x|FUN_|ram:0x)?0*([0-9A-Fa-f]{4,16})")


class CatalogError(Exception):
    pass


def targets(conf):
    result = []
    for entry in conf.section("re").get("targets") or []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
            raise cfg.ConfigError("each [[re.targets]] entry needs name and path (and sha256)")
        result.append({"name": entry["name"], "path": Path(cfg.expand(entry["path"], conf.values)),
                       "sha256": (entry.get("sha256") or "").lower()})
    return result


def target(conf, name=None):
    found = targets(conf)
    if not found:
        raise CatalogError("no [[re.targets]] configured in theseus.toml")
    if name is None:
        return found[0]
    for entry in found:
        if entry["name"] == name:
            return entry
    raise CatalogError(f"no target named {name}; configured: {', '.join(t['name'] for t in found)}")


def directory(conf, entry):
    if not entry["sha256"]:
        raise CatalogError(f"target {entry['name']} has no sha256; record it before building a catalog")
    return conf.data / "re" / entry["sha256"][:16]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean(text):
    return text.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def owner_and_member(demangled):
    match = re.search(r"((?:\w+::)*\w+)::(~?\w+|operator\S+)\(", demangled)
    return (match.group(1), match.group(2)) if match else ("", "")


def build(conf, entry, allow_other=False):
    exe = entry["path"]
    digest = sha256(exe)
    if entry["sha256"] and digest != entry["sha256"] and not allow_other:
        raise CatalogError(f"{exe} SHA-256 {digest} is not the recorded {entry['sha256']}; pass --allow-other to catalog it anyway")
    if shutil.which("rz-bin") is None:
        raise CatalogError("rz-bin (rizin) is not installed")
    out = conf.data / "re" / digest[:16]
    out.mkdir(parents=True, exist_ok=True)
    rz = lambda flag: json.loads(subprocess.run(["rz-bin", flag, str(exe)], check=True, capture_output=True, text=True).stdout)
    rows = []
    for item in rz("-Ej").get("exports", []):
        demangled = item.get("demname") or item.get("name") or ""
        owner, member = owner_and_member(demangled)
        rows.append((item["vaddr"], owner, member, demangled, item.get("realname") or ""))
    rows.sort()
    with open(out / "exports.tsv", "w") as handle:
        handle.write("vaddr\towner\tmember\tdemangled\tmangled\n")
        for vaddr, owner, member, demangled, mangled in rows:
            handle.write(f"0x{vaddr:08x}\t{owner}\t{member}\t{clean(demangled)}\t{mangled}\n")
    strings = rz("-zj")
    strings = strings.get("strings", strings)
    kept = sorted((s["vaddr"], s["section"], s["type"], s["string"]) for s in strings if s["type"] in STRING_TYPES)
    with open(out / "strings.tsv", "w") as handle:
        handle.write("vaddr\tsection\ttype\tstring\n")
        for vaddr, section, kind, text in kept:
            handle.write(f"0x{vaddr:08x}\t{section}\t{kind}\t{clean(text)}\n")
    manifest = {"target": entry["name"], "executable": str(exe), "sha256": digest, "exports": len(rows), "strings": len(kept),
                "tool": subprocess.run(["rz-bin", "-v"], capture_output=True, text=True).stdout.splitlines()[0]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return f"{len(rows)} exports, {len(kept)} strings -> {out}"


def query_pattern(text, case):
    match = re.fullmatch(r"(?:0x|FUN_|DAT_)?0*([0-9A-Fa-f]{5,16})", text)
    if match:
        digits = match.group(1).lower()
        return re.compile(rf"(?<![0-9a-f])(?:0x|FUN_|DAT_)?0*{digits}(?![0-9a-f])", re.IGNORECASE)
    return re.compile(text, 0 if case else re.IGNORECASE)


def find(conf, entry, pattern, what="all", case=False, limit=200):
    regex = query_pattern(pattern, case)
    base = directory(conf, entry) if entry else None
    sources = []
    if base and what in ("all", "exports"):
        sources.append(("export", base / "exports.tsv"))
    if base and what in ("all", "strings"):
        sources.append(("string", base / "strings.tsv"))
    if what in ("all", "index"):
        sources.append(("index", conf.root / cfg.CITATION_INDEX))
    lines, notes = [], []
    for label, path in sources:
        if not path.exists():
            notes.append(f"[{label}] missing {path}; " + ("run `theseus re build`" if label != "index" else "run `theseus docs index`"))
            continue
        with open(path, errors="replace") as handle:
            for line in handle:
                if regex.search(line):
                    lines.append(f"[{label}] {line.rstrip()}")
                    if len(lines) >= limit:
                        lines.append(f"... truncated at --limit {limit}; narrow the pattern or raise the limit")
                        return lines, notes
    return lines, notes


def corpus(conf, entry):
    base = directory(conf, entry) / "decompiled"
    index = base / "decompile-index.json"
    if not index.exists():
        raise CatalogError(f"missing {index}; decompile the target once with `theseus re rea decompile-all` (see the theseus-native-re skill)")
    return base, [e for e in json.loads(index.read_text())["functions"] if e.get("status") == "written"]


def parse_address(text):
    match = re.fullmatch(r"(?:ram:)?(?:0x|FUN_)?([0-9A-Fa-f]{4,16})", text)
    return int(match.group(1), 16) if match else None


def show(conf, entry, address, header=False):
    target_address = parse_address(address)
    if target_address is None:
        raise CatalogError("show takes an address such as 0x401000")
    base, entries = corpus(conf, entry)
    for item in entries:
        for span in item["body"]["ranges"]:
            if int(span["start"], 16) <= target_address <= int(span["end"], 16):
                text = (base / item["relative_path"]).read_text(errors="replace")
                if not header:
                    text = "\n".join(line for line in text.splitlines() if not line.startswith("// limitations:")) + "\n"
                return text
    raise CatalogError(f"no decompiled function contains 0x{target_address:08x}")


def build_calls(conf, entry):
    base, entries = corpus(conf, entry)
    by_name = {}
    for item in entries:
        by_name.setdefault(item["name"], set()).add(int(item["address"], 16))
    call = re.compile(r"\b((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\(")
    rows = set()
    for item in entries:
        caller = int(item["address"], 16)
        body = "\n".join(line for line in (base / item["relative_path"]).read_text(errors="replace").splitlines()
                         if not line.startswith("//"))
        for match in call.finditer(body):
            name = match.group(1)
            found = by_name.get(name)
            if found is None and re.fullmatch(r"FUN_[0-9a-fA-F]{6,16}", name):
                found = {int(name[4:], 16)}
            for callee in found or ():
                if callee != caller:
                    rows.add((caller, callee, name))
    out = base / "calls.tsv"
    with open(out, "w") as handle:
        handle.write("caller\tcallee\tcallee_name\n")
        for caller, callee, name in sorted(rows):
            handle.write(f"0x{caller:08x}\t0x{callee:08x}\t{name}\n")
    return f"{len(rows)} call edges from {len(entries)} functions -> {out}"


def call_lookup(conf, entry, address, direction):
    base, entries = corpus(conf, entry)
    names = {int(e["address"], 16): e["name"] for e in entries}
    path = base / "calls.tsv"
    if not path.exists():
        raise CatalogError(f"missing {path}; run `theseus re calls`")
    target_address = parse_address(address)
    if target_address is None:
        matches = [a for a, n in names.items() if n == address]
        if not matches:
            raise CatalogError(f"no function named {address}")
        target_address = matches[0]
    column, other = (1, 0) if direction == "callers" else (0, 1)
    hits = set()
    with open(path) as handle:
        next(handle)
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if int(parts[column], 16) == target_address:
                hits.add(int(parts[other], 16))
    lines = [f"0x{a:08x}\t{names.get(a, '?')}" for a in sorted(hits)]
    return lines, f"{len(hits)} {direction} of 0x{target_address:08x} {names.get(target_address, '')}".rstrip()


def rea(conf, entry, arguments):
    """Answer single-address decompiles from the corpus; everything else goes to REA."""
    if arguments[:1] == ["decompile"] and os.environ.get("REA_LIVE", "0") != "1" and entry is not None:
        addresses, other = [], False
        for argument in arguments[1:]:
            if parse_address(argument) is not None and (argument.startswith(("0x", "ram:0x", "FUN_"))):
                addresses.append(argument)
            elif argument in ("--provider", "ghidra", "--json") or argument == str(entry["path"]):
                continue
            else:
                other = True
        try:
            corpus(conf, entry)
            available = True
        except CatalogError:
            available = False
        if addresses and not other and available:
            print("theseus re rea: answering from the decompiled corpus; set REA_LIVE=1 for a live session or an evidence export", file=sys.stderr)
            for address in addresses:
                print(show(conf, entry, address), end="")
            return 0
    from .. import tools
    command = tools.rea_command(conf)
    if not command:
        raise CatalogError("REA was not found; install it (npm install --global rea-agents) or `theseus tools set rea COMMAND`")
    return subprocess.run([*command, *arguments], env={**os.environ, **conf.environment()}).returncode


def add_parser(sub):
    parser = sub.add_parser("re", help="lookups in the original binaries: catalog, decompiled corpus, call graph, REA")
    actions = parser.add_subparsers(dest="re_action", required=True)
    for name, text in (("build", "extract exports and strings with rz-bin"), ("find", "search exports, strings and the citation index"),
                       ("show", "print the decompiled function containing an address"), ("calls", "parse the corpus into calls.tsv"),
                       ("callers", "list callers of a function"), ("callees", "list callees of a function"),
                       ("rea", "run REA (corpus answers single decompiles)"), ("paths", "print the catalog and corpus directories"),
                       ("status", "which targets already have a catalog, a decompiled corpus and a call graph"),
                       ("inventory", "inventory every executable image under files or directories (CSV and Markdown)")):
        p = actions.add_parser(name, help=text)
        p.add_argument("--target", help="target name (default: the first [[re.targets]] entry)")
        if name == "build":
            p.add_argument("--allow-other", action="store_true")
        if name == "find":
            p.add_argument("pattern")
            p.add_argument("--what", choices=("all", "exports", "strings", "index"), default="all")
            p.add_argument("--case", action="store_true")
            p.add_argument("--limit", type=int, default=200)
        if name in ("show", "callers", "callees"):
            p.add_argument("address")
        if name == "show":
            p.add_argument("--header", action="store_true")
        if name == "rea":
            p.add_argument("arguments", nargs=argparse_remainder())
        if name == "inventory":
            p.add_argument("inputs", nargs="+", help="files or directories to scan")
            p.add_argument("--out", required=True, help="CSV path; a Markdown table is written next to it")
            p.add_argument("--relative-to", help="show paths relative to this directory")
    return parser


def argparse_remainder():
    import argparse
    return argparse.REMAINDER


def main(conf, args):
    try:
        action = args.re_action
        if action == "find":
            try:
                entry = target(conf, args.target)
            except CatalogError:
                entry = None
            lines, notes = find(conf, entry, args.pattern, args.what, args.case, args.limit)
            for note in notes:
                print(note, file=sys.stderr)
            for line in lines:
                print(line)
            return 0 if lines else 1
        if action == "inventory":
            from . import inventory
            rows = inventory.inventory(args.inputs, args.relative_to)
            csv_path, markdown = inventory.write(rows, args.out)
            formats = {}
            for row in rows:
                formats[f"{row['format']} {row['machine']}"] = formats.get(f"{row['format']} {row['machine']}", 0) + 1
            print(f"{len(rows)} executable images -> {csv_path} and {markdown}: " + ", ".join(f"{k} {v}" for k, v in sorted(formats.items())))
            return 0
        if action == "status":
            from . import inventory
            rows = inventory.status(conf)
            if not rows:
                print("no [[re.targets]] configured")
            for row in rows:
                print(f"{row['target']:<16} file {row['file']:<14} catalog {row['catalog']:<6} corpus {row['corpus']:<16} calls {row['calls']}")
            return 0
        if action == "rea":
            try:
                entry = target(conf, args.target)
            except CatalogError:
                entry = None
            return rea(conf, entry, args.arguments)
        entry = target(conf, args.target)
        if action == "build":
            print(build(conf, entry, args.allow_other))
        elif action == "show":
            print(show(conf, entry, args.address, args.header), end="")
        elif action == "calls":
            print(build_calls(conf, entry))
        elif action in ("callers", "callees"):
            lines, summary = call_lookup(conf, entry, args.address, action)
            for line in lines:
                print(line)
            print(summary, file=sys.stderr)
        elif action == "paths":
            base = directory(conf, entry)
            print(f"catalog {base}\ncorpus  {base / 'decompiled'}")
        return 0
    except CatalogError as error:
        print(f"theseus re: {error}", file=sys.stderr)
        return 1
