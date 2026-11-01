"""A small read-only parser for Windows PE files (.exe, .dll).

Reads what the inspector needs: headers, sections, imports, delay imports,
export count, TLS callbacks and the CodeView (PDB) path. Nothing is executed.
"""

import math
import mmap
import struct
from collections import Counter
from dataclasses import dataclass, field

MACHINES = {0x14C: "x86", 0x8664: "x86-64", 0xAA64: "arm64", 0xA641: "arm64ec", 0x1C4: "arm"}
SUBSYSTEMS = {2: "windows-gui", 3: "windows-cui", 10: "efi-app"}

DIR_EXPORT, DIR_IMPORT, DIR_DEBUG, DIR_TLS, DIR_DELAY_IMPORT = 0, 1, 6, 9, 13
SCN_EXECUTE = 0x20000000


class NotPE(ValueError):
    pass


@dataclass
class Section:
    name: str
    vaddr: int
    vsize: int
    raw_ptr: int
    raw_size: int
    characteristics: int
    entropy: float | None = None

    @property
    def executable(self):
        return bool(self.characteristics & SCN_EXECUTE)


@dataclass
class PE:
    path: str
    size: int
    machine: str
    timestamp: int
    is_dll: bool
    pe32_plus: bool
    entry: int
    image_base: int
    subsystem: str
    sections: list = field(default_factory=list)
    imports: dict = field(default_factory=dict)
    delay_imports: dict = field(default_factory=dict)
    exports: int = 0
    tls_callbacks: int = 0
    pdb: str | None = None

    @property
    def code_size(self):
        return sum(s.vsize for s in self.sections if s.executable)

    def imported_dlls(self):
        return sorted({d.lower() for d in (*self.imports, *self.delay_imports)})


def entropy(data):
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def _sampled(buf, start, length, chunks=64, chunk=64 * 1024):
    """Bytes from evenly spaced chunks: enough for an entropy estimate."""
    if length <= chunks * chunk:
        return buf[start:start + length]
    step = (length - chunk) // (chunks - 1)
    return b"".join(buf[start + i * step:start + i * step + chunk] for i in range(chunks))


class _Reader:
    def __init__(self, buf, sections):
        self.buf = buf
        self.sections = sections

    def offset(self, rva):
        for s in self.sections:
            if s.vaddr <= rva < s.vaddr + max(s.vsize, s.raw_size):
                off = rva - s.vaddr
                return s.raw_ptr + off if off < s.raw_size else None
        return None

    def u16(self, rva):
        off = self.offset(rva)
        return struct.unpack_from("<H", self.buf, off)[0] if off is not None else 0

    def u32(self, rva):
        off = self.offset(rva)
        return struct.unpack_from("<I", self.buf, off)[0] if off is not None else 0

    def u64(self, rva):
        off = self.offset(rva)
        return struct.unpack_from("<Q", self.buf, off)[0] if off is not None else 0

    def cstr(self, rva, limit=512):
        off = self.offset(rva)
        if off is None:
            return ""
        end = self.buf.find(b"\0", off, off + limit)
        return bytes(self.buf[off:end if end >= 0 else off + limit]).decode("latin-1")


def _thunks(r, rva, wide, limit=100_000):
    size, ordinal_bit = (8, 1 << 63) if wide else (4, 1 << 31)
    names = []
    while rva and len(names) < limit:
        value = r.u64(rva) if wide else r.u32(rva)
        if not value:
            break
        if value & ordinal_bit:
            names.append(f"#{value & 0xFFFF}")
        else:
            names.append(r.cstr((value & 0x7FFFFFFF) + 2))
        rva += size
    return names


def _imports(r, rva, wide):
    out = {}
    while rva:
        oft, _, _, name_rva, ft = (r.u32(rva + i * 4) for i in range(5))
        if not (oft or name_rva or ft):
            break
        if name := r.cstr(name_rva):
            out.setdefault(name, []).extend(_thunks(r, oft or ft, wide))
        rva += 20
        if len(out) > 4096:
            break
    return out


def _delay_imports(r, rva, wide):
    out = {}
    while rva:
        attrs, name_rva, _, _, int_rva = (r.u32(rva + i * 4) for i in range(5))
        if not name_rva:
            break
        if not attrs & 1:  # pre-VC7 layout: VAs, not RVAs; skip rather than guess
            rva += 32
            continue
        if name := r.cstr(name_rva):
            out.setdefault(name, []).extend(_thunks(r, int_rva, wide))
        rva += 32
        if len(out) > 4096:
            break
    return out


