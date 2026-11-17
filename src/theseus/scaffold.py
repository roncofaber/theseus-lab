"""Create the workflow layout in a target repository: governance docs, task file, docs layout, generic skills,
Supervisor rules, the pre-push hook and theseus.toml. Existing files are kept unless --force."""
import datetime
import os
import stat
from importlib import resources
from pathlib import Path

from . import config as cfg
from . import docs
from . import gitutil as g

EXECUTABLE = {"tools/git-hooks/pre-push"}


def template_root():
    return resources.files("theseus") / "templates" / "project"


def destination(relative):
    parts = [("." + p[len("dot-"):]) if p.startswith("dot-") else p for p in relative.split("/")]
    return "/".join(parts)


def walk(node, prefix=""):
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        relative = f"{prefix}{child.name}"
        if child.is_dir():
            if child.name == "__pycache__":
                continue
            yield from walk(child, relative + "/")
        else:
            yield relative, child


def render(text, values):
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(target, name=None, force=False, init_git=True, hooks=True, adopt=False):
    """Write the project template. adopt brings theseus into an existing repository: nothing is overwritten, files
    that differ from the template get the theseus version under theseus-adopt/ for a manual merge, and the push hook
    and generated indexes are left for the coordinator to switch."""
    force = force and not adopt
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    name = name or target.name
    values = {"PROJECT": name, "PREFIX": cfg.slug(name) or "project", "DATE": datetime.date.today().isoformat()}
    report = []
    if init_git and not (target / ".git").exists():
        g.git(target, "init", "-q", "-b", "main")
        report.append("initialized a Git repository (no commit made)")
    for relative, source in walk(template_root()):
        path = target / destination(relative)
        if destination(relative) == ".gitignore" and path.exists() and not force:
            present = set(path.read_text().splitlines())
            missing = [line for line in render(source.read_text(), values).splitlines() if line and line not in present]
            if missing:
                with open(path, "a") as handle:
                    handle.write(("" if path.read_text().endswith("\n") or not path.read_text() else "\n") + "\n".join(missing) + "\n")
                report.append(f"updated  .gitignore (added {', '.join(missing)})")
            continue
        if path.exists() and not force:
            rendered = render(source.read_text(), values)
            if adopt and path.read_text(errors="replace") != rendered:
                copy = target / "theseus-adopt" / destination(relative)
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_text(rendered)
                report.append(f"kept     {path.relative_to(target)} (differs; theseus version in {copy.relative_to(target)})")
            else:
                report.append(f"kept     {path.relative_to(target)} (exists; --force overwrites)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(source.read_text(), values))
        if destination(relative) in EXECUTABLE:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        report.append(f"wrote    {path.relative_to(target)}")
    link = target / ".claude/skills"
    if not link.exists() and not link.is_symlink():
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../" + cfg.SKILLS, link)
        report.append("linked   .claude/skills -> ../.agents/skills")
    if adopt:
        report.append("adopt: generated indexes untouched; run `theseus docs index` once the docs layout is merged")
        report.append("adopt: push hook unchanged; switch with `git config core.hooksPath tools/git-hooks` after a passing "
                      "`theseus verify`")
        if (target / "theseus-adopt").exists():
            report.append("adopt: merge what you need from theseus-adopt/, then delete it")
        return report
    conf = cfg.load(root=target)
    for written in docs.regenerate(conf):
        report.append(f"indexed  {written}")
    if hooks and (target / ".git").exists():
        g.git(target, "config", "core.hooksPath", "tools/git-hooks")
        report.append("set git config core.hooksPath tools/git-hooks")
    return report


OWNED = {
    ".agents/skills/theseus-coordinating/SKILL.md",
    ".agents/skills/theseus-worker/SKILL.md",
    ".agents/skills/theseus-worker/prompt-template.md",
    ".agents/skills/theseus-worker/instance-brief.md",
    ".agents/skills/theseus-native-re/SKILL.md",
    ".agents/skills/theseus-reference-runs/SKILL.md",
    ".agents/skills/theseus-recipe/SKILL.md",
    "tools/lib/xvfb.sh",
    "tools/git-hooks/pre-push",
    "tools/Supervisor/README.md",
}


def missing_keys(template, current, prefix=""):
    found = []
    for key, value in template.items():
        name = f"{prefix}{key}"
        if key not in current:
            found.append(name)
        elif isinstance(value, dict) and isinstance(current.get(key), dict):
            found += missing_keys(value, current[key], name + ".")
    return found


def upgrade(target, dry_run=False):
    """Refresh the files theseus owns (generic skills, hook, display helper), add template files the project lacks,
    and list configuration keys the project's theseus.toml does not have yet. Project-owned files are never touched."""
    import tomllib
    target = Path(target).resolve()
    conf = cfg.load(root=target)
    values = {"PROJECT": conf.name, "PREFIX": conf.prefix, "DATE": datetime.date.today().isoformat()}
    report = []
    for relative, source in walk(template_root()):
        dest = destination(relative)
        path = target / dest
        text = render(source.read_text(), values)
        if path.exists() and dest not in OWNED:
            if dest == "theseus.toml":
                for key in missing_keys(tomllib.loads(text), tomllib.loads(path.read_text())):
                    report.append(f"config   theseus.toml lacks {key} (see the template; defaults apply until set)")
            continue
        if path.exists() and path.read_text() == text:
            continue
        report.append(f"{'would ' if dry_run else ''}{'update' if path.exists() else 'add'}   {dest}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if dest in EXECUTABLE:
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not dry_run:
        docs.regenerate(conf)
    return report or ["already current"]
