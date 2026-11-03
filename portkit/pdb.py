"""A small read-only reader for Microsoft PDB files (MSF 7.00).

Reads what the recompilation needs: the signature (a PDB belongs to a binary
when its GUID and age match the binary's CodeView record, which pe.parse reads
as pdb_guid and pdb_age), every function with its source file, lines and named
locals, and the public symbols with their decorated names. `Details` reads one
function further: the line of every instruction, its inlined calls, and where
each typed local lives; `Types.field_path` names the member at an offset.
"""

import math
import re
import struct
from dataclasses import dataclass, field

MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"

S_END, S_GPROC32, S_LPROC32, S_PUB32 = 0x0006, 0x1110, 0x110F, 0x110E
S_LOCAL, S_REGREL32, S_INLINESITE, S_INLINESITE_END = 0x113E, 0x1111, 0x114D, 0x114E
S_GPROC32_ID, S_LPROC32_ID, S_INLINESITE2 = 0x1147, 0x1146, 0x115D
S_BLOCK32, S_FRAMEPROC = 0x1103, 0x1012
S_DEFRANGE_REGISTER, S_DEFRANGE_FRAMEPOINTER_REL, S_DEFRANGE_SUBFIELD_REGISTER = 0x1141, 0x1142, 0x1143
S_DEFRANGE_FRAMEPOINTER_REL_FULL_SCOPE, S_DEFRANGE_REGISTER_REL = 0x1144, 0x1145
CV_RSP, CV_RBP, CV_R13 = 335, 334, 341  # CodeView's x64 registers: rax 328, rbx, rcx, rdx, rsi, rdi, rbp, rsp, r8-r15
PROCS = {S_GPROC32, S_LPROC32, S_GPROC32_ID, S_LPROC32_ID}
DEBUG_S_LINES, DEBUG_S_FILECHKSMS, DEBUG_S_INLINEELINES = 0xF2, 0xF4, 0xF6
HIDDEN_LINE = 0xF00000  # 0xfeefee and 0xf00f00 mark code with no source line
LF_FUNC_ID, LF_MFUNC_ID, LF_STRING_ID = 0x1601, 0x1602, 0x1605


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
    module: int = 0xFFFF  # the module stream holding its symbols, and the offset of its record there (Details)
    record: int = 0


@dataclass
class InlineSite:
    name: str
    file: str | None   # where the inlinee is defined, and its first line (DEBUG_S_INLINEELINES)
    line: int | None
    depth: int         # 0: inlined into the function itself, 1: into an inline site of depth 0...
    ranges: list = field(default_factory=list)   # (start, end, file, line), offsets from the function's start


@dataclass
class Variable:
    name: str
    type_index: int
    homes: list = field(default_factory=list)  # (kind, register, offset, [(start, end)]): where it lives, when
    param: bool = False


# Home kinds: "register": the value is in the register; "field": the variable's field at `offset` is in the
# register; "memory": the variable is at [register + offset]. Ranges are offsets from the function's start,
# gaps removed; ranges the compiler marks "may have no name" (not on every path) are left out.


@dataclass
class Detail:
    lines: list      # (offset, file, line) from the line table, every file, in address order
    inlines: list    # InlineSite, in the order of their records (a parent before its children)
    variables: list = field(default_factory=list)  # Variable, the function's and its inline sites'


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


def _files(c13, names):
    """{checksum offset: source file} from a module's DEBUG_S_FILECHKSMS."""
    files = {}
    for kind, body in _subsections(c13):
        if kind == DEBUG_S_FILECHKSMS:
            q = 0
            while q + 6 <= len(body):
                name_off, size, _ = struct.unpack_from("<IBB", body, q)
                files[q] = _cstr(names, 12 + name_off)
                q = (q + 6 + size + 3) & ~3
    return files


def _line_tables(c13):
    """For each DEBUG_S_LINES subsection: (segment, offset, [(file checksum offset, [(offset from the function's
    start, line)])]), hidden lines left out. Functions folded at one address in one module each have their own
    subsection, in the order of their records."""
    for kind, body in _subsections(c13):
        if kind == DEBUG_S_LINES and len(body) >= 12:
            off, seg, _, _ = struct.unpack_from("<IHHI", body)
            q, blocks = 12, []
            while q + 12 <= len(body):
                fid, n, size = struct.unpack_from("<III", body, q)
                entries = []
                for i in range(n):
                    at, number = struct.unpack_from("<II", body, q + 12 + 8 * i)
                    if number & 0xFFFFFF < HIDDEN_LINE:
                        entries.append((at, number & 0xFFFFFF))
                blocks.append((fid, entries))
                q += max(size, 12)
            yield seg, off, blocks


