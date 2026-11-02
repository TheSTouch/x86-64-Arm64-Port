"""A small read-only reader for Microsoft PDB files (MSF 7.00).

Reads what the recompilation needs: the signature (a PDB belongs to a binary
when its GUID and age match the binary's CodeView record, which pe.parse reads
as pdb_guid and pdb_age), every function with its source file, lines and named
locals, and the public symbols with their decorated names.
"""

import math
import struct
from dataclasses import dataclass, field

MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"

S_END, S_GPROC32, S_LPROC32, S_PUB32 = 0x0006, 0x1110, 0x110F, 0x110E
S_LOCAL, S_REGREL32, S_INLINESITE, S_INLINESITE_END = 0x113E, 0x1111, 0x114D, 0x114E
S_GPROC32_ID, S_LPROC32_ID = 0x1147, 0x1146
PROCS = {S_GPROC32, S_LPROC32, S_GPROC32_ID, S_LPROC32_ID}
DEBUG_S_LINES, DEBUG_S_FILECHKSMS = 0xF2, 0xF4


class NotPDB(ValueError):
    pass


@dataclass
class Function:
    name: str
    segment: int
    offset: int
    length: int
    compiland: str
    source: str | None = None
    first_line: int | None = None
    last_line: int | None = None
    locals: list = field(default_factory=list)
    mangled: str | None = None
    type_index: int = 0
    local_types: dict = field(default_factory=dict)  # local name -> type index


class MSF:
    """The stream container: read(i) returns stream i's bytes."""

    def __init__(self, path):
        self.f = open(path, "rb")
        head = self.f.read(56)
        if len(head) < 56 or head[:32] != MAGIC:
            self.f.close()
            raise NotPDB(f"{path}: not an MSF 7.00 file")
        self.block, _, _, dir_bytes, _, map_block = struct.unpack_from("<6I", head, 32)
        if not self.block or not dir_bytes:
            self.f.close()
            raise NotPDB(f"{path}: empty stream directory")
        n_dir = math.ceil(dir_bytes / self.block)
        dir_blocks = struct.unpack_from(f"<{n_dir}I", self._block(map_block))
        directory = b"".join(self._block(b) for b in dir_blocks)[:dir_bytes]
        n = struct.unpack_from("<I", directory)[0]
        sizes = struct.unpack_from(f"<{n}I", directory, 4)
        self.streams, off = [], 4 + 4 * n
        for size in sizes:
            k = 0 if size == 0xFFFFFFFF else math.ceil(size / self.block)
            self.streams.append((0 if size == 0xFFFFFFFF else size, struct.unpack_from(f"<{k}I", directory, off)))
            off += 4 * k

    def _block(self, i):
        self.f.seek(i * self.block)
        return self.f.read(self.block)

    def read(self, i, limit=None):
        if i >= len(self.streams) or i == 0xFFFF:
            return b""
        size, blocks = self.streams[i]
        size = size if limit is None else min(size, limit)
        return b"".join(self._block(b) for b in blocks[:math.ceil(size / self.block)])[:size]

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def signature(path):
    """(guid, age) from the PDB info stream (stream 1); guid as the 16 bytes stored in an RSDS record."""
    with MSF(path) as m:
        info = m.read(1, 28)
    if len(info) < 28:
        raise NotPDB(f"{path}: PDB info stream too short")
    return bytes(info[12:28]), struct.unpack_from("<I", info, 8)[0]


def _cstr(buf, off):
    end = buf.find(b"\0", off)
    return bytes(buf[off:end if end >= 0 else len(buf)]).decode("utf-8", "replace")


def _named_streams(info):
    """The PDB info stream's name -> stream index map ("/names", ...)."""
    off = 28
    strsz = struct.unpack_from("<I", info, off)[0]
    strings, off = info[off + 4:off + 4 + strsz], off + 4 + strsz
    _, capacity = struct.unpack_from("<II", info, off)
    off += 8
    words = struct.unpack_from("<I", info, off)[0]
    present = struct.unpack_from(f"<{words}I", info, off + 4)
    off += 4 + 4 * words
    off += 4 + 4 * struct.unpack_from("<I", info, off)[0]
    out = {}
    for i in range(capacity):
        if i // 32 < len(present) and present[i // 32] >> (i % 32) & 1:
            key, value = struct.unpack_from("<II", info, off)
            out[_cstr(strings, key)] = value
            off += 8
    return out


