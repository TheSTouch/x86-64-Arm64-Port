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


# Construct's code for build_pdb(fields=True): this in rcx at entry, then Widget in rax (+0x14..+0x1e)
FIELDS_CODE = bytes.fromhex(
    "488b01"            # +00 mov rax, qword ptr [rcx]
    "8b5114"            # +03 mov edx, dword ptr [rcx + 0x14]
    "8b5124"            # +06 mov edx, dword ptr [rcx + 0x24]
    "488d5120"          # +09 lea rdx, [rcx + 0x20]
    "488b8140000000"    # +0d mov rax, qword ptr [rcx + 0x40]
    "0fb64950"          # +14 movzx ecx, byte ptr [rcx + 0x50]
    "8b4808"            # +18 mov ecx, dword ptr [rax + 0x8]
    "8b4a08"            # +1b mov ecx, dword ptr [rdx + 0x8]
    "0fb6442470"        # +1e movzx eax, byte ptr [rsp + 0x70]
    "8b4c2428"          # +23 mov ecx, dword ptr [rsp + 0x28]
    "8b4108"            # +27 mov eax, dword ptr [rcx + 0x8]
    "89442470"          # +2a mov dword ptr [rsp + 0x70], eax: Copy's last instruction there
    "c3")               # +2e ret


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
    def test_source_lines_and_inline_sites_between_instructions(self):
        m = decomp.Module(self.dll)
        body = m.disassemble(m.function("FGScreen::Construct"), length=0x12).split("; locals: int Args, int Slot\n")[1]
        listing = [line if line.startswith(";") else line.split()[0] for line in body.splitlines()]
        self.assertEqual(listing, [
            "; Screen.cpp:20", "+00000",
            "; > inlined Screen::Reset (Array.h:100)", ";   Array.h:101", "+00006",
            ";   > inlined Detail::Clear (Array.h:50)", ";     Array.h:51", "+0000d",
            ";   < Detail::Clear", ";   Array.h:102", "+0000e",
            "; < Screen::Reset", "; Screen.cpp:21", "+00010"])

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_fields_named_where_the_pdb_says_which_register_holds_what(self):
        data = bytearray(self.dll.read_bytes())
        data[0x410:0x410 + len(FIELDS_CODE)] = FIELDS_CODE
        self.dll.write_bytes(bytes(data))
        self.dll.with_suffix(".pdb").write_bytes(build_pdb(fields=True))
        m = decomp.Module(self.dll)
        notes = {}
        for line in m.disassemble(m.function("FGScreen::Construct"), length=len(FIELDS_CODE)).splitlines():
            if line.startswith("  +"):
                notes[line.split()[0]] = line.partition(" ; ")[2]
        self.assertEqual(notes, {
            "+00000": "this->__vfptr", "+00003": "this->mSlots[1]", "+00006": "this->mPair.B",
            "+00009": "&this->mPair", "+0000d": "this->mWidget", "+00014": "this->mType",
            "+00018": "Widget->Count",  # a register range from the PDB; Count is in the base class
            "+0001b": "",               # rdx "may have no name" there
            "+0001e": "Copy.mType",     # frame-relative: [rsp + 0x20] is Copy
            "+00023": "Slot",
            "+00027": "",               # rcx was written at +0x14: no longer this
            "+0002a": "",               # a store ending Copy's range: what it writes is no longer Copy
            "+0002e": ""})

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
        self.assertTrue(index.startswith("name\trva\tlength\tsource\tfirst_line\tlast_line\tlocals\tmangled\tspan\n"))
        self.assertIn("FGScreen::Construct\t0x1010\t64\tScreen.cpp\t20\t27", index)
        self.assertIn("\tD:\\Game\\Source\\Game\\Private\\Screen.cpp:20-27\n", index)  # the span, last
        self.assertTrue((out / "Game-Screen" / "Screen.cpp" / "FGScreen__Construct.asm").is_file())


if __name__ == "__main__":
    unittest.main()


class TraceTest(unittest.TestCase):
    def test_trace_rows_get_their_function_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d) / "Game-Screen-Win64-Shipping.dll"
            dll.write_bytes(build_dll())
            dll.with_suffix(".pdb").write_bytes(build_pdb())
            tsv = Path(d) / "RecompTrace.tsv"
            tsv.write_text("# first execution of each function\n5\t100\t00001010\n9\t200\t00009999\n")
            rows, files = decomp.trace(dll, tsv)
            self.assertEqual([(r.ms, r.thread, r.rva, r.name) for r in rows],
                             [(5, 100, 0x1010, "FGScreen::Construct"), (9, 200, 0x9999, "?")])
            self.assertEqual(rows[0].source, "Screen.cpp")
            # file, functions, bytes, first ms, distinct source spans, their bytes (each span once)
            self.assertEqual(files, [("Screen.cpp", 1, 0x40, 5, 1, 0x40)])

    def test_trace_counts_each_source_span_once(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d) / "Game-Screen-Win64-Shipping.dll"
            dll.write_bytes(build_dll())
            dll.with_suffix(".pdb").write_bytes(build_pdb(twin=True))
            a, b = Path(d) / "a.tsv", Path(d) / "b.tsv"
            a.write_text("5\t100\t00001010\n")
            b.write_text("3\t100\t00001050\n7\t100\t00001010\n")   # two traces: 0x1010 counted once
            rows, files = decomp.trace(dll, [a, b])
            self.assertEqual([r.rva for r in rows], [0x1010, 0x1050])
            self.assertEqual(rows[1].span, "D:\\Game\\Source\\Game\\Private\\Screen.cpp:20-27")
            # FGScreen::Construct and TScreen<int>::Construct come from the same lines: 2 functions, 1 span
            self.assertEqual(files, [("Screen.cpp", 2, 0x60, 5, 1, 0x40)])