def _lines(c13, names):
    """{(segment, offset): [(source file, first line, last line) or None]} from a module's C13 line info, one
    entry per subsection: the lines of the function's first file."""
    files, spans = _files(c13, names), {}
    for seg, off, blocks in _line_tables(c13):
        nums = [n for fid, entries in blocks if fid == blocks[0][0] for _, n in entries] if blocks else []
        spans.setdefault((seg, off), []).append((files.get(blocks[0][0]), min(nums), max(nums)) if nums else None)
    return spans


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
            out += _module_functions(data[:sym_bytes], compiland, lines, stream)
    pubs, seen, unique = publics(path), set(), []
    for f in sorted(out, key=lambda f: (f.segment, f.offset)):
        if (key := (f.name, f.segment, f.offset)) in seen:  # inline functions emitted by several compilands
            continue
        seen.add(key)
        f.mangled = pubs.get((f.segment, f.offset))
        f.locals = list(dict.fromkeys(f.locals))  # parameters appear as S_LOCAL and S_REGREL32
        unique.append(f)
    return unique


def _module_functions(syms, compiland, lines, stream=0xFFFF):
    out, current, end_at, inline_depth, folded = [], None, None, 0, {}
    for off, kind, body in _records(syms, 4):
        if kind in PROCS and len(body) >= 35:
            _, p_end, _, length, _, _, ti, offset, seg = struct.unpack_from("<IIIIIIIIH", body)
            current = Function(_cstr(body, 35), seg, offset, length, compiland, type_index=ti, module=stream, record=off)
            k = folded[(seg, offset)] = folded.get((seg, offset), -1) + 1
            spans = lines.get((seg, offset)) or [None]
            current.source, current.first_line, current.last_line = spans[min(k, len(spans) - 1)] or (None, None, None)
            out.append(current)
            end_at, inline_depth = p_end, 0
        elif current is None:
            continue
        elif off == end_at:
            current = None
        elif kind in (S_INLINESITE, S_INLINESITE2):
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


def _annotations(data):
    """The binary annotations of an inline site, as (opcode, operands...): line offsets signed, the combined
    ChangeCodeOffsetAndLineOffset split into (code delta, line delta)."""
    q = 0

    def operand():
        nonlocal q
        b = data[q]
        if b & 0x80 == 0:
            q += 1
            return b
        if b & 0xC0 == 0x80:
            q += 2
            return (b & 0x3F) << 8 | data[q - 1]
        q += 4
        return (b & 0x1F) << 24 | data[q - 3] << 16 | data[q - 2] << 8 | data[q - 1]

    def signed(v):
        return -(v >> 1) if v & 1 else v >> 1

    while q < len(data):
        op = operand()
        if op == 0 or op > 13:  # 0: the padding after the last one
            return
        if op == 11:
            v = operand()
            yield op, v & 0xF, signed(v >> 4)
        elif op == 12:
            yield op, operand(), operand()
        elif op in (6, 10):
            yield op, signed(operand())
        else:
            yield op, operand()


def _inline_ranges(data, file, line, files):
    """An inline site's code ranges, from its binary annotations: (start, end, file, line), offsets from the
    function's start. Code offsets accumulate their deltas; a range ends where the next starts, or after an
    explicit length (ChangeCodeLength, which also moves past it, as LLVM writes it)."""
    ranges, code, open_ = [], 0, None

    def start(at):
        nonlocal open_
        if open_ is not None:
            ranges[open_] = (ranges[open_][0], at) + ranges[open_][2:]
        ranges.append((at, None, file, line))
        open_ = len(ranges) - 1

    for op, *v in _annotations(data):
        if op == 1:                        # CodeOffset
            code = v[0]
            start(code)
        elif op == 3:                      # ChangeCodeOffset
            code += v[0]
            start(code)
        elif op == 4 and open_ is not None:  # ChangeCodeLength
            ranges[open_] = (ranges[open_][0], ranges[open_][0] + v[0]) + ranges[open_][2:]
            code, open_ = ranges[open_][0] + v[0], None
        elif op == 5:                      # ChangeFile
            file = files.get(v[0], file)
        elif op == 6:                      # ChangeLineOffset
            line += v[0]
        elif op == 11:                     # ChangeCodeOffsetAndLineOffset
            code, line = code + v[0], line + v[1]
            start(code)
        elif op == 12:                     # ChangeCodeLengthAndCodeOffset
            code += v[1]
            start(code)
            ranges[open_] = (code, code + v[0]) + ranges[open_][2:]
            open_ = None
    return [r for r in ranges if r[1] is not None and r[1] > r[0]]