def _records(buf, start=0, end=None):
    """(offset, kind, body) for each CodeView symbol record."""
    off, end = start, len(buf) if end is None else end
    while off + 4 <= end:
        length, kind = struct.unpack_from("<HH", buf, off)
        if length < 2:
            break
        yield off, kind, buf[off + 4:off + 2 + length]
        off += 2 + length


def _dbi(m):
    dbi = m.read(3)
    if len(dbi) < 64:
        raise NotPDB("no DBI stream")
    return dbi, struct.unpack_from("<H", dbi, 20)[0], struct.unpack_from("<i", dbi, 24)[0]


def publics(path):
    """{(segment, offset): decorated name} from the public symbols (S_PUB32)."""
    with MSF(path) as m:
        _, symrec, _ = _dbi(m)
        out = {}
        for _, kind, body in _records(m.read(symrec)):
            if kind == S_PUB32 and len(body) >= 10:
                _, off, seg = struct.unpack_from("<IIH", body)
                out[(seg, off)] = _cstr(body, 10)
        return out


def public_symbols(path):
    """Every public symbol (S_PUB32) as (decorated name, segment, offset, flags), in the stream's order.
    flags: 1 code, 2 function. Unlike `publics`, names sharing an address (folded functions) all stay."""
    with MSF(path) as m:
        _, symrec, _ = _dbi(m)
        out = []
        for _, kind, body in _records(m.read(symrec)):
            if kind == S_PUB32 and len(body) >= 10:
                flags, off, seg = struct.unpack_from("<IIH", body)
                out.append((_cstr(body, 10), seg, off, flags))
        return out


def _lines(c13, names):
    """{(segment, offset): (source file, first line, last line)} from a module's C13 line info."""
    files, spans = {}, []
    for kind, body in _subsections(c13):
        if kind == DEBUG_S_FILECHKSMS:
            q = 0
            while q + 6 <= len(body):
                name_off, size, _ = struct.unpack_from("<IBB", body, q)
                files[q] = _cstr(names, 12 + name_off)
                q = (q + 6 + size + 3) & ~3
        elif kind == DEBUG_S_LINES and len(body) >= 12:
            off, seg, _, _ = struct.unpack_from("<IHHI", body)
            q, nums, first_file = 12, [], None
            while q + 12 <= len(body):
                fid, n, size = struct.unpack_from("<III", body, q)
                first_file = fid if first_file is None else first_file
                if fid == first_file:
                    nums += [struct.unpack_from("<I", body, q + 12 + 8 * i + 4)[0] & 0xFFFFFF for i in range(n)]
                q += max(size, 12)
            spans.append(((seg, off), first_file, nums))
    return {key: (files.get(fid), min(n), max(n)) for key, fid, n in spans if n}


def _subsections(c13):
    q = 0
    while q + 8 <= len(c13):
        kind, length = struct.unpack_from("<II", c13, q)
        yield kind & 0x7FFFFFFF, c13[q + 8:q + 8 + length]
        q += 8 + ((length + 3) & ~3)


def functions(path):
    """Every function (S_GPROC32 / S_LPROC32) in every compiland, in address order."""
    with MSF(path) as m:
        dbi, _, modi_size = _dbi(m)
        names = m.read(_named_streams(m.read(1)).get("/names", 0xFFFF))
        out, off, end = [], 64, 64 + modi_size
        while off < end:
            stream, sym_bytes, c11, c13 = struct.unpack_from("<HIII", dbi, off + 34)
            name_end = dbi.index(b"\0", off + 64)
            compiland = _cstr(dbi, off + 64)
            off = (dbi.index(b"\0", name_end + 1) + 4) & ~3
            if stream == 0xFFFF:
                continue
            data = m.read(stream)
            lines = _lines(data[sym_bytes + c11:sym_bytes + c11 + c13], names)
            out += _module_functions(data[:sym_bytes], compiland, lines)
    pubs, seen, unique = publics(path), set(), []
    for f in sorted(out, key=lambda f: (f.segment, f.offset)):
        if (key := (f.name, f.segment, f.offset)) in seen:  # inline functions emitted by several compilands
            continue
        seen.add(key)
        f.mangled = pubs.get((f.segment, f.offset))
        f.locals = list(dict.fromkeys(f.locals))  # parameters appear as S_LOCAL and S_REGREL32
        unique.append(f)
    return unique


