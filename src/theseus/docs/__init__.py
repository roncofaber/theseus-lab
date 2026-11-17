"""Documentation tooling: report index, citation index, and folding reports into living topic documents.

A project with its own index generator sets [docs] index_command (writes the indexes), check_command (exits nonzero
when they are stale) and generated (the paths they produce); theseus then runs those instead of its own writers in
merge, `theseus docs index|check` and the push guard. The fold check always runs.
"""
import os
import subprocess


def external(conf):
    section = conf.section("docs")
    return bool(section.get("index_command") or section.get("check_command"))


def run_commands(conf, key):
    commands = conf.section("docs").get(key) or []
    commands = [commands] if isinstance(commands, str) else list(commands)
    failures = []
    for command in commands:
        result = subprocess.run(command, shell=True, cwd=conf.root, capture_output=True, text=True,
                                env={**os.environ, **conf.environment()})
        if result.returncode != 0:
            failures.append(f"[docs] {key} `{command}` exited {result.returncode}: "
                            f"{(result.stdout + result.stderr).strip()[-600:]}")
    return failures


def generated_paths(conf):
    """Repository-relative paths of every generated index (what regenerate writes)."""
    from . import index
    from .. import config as cfg
    if external(conf):
        return sorted(conf.section("docs").get("generated") or [])
    return sorted({p.relative_to(conf.root).as_posix() for p in index.outputs(conf.root)} | {cfg.citation_index(conf)})


def regenerate(conf):
    """Rewrite every generated index; returns the repository-relative paths written."""
    from . import citations, index
    if external(conf):
        failures = run_commands(conf, "index_command")
        if failures:
            raise RuntimeError("\n".join(failures))
        return generated_paths(conf)
    written = index.write(conf.root)
    written += citations.write(conf)
    return written


def check(conf):
    """Problems with the generated indexes and the fold map; empty when everything is current."""
    from . import citations, fold, index
    if external(conf):
        problems = run_commands(conf, "check_command")
    else:
        problems = index.check(conf.root)
        problems += citations.check(conf)
    problems += fold.run(conf, [], True)
    return problems
