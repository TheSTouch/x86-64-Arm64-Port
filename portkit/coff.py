"""A small read-only reader for COFF object files (.obj) from MSVC, x64, regular or /bigobj.

Reads what matching needs: each function's code and the bytes the linker will fill (relocated
operands), so a compiled function can be compared with the shipped one byte for byte.
"""

import struct
from dataclasses import dataclass, field

BIGOBJ_CLASS = bytes.fromhex("c7a1bad1eebaa94baf20faf66aa4dcb8")
SCN_CNT_CODE, SCN_LNK_NRELOC_OVFL = 0x20, 0x01000000
DTYPE_FUNCTION = 0x20
# AMD64 relocation types -> bytes the linker writes (IMAGE_REL_AMD64_*)
RELOC_SIZE = {0x1: 8, 0x2: 4, 0x3: 4, 0x4: 4, 0x5: 4, 0x6: 4, 0x7: 4, 0x8: 4, 0x9: 4,
              0xA: 2, 0xB: 4, 0xC: 1, 0xD: 4, 0xE: 4, 0x10: 4}


class NotCOFF(ValueError):
    pass


@dataclass
class Section:
    name: str
    data: bytes
    characteristics: int
    relocations: list = field(default_factory=list)   # (offset, symbol index, type)

    @property
    def code(self):
        return bool(self.characteristics & SCN_CNT_CODE)


@dataclass
class Function:
    name: str
    code: bytes
    masked: set   # offsets (from the function's start) of bytes a relocation fills


@dataclass
class Object:
    machine: int
    sections: list
    symbols: list   # (index, name, value, section number, type, storage class)

    def functions(self):
        """Every function defined in a code section, cut at the next function of the same section."""
        starts = {}
        for _, name, value, number, kind, storage in self.symbols:
            if number > 0 and kind == DTYPE_FUNCTION and storage in (2, 3) and not name.startswith("$"):
                if self.sections[number - 1].code:
                    starts.setdefault(number, []).append((value, name))
        out = []
        for number, entries in sorted(starts.items()):
            s = self.sections[number - 1]
            entries.sort()
            for i, (value, name) in enumerate(entries):
                end = entries[i + 1][0] if i + 1 < len(entries) else len(s.data)
                masked = set()
                for off, _, kind in s.relocations:
                    masked.update(o - value for o in range(off, off + RELOC_SIZE.get(kind, 0)) if value <= o < end)
                out.append(Function(name, s.data[value:end], masked))
        return out


def parse(data):
    if len(data) >= 56 and data[:4] == b"\0\0\xff\xff" and data[12:28] == BIGOBJ_CLASS:
        machine = struct.unpack_from("<H", data, 6)[0]
        nsec, table, nsym = struct.unpack_from("<III", data, 44)
        header, sym_size, sym_fmt = 56, 20, "<IiHBB"
    elif len(data) >= 20 and struct.unpack_from("<H", data)[0] in (0x8664, 0x14C, 0xAA64):
        machine, nsec, _, table, nsym, opt, _ = struct.unpack_from("<HHIIIHH", data)
        if opt:
            raise NotCOFF("an image, not an object file")
        header, sym_size, sym_fmt = 20, 18, "<IhHBB"
    else:
        raise NotCOFF("not a COFF object file")
    strings_at = table + nsym * sym_size
    if strings_at + 4 > len(data):
        raise NotCOFF("truncated symbol table")

    def string(off):
        start = strings_at + off
        return data[start:data.index(b"\0", start)].decode("utf-8", "replace")

    def name_of(field_):
        if field_[:4] == b"\0\0\0\0":
            return string(struct.unpack_from("<I", field_, 4)[0])
        return field_.rstrip(b"\0").decode("utf-8", "replace")

    sections = []
    for i in range(nsec):
        raw_name, _, _, size, ptr, rel_ptr, _, nrel, _, flags = struct.unpack_from("<8sIIIIIIHHI", data, header + 40 * i)
        name = string(int(raw_name[1:].rstrip(b"\0"))) if raw_name[:1] == b"/" else raw_name.rstrip(b"\0").decode()
        relocs, first = [], 0
        if flags & SCN_LNK_NRELOC_OVFL and nrel == 0xFFFF:
            nrel, first = struct.unpack_from("<I", data, rel_ptr)[0], 1   # the first record holds the count
        for r in range(first, nrel):
            relocs.append(struct.unpack_from("<IIH", data, rel_ptr + 10 * r))
        sections.append(Section(name, data[ptr:ptr + size] if ptr else b"", flags, relocs))
    symbols, i = [], 0
    while i < nsym:
        off = table + i * sym_size
        value, number, kind, storage, aux = struct.unpack_from(sym_fmt, data, off + 8)
        symbols.append((i, name_of(data[off:off + 8]), value, number, kind, storage))
        i += 1 + aux
    return Object(machine, sections, symbols)
