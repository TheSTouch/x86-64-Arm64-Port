import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portkit import config, pdb

BLOCK = 0x200
GUID = bytes(range(16))


def msf(streams):
    """An MSF 7.00 file holding `streams` (stream i = streams[i]; None = absent)."""
    blocks, data = [], []
    nxt = 3  # 0 superblock, 1-2 free block maps

    def place(payload):
        nonlocal nxt
        ids = []
        for i in range(0, max(len(payload), 1), BLOCK):
            ids.append(nxt)
            data.append((nxt, payload[i:i + BLOCK]))
            nxt += 1
        return ids if payload else []

    for s in streams:
        blocks.append(place(s or b""))
    directory = struct.pack("<I", len(streams))
    directory += b"".join(struct.pack("<I", 0xFFFFFFFF if s is None else len(s)) for s in streams)
    directory += b"".join(struct.pack(f"<{len(b)}I", *b) for b in blocks)
    dir_blocks = place(directory)
    map_block = nxt
    data.append((map_block, struct.pack(f"<{len(dir_blocks)}I", *dir_blocks)))
    nxt += 1
    buf = bytearray(BLOCK * nxt)
    buf[:32] = pdb.MAGIC
    struct.pack_into("<6I", buf, 32, BLOCK, 1, nxt, len(directory), 0, map_block)
    for i, payload in data:
        buf[i * BLOCK:i * BLOCK + len(payload)] = payload
    return bytes(buf)


def rec(kind, body):
    body += b"\0" * (-(len(body) + 4) % 4)
    return struct.pack("<HH", len(body) + 2, kind) + body


def info_stream(names_stream):
    strings = b"/names\0"
    out = struct.pack("<III", 20000404, 0, 1) + GUID
    out += struct.pack("<I", len(strings)) + strings
    out += struct.pack("<II", 1, 1)            # size, capacity
    out += struct.pack("<II", 1, 1)            # present bit vector: bucket 0
    out += struct.pack("<I", 0)                # deleted bit vector: empty
    out += struct.pack("<II", 0, names_stream)
    return out


