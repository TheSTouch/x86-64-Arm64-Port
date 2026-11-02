import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import coff, decomp
from portkit.util import Fail
from tests.test_decomp import build_dll
from tests.test_pdb import build_pdb

CONSTRUCT = "?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z"
BIGOBJ_CLASS = bytes.fromhex("c7a1bad1eebaa94baf20faf66aa4dcb8")


def shipped_body():
    """The fixture DLL's FGScreen::Construct (0x40 bytes at RVA 0x1010), as the compiler leaves it in an
    object file: the call's and the lea's displacements are relocations, zero until the linker fills them."""
    code = b"\xff\x15\0\0\0\0" + b"\x48\x8d\x0d\0\0\0\0" + b"\xc3"
    return code + b"\0" * (0x40 - len(code))


def build_obj(functions, bigobj=False):
    """A COFF object, one COMDAT .text$mn section per function. functions: [(name, code, [(offset, type)])];
    relocations point at an external symbol. A long name goes to the string table."""
    nsec = len(functions)
    header_size = 56 if bigobj else 20
    sym_size = 20 if bigobj else 18
    sections, raw, relocs = b"", b"", b""
    data_at = header_size + 40 * nsec
    blobs = []
    for name, code, rels in functions:
        blobs.append((code, b"".join(struct.pack("<IIH", off, 0, kind) for off, kind in rels)))
    offset = data_at
    for code, rel in blobs:
        sections += struct.pack("<8sIIIIIIHHI", b".text$mn", 0, 0, len(code), offset, offset + len(code), 0,
                                len(rel) // 10, 0, 0x60501020)   # code, COMDAT, align 16, execute, read
        offset += len(code) + len(rel)
        raw += code + rel
    strings = b""
    symbols = []

    def symbol(name, value, section, kind, storage, aux=0):
        nonlocal strings
        encoded = name.encode()
        if len(encoded) <= 8:
            field = encoded.ljust(8, b"\0")
        else:
            field = struct.pack("<II", 0, 4 + len(strings))
            strings += encoded + b"\0"
        if bigobj:
            return field + struct.pack("<IiHBB", value, section, kind, storage, aux)
        return field + struct.pack("<IhHBB", value, section, kind, storage, aux)

    symbols.append(symbol("__imp_X", 0, 0, 0, 2))                    # the relocations' target
    for i, (name, code, rels) in enumerate(functions, 1):
        symbols.append(symbol(".text$mn", 0, i, 0, 3, 1))            # the section symbol and its aux record
        symbols.append(struct.pack("<IHHIHB", len(code), len(rels), 0, 0, 0, 2).ljust(sym_size, b"\0"))
        symbols.append(symbol("$LN3", 0, i, 0x20, 6))                # a label: not a function
        symbols.append(symbol(name, 0, i, 0x20, 2))
    table = data_at + len(raw)
    if bigobj:
        head = struct.pack("<HHHHI16sIIIIIII", 0, 0xFFFF, 2, 0x8664, 0, BIGOBJ_CLASS, 0, 0, 0, 0,
                           nsec, table, len(symbols))
    else:
        head = struct.pack("<HHIIIHH", 0x8664, nsec, 0, table, len(symbols), 0, 0)
    return head + sections + raw + b"".join(symbols) + struct.pack("<I", 4 + len(strings)) + strings


class CoffTest(unittest.TestCase):
    def test_functions_their_code_and_relocations(self):
        for bigobj in (False, True):
            obj = coff.parse(build_obj([(CONSTRUCT, shipped_body(), [(2, 4), (9, 4)])], bigobj=bigobj))
            self.assertEqual(obj.machine, 0x8664)
            (f,) = obj.functions()                     # the label and the section symbol are not functions
            self.assertEqual(f.name, CONSTRUCT)
            self.assertEqual(f.code, shipped_body())
            self.assertEqual(f.masked, {2, 3, 4, 5, 9, 10, 11, 12})

    def test_not_an_object(self):
        with self.assertRaises(coff.NotCOFF):
            coff.parse(b"MZ" + b"\0" * 100)


class MatchTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.dll = self.dir / "Game-Screen-Win64-Shipping.dll"
        self.dll.write_bytes(build_dll())
        self.dll.with_suffix(".pdb").write_bytes(build_pdb())

    def match(self, functions, bigobj=False):
        obj = self.dir / "Screen.cpp.obj"
        obj.write_bytes(build_obj(functions, bigobj))
        return {r.name: r for r in decomp.match(self.dll, obj)}

    def test_same_code_with_relocated_operands_masked_matches(self):
        for bigobj in (False, True):
            r = self.match([(CONSTRUCT, shipped_body(), [(2, 4), (9, 4)])], bigobj)[CONSTRUCT]
            self.assertEqual(r.state, "matched")
            self.assertEqual(r.shipped, "FGScreen::Construct")
            self.assertEqual(r.length, 0x40)

    def test_an_unmasked_displacement_differs(self):
        r = self.match([(CONSTRUCT, shipped_body(), [(2, 4)])])[CONSTRUCT]   # the lea's relocation is missing
        self.assertEqual(r.state, "differs")
        self.assertEqual(r.offset, 9)

    def test_first_difference_is_reported(self):
        body = bytearray(shipped_body())
        body[0x0d] = 0x90          # nop where the shipped code returns
        body[0x20] = 0xcc
        r = self.match([(CONSTRUCT, bytes(body), [(2, 4), (9, 4)])])[CONSTRUCT]
        self.assertEqual((r.state, r.offset), ("differs", 0x0d))
        self.assertIn("c3", r.detail)      # the shipped byte
        self.assertIn("90", r.detail)      # ours
        if shutil.which("objdump"):
            self.assertIn("ret", r.detail)  # the shipped instruction at that offset

    def test_a_different_length_differs(self):
        r = self.match([(CONSTRUCT, shipped_body()[:0x30], [(2, 4), (9, 4)])])[CONSTRUCT]
        self.assertEqual(r.state, "differs")
        self.assertEqual(r.offset, 0x30)
        self.assertIn("0x30 bytes", r.detail)

    def test_matched_scores_zero(self):
        r = self.match([(CONSTRUCT, shipped_body(), [(2, 4), (9, 4)])])[CONSTRUCT]
        self.assertEqual(r.score, 0)
        self.assertTrue(decomp.match_line(r).endswith(" score 0"))
        missing = self.match([("?Unknown@@YAXXZ", b"\xc3", [])])["?Unknown@@YAXXZ"]
        self.assertTrue(decomp.match_line(missing).endswith(" score -"))

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_every_difference_side_by_side(self):
        # a nop inserted after the call: the rest shifts by one byte but still aligns
        code = shipped_body()
        code = code[:6] + b"\x90" + code[6:] + b""
        obj = self.dir / "Screen.cpp.obj"
        obj.write_bytes(build_obj([(CONSTRUCT, code, [(2, 4), (10, 4)])]))
        (r,) = decomp.match(self.dll, obj, diff=["FGScreen::Construct"])
        self.assertEqual((r.state, r.score), ("differs", 1))
        marks = [row[0] for row in r.rows]
        self.assertEqual(marks[:4], ["=", ">", "=", "="])
        self.assertEqual(marks.count("="), len(marks) - 1)
        self.assertEqual(r.rows[0][1:], ((0, "call qword ptr [rip + <reloc>]"), (0, "call qword ptr [rip + <reloc>]")))
        self.assertEqual(r.rows[1][1:], (None, (6, "nop")))
        self.assertEqual(r.rows[2][1:], ((6, "lea rcx, [rip + <reloc>]"), (7, "lea rcx, [rip + <reloc>]")))
        listing = decomp.diff_listing(r)
        self.assertIn("> " + " " * 60 + "+00006  nop", listing)
        self.assertTrue(listing.rstrip().endswith("score 1"))
        self.assertTrue(decomp.match_line(r).endswith(" score 1"))

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_a_changed_instruction_scores_one(self):
        body = bytearray(shipped_body())
        body[8] = 0x15          # lea rdx instead of lea rcx
        r = self.match([(CONSTRUCT, bytes(body), [(2, 4), (9, 4)])])[CONSTRUCT]
        self.assertEqual((r.state, r.score), ("differs", 1))
        self.assertEqual([row[0] for row in r.rows].count("!"), 1)

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_jumps_inside_the_function_compare_by_displacement(self):
        jumps = b"\x74\x02" + b"\x90\x90" + b"\xc3"     # je over two nops
        shifted = b"\x90" + jumps                          # one more instruction before: same jump bytes
        keys = [decomp._normalize(t, o, n, len(jumps), None)[1] for o, n, t in decomp._disassemble([jumps])[0]]
        shifted_keys = [decomp._normalize(t, o, n, len(shifted), None)[1]
                        for o, n, t in decomp._disassemble([shifted])[0]]
        self.assertEqual(keys, shifted_keys[1:])
        self.assertEqual(decomp._normalize("je\t0x4 <f0+0x4>", 0, 2, 5, None)[0], "je +00004")
        self.assertEqual(decomp._normalize("call\t0x7f00", 0, 5, 5, None)[0], "call <reloc>")

    def test_a_function_the_module_does_not_have_is_missing(self):
        r = self.match([("?Unknown@@YAXXZ", b"\xc3", [])])["?Unknown@@YAXXZ"]
        self.assertEqual(r.state, "missing")

    def test_falls_back_to_the_qualified_name(self):
        # No public symbol has this decoration; FGScreen::Construct is the only function of that name.
        name = "?Construct@FGScreen@@QEAAXXZ"
        r = self.match([(name, shipped_body(), [(2, 4), (9, 4)])])[name]
        self.assertEqual((r.state, r.shipped), ("matched", "FGScreen::Construct"))
        self.assertIn("by name", r.detail)

    def test_qualified_names(self):
        self.assertEqual(decomp.qualified_name(CONSTRUCT), "FGScreen::Construct")
        self.assertEqual(decomp.qualified_name("?CreateBrushFromPng@@YAXAEAVFSlateStyleSet@@PEBD1W4Type@ESlateBrushTileType@@@Z"),
                         "CreateBrushFromPng")
        self.assertIsNone(decomp.qualified_name("??0FName@@QEAA@PEB_WW4EFindName@@@Z"))   # operators, constructors
        self.assertIsNone(decomp.qualified_name("main"))

    def test_not_an_object_file(self):
        bad = self.dir / "x.obj"
        bad.write_bytes(b"not an object")
        with self.assertRaises(Fail):
            decomp.match(self.dll, bad)


if __name__ == "__main__":
    unittest.main()
