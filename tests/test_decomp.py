import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import decomp, pdb
from tests.test_pdb import build_pdb
from tests.test_pe import build_pe

# The function is at RVA 0x1010: call [rip -> IAT slot 0x2040]; lea rcx, [rip -> u"Hello, loading"]; ret
STRING_RVA = 0x2180  # in .idata, the fixture's data section (file offset 0x1400 + 0x180)
CODE = b"\xcc" * 0x10 + b"\xff\x15" + struct.pack("<i", 0x2040 - 0x1016)
CODE += b"\x48\x8d\x0d" + struct.pack("<i", STRING_RVA - 0x101d) + b"\xc3"


def build_dll():
    image = bytearray(build_pe(CODE))
    text = "Hello, loading".encode("utf-16-le") + b"\0\0"
    image[0x1580:0x1580 + len(text)] = text
    return bytes(image)


class DecompTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.dll = self.dir / "Game-Screen-Win64-Shipping.dll"
        self.dll.write_bytes(build_dll())
        self.dll.with_suffix(".pdb").write_bytes(build_pdb())

    def test_functions_are_deduplicated_with_their_locals(self):
        fns = pdb.functions(self.dll.with_suffix(".pdb"))
        self.assertEqual(len(fns), len({(f.name, f.segment, f.offset) for f in fns}))
        self.assertEqual(fns[0].locals, ["Args", "Slot"])

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_disassembly_names_imports_and_strings(self):
        m = decomp.Module(self.dll)
        f = m.function("FGScreen::Construct")
        text = m.disassemble(f, length=14)
        self.assertIn("KERNEL32.dll!ExitProcess", text)
        self.assertIn('u"Hello, loading"', text)
        self.assertTrue(text.splitlines()[0].startswith("; FGScreen::Construct"))
        self.assertIn("; void FGScreen::Construct(char*)", text)
        self.assertIn("; locals: int Args, int Slot", text)

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_jumps_inside_the_function_read_as_offsets(self):
        data = bytearray(self.dll.read_bytes())
        data[0x410:0x412] = b"\xeb\xfe"  # jmp to itself, at the function's start (file offset 0x400 + 0x10)
        self.dll.write_bytes(bytes(data))
        m = decomp.Module(self.dll)
        self.assertRegex(m.disassemble(m.function("FGScreen::Construct"), length=2), r"jmp\s+\+00000")

    def test_strings_only_in_data_and_literal_symbols_show_the_text(self):
        m = decomp.Module(self.dll)
        self.assertIsNone(m.string_at(0x1010))  # code, which can decode as UTF-16, is never a string
        m.names[STRING_RVA] = "??_C@_1BM@JIAFBGPB@?$AAH?$AAe@"
        self.assertEqual(m.label(STRING_RVA), 'u"Hello, loading"')

    def test_game_functions_select_by_source_folder(self):
        m = decomp.Module(self.dll)
        self.assertEqual([f.name for f in m.game_functions("\\Game\\Source\\")], ["FGScreen::Construct"])
        self.assertEqual([f.name for f in m.game_functions()], ["FGScreen::Construct"])  # D:\\Game\\Source\\...
        self.assertFalse(decomp._is_game_source("D:\\W\\Engine\\Source\\Runtime\\Core\\Array.h"))
        self.assertFalse(decomp._is_game_source("D:\\W\\Game\\Intermediate\\Build\\UHT\\Screen.gen.cpp"))

    def test_dump_refuses_a_folder_inside_this_repository(self):
        with self.assertRaises(decomp.Fail):
            decomp.dump(self.dll, Path(__file__).resolve().parent / "out", "\\Game\\Source\\")

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_dump_writes_one_folder_per_source_file(self):
        out = self.dir / "work"
        decomp.dump(self.dll, out, "\\Game\\Source\\")
        index = (out / "Game-Screen" / "functions.tsv").read_text()
        self.assertIn("FGScreen::Construct\t0x1010\t64\tScreen.cpp\t20\t27", index)
        self.assertTrue((out / "Game-Screen" / "Screen.cpp" / "FGScreen__Construct.asm").is_file())


if __name__ == "__main__":
    unittest.main()