def _unqualified(name):
    """The last component of a qualified name, "::" inside template arguments left alone."""
    depth, cut = 0, 0
    for i, c in enumerate(name):
        depth += (c == "<") - (c == ">")
        if depth == 0 and name.startswith("::", i):
            cut = i + 2
    return name[cut:]


def _range(body, base):
    """A CV_LVAR_ADDR_RANGE and the gaps after it, as [(start, end)] from `base` (the function's offset)."""
    start, _, length = struct.unpack_from("<IHH", body)
    out, at = [], start - base
    for q in range(8, len(body) - 3, 4):
        gap, size = struct.unpack_from("<HH", body, q)
        out.append((at, start - base + gap))
        at = start - base + gap + size
    out.append((at, start - base + length))
    return [(a, b) for a, b in out if b > a]


class Details:
    """What a function's records say beyond its name and lines: the line of every instruction, its inlined
    calls, its typed locals and where they live. Reads one module at a time (the last few are kept)."""

    def __init__(self, path, types=None):
        self.path, self._types, self._ipi, self._modules = path, types, None, {}
        with MSF(path) as m:
            dbi, _, modi_size = _dbi(m)
            self.names = m.read(_named_streams(m.read(1)).get("/names", 0xFFFF))
        self.layout, off = {}, 64
        while off < 64 + modi_size:
            stream, sym_bytes, c11, c13 = struct.unpack_from("<HIII", dbi, off + 34)
            name_end = dbi.index(b"\0", off + 64)
            off = (dbi.index(b"\0", name_end + 1) + 4) & ~3
            self.layout[stream] = (sym_bytes, c11, c13)

    @property
    def types(self):
        if self._types is None:
            self._types = Types(self.path)
        return self._types

    @property
    def ipi(self):
        if self._ipi is None:
            self._ipi = Types(self.path, stream=4)
        return self._ipi

    def _module(self, stream):
        if stream not in self._modules:
            if len(self._modules) >= 8:
                self._modules.pop(next(iter(self._modules)))
            sym_bytes, c11, c13 = self.layout.get(stream, (0, 0, 0))
            with MSF(self.path) as m:
                data = m.read(stream)
            c13 = data[sym_bytes + c11:sym_bytes + c11 + c13]
            files, lines, inlinees = _files(c13, self.names), {}, {}
            for seg, off, blocks in _line_tables(c13):
                entries = sorted((at, files.get(fid), n) for fid, rows in blocks for at, n in rows)
                lines.setdefault((seg, off), []).append(entries)
            ordinal, count = {}, {}
            for off, kind, body in _records(data[:sym_bytes], 4):
                if kind in PROCS and len(body) >= 35:
                    key = struct.unpack_from("<IH", body, 28)
                    ordinal[off] = count[key] = count.get(key, -1) + 1
            for kind, body in _subsections(c13):
                if kind == DEBUG_S_INLINEELINES and len(body) >= 4:
                    extended, q = struct.unpack_from("<I", body)[0] == 1, 4
                    while q + 12 <= len(body):
                        inlinee, fid, line = struct.unpack_from("<III", body, q)
                        inlinees[inlinee] = (files.get(fid), line)
                        q += 12 + (4 + 4 * struct.unpack_from("<I", body, q + 12)[0] if extended else 0)
            self._modules[stream] = (data[:sym_bytes], files, lines, inlinees, ordinal)
        return self._modules[stream]

    def inlinee_name(self, item):
        kind, body = self.ipi.record(item)
        if kind == LF_MFUNC_ID:
            cls, name = self.types.name(struct.unpack_from("<I", body)[0]), _cstr(body, 8)
            if name in ("{ctor}", "{dtor}"):  # MSVC's names for them
                short = re.sub(r"<.*", "", _unqualified(cls))
                name = short if name == "{ctor}" else "~" + short
            return f"{cls}::{name}"
        if kind == LF_FUNC_ID:
            scope, name = struct.unpack_from("<I", body)[0], _cstr(body, 8)
            skind, sbody = self.ipi.record(scope) if scope else (None, b"")
            return f"{_cstr(sbody, 4)}::{name}" if skind == LF_STRING_ID else name
        return f"<inlinee {item:#x}>"

    def detail(self, f):
        syms, files, lines, inlinees, ordinal = self._module(f.module)
        tables = lines.get((f.segment, f.offset)) or [[]]
        out = Detail(tables[min(ordinal.get(f.record, 0), len(tables) - 1)], [])
        if f.record + 4 > len(syms):
            return out
        end_at = struct.unpack_from("<I", syms, f.record + 8)[0]
        scopes, variable, frame, framed = [("proc", [(0, f.length)])], None, 0, []
        for off, kind, body in _records(syms, f.record, end_at):
            if kind in (S_INLINESITE, S_INLINESITE2) and len(body) >= 12:
                inlinee = struct.unpack_from("<I", body, 8)[0]
                file, line = inlinees.get(inlinee, (None, None))
                annotations = body[16 if kind == S_INLINESITE2 else 12:]
                site = InlineSite(self.inlinee_name(inlinee), file, line, sum(k == "inline" for k, _ in scopes),
                                  _inline_ranges(annotations, file, line or 0, files))
                out.inlines.append(site)
                scopes.append(("inline", [(a, b) for a, b, *_ in site.ranges]))
            elif kind == S_INLINESITE_END and scopes[-1][0] == "inline":
                scopes.pop()
            elif kind == S_BLOCK32 and len(body) >= 18:
                length, start = struct.unpack_from("<II", body, 8)
                scopes.append(("block", [(start - f.offset, start - f.offset + length)]))
            elif kind == S_END and scopes[-1][0] == "block":
                scopes.pop()
            elif kind == S_LOCAL and len(body) >= 6:
                ti, flags = struct.unpack_from("<IH", body)
                variable = Variable(_cstr(body, 6), ti, param=bool(flags & 1))
                out.variables.append(variable)
                continue
            elif kind == S_REGREL32 and len(body) >= 10:
                at, ti, reg = struct.unpack_from("<iIH", body)
                out.variables.append(Variable(_cstr(body, 10), ti, [("memory", reg, at, scopes[-1][1])]))
            elif kind == S_FRAMEPROC and len(body) >= 26:
                frame = struct.unpack_from("<I", body, 22)[0]
            elif variable is not None and kind == S_DEFRANGE_REGISTER and len(body) >= 12:
                reg, maybe = struct.unpack_from("<HH", body)
                if not maybe & 1 and (where := _range(body[4:], f.offset)):
                    variable.homes.append(("register", reg, 0, where))
                continue
            elif variable is not None and kind == S_DEFRANGE_SUBFIELD_REGISTER and len(body) >= 16:
                reg, maybe, parent = struct.unpack_from("<HHI", body)
                if not maybe & 1 and (where := _range(body[8:], f.offset)):
                    variable.homes.append(("field", reg, parent & 0xFFF, where))
                continue
            elif variable is not None and kind == S_DEFRANGE_REGISTER_REL and len(body) >= 16:
                reg, flags, at = struct.unpack_from("<HHi", body)
                if not flags & 1 and (where := _range(body[8:], f.offset)):  # 1: a spilled member, not the whole
                    variable.homes.append(("memory", reg, at, where))
                continue
            elif variable is not None and kind == S_DEFRANGE_FRAMEPOINTER_REL and len(body) >= 12:
                if where := _range(body[4:], f.offset):
                    framed.append((variable, struct.unpack_from("<i", body)[0], where))
                continue
            elif variable is not None and kind == S_DEFRANGE_FRAMEPOINTER_REL_FULL_SCOPE and len(body) >= 4:
                framed.append((variable, struct.unpack_from("<i", body)[0], scopes[-1][1]))
                continue
            variable = None
        # Frame-relative homes: from the frame register S_FRAMEPROC names (it follows them), one for
        # parameters, one for locals: 1 rsp after the prolog, 2 rbp, 3 r13.
        for v, at, where in framed:
            reg = {1: CV_RSP, 2: CV_RBP, 3: CV_R13}.get(frame >> (16 if v.param else 14) & 3)
            if reg:
                v.homes.append(("memory", reg, at, where))
        return out

