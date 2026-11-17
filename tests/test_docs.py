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

    def test_fold_check_reports_a_dropped_capture_address_and_evidence(self):
        self.theseus("docs", "fold-check", "--topic", "topic")
        self.write("docs/topics/topic.md", "# Topic\n\n| 0x00500010 | thing |\n")
        result = self.theseus("docs", "fold-check", "--topic", "topic", check=False)
        self.assertEqual(1, result.returncode)
        for missing in ("0x00500020", "0x00500030", "cap_00112233aabb@12", EVIDENCE_A):
            self.assertIn(missing, result.stderr + result.stdout)

    def test_citation_homed_in_another_topic_is_accepted(self):
        self.write("docs/topics/topic.md", f"# Topic\n\n| 0x00500010 | {EVIDENCE_A} | cap_00112233aabb@12 |\n| 0x00500020 |\n")
        self.assertEqual(1, self.theseus("docs", "fold-check", "--topic", "topic", check=False).returncode)
        self.write("docs/topics/other.md", "# Other\n\n| 0x00500030 | c |\n")
        self.theseus("docs", "fold-check", "--topic", "topic")

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

    def test_apply_refuses_when_the_topic_lost_a_citation(self):
        self.write("docs/topics/topic.md", "# Topic\n")
        result = self.theseus("docs", "fold-apply", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue((self.project / "docs/investigations/alpha.md").exists())

    def test_unmapped_archive_report_and_stale_links_are_reported(self):
        self.write("docs/project/archive/investigations/stray.md", "# Stray\n")
        self.git("mv", "docs/investigations/beta.md", "docs/project/archive/investigations/beta.md")
        result = self.theseus("docs", "fold-check", check=False)
        self.assertIn("stray.md has no FOLD-MAP.tsv row", result.stdout + result.stderr)
        self.assertIn("stale link beta.md", result.stdout + result.stderr)



class ExternalIndexTests(Sandbox):
    def test_a_project_generator_replaces_the_theseus_indexes(self):
        self.scaffold()
        self.write("tools/gen.sh", "#!/bin/sh\necho generated > docs/own-index.md\n")
        self.write("tools/check.sh", "#!/bin/sh\ngrep -q generated docs/own-index.md\n")
        self.set_config('[docs]\nindex_command = ["sh tools/gen.sh"]\ncheck_command = ["sh tools/check.sh"]\n'
                        'generated = ["docs/own-index.md"]\n')
        failed = self.theseus("docs", "check", check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("check_command", failed.stdout + failed.stderr)
        self.assertIn("docs/own-index.md", self.theseus("docs", "index").stdout)
        self.theseus("docs", "check")
        from theseus import config as cfg, docs
        self.assertEqual(["docs/own-index.md"], docs.generated_paths(cfg.load(root=self.project)))


if __name__ == "__main__":
    unittest.main()