def _module_functions(syms, compiland, lines):
    out, current, end_at, inline_depth = [], None, None, 0
    for off, kind, body in _records(syms, 4):
        if kind in PROCS and len(body) >= 35:
            _, p_end, _, length, _, _, ti, offset, seg = struct.unpack_from("<IIIIIIIIH", body)
            current = Function(_cstr(body, 35), seg, offset, length, compiland, type_index=ti)
            current.source, current.first_line, current.last_line = lines.get((seg, offset), (None, None, None))
            out.append(current)
            end_at, inline_depth = p_end, 0
        elif current is None:
            continue
        elif off == end_at:
            current = None
        elif kind == S_INLINESITE:
            inline_depth += 1
        elif kind == S_INLINESITE_END:
            inline_depth -= 1
        elif inline_depth == 0 and kind == S_LOCAL:
            current.locals.append(name := _cstr(body, 6))
            current.local_types.setdefault(name, struct.unpack_from("<I", body)[0])
        elif inline_depth == 0 and kind == S_REGREL32:
            current.locals.append(name := _cstr(body, 10))
            current.local_types.setdefault(name, struct.unpack_from("<I", body, 4)[0])
    return out


# Type records (TPI stream). Only what describing a class needs: layouts, bases, members and virtual functions.
LF_MODIFIER, LF_POINTER, LF_PROCEDURE, LF_MFUNCTION = 0x1001, 0x1002, 0x1008, 0x1009
LF_ARGLIST, LF_FIELDLIST, LF_BITFIELD, LF_METHODLIST = 0x1201, 0x1203, 0x1205, 0x1206
LF_BCLASS, LF_VBCLASS, LF_IVBCLASS, LF_INDEX, LF_VFUNCTAB = 0x1400, 0x1401, 0x1402, 0x1404, 0x1409
LF_ENUMERATE, LF_ARRAY, LF_CLASS, LF_STRUCTURE, LF_UNION, LF_ENUM = 0x1502, 0x1503, 0x1504, 0x1505, 0x1506, 0x1507
LF_MEMBER, LF_STMEMBER, LF_METHOD, LF_NESTTYPE, LF_ONEMETHOD = 0x150D, 0x150E, 0x150F, 0x1510, 0x1511
UDTS = {LF_CLASS, LF_STRUCTURE, LF_UNION, LF_ENUM}
INTRO_VIRTUAL = {4, 6}  # method property: introducing virtual, pure introducing virtual
SIMPLE = {0x03: "void", 0x08: "HRESULT", 0x10: "signed char", 0x20: "unsigned char", 0x68: "int8", 0x69: "uint8",
          0x70: "char", 0x71: "wchar_t", 0x7A: "char16_t", 0x7B: "char32_t", 0x7C: "char8_t",
          0x11: "short", 0x21: "unsigned short", 0x72: "int16", 0x73: "uint16", 0x12: "long", 0x22: "unsigned long",
          0x74: "int", 0x75: "unsigned int", 0x13: "__int64", 0x23: "unsigned __int64", 0x76: "int64", 0x77: "uint64",
          0x40: "float", 0x41: "double", 0x30: "bool", 0x31: "bool16", 0x32: "bool32", 0x33: "bool64"}


@dataclass
class UDT:
    kind: str
    name: str
    size: int
    bases: list = field(default_factory=list)      # (name, offset)
    members: list = field(default_factory=list)    # (offset, name, type)
    methods: list = field(default_factory=list)    # (name, type, vtable offset or None)
    statics: list = field(default_factory=list)    # (name, type)
    enumerators: list = field(default_factory=list)  # (name, value)


def _numeric(buf, off):
    """A CodeView numeric leaf: (value, next offset)."""
    v = struct.unpack_from("<H", buf, off)[0]
    if v < 0x8000:
        return v, off + 2
    fmt = {0x8000: "<b", 0x8001: "<h", 0x8002: "<H", 0x8003: "<i", 0x8004: "<I", 0x8009: "<q", 0x800A: "<Q"}.get(v)
    if not fmt:
        return 0, off + 2
    return struct.unpack_from(fmt, buf, off + 2)[0], off + 2 + struct.calcsize(fmt)


