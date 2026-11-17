"""Report index, citation index (addresses and capture references) and folding reports into topics."""
import unittest

from helpers import Sandbox

EVIDENCE_A = "ev_" + "a" * 64
HEADER = "stem\tclass\ttopic\tsuccessor\n"
FOLD_MAP = "docs/project/archive/investigations/FOLD-MAP.tsv"


class DocsFoldTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.write("docs/investigations/alpha.md",
                   f"# Alpha\n\nTask `tooling-alpha`\n\n| 0x00500010 | thing | {EVIDENCE_A} |\nFrame cap_00112233aabb@12 shows it.\n"
                   "See [beta](beta.md) and docs/investigations/beta.md.\n")
        self.write("docs/investigations/beta.md", "# Beta\n\nTask: `data-beta`\n\nCites 0x00500020 and FUN_00500030.\n")
        self.write("docs/investigations/open.md", "# Open\n\nlinks [alpha](alpha.md#x)\n")
        self.write("docs/topics/topic.md", f"# Topic\n\n| 0x00500010 | thing | {EVIDENCE_A} |\n| cap_00112233aabb@12 | frame |\n"
                                           "| 0x00500020 | b |\n| 0x00500030 | c |\n")
        self.write("src/thing.py", "# see docs/investigations/alpha.md\n")
        self.write(FOLD_MAP, HEADER + "alpha\tC\ttopic\t\nbeta\tS\ttopic\talpha\n")
        self.git("add", "-A")

    def test_index_lists_reports_and_every_citation_kind(self):
        self.theseus("docs", "index")
        index = (self.project / "docs/investigations/README.md").read_text()
        self.assertIn("| [alpha](alpha.md) | Alpha | `tooling-alpha` |", index)
        citations = (self.project / "docs/re/citation-index.md").read_text()
        self.assertIn("| address | 0x00500010 | docs/investigations/alpha.md:5 | ev_aaaaaaaaaaaa... |", citations)
        self.assertIn("| capture | cap_00112233aabb@12 | docs/investigations/alpha.md:6 |", citations)
        self.assertIn("| address | 0x00500030 | docs/investigations/beta.md:5 |", citations)
        self.theseus("docs", "check")
        self.write("docs/investigations/gamma.md", "# Gamma\n")
        self.assertNotEqual(0, self.theseus("docs", "check", check=False).returncode)

    def test_apply_moves_reports_rewrites_links_and_keeps_checks_green(self):
        self.theseus("docs", "fold-apply")
        archive = self.project / "docs/project/archive/investigations"
        self.assertTrue((archive / "alpha.md").exists())
        self.assertFalse((self.project / "docs/investigations/alpha.md").exists())
        self.assertIn("(../project/archive/investigations/alpha.md#x)", (self.project / "docs/investigations/open.md").read_text())
        self.assertIn("docs/project/archive/investigations/beta.md", (archive / "alpha.md").read_text())
        self.assertIn("docs/project/archive/investigations/alpha.md", (self.project / "src/thing.py").read_text())
        self.theseus("docs", "check")
        citations = (self.project / "docs/re/citation-index.md").read_text()
        self.assertIn("docs/project/archive/investigations/beta.md:5 (archived)", citations)

if __name__ == "__main__":
    unittest.main()