def build_pdb(folded=False, fields=False, twin=False, adjust=0):
    """Stream 5 is the module, 6 the symbol records (publics), 7 /names. folded: a second function at
    FGScreen::Construct's address, with its own line subsection after Construct's. fields: Construct's typed
    locals and where they live (FIELDS_CODE). twin: TScreen<int>::Construct at 0x80 (0x40 bytes), from the
    same lines as FGScreen::Construct (a template instance). adjust: Construct overrides a virtual of Screen's
    second base, ISave at +adjust, so its `this` points there (build_tpi); with fields, `this` is also in r14
    from +0xb for 4 bytes."""
    files = b"\0D:\\Game\\Source\\Game\\Private\\Screen.cpp\0D:\\Engine\\Core\\Array.h\0"
    names = struct.pack("<III", 0xEFFEEFFE, 1, len(files)) + files
    # module stream: signature, then symbols, then C13 line info
    syms = struct.pack("<I", 4)
    body = rec(0x113E, struct.pack("<IH", 0x74, 0) + b"Args\0")
    # Screen::Reset inlined (Array.h:100): line 101 at +6, line 102 at +0xd (+7, +1), then +0xe for 2 bytes
    body += rec(0x114D, struct.pack("<III", 0, 0, 0x1000) + bytes([6, 2, 3, 6, 0xB, 0x27, 0xC, 2, 1]))
    body += rec(0x113E, struct.pack("<IH", 0x74, 0) + b"Inlined\0")  # an inlined call's locals are not ours
    # inside it, Detail::Clear (Array.h:50) at +0xd for 1 byte, line 51
    body += rec(0x114D, struct.pack("<III", 0, 0, 0x1002) + bytes([6, 2, 0xC, 1, 0xD]))
    body += rec(0x114E, b"")
    body += rec(0x114E, b"")
    body += rec(0x1111, struct.pack("<IIH", 0x28, 0x74, 335) + b"Slot\0")
    if fields:
        def where(start, length, *gaps):  # a range at section offset 0x10 + start, and its gaps
            return struct.pack("<IHH", 0x10 + start, 1, length) + b"".join(struct.pack("<HH", *g) for g in gaps)
        body += rec(0x113E, struct.pack("<IH", 0x1005, 1) + b"this\0")            # no range: rcx at entry
        if adjust:
            body += rec(0x1141, struct.pack("<HH", 342, 0) + where(0x0b, 4))      # r14
        body += rec(0x113E, struct.pack("<IH", 0x1005, 0) + b"Widget\0")
        body += rec(0x1141, struct.pack("<HH", 328, 0) + where(0x14, 0x0a))       # rax, +0x14..+0x1e
        body += rec(0x113E, struct.pack("<IH", 0x1005, 0) + b"Other\0")
        body += rec(0x1141, struct.pack("<HH", 331, 1) + where(0x1b, 3))           # rdx, "may have no name"
        body += rec(0x1141, struct.pack("<HH", 330, 0) + where(0x20, 0x0b, (0, 0x0b)))  # rcx, all a gap
        body += rec(0x113E, struct.pack("<IH", 0x1004, 0) + b"Copy\0")
        body += rec(0x1142, struct.pack("<i", 0x20) + where(0, 0x2e))             # [frame + 0x20]
        body += rec(0x1012, struct.pack("<IIIIIHI", 0x80, 0, 0, 0, 0, 0, 1 << 14 | 1 << 16))  # frame: rsp
    head = len(rec(0x1110, b"\0" * 35 + b"FGScreen::Construct\0"))
    end_at = len(syms) + head + len(body)  # the S_END closing the procedure
    proc = rec(0x1110, struct.pack("<IIIIIIIIHB", 0, end_at, 0, 0x40, 4, 0x3C, 0x1015 if adjust else 0x1007, 0x10, 1, 0)
               + b"FGScreen::Construct\0")
    syms += proc + body + rec(0x0006, b"")
    syms += rec(0x110F, struct.pack("<IIIIIIIIHB", 0, 0, 0, 0x8, 0, 8, 0, 0x60, 1, 0) + b"`anonymous'::Helper\0")
    syms += rec(0x0006, b"")
    if twin:
        end_at = len(syms) + len(rec(0x110F, b"\0" * 35 + b"TScreen<int>::Construct\0"))
        syms += rec(0x110F, struct.pack("<IIIIIIIIHB", 0, end_at, 0, 0x40, 0, 0x40, 0, 0x80, 1, 0)
                    + b"TScreen<int>::Construct\0")
        syms += rec(0x0006, b"")
    if folded:
        end_at = len(syms) + len(rec(0x110F, b"\0" * 35 + b"FGScreen::Folded\0"))
        syms += rec(0x110F, struct.pack("<IIIIIIIIHB", 0, end_at, 0, 0x40, 0, 0x40, 0, 0x10, 1, 0) + b"FGScreen::Folded\0")
        syms += rec(0x0006, b"")
    checksums = struct.pack("<IBB", 1, 0, 0) + b"\0\0" + struct.pack("<IBB", 40, 0, 0) + b"\0\0"
    lines = struct.pack("<IHHI", 0x10, 1, 0, 0x40)
    lines += struct.pack("<III", 0, 4, 12 + 4 * 8)
    lines += struct.pack("<II", 0, 20) + struct.pack("<II", 8, 21) + struct.pack("<II", 0x30, 27)
    lines += struct.pack("<II", 0x34, 0x80FEEFEE)  # a hidden line (0xfeefee): no source, not a line number
    lines += struct.pack("<III", 8, 1, 12 + 8) + struct.pack("<II", 0x38, 300)  # Array.h, after the first file
    lines2 = struct.pack("<IHHI", 0x10, 1, 0, 0x40) + struct.pack("<III", 0, 2, 12 + 2 * 8)
    lines2 += struct.pack("<II", 0, 40) + struct.pack("<II", 8, 42)  # FGScreen::Folded's own lines
    inlinees = struct.pack("<I", 0) + struct.pack("<III", 0x1000, 8, 100) + struct.pack("<III", 0x1002, 8, 50)
    c13 = struct.pack("<II", 0xF4, len(checksums)) + checksums + struct.pack("<II", 0xF2, len(lines)) + lines
    if folded:
        c13 += struct.pack("<II", 0xF2, len(lines2)) + lines2
    if twin:
        lines3 = struct.pack("<IHHI", 0x80, 1, 0, 0x40) + struct.pack("<III", 0, 2, 12 + 2 * 8)
        lines3 += struct.pack("<II", 0, 20) + struct.pack("<II", 8, 27)
        c13 += struct.pack("<II", 0xF2, len(lines3)) + lines3
    c13 += struct.pack("<II", 0xF6, len(inlinees)) + inlinees
    module = syms + c13
    # DBI: header, one module info record
    modi = struct.pack("<I", 0) + b"\0" * 28 + struct.pack("<HHIII", 0, 5, len(syms), 0, len(c13))
    modi += b"\0" * 16 + b"Module.Game.1.cpp.obj\0Module.Game.1.cpp.obj\0"
    modi += b"\0" * (-len(modi) % 4)
    dbi = struct.pack("<iIIHHHHHH", -1, 19990903, 1, 0xFFFF, 0, 0xFFFF, 0, 6, 0)
    dbi += struct.pack("<iiiiiIiiHHI", len(modi), 0, 0, 0, 0, 0, 0, 0, 0, 0x8664, 0) + modi
    publics = rec(0x110E, struct.pack("<IIH", 2, 0x10, 1) + b"?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z\0")
    return msf([b"", info_stream(7), build_tpi(adjust), dbi, build_ipi(), module, publics, names])


