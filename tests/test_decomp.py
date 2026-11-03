import io
import re
import shutil
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from portkit import cli, decomp, pdb
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
    def test_fields_named_from_the_object_when_this_is_a_secondary_base(self):
        # Construct overrides a virtual of ISave, Screen's base at +0x58: `this` points there, in rcx at entry
        # and in r14 from +0xb (the PDB's range); members are named by their offset in Screen
        code = bytes.fromhex(
            "488b41e8"          # +00 mov rax, qword ptr [rcx - 0x18]: 0x40
            "0fb641f8"          # +04 movzx eax, byte ptr [rcx - 0x8]: 0x50
            "4989ce"            # +08 mov r14, rcx
            "418b56cc"          # +0b mov edx, dword ptr [r14 - 0x34]: 0x24
            "ff151b100000"      # +0f call [rip -> IAT slot 0x2040]: ExitProcess
            "c3")               # +15 ret
        data = bytearray(self.dll.read_bytes())
        data[0x410:0x410 + len(code)] = code
        self.dll.write_bytes(bytes(data))
        self.dll.with_suffix(".pdb").write_bytes(build_pdb(fields=True, adjust=0x58))
        m = decomp.Module(self.dll)
        f = m.function("FGScreen::Construct")
        self.assertEqual(m.types.this_adjust(f.type_index), (0x1004, 0x58))
        self.assertEqual(m.types.base_at(0x1004, 0x58), "ISave")
        variables = {v.name: v for v in m.details.detail(f).variables}
        self.assertEqual((variables["this"].inlined, variables["Inlined"].inlined), (False, True))
        text = m.disassemble(f, length=len(code))
        self.assertIn("; this is Screen's ISave base (+0x58)\n", text)
        notes = {line.split()[0]: line.partition(" ; ")[2] for line in text.splitlines() if line.startswith("  +")}
        self.assertEqual(notes, {"+00000": "this->mWidget", "+00004": "this->mType", "+00008": "",
                                 "+0000b": "this->mPair.B", "+0000f": "KERNEL32.dll!ExitProcess", "+00015": ""})
        # a function whose `this` is the object itself: no line, members as they were
        self.dll.with_suffix(".pdb").write_bytes(build_pdb(fields=True))
        self.assertNotIn("; this is", decomp.Module(self.dll).disassemble(f, length=len(code)))

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_several_functions_in_one_run(self):
        # --disasm and --by-line repeat; the module is read once for all of them
        out = io.StringIO()
        with mock.patch.object(decomp, "Module", wraps=decomp.Module) as read, redirect_stdout(out):
            cli.main(["decomp", str(self.dll), "--disasm", "FGScreen::Construct", "--disasm", "`anonymous'::Helper"])
        self.assertEqual(read.call_count, 1)
        text = out.getvalue()
        self.assertTrue(text.startswith("; FGScreen::Construct\n"))
        self.assertIn("\n\n; `anonymous'::Helper\n", text)   # a blank line between functions
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["decomp", str(self.dll), "--by-line", "FGScreen::Construct", "--by-line", "FGScreen::Construct"])
        first, second = out.getvalue().split("\n\n")
        self.assertTrue(first.startswith("; FGScreen::Construct\n") and second.startswith("; FGScreen::Construct\n"))

    def test_the_bits_an_instruction_reaches(self):
        self.assertEqual(decomp._mask("test", "byte ptr [rbx + 0x2b], 0x20", 1), 0x20)
        self.assertEqual(decomp._mask("or", "byte ptr [rbx + 0x2b], 0x3", 1), 0x3)
        self.assertEqual(decomp._mask("xor", "byte ptr [rbx], 0x80", 1), 0x80)
        self.assertEqual(decomp._mask("and", "byte ptr [rbx + 0x2b], 0xdf", 1), 0x20)    # the bits it clears
        self.assertEqual(decomp._mask("and", "dword ptr [rbx], 0xfffffeff", 4), 0x100)
        self.assertEqual(decomp._mask("and", "dword ptr [rbx], -0x101", 4), 0x100)
        self.assertEqual(decomp._mask("bt", "dword ptr [rbx], 0x9", 4), 0x200)
        self.assertEqual(decomp._mask("btr", "word ptr [rbx], 0x11", 2), 0x2)
        self.assertIsNone(decomp._mask("test", "byte ptr [rbx], al", 1))                # no immediate
        self.assertIsNone(decomp._mask("cmp", "byte ptr [rbx], 0x20", 1))               # a value, not bits
        self.assertIsNone(decomp._mask("test", "al, 0x20", None))

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

    def test_log_records_read_as_their_format_line_and_verbosity(self):
        data = bytearray(self.dll.read_bytes())
        base = 0x140000000
        struct.pack_into("<QQii", data, 0x1400 + 0x1c0, base + STRING_RVA, base + 0x21e0, 859, 3)
        data[0x1400 + 0x1e0:0x1400 + 0x1eb] = b"Screen.cpp\0"
        self.dll.write_bytes(bytes(data))
        m = decomp.Module(self.dll)
        self.assertEqual(m.label(0x21c0), 'log Warning, line 859: u"Hello, loading"')
        struct.pack_into("<i", data, 0x1400 + 0x1d4, 9)  # no such verbosity: not a record
        self.dll.write_bytes(bytes(data))
        self.assertIsNone(decomp.Module(self.dll).label(0x21c0))

    def test_globals_named_by_the_dynamic_initializer_that_writes_them(self):
        # `dynamic initializer for 'GScreenName'' at RVA 0x1080: sub rsp, 0x28; lea rdx, [string];
        # lea rcx, [rip -> 0x2100]; call [rip -> IAT]
        code = bytes.fromhex("4883ec28") + b"\x48\x8d\x15" + struct.pack("<i", STRING_RVA - 0x108b)
        code += b"\x48\x8d\x0d" + struct.pack("<i", 0x2100 - 0x1092) + b"\xff\x15" + struct.pack("<i", 0x2040 - 0x1098)
        data = bytearray(self.dll.read_bytes())
        data[0x480:0x480 + len(code)] = code
        self.dll.write_bytes(bytes(data))
        m = decomp.Module(self.dll)
        m.functions.append(pdb.Function("`dynamic initializer for 'GScreenName''", 1, 0x80, len(code), "a.obj"))
        self.assertEqual(m.label(0x2100), "GScreenName")
        self.assertEqual(m.label(0x2108), "GScreenName+0x8")       # FString's ArrayNum
        self.assertIsNone(m.label(0x2110))                         # past an FString

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_callers_from_the_module_itself(self):
        data = bytearray(self.dll.read_bytes())
        # `anonymous'::Helper (RVA 0x1060) calls FGScreen::Construct (0x1010)
        data[0x460:0x465] = b"\xe8" + struct.pack("<i", 0x1010 - 0x1065)
        # inside Construct, a mov rax, imm64 whose bytes read as a call to Helper: not an instruction there
        data[0x430:0x43a] = b"\x48\xb8\xe8" + struct.pack("<i", 0x1060 - 0x1037) + b"\0\0\0"
        self.dll.write_bytes(bytes(data))
        m = decomp.Module(self.dll)
        rows = decomp.callers(m, "FGScreen::Construct")
        self.assertEqual([(f.name, kind, site, same) for f, kind, site, same in rows],
                         [("`anonymous'::Helper", "call", 0x1060, True)])
        self.assertEqual(decomp.callers(m, "`anonymous'::Helper"), [])
        # an import: Construct calls ExitProcess through the import address table
        self.assertEqual([(f.name, kind, site, same) for f, kind, site, same in decomp.callers(m, "ExitProcess")],
                         [("FGScreen::Construct", "call", 0x1010, None)])

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_by_line_of_the_functions_own_source(self):
        data = bytearray(self.dll.read_bytes())
        data[0x420:0x427] = bytes.fromhex("f6412b20") + b"\xc3\x90\x90"  # +0x10: test byte ptr [rcx + 0x2b], 0x20
        self.dll.write_bytes(bytes(data))
        m = decomp.Module(self.dll)
        self.assertEqual(decomp.by_line(m, m.function("FGScreen::Construct")), [
            (20, ["call ExitProcess", "inl Screen::Reset", 'u"Hello, loading"', "inl Detail::Clear"]),
            (21, ["cmp 0x20"])])
        self.assertEqual(re.match(r";\s*> inlined (.+?)(?: \([^()]+:\d+\))?$",
                                  "; > inlined TFunction<void __cdecl(int)>::TFunction (Function.h:12)").group(1),
                         "TFunction<void __cdecl(int)>::TFunction")

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
        decomp.dump(self.dll, out)
        index = (out / "Game-Screen" / "functions.tsv").read_text()
        self.assertTrue(index.startswith("name\trva\tlength\tsource\tfirst_line\tlast_line\tlocals\tmangled\tspan\n"))
        self.assertIn("FGScreen::Construct\t0x1010\t64\tScreen.cpp\t20\t27", index)
        self.assertIn("\tD:\\Game\\Source\\Game\\Private\\Screen.cpp:20-27\n", index)  # the span, last
        self.assertTrue((out / "Game-Screen" / "Screen.cpp" / "FGScreen__Construct.asm").is_file())

    def test_dump_of_one_source_keeps_the_module_wide_list(self):
        # --source F writes its own list beside the module's; a second run replaces its files, no NAME@rva copies
        out = self.dir / "work"
        decomp.dump(self.dll, out)
        whole = (out / "Game-Screen" / "functions.tsv").read_text()
        for _ in range(2):
            decomp.dump(self.dll, out, "Screen.cpp")
        self.assertEqual((out / "Game-Screen" / "functions.tsv").read_text(), whole)
        self.assertIn("FGScreen::Construct", (out / "Game-Screen" / "functions.Screen.cpp.tsv").read_text())
        self.assertEqual(sorted(p.name for p in (out / "Game-Screen" / "Screen.cpp").iterdir()),
                         ["FGScreen__Construct.asm"])


