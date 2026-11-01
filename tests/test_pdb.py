import os
import struct
import tempfile
import unittest

from portkit import pdb

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


def build_pdb():
    """Stream 5 is the module, 6 the symbol records (publics), 7 /names."""
    files = b"\0D:\\Game\\Source\\Game\\Private\\Screen.cpp\0D:\\Engine\\Core\\Array.h\0"
    names = struct.pack("<III", 0xEFFEEFFE, 1, len(files)) + files
    # module stream: signature, then symbols, then C13 line info
    syms = struct.pack("<I", 4)
    body = rec(0x113E, struct.pack("<IH", 0x74, 0) + b"Args\0")
    body += rec(0x114D, struct.pack("<III", 0, 0, 0x1000))  # an inlined call: its locals are not ours
    body += rec(0x113E, struct.pack("<IH", 0x74, 0) + b"Inlined\0")
    body += rec(0x114E, b"")
    body += rec(0x1111, struct.pack("<IIH", 0x28, 0x74, 335) + b"Slot\0")
    head = len(rec(0x1110, b"\0" * 35 + b"FGScreen::Construct\0"))
    end_at = len(syms) + head + len(body)  # the S_END closing the procedure
    proc = rec(0x1110, struct.pack("<IIIIIIIIHB", 0, end_at, 0, 0x40, 4, 0x3C, 0x1007, 0x10, 1, 0)
               + b"FGScreen::Construct\0")
    syms += proc + body + rec(0x0006, b"")
    syms += rec(0x110F, struct.pack("<IIIIIIIIHB", 0, 0, 0, 0x8, 0, 8, 0, 0x60, 1, 0) + b"`anonymous'::Helper\0")
    syms += rec(0x0006, b"")
    checksums = struct.pack("<IBB", 1, 0, 0) + b"\0\0" + struct.pack("<IBB", 40, 0, 0) + b"\0\0"
    lines = struct.pack("<IHHI", 0x10, 1, 0, 0x40)
    lines += struct.pack("<III", 0, 3, 12 + 3 * 8)
    lines += struct.pack("<II", 0, 20) + struct.pack("<II", 8, 21) + struct.pack("<II", 0x30, 27)
    c13 = struct.pack("<II", 0xF4, len(checksums)) + checksums + struct.pack("<II", 0xF2, len(lines)) + lines
    module = syms + c13
    # DBI: header, one module info record
    modi = struct.pack("<I", 0) + b"\0" * 28 + struct.pack("<HHIII", 0, 5, len(syms), 0, len(c13))
    modi += b"\0" * 16 + b"Module.Game.1.cpp.obj\0Module.Game.1.cpp.obj\0"
    modi += b"\0" * (-len(modi) % 4)
    dbi = struct.pack("<iIIHHHHHH", -1, 19990903, 1, 0xFFFF, 0, 0xFFFF, 0, 6, 0)
    dbi += struct.pack("<iiiiiIiiHHI", len(modi), 0, 0, 0, 0, 0, 0, 0, 0, 0x8664, 0) + modi
    publics = rec(0x110E, struct.pack("<IIH", 2, 0x10, 1) + b"?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z\0")
    return msf([b"", info_stream(7), build_tpi(), dbi, b"", module, publics, names])


def leaf(kind, body):
    """A type record: length, kind, body padded to 4 bytes."""
    body += bytes(0xF0 + n for n in range(-(len(body) + 4) % 4, 0, -1))
    return struct.pack("<HH", len(body) + 2, kind) + body


def sub(kind, body):
    """A field-list entry, padded to 4 bytes with LF_PAD bytes."""
    out = struct.pack("<H", kind) + body
    pad = -len(out) % 4
    return out + bytes(0xF0 + pad - i for i in range(pad))


def build_tpi():
    """Types from 0x1000: a base with a virtual function, a class deriving from it, a pointer to it."""
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
        # 0x1003: Screen's field list: base IBase at +0, override of the virtual, TSharedPtr-like member at +0x40
        leaf(0x1203, sub(0x1400, struct.pack("<HIH", 3, 0x1001, 0))
             + sub(0x1511, struct.pack("<HI", 3 | (1 << 2), 0x1007) + b"InitSettingsFromConfig\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x1005, 0x40) + b"mWidget\0")
             + sub(0x150D, struct.pack("<HIH", 1, 0x20, 0x50) + b"mType\0")),
        # 0x1004: class Screen, size 0x58
        leaf(0x1504, struct.pack("<HHIIIH", 4, 0, 0x1003, 0, 0, 0x58) + b"Screen\0"),
        # 0x1005: Screen*
        leaf(0x1002, struct.pack("<II", 0x1004, 0x1000C)),
        # 0x1006: vtable shape (unused here)
        leaf(0x000A, struct.pack("<H", 0)),
        # 0x1007: void (IBase::*)(const char*)
        leaf(0x1009, struct.pack("<IIIBBHIi", 0x0003, 0x1001, 0, 0, 0, 1, 0x1008, 0)),
        # 0x1008: argument list (const char*)
        leaf(0x1201, struct.pack("<II", 1, 0x0670)),
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

    def test_publics(self):
        self.assertEqual(pdb.publics(self.write(build_pdb())),
                         {(1, 0x10): "?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z"})


    def test_types(self):
        t = pdb.Types(self.write(build_pdb()))
        screen = t.udt("Screen")
        self.assertEqual(screen.size, 0x58)
        self.assertEqual(screen.bases, [("IBase", 0)])
        self.assertEqual(screen.members, [(0x40, "mWidget", "Screen*"), (0x50, "mType", "unsigned char")])
        self.assertEqual(t.udt("IBase").members, [(8, "Count", "int")])
        self.assertEqual(t.vtable("Screen"), {0x80: "IBase::InitSettingsFromConfig(char*)"})
        self.assertIsNone(t.udt("Missing"))
        self.assertEqual(t.declaration(0x1007, "Screen::Load"), "void Screen::Load(char*)")
        self.assertIn("+0x0040  Screen* mWidget", t.describe("Screen"))
        self.assertIn("vtable +0x080  IBase::InitSettingsFromConfig(char*)", t.describe("Screen"))


if __name__ == "__main__":
    unittest.main()