def leaf(kind, body):
    """A type record: length, kind, body padded to 4 bytes."""
    body += bytes(0xF0 + n for n in range(-(len(body) + 4) % 4, 0, -1))
    return struct.pack("<HH", len(body) + 2, kind) + body


def sub(kind, body):
    """A field-list entry, padded to 4 bytes with LF_PAD bytes."""
    out = struct.pack("<H", kind) + body
    pad = -len(out) % 4
    return out + bytes(0xF0 + pad - i for i in range(pad))


def build_ipi():
    """Function ids from 0x1000: Screen::Reset (a member function), the string "Detail", Detail::Clear."""
    records = [leaf(0x1602, struct.pack("<II", 0x1004, 0x1007) + b"Reset\0"),
               leaf(0x1605, struct.pack("<I", 0) + b"Detail\0"),
               leaf(0x1601, struct.pack("<II", 0x1001, 0x1007) + b"Clear\0"),
               leaf(0x1602, struct.pack("<II", 0x1004, 0x1007) + b"{ctor}\0"),
               leaf(0x1602, struct.pack("<II", 0x1004, 0x1007) + b"{dtor}\0")]
    data = b"".join(records)
    return struct.pack("<IIIII", 20040203, 56, 0x1000, 0x1000 + len(records), len(data)) + b"\0" * 36 + data