def parse(path, with_entropy=False):
    with open(path, "rb") as f:
        try:
            buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # empty file
            raise NotPE(f"{path}: empty file")
    with buf:
        return _parse(path, buf, with_entropy)


def _parse(path, buf, with_entropy):
    if len(buf) < 0x40 or buf[:2] != b"MZ":
        raise NotPE(f"{path}: no MZ header")
    pe_off = struct.unpack_from("<I", buf, 0x3C)[0]
    if pe_off + 24 > len(buf) or buf[pe_off:pe_off + 4] != b"PE\0\0":
        raise NotPE(f"{path}: no PE signature")
    machine, nsections, timestamp, _, _, opt_size, chars = struct.unpack_from("<HHIIIHH", buf, pe_off + 4)
    opt = pe_off + 24
    magic = struct.unpack_from("<H", buf, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise NotPE(f"{path}: unknown optional header magic {magic:#x}")
    wide = magic == 0x20B
    entry = struct.unpack_from("<I", buf, opt + 16)[0]
    image_base = struct.unpack_from("<Q", buf, opt + 24)[0] if wide else struct.unpack_from("<I", buf, opt + 28)[0]
    subsystem = struct.unpack_from("<H", buf, opt + 68)[0]
    ndirs_off, dirs_off = (opt + 108, opt + 112) if wide else (opt + 92, opt + 96)
    ndirs = min(struct.unpack_from("<I", buf, ndirs_off)[0], 16)
    dirs = [struct.unpack_from("<II", buf, dirs_off + 8 * i) for i in range(ndirs)]
    dirs += [(0, 0)] * (16 - ndirs)

    sections = []
    sec_off = opt + opt_size
    for i in range(nsections):
        o = sec_off + 40 * i
        if o + 40 > len(buf):
            break
        raw_name, vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<8sIIII", buf, o)
        characteristics = struct.unpack_from("<I", buf, o + 36)[0]
        raw_size = max(0, min(raw_size, len(buf) - raw_ptr))
        s = Section(raw_name.rstrip(b"\0").decode("latin-1"), vaddr, vsize, raw_ptr, raw_size, characteristics)
        if with_entropy and raw_size:
            s.entropy = round(entropy(_sampled(buf, raw_ptr, raw_size)), 3)
        sections.append(s)

    pe = PE(
        path=str(path),
        size=len(buf),
        machine=MACHINES.get(machine, f"{machine:#x}"),
        timestamp=timestamp,
        is_dll=bool(chars & 0x2000),
        pe32_plus=wide,
        entry=entry,
        image_base=image_base,
        subsystem=SUBSYSTEMS.get(subsystem, str(subsystem)),
        sections=sections,
    )
    r = _Reader(buf, sections)
    if dirs[DIR_IMPORT][0]:
        pe.imports = _imports(r, dirs[DIR_IMPORT][0], wide)
    if dirs[DIR_DELAY_IMPORT][0]:
        pe.delay_imports = _delay_imports(r, dirs[DIR_DELAY_IMPORT][0], wide)
    if dirs[DIR_EXPORT][0]:
        pe.exports = r.u32(dirs[DIR_EXPORT][0] + 20)
    if dirs[DIR_TLS][0]:
        cb_va = r.u64(dirs[DIR_TLS][0] + 24) if wide else r.u32(dirs[DIR_TLS][0] + 12)
        rva, n = cb_va - image_base, 0
        while 0 < rva < 1 << 32 and n < 256 and (r.u64(rva) if wide else r.u32(rva)):
            n += 1
            rva += 8 if wide else 4
        pe.tls_callbacks = n
    if dirs[DIR_DEBUG][0]:
        for i in range(min(dirs[DIR_DEBUG][1] // 28, 32)):
            o = r.offset(dirs[DIR_DEBUG][0] + 28 * i)
            if o is None:
                break
            dtype, dsize, _, dptr = struct.unpack_from("<IIII", buf, o + 12)
            if dtype == 2 and buf[dptr:dptr + 4] == b"RSDS":
                end = buf.find(b"\0", dptr + 24, dptr + dsize)
                pe.pdb = bytes(buf[dptr + 24:end if end >= 0 else dptr + dsize]).decode("utf-8", "replace")
                break
    return pe