# Type records (TPI stream). Only what describing a class needs: layouts, bases, members and virtual functions.
LF_MODIFIER, LF_POINTER, LF_PROCEDURE, LF_MFUNCTION = 0x1001, 0x1002, 0x1008, 0x1009
LF_ARGLIST, LF_FIELDLIST, LF_BITFIELD, LF_METHODLIST = 0x1201, 0x1203, 0x1205, 0x1206
LF_BCLASS, LF_VBCLASS, LF_IVBCLASS, LF_INDEX, LF_VFUNCTAB = 0x1400, 0x1401, 0x1402, 0x1404, 0x1409
LF_ENUMERATE, LF_ARRAY, LF_CLASS, LF_STRUCTURE, LF_UNION, LF_ENUM = 0x1502, 0x1503, 0x1504, 0x1505, 0x1506, 0x1507
LF_MEMBER, LF_STMEMBER, LF_METHOD, LF_NESTTYPE, LF_ONEMETHOD = 0x150D, 0x150E, 0x150F, 0x1510, 0x1511
UDTS = {LF_CLASS, LF_STRUCTURE, LF_UNION, LF_ENUM}
INTRO_VIRTUAL = {4, 6}  # method property: introducing virtual, pure introducing virtual
SIMPLE_SIZE = {0x03: 0, 0x08: 4, 0x10: 1, 0x20: 1, 0x68: 1, 0x69: 1, 0x70: 1, 0x7C: 1, 0x30: 1, 0x71: 2, 0x7A: 2,
               0x11: 2, 0x21: 2, 0x72: 2, 0x73: 2, 0x31: 2, 0x12: 4, 0x22: 4, 0x74: 4, 0x75: 4, 0x7B: 4, 0x40: 4,
               0x32: 4, 0x13: 8, 0x23: 8, 0x76: 8, 0x77: 8, 0x41: 8, 0x33: 8}