class Types:
    """The type stream, indexed lazily: udt(name), vtable(name), describe(name), name(type index)."""

    def __init__(self, path):
        with MSF(path) as m:
            self.buf = m.read(2)
        if len(self.buf) < 56:
            raise NotPDB(f"{path}: no type stream")
        _, header, self.first, self.end, size = struct.unpack_from("<IIIII", self.buf)
        self.offsets, off = [], header
        while off + 4 <= min(header + size, len(self.buf)):
            self.offsets.append(off)
            off += 2 + struct.unpack_from("<H", self.buf, off)[0]
        self._by_name = None

    def record(self, ti):
        i = ti - self.first
        if not 0 <= i < len(self.offsets):
            return None, b""
        off = self.offsets[i]
        length, kind = struct.unpack_from("<HH", self.buf, off)
        return kind, self.buf[off + 4:off + 2 + length]

    def _index(self):
        if self._by_name is None:
            self._by_name = {}
            for i, off in enumerate(self.offsets):
                kind, prop = struct.unpack_from("<HH", self.buf, off + 2)[0], struct.unpack_from("<H", self.buf, off + 6)[0]
                if kind in UDTS and not prop & 0x80:  # 0x80: a forward reference
                    _, body = self.record(self.first + i)
                    self._by_name.setdefault(self._udt_name(kind, body), self.first + i)
        return self._by_name

    @staticmethod
    def _udt_name(kind, body):
        if kind == LF_ENUM:
            return _cstr(body, 12)
        off = 4 + 4 * 3 if kind != LF_UNION else 4 + 4
        _, off = _numeric(body, off)
        return _cstr(body, off)

    def name(self, ti, depth=0):
        if ti < 0x1000:
            base = SIMPLE.get(ti & 0xFF, f"T{ti & 0xFF:#x}")
            return base + ("*" if ti >> 8 & 0xF else "")
        if depth > 12:
            return "..."
        kind, body = self.record(ti)
        if kind in UDTS:
            return self._udt_name(kind, body)
        if kind == LF_POINTER:
            referent, attr = struct.unpack_from("<II", body)
            mode = attr >> 5 & 7
            return self.name(referent, depth + 1) + {1: "&", 4: "&&"}.get(mode, "*")
        if kind == LF_MODIFIER:
            ti2, mods = struct.unpack_from("<IH", body)
            return ("const " if mods & 1 else "") + ("volatile " if mods & 2 else "") + self.name(ti2, depth + 1)
        if kind == LF_ARRAY:
            elem = struct.unpack_from("<I", body)[0]
            size, _ = _numeric(body, 8)
            return f"{self.name(elem, depth + 1)}[{size}]"
        if kind == LF_BITFIELD:
            ti2, length, _ = struct.unpack_from("<IBB", body)
            return f"{self.name(ti2, depth + 1)}:{length}"
        if kind in (LF_PROCEDURE, LF_MFUNCTION):
            return self.signature(ti, "(*)", depth)
        return f"<type {ti:#x}>"

    def signature(self, ti, name="", depth=0):
        kind, body = self.record(ti)
        if kind == LF_MFUNCTION:
            ret, _, _, _, _, _, args = struct.unpack_from("<IIIBBHI", body)
        elif kind == LF_PROCEDURE:
            ret, _, _, _, args = struct.unpack_from("<IBBHI", body)
        else:
            return name
        _, alist = self.record(args)
        n = struct.unpack_from("<I", alist)[0] if len(alist) >= 4 else 0
        params = [self.name(struct.unpack_from("<I", alist, 4 + 4 * i)[0], depth + 1) for i in range(n)]
        return f"{name}({', '.join(p for p in params if p != 'void')})"

    def declaration(self, ti, name):
        """"ret name(params)" for a function type (LF_PROCEDURE or LF_MFUNCTION), else just the name."""
        kind, body = self.record(ti)
        if kind not in (LF_PROCEDURE, LF_MFUNCTION):
            return name
        return f"{self.name(struct.unpack_from('<I', body)[0])} {self.signature(ti, name)}"

    def udt(self, name):
        ti = self._index().get(name)
        if ti is None:
            return None
        kind, body = self.record(ti)
        if kind == LF_ENUM:
            fields = struct.unpack_from("<I", body, 8)[0]
            out = UDT("enum", name, 0)
        else:
            fields = struct.unpack_from("<I", body, 4)[0]
            size, _ = _numeric(body, 16 if kind != LF_UNION else 8)
            out = UDT({LF_CLASS: "class", LF_STRUCTURE: "struct", LF_UNION: "union"}[kind], name, size)
        self._fields(fields, out)
        return out

    def _fields(self, ti, out):
        kind, body = self.record(ti)
        if kind != LF_FIELDLIST:
            return
        off = 0
        while off + 2 <= len(body):
            leaf = struct.unpack_from("<H", body, off)[0]
            if leaf >= 0xF0 and leaf & 0xFF >= 0xF0:  # LF_PAD bytes
                off += 1
                continue
            p = off + 2
            if leaf == LF_BCLASS:
                _, base = struct.unpack_from("<HI", body, p)
                offset, p = _numeric(body, p + 6)
                out.bases.append((self.name(base), offset))
            elif leaf in (LF_VBCLASS, LF_IVBCLASS):
                _, base, _ = struct.unpack_from("<HII", body, p)
                _, p = _numeric(body, p + 10)
                _, p = _numeric(body, p)
                out.bases.append((self.name(base), None))
            elif leaf == LF_MEMBER:
                _, mtype = struct.unpack_from("<HI", body, p)
                offset, p = _numeric(body, p + 6)
                name = _cstr(body, p)
                out.members.append((offset, name, self.name(mtype)))
                p += len(name.encode()) + 1
            elif leaf == LF_STMEMBER:
                _, mtype = struct.unpack_from("<HI", body, p)
                name = _cstr(body, p + 6)
                out.statics.append((name, self.name(mtype)))
                p += 6 + len(name.encode()) + 1
            elif leaf == LF_ONEMETHOD:
                attr, mtype = struct.unpack_from("<HI", body, p)
                p += 6
                vbase = None
                if attr >> 2 & 7 in INTRO_VIRTUAL:
                    vbase = struct.unpack_from("<I", body, p)[0]
                    p += 4
                name = _cstr(body, p)
                out.methods.append((name, mtype, vbase))
                p += len(name.encode()) + 1
            elif leaf == LF_METHOD:
                count, mlist = struct.unpack_from("<HI", body, p)
                name = _cstr(body, p + 6)
                out.methods += [(name, mtype, vbase) for mtype, vbase in self._method_list(mlist, count)]
                p += 6 + len(name.encode()) + 1
            elif leaf in (LF_NESTTYPE, LF_VFUNCTAB, LF_INDEX):
                ti2 = struct.unpack_from("<I", body, p + 2)[0]
                p += 6
                if leaf == LF_NESTTYPE:
                    p += len(_cstr(body, p).encode()) + 1
                elif leaf == LF_INDEX:
                    self._fields(ti2, out)
            elif leaf == LF_ENUMERATE:
                value, p = _numeric(body, p + 2)
                name = _cstr(body, p)
                out.enumerators.append((name, value))
                p += len(name.encode()) + 1
            else:
                break  # an entry this reader does not know: stop rather than misread the rest
            off = p

    def _method_list(self, ti, count):
        kind, body = self.record(ti)
        out, p = [], 0
        while kind == LF_METHODLIST and p + 8 <= len(body) and len(out) < count:
            attr, _, mtype = struct.unpack_from("<HHI", body, p)
            p += 8
            vbase = None
            if attr >> 2 & 7 in INTRO_VIRTUAL:
                vbase = struct.unpack_from("<I", body, p)[0]
                p += 4
            out.append((mtype, vbase))
        return out

    def vtable(self, name, depth=0):
        """{vtable offset: "Class::method(args)"} for the primary vtable, bases first, from introducing virtuals."""
        u = self.udt(name)
        if u is None or depth > 16:
            return {}
        out = {}
        for base, offset in u.bases:
            if offset == 0:
                out.update(self.vtable(base, depth + 1))
        for mname, mtype, vbase in u.methods:
            if vbase is not None:
                out[vbase] = self.signature(mtype, f"{name}::{mname}")
        return out

    def describe(self, name):
        u = self.udt(name)
        if u is None:
            return f"{name}: not in this PDB's types\n"
        lines = [f"{u.kind} {u.name}  (size {u.size:#x})"]
        lines += [f"  base {b}" + (f" at +{o:#x}" if o is not None else " (virtual)") for b, o in u.bases]
        lines += [f"  +{o:#06x}  {t} {n}" for o, n, t in sorted(u.members)]
        lines += [f"  static {t} {n}" for n, t in u.statics]
        lines += [f"  {n} = {v}" for n, v in u.enumerators]
        lines += [f"  vtable +{o:#05x}  {m}" for o, m in sorted(self.vtable(name).items())]
        lines += [f"  method {self.signature(t, n)}" for n, t, _ in u.methods]
        return "\n".join(lines) + "\n"
