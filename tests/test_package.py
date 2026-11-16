"""Package hygiene: shell scripts lint clean, the shipped files are generic (no developer paths or private project
names, no em dashes or emojis), and a built wheel installed in a fresh virtual environment scaffolds a project whose
Supervisor suite passes.

Private words to keep out of the package (other project names, user names) go one per line in the Git-ignored
tests/forbidden.local.txt or in the file THESEUS_FORBIDDEN_WORDS names; the shipped patterns are generic."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import REPO, base_env

from theseus import rules

PACKAGE = REPO / "src/theseus"
GENERIC_FORBIDDEN = [r"/home/[a-z][\w.-]*/", r"/Users/[A-Za-z][\w.-]*/", r"\.local/share/[\w.-]+/worktree", r"%1\d\d\b"]


def forbidden_pattern():
    words = list(GENERIC_FORBIDDEN)
    local = Path(os.environ.get("THESEUS_FORBIDDEN_WORDS") or REPO / "tests/forbidden.local.txt")
    if local.exists():
        words += [re.escape(w.strip()) for w in local.read_text().splitlines() if w.strip() and not w.startswith("#")]
    return re.compile("|".join(words), re.IGNORECASE)


FORBIDDEN = forbidden_pattern()
SITE_SPECIFIC = re.compile(r"cborg|lbl/|berkeley|\blbnl\b", re.IGNORECASE)
EM_DASH = chr(0x2014)
EMOJI = re.compile("[" + chr(0x1F300) + "-" + chr(0x1FAFF) + chr(0x2600) + "-" + chr(0x27BF) + "]")


def shipped_files():
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


class ShellTests(unittest.TestCase):
    def test_every_shipped_shell_script_parses_and_passes_shellcheck(self):
        scripts = rules.shell_scripts(PACKAGE / "scripts") + rules.shell_scripts(PACKAGE / "templates")
        self.assertGreaterEqual(len(scripts), 12)
        self.assertEqual([], rules.syntax_errors(scripts))
        if not rules.shellcheck_available():
            self.skipTest("shellcheck is not installed; lint skipped")
        self.assertEqual([], rules.shellcheck_errors(scripts, cwd=PACKAGE / "scripts"))


class GenericityTests(unittest.TestCase):
    def test_shipped_files_name_no_source_project_tool_or_developer(self):
        offenders = []
        for path in shipped_files():
            text = path.read_text(errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if FORBIDDEN.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
        self.assertEqual([], offenders)

    def test_package_and_docs_are_site_neutral(self):
        offenders = []
        for path in [*shipped_files(), REPO / "README.md", *(REPO / "docs").rglob("*.md")]:
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if SITE_SPECIFIC.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
        self.assertEqual([], offenders)

    def test_no_em_dashes_or_emojis_anywhere(self):
        offenders = []
        for path in [*shipped_files(), *REPO.glob("*.md"), *REPO.glob("docs/*.md"), *REPO.glob("tests/*.py")]:
            text = path.read_text(errors="replace")
            if EM_DASH in text or EMOJI.search(text):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual([], offenders)

    def test_wine_appears_only_as_a_reference_run_backend(self):
        mentions = {str(p.relative_to(PACKAGE)) for p in shipped_files() if re.search(r"\bwine", p.read_text(errors="replace"), re.I)}
        self.assertEqual({"templates/project/dot-agents/skills/theseus-reference-runs/SKILL.md", "tools.py"}, mentions)


class WheelTests(unittest.TestCase):
    def test_wheel_in_a_fresh_venv_scaffolds_a_passing_project(self):
        with tempfile.TemporaryDirectory(prefix="theseus-wheel-") as temporary:
            root = Path(temporary)
            (root / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
            builder = sys.executable if importlib.util.find_spec("pip") else shutil.which("python3")
            build = subprocess.run([builder, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                                    "-w", str(root / "dist"), str(REPO)], capture_output=True, text=True)
            self.assertEqual(0, build.returncode, build.stderr)
            shutil.rmtree(REPO / "build", ignore_errors=True)
            wheel = next((root / "dist").glob("theseus_lab-*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                packaged = {name for name in archive.namelist() if name.startswith("theseus/")}
            expected = {"theseus/" + p.relative_to(PACKAGE).as_posix() for p in shipped_files()}
            self.assertEqual(set(), expected - packaged, "files missing from the wheel")
            subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
            python = root / "venv/bin/python"
            install = subprocess.run([str(python), "-m", "pip", "install", "-q", "--no-deps", str(wheel)], capture_output=True, text=True)
            self.assertEqual(0, install.returncode, install.stderr)
            env = base_env(root)
            env.pop("PYTHONPATH")
            env["PATH"] = f"{root / 'venv/bin'}{os.pathsep}{env['PATH']}"
            theseus = str(root / "venv/bin/theseus")
            scaffold = subprocess.run([theseus, "scaffold", str(root / "project")], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, scaffold.returncode, scaffold.stderr)
            self.assertIn("linked   .claude/skills", scaffold.stdout)
            check = subprocess.run([theseus, "check"], cwd=root / "project", env=env, capture_output=True, text=True)
            self.assertEqual(0, check.returncode, check.stdout + check.stderr)
            self.assertIn("OK", check.stderr)
            located = subprocess.run([str(python), "-c", "import theseus; print(theseus.__file__)"], capture_output=True, text=True, cwd=root)
            self.assertIn(str(root / "venv"), located.stdout)
            self.assertTrue(os.access(root / "project/tools/git-hooks/pre-push", os.X_OK))


if __name__ == "__main__":
    unittest.main()
