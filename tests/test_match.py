import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import coff, decomp, pdb
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
            self.assertEqual(f.targets, {2: "__imp_X", 9: "__imp_X"})

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
    def test_matched_scores_zero_with_its_listing_too(self):
        # the lea's displacement resolved by the assembler (no relocation), the shipped one masked as
        # rip-relative: the listing shows the line as changed, the bytes match all the same
        body = bytearray(shipped_body())
        obj = self.dir / "Screen.cpp.obj"
        data = bytearray(build_dll())
        body[9:13] = data[0x419:0x41d]
        obj.write_bytes(build_obj([(CONSTRUCT, bytes(body), [(2, 4)])]))
        (r,) = decomp.match(self.dll, obj, diff=[CONSTRUCT])
        self.assertEqual((r.state, r.score), ("matched", 0))
        self.assertIn("!", [row[0] for row in r.rows])

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_a_changed_instruction_scores_one(self):
        body = bytearray(shipped_body())
        body[8] = 0x15          # lea rdx instead of lea rcx
        r = self.match([(CONSTRUCT, bytes(body), [(2, 4), (9, 4)])])[CONSTRUCT]
        self.assertEqual((r.state, r.score), ("differs", 1))
        self.assertEqual([row[0] for row in r.rows].count("!"), 1)

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump (Xcode command line tools)")
    def test_verdicts_of_the_listing(self):
        body = bytearray(shipped_body())
        body[8] = 0x15          # lea rdx instead of lea rcx
        obj = self.dir / "Screen.cpp.obj"
        obj.write_bytes(build_obj([(CONSTRUCT, bytes(body), [(2, 4), (9, 4)])]))
        (r,) = decomp.match(self.dll, obj, diff=[CONSTRUCT])
        self.assertEqual(r.verdict, "registers only")
        self.assertIn("\nverdict: registers only\nscore 1", decomp.diff_listing(r))
        # ExitProcess's call replaced by code of its own: what the shipped module calls, ours has inline
        body = b"\x31\xc0" + b"\xb9\x01\0\0\0" + shipped_body()[6:]
        obj.write_bytes(build_obj([(CONSTRUCT, body[:0x40], [(10, 4)])]))
        (r,) = decomp.match(self.dll, obj, diff=[CONSTRUCT])
        self.assertEqual(r.verdict, "inlined here, called there: ExitProcess")
        # anything else: the first differing source line
        body = bytearray(shipped_body())
        body[0x0d] = 0x90
        obj.write_bytes(build_obj([(CONSTRUCT, bytes(body), [(2, 4), (9, 4)])]))
        (r,) = decomp.match(self.dll, obj, diff=[CONSTRUCT])
        self.assertEqual(r.verdict, "first difference at Screen.cpp:21, in inlined Screen::Reset; 1 instruction "
                                    "differs besides register allocation")

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

    def test_names_inside_a_function_scope(self):
        self.assertEqual(decomp.scoped_name("??R<lambda_1>@?1??Load@FGScreen@@QEAAXXZ@QEBA@H@Z"),
                         "`FGScreen::Load'::`2'::<lambda_1>::operator()")
        self.assertEqual(decomp.scoped_name("?ExecCheckImplInternal@Impl@?5??Save@FGScreen@@QEAAXH@Z@SAXXZ"),
                         "`FGScreen::Save'::`6'::Impl::ExecCheckImplInternal")
        self.assertEqual(decomp.scoped_name("??R<lambda_1>@?9???R<lambda_2>@?1??Load@FGScreen@@QEAAXXZ@QEBA@H@Z@QEBAXXZ"),
                         "``FGScreen::Load'::`2'::<lambda_2>::operator()'::`10'::<lambda_1>::operator()")
        self.assertEqual(decomp.scoped_name("?Run@Impl@?BA@??Save@FGScreen@@QEAAXH@Z@SAXXZ"),
                         "`FGScreen::Save'::`16'::Impl::Run")
        self.assertEqual(decomp.scoped_name("??1<lambda_3>@?1??Load@FGScreen@@QEAAXXZ@QEAA@XZ"),
                         "`FGScreen::Load'::`2'::<lambda_3>::~<lambda_3>")
        self.assertEqual(decomp.scoped_name("??__E?sNames@FGScreen@@2V?$TArray@H@@A@@YAXXZ"),
                         "`dynamic initializer for 'FGScreen::sNames''")
        self.assertEqual(decomp.scoped_name("??__EGScreens@@YAXXZ"), "`dynamic initializer for 'GScreens''")
        self.assertEqual(decomp.scoped_name("??__F?sNames@FGScreen@@2V?$TArray@H@@A@@YAXXZ"),
                         "`dynamic atexit destructor for 'FGScreen::sNames''")
        self.assertEqual(decomp.scoped_name("??_EFGScreen@@UEAAPEAXI@Z"), "FGScreen::`vector deleting destructor'")
        self.assertIsNone(decomp.scoped_name("??_EFGScreen@@WBA@EAAPEAXI@Z"))     # its adjustor thunk
        self.assertIsNone(decomp.scoped_name(CONSTRUCT))                          # no scope: the other rules
        self.assertIsNone(decomp.scoped_name("?Helper@?A0x1b2c3d4e@@YAXXZ"))      # an anonymous namespace
        self.assertIsNone(decomp.scoped_name("??$Get@H@?1??Load@FGScreen@@QEAAXXZ@QEAAXXZ"))  # a template

    def test_pairs_by_scope_when_unsure_not_at_all(self):
        def fn(name, offset):
            return pdb.Function(name, 1, offset, 8, "Module.Game.1.cpp.obj")
        shipped = [fn("`FGScreen::Load'::`2'::<lambda_1>::operator()", 0x10),
                   fn("`FGScreen::Load'::`3'::<lambda_2>::operator()", 0x20),
                   fn("`FGScreen::Save'::`6'::Impl::ExecCheckImplInternal", 0x30),
                   fn("`FGScreen::Save'::`9'::Impl::ExecCheckImplInternal", 0x40),
                   fn("`dynamic initializer for 'FGScreen::sNames''", 0x50),
                   fn("`FGScreen::Draw'::`2'::Impl::ExecCheckImplInternal", 0x60),
                   fn("`FGScreen::Draw'::`5'::Impl::ExecCheckImplInternal", 0x70)]
        names = ["??R<lambda_1>@?1??Load@FGScreen@@QEAAXXZ@QEBA@H@Z",          # the same name
                 "??R<lambda_2>@?4??Load@FGScreen@@QEAAXXZ@QEBA@H@Z",          # scope 5 there, 3 here: lambda_2
                 "?ExecCheckImplInternal@Impl@?6??Save@FGScreen@@QEAAXH@Z@SAXXZ",   # scopes 7 and 11: 1st, 2nd
                 "?ExecCheckImplInternal@Impl@?L@??Save@FGScreen@@QEAAXH@Z@SAXXZ",
                 "??__E?sNames@FGScreen@@2V?$TArray@H@@A@@YAXXZ",
                 "?ExecCheckImplInternal@Impl@?2??Draw@FGScreen@@QEAAXXZ@SAXXZ",    # one of two: which one?
                 "??R<lambda_3>@?1??Load@FGScreen@@QEAAXXZ@QEBA@H@Z"]           # none shipped
        pairs = decomp.pair_scoped(names, shipped)
        self.assertEqual({n: f.offset for n, f in pairs.items()},
                         {names[0]: 0x10, names[1]: 0x20, names[2]: 0x30, names[3]: 0x40, names[4]: 0x50})
        # a header's static: an initializer in every unity blob, the object's own blob decides
        shipped = [fn("`dynamic initializer for 'GScreens''", 0x10), fn("`dynamic initializer for 'GScreens''", 0x20)]
        shipped[1].compiland = "D:\\Game\\Intermediate\\Module.Game.2.cpp.obj"
        self.assertEqual(decomp.pair_scoped(["??__EGScreens@@YAXXZ"], shipped), {})
        pairs = decomp.pair_scoped(["??__EGScreens@@YAXXZ"], shipped, "Module.Game.2.cpp.obj")
        self.assertEqual(pairs["??__EGScreens@@YAXXZ"].offset, 0x20)

    def test_renamed_registers_and_stack_slots(self):
        theirs = [(0, "mov rax, qword ptr [rcx + 0x8]"), (4, "add eax, edx"), (6, "je +0000a"), (8, "nop"), (0xa, "ret")]
        ours = [(0, "mov r8, qword ptr [rcx + 0x8]"), (4, "add r8d, edx"), (7, "je +0000b"), (9, "nop"), (0xb, "ret")]
        self.assertEqual(decomp._renamed(theirs, ours), "registers only")
        self.assertIsNone(decomp._renamed([(0, "mov rax, rcx"), (3, "mov rdx, rcx")],
                                          [(0, "mov rax, rcx"), (3, "mov rax, rcx")]))   # rax and rdx both rax
        self.assertIsNone(decomp._renamed([(0, "mov eax, ecx")], [(0, "mov rax, rcx")]))  # another width
        self.assertIsNone(decomp._renamed([(0, "je +00002"), (2, "nop"), (3, "ret")],
                                          [(0, "je +00003"), (2, "nop"), (3, "ret")]))  # another target
        stack = [(0, "mov qword ptr [rsp + 0x20], rax"), (5, "mov rcx, qword ptr [rsp + 0x28]")]
        swapped = [(0, "mov qword ptr [rsp + 0x28], rax"), (5, "mov rcx, qword ptr [rsp + 0x20]")]
        self.assertEqual(decomp._renamed(stack, swapped), "stack slots only")
        both = [(0, "mov qword ptr [rsp + 0x28], rdx"), (5, "mov rcx, qword ptr [rsp + 0x20]")]
        self.assertEqual(decomp._renamed(stack, both), "registers and stack slots only")
        self.assertIsNone(decomp._renamed(stack, stack[:1]))

    def test_register_allocation_only(self):
        # this kept in rsi there, a callee-saved register spilled here: the same computation
        theirs = [(0, "mov rsi, rcx"), (3, "mov eax, dword ptr [rsi + 0x8]"), (6, "add rsi, 0x10"), (0xa, "ret")]
        ours = [(0, "mov qword ptr [rsp + 0x8], rbx"), (5, "mov eax, dword ptr [rcx + 0x8]"),
                (8, "lea rbx, [rcx + 0x10]"), (0xc, "nop"), (0xd, "ret")]
        self.assertEqual(decomp._allocation(theirs, ours), (True, None, 0))
        ours[1] = (5, "mov eax, dword ptr [rcx + 0xc]")      # another field: a difference of its own
        self.assertEqual(decomp._allocation(theirs, ours), (False, 3, 1))

    def test_inlined_on_one_side_called_on_the_other(self):
        get = (0x5000, "Widget::Get")
        theirs = [(0, "mov rcx, rbx", None), (3, "call <reloc>", get), (8, "test eax, eax", None)]
        ours = [(0, "mov rcx, rbx", None), (3, "mov eax, dword ptr [rcx + 0x8]", None), (6, "test eax, eax", None)]
        self.assertEqual(decomp._inlined(theirs, ours), "inlined here, called there: Widget::Get")
        # the other way round, when the PDB says what the shipped code has inline there
        site = pdb.InlineSite("Widget::Get", None, None, 0, [(3, 6, None, None)])
        self.assertEqual(decomp._inlined(ours, theirs, [site]),
                         "called here, inlined there: Widget::Get")
        self.assertIsNone(decomp._inlined(ours, theirs))          # no evidence: nothing
        cookie = [(0, "mov rcx, rbx", None), (3, "call <reloc>", (0x6000, "__security_check_cookie")),
                  (8, "test eax, eax", None)]
        self.assertIsNone(decomp._inlined(cookie, ours))       # a helper is never inlined
        # a call aligned with a call to something else: still what the shipped code calls
        other = [(0, "mov rcx, rbx", None), (3, "call <reloc>", (0x7000, "Widget::Other")),
                 (8, "mov eax, dword ptr [rcx + 0x8]", None), (0xb, "test eax, eax", None)]
        self.assertEqual(decomp._inlined(theirs, other), "inlined here, called there: Widget::Get")
        self.assertIsNone(decomp._inlined(theirs, other[:2] + other[3:]))   # another call in its place: no
        local = [(0, "mov rcx, rbx", None), (3, "call <reloc>", (("symbol", "?Get@Widget@@"), "Widget::Get")),
                 (8, "mov eax, dword ptr [rcx + 0x8]", None), (0xb, "test eax, eax", None)]
        self.assertIsNone(decomp._inlined(theirs, local))   # called here too, under a symbol not resolved
        self.assertEqual(decomp._short("__imp_??0FString@@QEAA@PEBD@Z"), "FString::FString")
        # a call ours makes too is not inlined
        moved = [(0, "call <reloc>", get), (5, "mov rcx, rbx", None), (8, "test eax, eax", None)]
        self.assertIsNone(decomp._inlined(theirs, moved))

    def test_not_an_object_file(self):
        bad = self.dir / "x.obj"
        bad.write_bytes(b"not an object")
        with self.assertRaises(Fail):
            decomp.match(self.dll, bad)


if __name__ == "__main__":
    unittest.main()
