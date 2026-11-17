"""Documentation tooling: report index, citation index, and folding reports into living topic documents."""


def generated_paths(conf):
    """Repository-relative paths of every generated index (what regenerate writes)."""
    from . import index
    from .. import config as cfg
    return sorted({p.relative_to(conf.root).as_posix() for p in index.outputs(conf.root)} | {cfg.CITATION_INDEX})


def regenerate(conf):
    """Rewrite every generated index; returns the repository-relative paths written."""
    from . import citations, index
    written = index.write(conf.root)
    written += citations.write(conf)
    return written


def check(conf):
    """Problems with the generated indexes and the fold map; empty when everything is current."""
    from . import citations, fold, index
    problems = index.check(conf.root)
    problems += citations.check(conf)
    problems += fold.run(conf, [], True)
    return problems