VFPTR = 0x0603  # a 64-bit void*: the type given to a class's vtable pointer
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
    fields: list = field(default_factory=list)     # (offset, name, type index): members, bases (name None), vfptr


def _join_path(name, path):
    return f"{name}{path}" if path.startswith("[") else f"{name}.{path}"


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
    """The type stream, indexed lazily: udt(name), vtable(name), describe(name), name(type index). With
    stream=4, the IPI stream (function ids), which has the same layout."""

    def __init__(self, path, stream=2):
        with MSF(path) as m:
            self.buf = m.read(stream)
        if len(self.buf) < 56:
            raise NotPDB(f"{path}: no type stream")
        _, header, self.first, self.end, size = struct.unpack_from("<IIIII", self.buf)
        self.offsets, off = [], header
        while off + 4 <= min(header + size, len(self.buf)):
            self.offsets.append(off)
            off += 2 + struct.unpack_from("<H", self.buf, off)[0]
        self._by_name, self._layouts = None, {}

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
        return None if ti is None else self.udt_at(ti)

    def udt_at(self, ti):
        """The class, struct, union or enum defined at type index ti (a forward reference: its definition)."""
        ti = self.resolve(ti)
        kind, body = self.record(ti)
        if kind not in UDTS:
            return None
        name = self._udt_name(kind, body)
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
                out.fields.append((offset, None, base))
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
                out.fields.append((offset, name, mtype))
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
                if leaf == LF_VFUNCTAB:
                    out.fields.append((0, "__vfptr", VFPTR))
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

    def resolve(self, ti):
        """ti with its modifiers (const, volatile) taken off, and a forward reference replaced by the definition."""
        for _ in range(8):
            kind, body = self.record(ti)
            if kind == LF_MODIFIER:
                ti = struct.unpack_from("<I", body)[0]
            elif kind in UDTS and struct.unpack_from("<H", body, 2)[0] & 0x80:
                return self._index().get(self._udt_name(kind, body), ti)
            else:
                return ti
        return ti

    def pointee(self, ti):
        """The type a pointer or reference type points to, resolved; None for anything else."""
        kind, body = self.record(self.resolve(ti))
        return self.resolve(struct.unpack_from("<I", body)[0]) if kind == LF_POINTER else None

    def size(self, ti, depth=0):
        if ti < 0x1000:
            return 8 if ti >> 8 & 0xF else SIMPLE_SIZE.get(ti & 0xFF, 0)
        kind, body = self.record(self.resolve(ti))
        if kind == LF_POINTER:
            return struct.unpack_from("<I", body, 4)[0] >> 13 & 0x3F
        if kind == LF_ARRAY:
            return _numeric(body, 8)[0]
        if kind in (LF_CLASS, LF_STRUCTURE, LF_UNION):
            return _numeric(body, 16 if kind != LF_UNION else 8)[0]
        if kind in (LF_ENUM, LF_BITFIELD) and depth < 4:
            return self.size(struct.unpack_from("<I", body, 4 if kind == LF_ENUM else 0)[0], depth + 1)
        return 0

    def field_path(self, ti, offset, size=None, mask=None):
        """The member of type ti at `offset`, read `size` bytes at a time (None: its address is taken):
        "mWidget", "mPair.B", "mSlots[1]", "mWidget+0x2", a base class's member by its name; "" for an access
        covering the whole of ti; None in padding or past the end. A wider access is named by the member it
        starts at. Bitfields are named by the bits the access reaches, their positions from the PDB: those of
        `mask` (the immediate of a test, and, or) in the bytes at `offset`, else every bit of those bytes."""
        if offset == 0 and size is not None and size >= self.size(ti) > 0 and mask is None:
            return ""
        path = self._path(ti, offset, size, 0, mask)
        return path.lstrip(".") if path else None

    def _path(self, ti, offset, size, depth, mask=None):
        ti = self.resolve(ti)
        kind, body = self.record(ti)
        if depth > 12 or offset < 0:
            return None
        if kind == LF_ARRAY:
            each = self.size(struct.unpack_from("<I", body)[0])
            if not each or offset >= self.size(ti):
                return None
            return f"[{offset // each}]" + self._inside(struct.unpack_from("<I", body)[0], offset % each, size, depth,
                                                       mask)
        if kind not in (LF_CLASS, LF_STRUCTURE, LF_UNION):
            return None
        if ti not in self._layouts:
            self._layouts[ti] = self.udt_at(ti).fields
        fields = self._layouts[ti]
        hits = [(o, n, t) for o, n, t in fields if n and o <= offset < o + max(self.size(t), 1)]
        if hits:
            o, n, t = hits[0]
            if self.record(t)[0] == LF_BITFIELD:  # bitfields share their bytes: those whose bits it reaches
                names = self._bits([(n2, t2) for o2, n2, t2 in hits if o2 == o], offset - o, size, mask)
                return "." + "|".join(names[:4]) + ("|..." if len(names) > 4 else "") if names else None
            return "." + n + self._inside(t, offset - o, size, depth, mask)
        for o, n, t in fields:
            if n is None and o is not None and o <= offset < o + self.size(t):
                return self._path(t, offset - o, size, depth + 1, mask)
        return None

    def _bits(self, members, offset, size, mask):
        """The bitfields of one storage unit [(name, type)] that an access `offset` bytes into it reaches."""
        if mask is not None:
            bits = mask << 8 * offset
        elif size:
            bits = ((1 << 8 * size) - 1) << 8 * offset
        else:
            bits = -1  # the address of them all
        out = []
        for name, ti in members:
            kind, body = self.record(ti)
            if kind == LF_BITFIELD:
                _, length, position = struct.unpack_from("<IBB", body)
                if bits >> position & ((1 << length) - 1):
                    out.append(name)
        return out

    def member_at(self, ti, offset, depth=0):
        """(path, type index) of the member that starts at `offset` and holds no member of its own there:
        ("SharedReferenceCount.ReferenceController", ...) inside a TSharedPtr; None if there is none."""
        ti = self.resolve(ti)
        kind, _ = self.record(ti)
        if kind not in (LF_CLASS, LF_STRUCTURE, LF_UNION) or depth > 12:
            return None
        if ti not in self._layouts:
            self._layouts[ti] = self.udt_at(ti).fields
        for o, n, t in self._layouts[ti]:
            if o is not None and o <= offset < o + max(self.size(t), 1):
                inner = self.member_at(t, offset - o, depth + 1)
                if n is None:
                    return inner
                if inner:
                    return _join_path(n, inner[0]), inner[1]
                return (n, t) if o == offset else None
        return None

    def _inside(self, ti, offset, size, depth, mask=None):
        """What follows a member's name: nothing when the access covers the whole member, else the field
        inside it, else +offset."""
        if offset == 0 and (size is None or size >= self.size(ti)) and mask is None:
            return ""
        return self._path(ti, offset, size, depth + 1, mask) or (f"+{offset:#x}" if offset else "")

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
