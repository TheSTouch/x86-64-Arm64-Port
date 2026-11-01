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
            _, p_end, _, length, _, _, _, offset, seg = struct.unpack_from("<IIIIIIIIH", body)
            current = Function(_cstr(body, 35), seg, offset, length, compiland)
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
            current.locals.append(_cstr(body, 6))
        elif inline_depth == 0 and kind == S_REGREL32:
            current.locals.append(_cstr(body, 10))
    return out