class FamiliesTest(unittest.TestCase):
    def test_shape_keeps_mnemonics_and_registers(self):
        self.assertEqual(decomp._shape("mov\trax, qword ptr [rcx + 0x408]"), "mov rax, qword ptr [rcx + #]")
        self.assertEqual(decomp._shape("mov\trax, qword ptr [rbp - 0x10]"), "mov rax, qword ptr [rbp + #]")
        self.assertEqual(decomp._shape("call\t0x180bf8a50 <foo>"), "call #")
        self.assertEqual(decomp._shape("lea\trdi, [rbx + 8*rax]"), "lea rdi, [rbx + 8*rax]")

    def test_exact_and_near_groups(self):
        body = [f"op{i % 7} r{i % 3}" for i in range(20)]
        near = body[:10] + ["other"] + body[11:]                 # 19 of 20 the same
        far = body[:14] + ["x"] * 6                               # 14 of 20
        items = [(100, tuple(body)), (100, tuple(body)), (104, tuple(near)), (100, tuple(far)),
                 (200, tuple(body)), (6, ("ret",)), (6, ("ret",))]
        # 200 bytes: the same instructions, but outside 10 % of the others' length
        groups = decomp._groups(items)
        # 0, 1 and 4 the same; 2 near 0 and 1 (95 %, 4 % longer): one near family; 3 alone (70 %)
        self.assertEqual(sorted((sorted(g), k) for g, k in groups), [([0, 1, 2, 4], "near"), ([5, 6], "exact")])
        groups = decomp._groups(items[:4])
        self.assertEqual([(sorted(g), k) for g, k in groups], [([0, 1, 2], "near")])
        self.assertEqual(decomp._groups(items[:4], near=False), [([0, 1], "exact")])

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_families_of_a_module(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d) / "Game-Screen-Win64-Shipping.dll"
            data = bytearray(build_dll())
            data[0x480:0x48e] = data[0x410:0x41e]                # TScreen<int>::Construct: the same code
            data[0x482] ^= 0x40                                  # another displacement
            dll.write_bytes(bytes(data))
            dll.with_suffix(".pdb").write_bytes(build_pdb(twin=True))
            (family,) = decomp.families(dll)
            self.assertEqual(family.kind, "exact")
            self.assertEqual([(names, where) for _, _, names, where in family.members],
                             [(("FGScreen::Construct",), "Screen.cpp:20"), (("TScreen<int>::Construct",), "Screen.cpp:20")])
            self.assertEqual(family.bytes, 0x80)
            tsv = Path(d) / "t.tsv"
            tsv.write_text("1\t1\t00001010\n")
            self.assertEqual(decomp.families(dll, [tsv]), [])     # only one of them was run


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
            b.write_text("3\t100\t00001080\n7\t100\t00001010\n")   # two traces: 0x1010 counted once
            rows, files = decomp.trace(dll, [a, b])
            self.assertEqual([r.rva for r in rows], [0x1010, 0x1080])
            self.assertEqual(rows[1].span, "D:\\Game\\Source\\Game\\Private\\Screen.cpp:20-27")
            # FGScreen::Construct and TScreen<int>::Construct come from the same lines: 2 functions, 1 span
            self.assertEqual(files, [("Screen.cpp", 2, 0x80, 5, 1, 0x40)])
