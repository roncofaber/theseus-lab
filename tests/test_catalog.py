"""Catalog lookups against a synthetic decompiled corpus written by the test itself (never real original content)."""
import hashlib
import json
import unittest

from helpers import Sandbox

FUNCTIONS = {
    "main": (0x401000, 0x40103f, "int main(void) {\n  init_device();\n  FUN_00401100();\n  return 0;\n}\n"),
    "init_device": (0x401040, 0x4010ff, "// limitations: synthetic\nvoid init_device(void) {\n  send_frame(0x12);\n}\n"),
    "FUN_00401100": (0x401100, 0x40117f, "void FUN_00401100(void) {\n  send_frame(0x34);\n}\n"),
    "send_frame": (0x401180, 0x4011ff, "void send_frame(int x) {\n  return;\n}\n"),
}


class CorpusSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        binary = self.tmp / "original/app.exe"
        binary.parent.mkdir()
        binary.write_bytes(b"MZ synthetic test binary")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        self.write("theseus.local.toml", f'[re]\ntargets = [{{ name = "app", path = "{binary}", sha256 = "{digest}" }}]\n')
        corpus = self.tmp / "data/re" / digest[:16] / "decompiled"
        entries = []
        for name, (start, end, body) in FUNCTIONS.items():
            relative = f"functions/0x{start:08x}.c"
            self.write(relative, body, root=corpus)
            entries.append({"name": name, "address": f"0x{start:08x}", "status": "written", "relative_path": relative,
                            "body": {"ranges": [{"start": f"0x{start:08x}", "end": f"0x{end:08x}"}]}})
        self.write("decompile-index.json", json.dumps({"functions": entries}), root=corpus)
        self.write("exports.tsv", "vaddr\towner\tmember\tdemangled\tmangled\n0x00401180\t\t\tsend_frame\tsend_frame\n", root=corpus.parent)
        self.write("strings.tsv", "vaddr\tsection\ttype\tstring\n0x00405000\t.rdata\tascii\tDevice not found\n", root=corpus.parent)


class CatalogTests(CorpusSandbox):
    def test_show_finds_the_function_containing_an_address(self):
        out = self.theseus("re", "show", "0x401050").stdout
        self.assertIn("void init_device", out)
        self.assertNotIn("limitations", out)
        self.assertIn("limitations", self.theseus("re", "show", "FUN_00401050", "--header").stdout)
        self.assertNotEqual(0, self.theseus("re", "show", "0x500000", check=False).returncode)

    def test_call_graph(self):
        self.assertIn("4 call edges", self.theseus("re", "calls").stdout)
        callers = self.theseus("re", "callers", "send_frame").stdout
        self.assertIn("0x00401040\tinit_device", callers)
        self.assertIn("0x00401100\tFUN_00401100", callers)
        callees = self.theseus("re", "callees", "0x401000").stdout
        self.assertEqual(["0x00401040\tinit_device", "0x00401100\tFUN_00401100"], callees.splitlines())

    def test_find_searches_exports_strings_and_the_citation_index(self):
        self.write("docs/investigations/frames.md", "# Frames\n\nTask `frames` on base x.\n\nsend_frame lives at 0x00401180.\n")
        self.theseus("docs", "index")
        out = self.theseus("re", "find", "0x401180").stdout
        self.assertIn("[export] 0x00401180", out)
        self.assertIn("[index] | address | 0x00401180", out)
        self.assertIn("[string]", self.theseus("re", "find", "device not").stdout)
        self.assertEqual(1, self.theseus("re", "find", "nothing-matches-this", check=False).returncode)

    def test_rea_wrapper_answers_single_decompiles_from_the_corpus(self):
        env = dict(self.env, PATH="/usr/bin:/bin")
        out = self.theseus("re", "rea", "decompile", "ram:0x00401100", "--provider", "ghidra", env=env)
        self.assertIn("FUN_00401100", out.stdout)
        self.assertIn("answering from the decompiled corpus", out.stderr)



def synthetic_pe(machine):
    header = bytearray(0x100)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    header[0x80:0x84] = b"PE\0\0"
    header[0x84:0x86] = machine.to_bytes(2, "little")
    return bytes(header)


class InventoryTests(CorpusSandbox):
    def test_inventory_records_only_executable_images(self):
        root = self.tmp / "unpacked"
        self.write("drivers/driver.sys", "", root=root).write_bytes(synthetic_pe(0x014C))
        self.write("app64.dll", "", root=root).write_bytes(synthetic_pe(0x8664))
        self.write("tool", "", root=root).write_bytes(b"\x7fELF\x02\x01" + bytes(10) + b"\x02\x00" + (62).to_bytes(2, "little") + bytes(40))
        self.write("readme.txt", "not a binary\n", root=root)
        out = self.theseus("re", "inventory", root, "--relative-to", root, "--out", self.tmp / "inv/binaries.csv").stdout
        self.assertIn("3 executable images", out)
        rows = (self.tmp / "inv/binaries.csv").read_text().splitlines()
        self.assertEqual(4, len(rows))
        text = "\n".join(rows)
        self.assertIn("drivers/driver.sys,256,", text)
        self.assertIn(",PE,i386,32,", text)
        self.assertIn(",PE,x86-64,64,", text)
        self.assertIn(",ELF,x86-64,64,", text)
        self.assertTrue((self.tmp / "inv/binaries.md").exists())

    def test_status_reports_what_each_target_already_has(self):
        out = self.theseus("re", "status").stdout
        self.assertRegex(out, r"app\s+file ok\s+catalog built\s+corpus 4 functions\s+calls -")
        self.theseus("re", "calls")
        self.assertRegex(self.theseus("re", "status").stdout, r"calls built")
if __name__ == "__main__":
    unittest.main()