def build_tpi(adjust=0):
    """Types from 0x1000: a base with a virtual function, a class deriving from it, a pointer to it. adjust:
    Screen also derives from ISave at +adjust (and grows to hold it), and 0x1015 is a member function of
    Screen whose `this` points at that base."""
    T = 0x1000
    records = [
        # 0x1000: the base's field list: vfptr, an introducing virtual at vtable offset 0x80, an int at +8
        leaf(0x1203, sub(0x1409, struct.pack("<HI", 0, 0x1006))
             + sub(0x1511, struct.pack("<HII", 3 | (4 << 2), 0x1007, 0x80) + b"InitSettingsFromConfig\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x74, 8) + b"Count\0")),
        # 0x1001: class IBase, size 0x10
        leaf(0x1504, struct.pack("<HHIIIH", 3, 0, T, 0, 0, 0x10) + b"IBase\0"),
        # 0x1002: forward reference to Screen (ignored by lookups)
        leaf(0x1504, struct.pack("<HHIIIH", 0, 0x80, 0, 0, 0, 0) + b"Screen\0"),
        # 0x1003: Screen's field list: base IBase at +0, override of the virtual, an int[3] at +0x10, an FPair
        # at +0x20, TSharedPtr-like member at +0x40
        leaf(0x1203, sub(0x1400, struct.pack("<HIH", 3, 0x1001, 0))
             + sub(0x1511, struct.pack("<HI", 3 | (1 << 2), 0x1007) + b"InitSettingsFromConfig\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x100B, 0x10) + b"mSlots\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x100A, 0x20) + b"mPair\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x1005, 0x40) + b"mWidget\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x20, 0x50) + b"mType\0")
             + (sub(0x1400, struct.pack("<HIH", 3, 0x1014, adjust)) if adjust else b"")),
        # 0x1004: class Screen, size 0x58 (with ISave, past it)
        leaf(0x1504, struct.pack("<HHIIIH", 4, 0, 0x1003, 0, 0, max(0x58, adjust + 8)) + b"Screen\0"),
        # 0x1005: Screen*
        leaf(0x1002, struct.pack("<II", 0x1004, 0x1000C)),
        # 0x1006: vtable shape (unused here)
        leaf(0x000A, struct.pack("<H", 0)),
        # 0x1007: void (IBase::*)(const char*)
        leaf(0x1009, struct.pack("<IIIBBHIi", 0x0003, 0x1001, 0, 0, 0, 1, 0x1008, 0)),
        # 0x1008: argument list (const char*)
        leaf(0x1201, struct.pack("<II", 1, 0x0670)),
        # 0x1009: FPair's field list: int A at +0, int B at +4
        leaf(0x1203, sub(0x150D, struct.pack("<HIH", 3, 0x74, 0) + b"A\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x74, 4) + b"B\0")),
        # 0x100A: struct FPair, size 8
        leaf(0x1505, struct.pack("<HHIIIH", 2, 0, 0x1009, 0, 0, 8) + b"FPair\0"),
        # 0x100B: int[3]
        leaf(0x1503, struct.pack("<IIH", 0x74, 0x23, 12) + b"\0"),
        # 0x100C-0x1010: bitfields (type, bits, position): uint8 A:1 at 0, B:2 at 1, C:1 at 5; uint32 D:3 at 9,
        # E:1 at 20
        leaf(0x1205, struct.pack("<IBB", 0x20, 1, 0)), leaf(0x1205, struct.pack("<IBB", 0x20, 2, 1)),
        leaf(0x1205, struct.pack("<IBB", 0x20, 1, 5)), leaf(0x1205, struct.pack("<IBB", 0x75, 3, 9)),
        leaf(0x1205, struct.pack("<IBB", 0x75, 1, 20)),
        # 0x1011: FFlags's field list: A, B, C in the byte at +0, D and E in the dword at +4
        leaf(0x1203, sub(0x150D, struct.pack("<HIH", 3, 0x100C, 0) + b"A\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x100D, 0) + b"B\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x100E, 0) + b"C\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x100F, 4) + b"D\0")
             + sub(0x150D, struct.pack("<HIH", 3, 0x1010, 4) + b"E\0")),
        # 0x1012: struct FFlags, size 8
        leaf(0x1505, struct.pack("<HHIIIH", 5, 0, 0x1011, 0, 0, 8) + b"FFlags\0"),
        # 0x1013: ISave's field list: its vfptr; 0x1014: class ISave, size 8
        leaf(0x1203, sub(0x1409, struct.pack("<HI", 0, 0x1006))),
        leaf(0x1504, struct.pack("<HHIIIH", 1, 0, 0x1013, 0, 0, 8) + b"ISave\0"),
        # 0x1015: void (Screen::*)(const char*), `this` adjusted by `adjust`
        leaf(0x1009, struct.pack("<IIIBBHIi", 0x0003, 0x1004, 0x1005, 0, 0, 1, 0x1008, adjust)),
    ]
    data = b"".join(records)
    header = struct.pack("<IIIII", 20040203, 56, T, T + len(records), len(data)) + b"\0" * 36
    return header + data


class PDBTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".pdb", delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_signature(self):
        self.assertEqual(pdb.signature(self.write(build_pdb())), (GUID, 1))

    def test_functions(self):
        fns = pdb.functions(self.write(build_pdb()))
        self.assertEqual([f.name for f in fns], ["FGScreen::Construct", "`anonymous'::Helper"])
        f = fns[0]
        self.assertEqual((f.segment, f.offset, f.length), (1, 0x10, 0x40))
        self.assertEqual(f.source, "D:\\Game\\Source\\Game\\Private\\Screen.cpp")
        self.assertEqual((f.first_line, f.last_line), (20, 27))
        self.assertEqual(f.locals, ["Args", "Slot"])
        self.assertEqual(f.local_types, {"Args": 0x74, "Slot": 0x74})
        self.assertEqual(f.type_index, 0x1007)
        self.assertEqual(f.compiland, "Module.Game.1.cpp.obj")
        self.assertEqual(f.mangled, "?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z")
        self.assertIsNone(fns[1].source)

    def test_lines_of_every_file_and_inline_sites(self):
        path = self.write(build_pdb())
        f = pdb.functions(path)[0]
        d = pdb.Details(path).detail(f)
        screen, array = "D:\\Game\\Source\\Game\\Private\\Screen.cpp", "D:\\Engine\\Core\\Array.h"
        self.assertEqual(d.lines, [(0, screen, 20), (8, screen, 21), (0x30, screen, 27), (0x38, array, 300)])
        reset, clear = d.inlines
        self.assertEqual((reset.name, reset.file, reset.line, reset.depth), ("Screen::Reset", array, 100, 0))
        self.assertEqual(reset.ranges, [(6, 0xd, array, 101), (0xd, 0xe, array, 102), (0xe, 0x10, array, 102)])
        self.assertEqual((clear.name, clear.line, clear.depth), ("Detail::Clear", 50, 1))
        self.assertEqual(clear.ranges, [(0xd, 0xe, array, 51)])
        self.assertEqual(pdb.Details(path).inlinee_name(0x1003), "Screen::Screen")
        self.assertEqual(pdb.Details(path).inlinee_name(0x1004), "Screen::~Screen")
        self.assertEqual(pdb._unqualified("TMixin<SOverlay::FSlot>::FArgs<A::B>"), "FArgs<A::B>")

    def test_functions_folded_at_one_address_keep_their_own_lines(self):
        path = self.write(build_pdb(folded=True))
        construct, folded = pdb.functions(path)[:2]
        self.assertEqual((folded.name, folded.first_line, folded.last_line), ("FGScreen::Folded", 40, 42))
        self.assertEqual((construct.first_line, construct.last_line), (20, 27))
        screen = "D:\\Game\\Source\\Game\\Private\\Screen.cpp"
        self.assertEqual(pdb.Details(path).detail(folded).lines, [(0, screen, 40), (8, screen, 42)])
        self.assertEqual(pdb.Details(path).detail(construct).lines[0], (0, screen, 20))

    def test_field_paths(self):
        t = pdb.Types(self.write(build_pdb()))
        self.assertEqual(t.pointee(0x1005), 0x1004)
        self.assertEqual(t.size(0x1004), 0x58)
        self.assertEqual(t.field_path(0x1004, 0x40, 8), "mWidget")
        self.assertEqual(t.field_path(0x1004, 0x14, 4), "mSlots[1]")
        self.assertEqual(t.field_path(0x1004, 0x24, 4), "mPair.B")
        self.assertEqual(t.field_path(0x1004, 0x20, None), "mPair")       # lea: the member's address
        self.assertEqual(t.field_path(0x1004, 0x20, 8), "mPair")          # the whole struct
        self.assertEqual(t.field_path(0x1004, 0x42, 2), "mWidget+0x2")
        self.assertEqual(t.field_path(0x1004, 8, 4), "Count")             # in the base class
        self.assertEqual(t.field_path(0x1004, 0, 8), "__vfptr")
        self.assertIsNone(t.field_path(0x1004, 0x30, 8))                  # padding
        self.assertIsNone(t.field_path(0x1004, 0x58, 4))                  # past the end
        # what a register holds when the PDB says it holds a variable's field (S_DEFRANGE_SUBFIELD_REGISTER)
        self.assertEqual(t.member_at(0x1004, 0x40), ("mWidget", 0x1005))
        self.assertEqual(t.member_at(0x1004, 0x24), ("mPair.B", 0x74))
        self.assertEqual(t.member_at(0x1004, 8), ("Count", 0x74))
        self.assertIsNone(t.member_at(0x1004, 0x42))

    def test_bitfields_named_by_the_bits_an_access_reaches(self):
        t = pdb.Types(self.write(build_pdb()))
        flags = 0x1012
        self.assertEqual(t.field_path(flags, 0, 1), "A|B|C")              # every bit of the byte
        self.assertEqual(t.field_path(flags, 0, 1, mask=0x20), "C")       # test byte ptr [x], 0x20
        self.assertEqual(t.field_path(flags, 0, 1, mask=0x04), "B")
        self.assertEqual(t.field_path(flags, 0, 1, mask=0x03), "A|B")
        self.assertIsNone(t.field_path(flags, 0, 1, mask=0x40))           # a bit no field has
        self.assertEqual(t.field_path(flags, 5, 1, mask=0x02), "D")       # bit 9 of the dword at +4
        self.assertEqual(t.field_path(flags, 6, 1, mask=0x10), "E")       # bit 20
        self.assertEqual(t.field_path(flags, 5, 1), "D")                  # bits 8-15
        self.assertEqual(t.field_path(flags, 4, 4), "D|E")
        self.assertEqual(t.field_path(flags, 4, None), "D|E")             # lea: the address of them all

    def test_variables_and_where_they_live(self):
        path = self.write(build_pdb(fields=True))
        v = {x.name: x for x in pdb.Details(path).detail(pdb.functions(path)[0]).variables}
        self.assertEqual(v["this"].homes, [])
        self.assertEqual(v["Widget"].homes, [("register", 328, 0, [(0x14, 0x1e)])])
        self.assertEqual(v["Other"].homes, [])                            # only "may have no name" or gaps
        self.assertEqual(v["Copy"].homes, [("memory", 335, 0x20, [(0, 0x2e)])])
        self.assertEqual(v["Slot"].homes, [("memory", 335, 0x28, [(0, 0x40)])])
        self.assertEqual(pdb._range(struct.pack("<IHH", 0x20, 1, 0x10) + struct.pack("<HHHH", 2, 3, 8, 8), 0x10),
                         [(0x10, 0x12), (0x15, 0x18)])

    def test_binary_annotations(self):
        # ChangeCodeOffset 0x1743, ChangeLineOffset +4, ChangeCodeLengthAndCodeOffset (0x12, 0x12): MSVC's
        # encoding, the offset counted from the previous range's start; operands of 2 and 4 bytes
        self.assertEqual(list(pdb._annotations(bytes([6, 2, 3, 0x97, 0x43, 6, 8, 0xC, 0x12, 0x12, 0]))),
                         [(6, 1), (3, 0x1743), (6, 4), (0xC, 0x12, 0x12)])
        self.assertEqual(list(pdb._annotations(bytes([3, 0xC0, 0x01, 0x02, 0x03, 0xB, 0x2E]))),
                         [(3, 0x10203), (0xB, 0xE, 1)])
        self.assertEqual(pdb._inline_ranges(bytes([6, 2, 3, 0x97, 0x43, 6, 8, 0xC, 0x12, 0x12]), "a.h", 10, {}),
                         [(0x1743, 0x1755, "a.h", 11), (0x1755, 0x1767, "a.h", 15)])
        # LLVM's encoding: ChangeCodeLength closes the range and moves past it; ChangeFile; a negative line
        self.assertEqual(pdb._inline_ranges(bytes([3, 4, 4, 2, 5, 8, 6, 3, 3, 1, 4, 1]), "a.h", 10, {8: "b.h"}),
                         [(4, 6, "a.h", 10), (7, 8, "b.h", 9)])

    def test_publics(self):
        self.assertEqual(pdb.publics(self.write(build_pdb())),
                         {(1, 0x10): "?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z"})


    def test_types(self):
        t = pdb.Types(self.write(build_pdb()))
        screen = t.udt("Screen")
        self.assertEqual(screen.size, 0x58)
        self.assertEqual(screen.bases, [("IBase", 0)])
        self.assertEqual(screen.members, [(0x10, "mSlots", "int[12]"), (0x20, "mPair", "FPair"),
                                          (0x40, "mWidget", "Screen*"), (0x50, "mType", "unsigned char")])
        self.assertEqual(t.udt("IBase").members, [(8, "Count", "int")])
        self.assertEqual(t.vtable("Screen"), {0x80: "IBase::InitSettingsFromConfig(char*)"})
        self.assertIsNone(t.udt("Missing"))
        self.assertEqual(t.declaration(0x1007, "Screen::Load"), "void Screen::Load(char*)")
        self.assertIn("+0x0040  Screen* mWidget", t.describe("Screen"))
        self.assertIn("vtable +0x080  IBase::InitSettingsFromConfig(char*)", t.describe("Screen"))


if __name__ == "__main__":
    unittest.main()


class CacheTest(unittest.TestCase):
    """A large PDB's functions and public symbols are kept on disk (config.PDB_CACHE), keyed by the file, its
    size, time and signature, and pdb.py itself: read again when any changes."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.cache = self.dir / "cache"
        self.cache.mkdir()
        for name, value in (("CACHE_MIN", 0),):
            old = getattr(pdb, name)
            setattr(pdb, name, value)
            self.addCleanup(setattr, pdb, name, old)
        old = config.PDB_CACHE
        config.PDB_CACHE = self.cache
        self.addCleanup(setattr, config, "PDB_CACHE", old)
        self.path = self.dir / "Game.pdb"
        self.path.write_bytes(build_pdb())

    def test_read_once_then_from_the_cache(self):
        fns, pubs = pdb.functions(self.path), pdb.public_symbols(self.path)
        self.assertEqual(len(list(self.cache.iterdir())), 2)
        with mock.patch.object(pdb, "_read_functions", side_effect=AssertionError("read again")), \
                mock.patch.object(pdb, "_read_public_symbols", side_effect=AssertionError("read again")):
            self.assertEqual(pdb.functions(self.path), fns)
            self.assertEqual(pdb.public_symbols(self.path), pubs)
            self.assertEqual(pdb.publics(self.path), {(1, 0x10): pubs[0][0]})

    def test_a_changed_pdb_is_read_again_and_its_old_entry_removed(self):
        self.assertEqual([f.name for f in pdb.functions(self.path)][-1], "`anonymous'::Helper")
        self.path.write_bytes(build_pdb(twin=True))
        self.assertIn("TScreen<int>::Construct", [f.name for f in pdb.functions(self.path)])
        self.assertEqual(len(list(self.cache.iterdir())), 2)   # functions, public symbols: the old entries gone

    def test_small_pdbs_and_the_repository_are_not_cached(self):
        pdb.CACHE_MIN = 1 << 30
        pdb.functions(self.path)
        config.PDB_CACHE = config.ROOT / "pdb-cache"
        pdb.CACHE_MIN = 0
        pdb.functions(self.path)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertFalse(config.PDB_CACHE.exists())
