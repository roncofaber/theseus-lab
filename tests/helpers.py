"""Shared fixtures: every test runs against temporary data, scratch and worktree roots, a temporary host database,
backup directory and host configuration (THESEUS_CONFIG, XDG_CONFIG_HOME, XDG_DATA_HOME) and a throwaway Git identity, so nothing touches the user's real projects, host store, tmux server or
model gateway."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def isolate_session():
    """Point the host store, host configuration and XDG roots of this whole test process, and of every subprocess it
    starts, at a private temporary directory before any test runs, and make theseus refuse the user's real host
    database (THESEUS_DB_GUARD) should a test or a subprocess drop that isolation."""
    import pwd
    fenced = {Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/theseus",
              Path(os.path.expanduser("~/.local/share")) / "theseus"}
    if os.environ.get("XDG_DATA_HOME"):
        fenced.add(Path(os.environ["XDG_DATA_HOME"]) / "theseus")
    if os.environ.get("THESEUS_DB"):
        fenced.add(Path(os.environ["THESEUS_DB"]))
    session = Path(tempfile.mkdtemp(prefix="theseus-session-"))
    os.environ.update({
        "XDG_DATA_HOME": str(session / "xdg-data"),
        "XDG_CONFIG_HOME": str(session / "xdg-config"),
        "THESEUS_DB": str(session / "xdg-data/theseus/theseus.db"),
        "THESEUS_CONFIG": str(session / "xdg-config/theseus/config.toml"),
        "THESEUS_DB_GUARD": os.pathsep.join(sorted(map(str, fenced))),
    })
    import atexit
    import shutil
    atexit.register(shutil.rmtree, session, True)


if "THESEUS_DB_GUARD" not in os.environ:
    isolate_session()


def base_env(root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("THESEUS_", "GIT_")) and key not in ("JAVA_HOME", "GHIDRA_INSTALL_DIR")}
    env.update({
        "THESEUS_DB_GUARD": os.environ["THESEUS_DB_GUARD"],
        "THESEUS_DATA": str(root / "data"),
        "THESEUS_SCRATCH": str(root / "scratch"),
        "THESEUS_WORKTREES": str(root / "worktrees"),
        "THESEUS_REGISTRY": str(root / "registry/projects.json"),
        "THESEUS_POOLS": str(root / "pools.toml"),
        "THESEUS_POOL_STATE": str(root / "pool-state"),
        "THESEUS_DB": str(root / "host/theseus.db"),
        "THESEUS_BACKUPS": str(root / "host/backups"),
        "THESEUS_STORE_CONFIG": str(root / "store.toml"),
        "THESEUS_CONFIG": str(root / "config.toml"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONPATH": str(SRC) + os.pathsep + env.get("PYTHONPATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


class Sandbox(unittest.TestCase):
    """A temporary directory with private theseus roots; run the CLI with self.theseus(...)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="theseus-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        self.env = base_env(self.tmp)
        self.project = self.tmp / "project"
        self.saved_environ = dict(os.environ)
        os.environ.update({k: v for k, v in self.env.items() if k.startswith(("THESEUS_", "GIT_", "XDG_"))})
        self.addCleanup(self.restore_environ)

    def restore_environ(self):
        os.environ.clear()
        os.environ.update(self.saved_environ)

    def run_cmd(self, args, cwd=None, check=True, env=None, input=None, timeout=120):
        result = subprocess.run(args, cwd=cwd or self.project, env=env or self.env, capture_output=True, text=True,
                                input=input, timeout=timeout)
        if check and result.returncode != 0:
            self.fail(f"{' '.join(map(str, args))} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def theseus(self, *args, check=True, cwd=None, env=None, input=None, timeout=120):
        return self.run_cmd([sys.executable, "-m", "theseus", *map(str, args)], cwd=cwd, check=check, env=env,
                            input=input, timeout=timeout)

    def git(self, *args, cwd=None, check=True):
        return self.run_cmd(["git", *map(str, args)], cwd=cwd, check=check)

    def scaffold(self, name="demo-port"):
        self.theseus("scaffold", self.project, "--name", name, cwd=self.tmp)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "scaffold")

    def set_config(self, text):
        path = self.project / "theseus.toml"
        path.write_text(path.read_text() + "\n" + text)

    def replace_config(self, old, new):
        path = self.project / "theseus.toml"
        content = path.read_text()
        self.assertIn(old, content)
        path.write_text(content.replace(old, new, 1))

    def write(self, relative, text, root=None):
        path = (root or self.project) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path
