"""Work material for recovering a module's source: its functions from the PDB, and their disassembly.

The disassembly names what each instruction reaches: imported functions (through
the import address table), the module's own functions (from the PDB) and string
literals, and the members typed locals reach (`this->mFoo`); between instructions,
the source line and the inlined calls. What `dump`
writes is derived from the game, so it refuses to write inside this repository
(docs/SATISFACTORY-RECOMP.md: recovered code stays on the owner's disks). `match`
compares a compiled object file with the shipped module, and scores each function
by its instructions that differ once both disassemblies are aligned. `families`
groups functions whose code is the same once numbers are masked.
"""

import bisect
import difflib
import os
from collections import Counter
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import coff, config, pdb, pe
from .util import Fail

ADDRESS = re.compile(r"0x([0-9a-f]{6,16})")
LINE = re.compile(r"^\s*([0-9a-f]+):\s+(.*)$")
MEMORY = re.compile(r"(?:(\w+) ptr )?\[(\w+)(?:\s*([+-])\s*(0x[0-9a-f]+|\d+))?\]")  # [reg], [reg + disp]: no index
REGISTERS = {r: 328 + i for i, r in enumerate(("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
                                                "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"))}
ACCESS = {"byte": 1, "word": 2, "dword": 4, "fword": 6, "qword": 8, "tbyte": 10, "xmmword": 16, "ymmword": 32,
          "zmmword": 64}
RCX = {"rcx", "ecx", "cx", "cl", "ch"}
VERBOSITY = {1: "Fatal", 2: "Error", 3: "Warning", 4: "Display", 5: "Log", 6: "Verbose", 7: "VeryVerbose"}
INITIALIZER = "`dynamic initializer for '"
FOLDED_SHOWN = 6  # the functions folded at a call's target listed after its name
SCN_MEM_WRITE = 0x80000000


def objdump():
    """Xcode's objdump on the Mac, LLVM's llvm-objdump on Windows (the same tool; both read COFF and PE)."""
    return shutil.which("objdump") or shutil.which("llvm-objdump")


class Module:
    @staticmethod
    def pdb_of(dll):
        path = Path(dll).with_suffix(".pdb")
        if not path.is_file():
            raise Fail(f"{path.name} not found next to {Path(dll).name}")
        return path

    def __init__(self, dll):
        self.dll = Path(dll)
        self.pdb = self.pdb_of(dll)
        self.image = pe.parse(self.dll)
        self.data = self.dll.read_bytes()
        self.functions = pdb.functions(self.pdb)
        self.names = {self.rva(s, o): n for (s, o), n in pdb.publics(self.pdb).items() if 0 < s <= len(self.image.sections)}
        self.names.update({self.rva(f.segment, f.offset): f.name for f in self.functions})
        self._types = self._details = self._symbols = self._globals = self._folded = None
        self.paired = {}  # a compiled object's decorated name -> the RVA of the shipped function match paired it with

    @property
    def types(self):
        if self._types is None:
            self._types = pdb.Types(self.pdb)
        return self._types

    @property
    def details(self):
        if self._details is None:
            self._details = pdb.Details(self.pdb, self.types)
        return self._details

    def rva(self, segment, offset):
        return self.image.sections[segment - 1].vaddr + offset

    def rva_of(self, symbol):
        """The RVA in the shipped module of what a compiled object's symbol names: an import's slot in the
        import address table (__imp_X), else the function `match` paired it with, the public symbol's or the
        function's address, the function of that name (scoped_name, qualified_name) if only one; None if unknown."""
        if self._symbols is None:
            self._symbols = {f.mangled: self.rva(f.segment, f.offset) for f in self.functions if f.mangled}
            self._symbols.update({n: self.rva(s, o) for n, s, o, _ in pdb.public_symbols(self.pdb)
                                  if 0 < s <= len(self.image.sections)})
            self._symbols.update({"__imp_" + n.rpartition("!")[2]: rva for rva, n in self.image.iat.items()})
            self._by_name = {}
            for f in self.functions:
                self._by_name.setdefault(f.name, []).append(self.rva(f.segment, f.offset))
        if symbol in self.paired or symbol in self._symbols:
            return self.paired.get(symbol, self._symbols.get(symbol))
        rvas = set(self._by_name.get(scoped_name(symbol) or qualified_name(symbol) or "", ()))
        return rvas.pop() if len(rvas) == 1 else None

    def function(self, name):
        for f in self.functions:
            if f.name == name or f.mangled == name:
                return f
        raise Fail(f"no function {name!r} in {self.pdb.name}")

    def game_functions(self, source_marker=None):
        """The functions compiled from the game's own sources: a source path containing `source_marker`, or by
        default a \\Source\\ path outside the engine, UHT's generated files and third-party code."""
        if source_marker:
            return [f for f in self.functions if f.source and source_marker.lower() in f.source.lower()]
        return [f for f in self.functions if f.source and _is_game_source(f.source)]

    def _bytes_at(self, rva, n, data_only=False):
        for s in self.image.sections:
            if data_only and s.executable:
                continue
            if s.vaddr <= rva < s.vaddr + max(s.vsize, s.raw_size) and rva - s.vaddr < s.raw_size:
                start = s.raw_ptr + rva - s.vaddr
                return self.data[start:min(start + n, s.raw_ptr + s.raw_size)]
        return b""

    def string_at(self, rva, limit=200):
        raw = self._bytes_at(rva, 2 * limit, data_only=True)
        wide = raw[:len(raw) & ~1].decode("utf-16-le", "replace").split("\0", 1)[0]
        if len(wide) >= 3 and wide.isprintable() and all(ord(c) < 0x2500 for c in wide):
            return 'u"' + wide + '"'
        narrow = raw.split(b"\0", 1)[0]
        if len(narrow) >= 4 and all(32 <= c < 127 for c in narrow):
            return '"' + narrow.decode() + '"'
        return None

    def record_at(self, rva):
        """The FStaticBasicLogRecord at rva (Format, File, Line, Verbosity: what UE_LOG passes) as
        `log Warning, line 859: u"..."`; None if what is there does not read as one."""
        raw = self._bytes_at(rva, 24, data_only=True)
        if len(raw) < 24:
            return None
        text, file, line, verbosity = struct.unpack("<QQii", raw)
        base = self.image.image_base
        if not 0 < line < 1 << 20 or verbosity & 0xF not in VERBOSITY or not all(
                base < x < base + (1 << 31) for x in (text, file)):
            return None
        text, file = self.string_at(text - base), self.string_at(file - base)
        if not text or not text.startswith('u"') or not file or file.startswith('u"'):
            return None
        return f"log {VERBOSITY[verbosity & 0xF]}, line {line}: {text}"

    def global_at(self, rva):
        """The global a dynamic initializer constructs at rva, by the initializer's name: the object its first
        lea rcx, [rip + X] passes (an FString's constructor's this), in a writable section, not a public
        symbol; within its first 16 bytes as NAME+0x8. None otherwise."""
        if self._globals is None:
            names = self._initialized()
            self._globals = sorted(names), names
        keys, names = self._globals
        i = bisect.bisect_right(keys, rva) - 1
        if i < 0 or rva - keys[i] >= 0x10:
            return None
        return names[keys[i]] + (f"+{rva - keys[i]:#x}" if rva != keys[i] else "")

    def _initialized(self):
        writable = [s for s in self.image.sections if s.characteristics & SCN_MEM_WRITE]
        found = {}
        for f in self.functions:
            if not f.name.startswith(INITIALIZER) or not f.name.endswith("''"):
                continue
            start = self.rva(f.segment, f.offset)
            code = self._bytes_at(start, min(f.length, 32))
            at = code.find(b"\x48\x8d\x0d")
            if at < 0 or at + 7 > len(code):
                continue
            target = start + at + 7 + struct.unpack_from("<i", code, at + 3)[0]
            if target not in self.names and any(s.vaddr <= target < s.vaddr + s.vsize for s in writable):
                found.setdefault(target, set()).add(f.name[len(INITIALIZER):-2])
        return {rva: names.pop() for rva, names in found.items() if len(names) == 1}  # two claims: neither

    def folded(self, rva, near=None):
        """The names of the functions the linker folded at rva (identical code kept once); [] where only one
        function is. Game functions first, then in the PDB's order; with `near` (the calling function), the
        closest to it first: the most words of its name shared, then its class, source file, unity blob."""
        if self._folded is None:
            at = {}
            for f in self.functions:
                at.setdefault(self.rva(f.segment, f.offset), {}).setdefault(f.name, f)
            self._folded = {where: sorted(fs.values(), key=lambda f: not (f.source and _is_game_source(f.source)))
                            for where, fs in at.items() if len(fs) > 1}
        fs = self._folded.get(rva, [])
        if near is not None:
            fs = sorted(fs, key=lambda g: _closeness(near, g), reverse=True)
        return [f.name for f in fs]

    def label(self, rva):
        if rva in self.image.iat:
            return self.image.iat[rva]
        if folded := self.folded(rva):
            return folded[0]
        if rva in self.names and not self.names[rva].startswith("??_C@"):  # ??_C@: a string literal's symbol
            return self.names[rva]
        return self.string_at(rva) or self.record_at(rva) or self.global_at(rva)

    def disassemble(self, f, length=None):
        if not objdump():
            raise Fail("objdump not found: install Xcode's command line tools")
        base = self.image.image_base
        start = base + self.rva(f.segment, f.offset)
        out = subprocess.run([objdump(), "-d", "--no-show-raw-insn", "--x86-asm-syntax=intel",
                              f"--start-address={start:#x}", f"--stop-address={start + (length or f.length):#x}",
                              str(self.dll)], capture_output=True, text=True, check=False)
        lines = [f"; {f.name}",
                 f"; {self.types.declaration(f.type_index, f.name)}",
                 f"; {Path(f.source.replace(chr(92), '/')).name}:{f.first_line}-{f.last_line}" if f.source else "; no line info",
                 f"; rva {self.rva(f.segment, f.offset):#x}, {f.length} bytes, compiland {f.compiland}"]
        if f.mangled:
            lines.append(f"; {f.mangled}")
        if f.locals:
            lines.append("; locals: " + ", ".join(
                f"{self.types.name(f.local_types[n])} {n}" if n in f.local_types else n for n in f.locals))
        if adjusted := self.types.this_adjust(f.type_index):
            cls, at = self.types.name(adjusted[0]), adjusted[1]
            inner = self.types.base_at(*adjusted)
            lines.append(f"; this is {cls}'s {inner} base (+{at:#x})" if inner else f"; this is {cls} + {at:#x}")
        rows = []
        for raw in out.stdout.splitlines():
            if not (hit := LINE.match(raw)):
                continue
            addr, insn = int(hit.group(1), 16), re.sub(r"\s*<[^>]*>", "", hit.group(2)).strip()
            insn, _, _ = insn.partition(" # ")
            end = start + (length or f.length)
            insn = ADDRESS.sub(lambda a: f"+{int(a.group(1), 16) - start:05x}"
                               if start <= int(a.group(1), 16) < end else a.group(0), insn)
            notes = []
            for a in ADDRESS.findall(hit.group(2)):
                rva = int(a, 16) - base
                if not 0 < rva < 1 << 31 or start <= int(a, 16) < end:
                    continue
                folded = self.folded(rva, near=f)
                note = (folded[0] if folded else self.label(rva)) or (f"[{rva:#x}]" if "rip" in insn else None)
                if note and note not in notes:
                    notes.append(note)
                    if others := folded[1:]:
                        notes.append(f"folded with {len(others)}: " + ", ".join(others[:FOLDED_SHOWN])
                                     + (", ..." if len(others) > FOLDED_SHOWN else ""))
            rows.append((addr - start, insn, notes))
        detail = self.details.detail(f) if f.module != 0xFFFF else None
        source, fields = _Source(detail), _Fields(self.types, detail, f, rows)
        for i, (offset, insn, notes) in enumerate(rows):
            after = rows[i + 1][0] if i + 1 < len(rows) else f.length
            notes += [n for n in fields.notes(offset, insn, after) if n not in notes]
            lines += source.at(offset)
            lines.append(f"  +{offset:05x}  {insn:<56}" + (f" ; {'; '.join(notes)}" if notes else ""))
        return "\n".join(lines) + "\n"


def _closeness(f, g):
    """How close function g is to function f, for ranking g among functions folded at what f calls: the words
    of their unqualified names shared (GetLocalNetworkVersion, GetPlatformNetworkVersion: 3), then the same
    class, the same source file, the same unity blob, a game function."""
    def words(name):
        return set(re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?![a-z])|\d+", name.rpartition("::")[2]))
    return (len(words(f.name) & words(g.name)), f.name.rpartition("::")[0] == g.name.rpartition("::")[0],
            bool(f.source) and f.source == g.source, f.compiland == g.compiland,
            bool(g.source) and _is_game_source(g.source))


class _Fields:
    """Names what a memory operand [reg + disp] reaches, where the PDB says what reg holds: `this->mFoo`,
    `Local.mBar`, `&this->mFoo` for a lea. A store into a variable's memory is named only if the variable
    still lives there after it. Besides the PDB's ranges, `this` is in rcx at entry: up to the first
    instruction that writes rcx or calls (included: it reads its operands first), and not past where a jump
    from further on lands. Where the function's type says its `this` points at a base inside the object (a
    virtual of a base at a non-zero offset), the function's own `this` names members by their offset in the
    object: [this + disp] is the object's disp + that base's offset."""

    def __init__(self, types, detail, f, rows):
        self.types, self.variables = types, detail.variables if detail else []
        this = f.local_types.get("this")
        self.this = types.pointee(this) if this is not None else None
        adjusted = types.this_adjust(f.type_index) if self.this is not None else None
        self.adjust = adjusted[1] if adjusted else 0
        self.entry = -1
        if self.this is not None and rows:
            self.entry = next((o for o, insn, _ in rows if _writes_rcx(insn)), rows[-1][0])
            for o, insn, _ in rows:
                if o >= self.entry and insn.startswith("j") and (hit := re.search(r"\+([0-9a-f]{5})$", insn)):
                    self.entry = min(self.entry, int(hit.group(1), 16) - 1)

    def notes(self, offset, insn, after):
        mnemonic, _, operands = insn.partition("\t")
        out = []
        for hit in MEMORY.finditer(operands):
            size, reg, sign, disp = hit.groups()
            if reg in REGISTERS:
                disp = (-1 if sign == "-" else 1) * int(disp, 0) if disp else 0
                store = hit.start() == 0 and not mnemonic.startswith(("cmp", "test", "push", "bt", "ucomis", "comis"))
                mask = _mask(mnemonic, operands, ACCESS.get(size)) if hit.start() == 0 else None
                name = self._name(offset, REGISTERS[reg], disp, None if mnemonic == "lea" else ACCESS.get(size),
                                  after if store else None, mask)
                if name:
                    out.append(("&" if mnemonic == "lea" else "") + name)
        return out

    def _name(self, offset, reg, disp, size, after, mask=None):
        t = self.types
        for v in self.variables:
            for kind, r, at, ranges in v.homes:
                if r != reg or not any(a <= offset < b for a, b in ranges):
                    continue
                if kind == "register" and (target := t.pointee(v.type_index)):
                    own = v.name == "this" and not v.inlined
                    if path := t.field_path(target, disp + (self.adjust if own else 0), size, mask):
                        return f"{v.name}->{path}"
                elif kind == "field" and (held := t.member_at(v.type_index, at)):
                    if (target := t.pointee(held[1])) and (path := t.field_path(target, disp, size, mask)):
                        return f"{_join(v.name, held[0])}->{path}"
                elif kind == "memory" and 0 <= disp - at < max(t.size(v.type_index), 1):
                    if after is not None and not any(a <= after < b for a, b in ranges):
                        continue  # a store that ends the variable's life there: what it writes is something else
                    path = t.field_path(v.type_index, disp - at, size, mask)
                    if path:
                        return _join(v.name, path)
                    return v.name + (f"+{disp - at:#x}" if path is None and disp != at else "")
        if reg == REGISTERS["rcx"] and offset <= self.entry and \
                (path := t.field_path(self.this, disp + self.adjust, size, mask)):
            return f"this->{path}"
        return None


def _mask(mnemonic, operands, size):
    """The bits an instruction on memory (its first operand, `size` bytes) tests or changes, from its immediate:
    test, or, xor: the bits set; and: those it clears; bt, bts, btr, btc: the bit numbered. None otherwise."""
    hit = re.fullmatch(r".*\],\s*(-?(?:0x[0-9a-f]+|\d+))", operands.strip())
    if not hit or not size:
        return None
    value, every = int(hit.group(1), 0), (1 << 8 * size) - 1
    if mnemonic in ("test", "or", "xor"):
        return value & every
    if mnemonic == "and":
        return ~value & every
    if mnemonic in ("bt", "bts", "btr", "btc"):
        return 1 << value % (8 * size)
    return None


def _join(name, path):
    return f"{name}{path}" if path.startswith("[") else f"{name}.{path}"


def _writes_rcx(insn):
    """Whether an instruction (Intel syntax, as objdump writes it) may change rcx: a call, a string
    instruction with a rep prefix, an instruction writing its first operand when that is rcx."""
    words = insn.replace("\t", " ").replace(",", " , ").split()
    if words and words[0] == "lock":
        words = words[1:]
    if not words:
        return False
    m = words[0]
    if m.startswith(("call", "rep", "loop")) or m in ("syscall", "cpuid", "rdtscp"):
        return True
    if m.startswith("j") or m in ("cmp", "test", "push", "bt", "ret", "nop", "comiss", "comisd", "ucomiss", "ucomisd"):
        return False
    operands = " ".join(words[1:]).split(" , ")
    if m in ("xchg", "xadd", "cmpxchg"):
        return any(o.strip() in RCX for o in operands)
    return operands[0].strip() in RCX


class _Source:
    """The comment lines before each instruction: `; File:line` where the line changes, and `; > inlined X
    (File:line)` / `; < X` where an inline site's range starts and ends, indented by depth."""

    def __init__(self, detail):
        self.lines = detail.lines if detail else []
        self.inlines = detail.inlines if detail else []
        self.stack, self.shown = [], None

    def at(self, offset):
        stack = sorted((s for s in self.inlines if any(r[0] <= offset < r[1] for r in s.ranges)), key=lambda s: s.depth)
        out, keep = [], 0
        while keep < min(len(stack), len(self.stack)) and stack[keep] is self.stack[keep]:
            keep += 1
        for s in reversed(self.stack[keep:]):
            out.append(f"; {'  ' * s.depth}< {s.name}")
        for s in stack[keep:]:
            out.append(f"; {'  ' * s.depth}> inlined {s.name}" + (f" ({_base(s.file)}:{s.line})" if s.file else ""))
        where = None
        for s in reversed(stack):
            where = next(((file, line) for a, b, file, line in s.ranges if a <= offset < b and file), None)
            if where:
                break
        if where is None:
            where = next(((file, line) for at, file, line in reversed(self.lines) if at <= offset and file), None)
        if where and (where != self.shown or out):
            out.append(f"; {'  ' * len(stack)}{_base(where[0])}:{where[1]}")
            self.shown = where
        self.stack = stack
        return out


def _base(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def callers(m, name):
    """The functions of the module that call `name` (its name or decorated name; every function folded at its
    address counts; or an imported function, by its decorated or undecorated name) or jump to it (a tail
    call): [(caller, "call" or "jmp", RVA of the instruction, whether the caller was compiled in the same
    unity blob; None for an import)], by address. A rel32 call or jump (through the import address table for
    an import) found in .text counts only where the caller's disassembly has an instruction starting there
    (needs objdump)."""
    targets = {m.rva(f.segment, f.offset): f for f in m.functions if f.name == name or f.mangled == name}
    slots = {rva for rva, n in m.image.iat.items() if name in (n.rpartition("!")[2], _short(n))}
    if not targets and not slots:
        raise Fail(f"no function {name!r} in {m.pdb.name}, nor imported")
    blobs = {_base(f.compiland) for f in targets.values()}
    starts = sorted({m.rva(f.segment, f.offset): f for f in reversed(m.functions)}.items())
    keys = [rva for rva, _ in starts]
    sites = {}
    for s in m.image.sections:
        if not s.executable:
            continue
        code = m._bytes_at(s.vaddr, s.raw_size)
        pattern = b"[\xe8\xe9]" if targets else b"\xff[\x15\x25]"
        for hit in re.finditer(pattern, code, re.DOTALL):
            at = hit.start()
            n = len(hit.group(0))
            if at + n + 4 > len(code):
                break
            rva = s.vaddr + at
            if rva + n + 4 + struct.unpack_from("<i", code, at + n)[0] not in (targets or slots):
                continue
            if code[at - 1:at] == b"\x48" and n == 2:
                rva, at = rva - 1, at - 1  # rex.w jmp [rip + X]: the instruction starts at the prefix
            i = bisect.bisect_right(keys, rva) - 1
            if i >= 0 and rva < keys[i] + starts[i][1].length and keys[i] not in targets:
                sites.setdefault(keys[i], []).append(rva)
    if not sites or not objdump():
        return []
    owners = sorted(sites)
    listings = _disassemble([m._bytes_at(rva, starts[keys.index(rva)][1].length) for rva in owners])
    out = []
    for rva, listing in zip(owners, listings):
        f = starts[keys.index(rva)][1]
        at = {o: text.split()[0] for o, _, text in listing}
        for site in sites[rva]:
            if at.get(site - rva) in ("call", "jmp"):
                out.append((f, at[site - rva], site, _base(f.compiland) in blobs if targets else None))
    return out


NOISE = re.compile(r"(TArray|FString::(Len|operator\*)|TSizedHeap|UE::Core|FMemory|TSet|TMapBase|TBitArray|TConstSetBit"
                   r"|TSparse|ConstructItems|FGenericPlatform|DestructItems|TCString|FVariant::~|TDefault|GetTypeHash"
                   r"|FCrc|__|_guard|memcpy|memmove|memset)")


def by_line(m, f):
    """Per line of the function's own source file, in address order, what the code of that line reaches:
    [(line, ["call X", "vcall [rax + 0x88]", "inl Y", 'u"..."', "log ...", "global Z", "this->mFoo",
    "cmp 0x20"])]. Code inlined from elsewhere counts for the line that inlined it; containers, memory and
    compiler helpers are left out."""
    if not f.source:
        raise Fail(f"{f.name}: no line information")
    own = re.compile(r";\s*" + re.escape(_base(f.source)) + r":(\d+)$")
    out, line = [], None
    for text in m.disassemble(f).splitlines()[4:]:
        items = []
        if hit := own.match(text):
            line = int(hit.group(1))
            continue
        if hit := re.match(r";\s*> inlined (.+?)(?: \([^()]+:\d+\))?$", text):
            if not NOISE.match(hit.group(1)):
                items.append("inl " + hit.group(1))
        elif hit := re.match(r"\s+\+[0-9a-f]{5}\s+(\S+)\t?([^;]*?)\s*(?:; (.*))?$", text):
            mnemonic, operands, notes = hit.group(1), hit.group(2).strip(), (hit.group(3) or "").split("; ")
            notes = [n for n in notes if n]
            labelled = "rip" in operands or re.match(r"(call|jmp)\b", mnemonic) and operands.startswith("0x")
            label = notes.pop(0) if labelled and notes else None
            if mnemonic == "call" and label:
                if not NOISE.match(_short(label)):
                    items.append("call " + _short(label))
            elif mnemonic == "call" and "ptr [r" in operands and "rip" not in operands:
                items.append("vcall " + operands.split("ptr ", 1)[1])
            elif label and label.startswith(('u"', '"')):
                if ":\\" not in label:  # the file of a check: not the line's own
                    items.append(label)
            elif label and label.startswith("log "):
                items.append(label)
            elif label and mnemonic != "jmp" and not NOISE.match(_short(label)):
                items.append("global " + _short(label))
            items += notes  # what typed locals reach
            if mnemonic in ("cmp", "test") and (imm := re.search(r", (0x[0-9a-f]+|\d+)$", operands)):
                items.append("cmp " + imm.group(1))
        if line is None:
            continue
        if not out or out[-1][0] != line:
            out.append((line, []))
        out[-1][1].extend(i for i in items if i not in out[-1][1])
    return [(n, items) for n, items in out if items]


def _is_game_source(path):
    p = path.replace("/", "\\").lower()
    return "\\source\\" in p and not any(x in p for x in ("\\engine\\", "\\intermediate\\", "\\thirdparty\\"))


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.replace("::", "__")).strip("_")[:120] or "anonymous"


def span(f):
    """Where a function comes from: "<source file>:<first line>-<last line>". Template instances and copies of
    one inline function share it; None without line info."""
    return f"{f.source}:{f.first_line}-{f.last_line}" if f.source else None


def dump(dll, out, source_marker=None):
    """One folder per module, holding functions.tsv and one .asm per game function, by source file. With
    source_marker, its functions only, listed in functions.<marker>.tsv: the module-wide list stays. A run
    replaces the .asm files it writes; two functions of one name in one file: the second as NAME@rva."""
    if config.inside_repository(out):
        raise Fail(f"{out} is inside this repository; game-derived material stays outside it")
    m = Module(dll)
    module = re.sub(r"-Win64-(Shipping|Test|Development)$", "", Path(dll).stem)
    root = Path(out) / module
    root.mkdir(parents=True, exist_ok=True)
    rows = ["name\trva\tlength\tsource\tfirst_line\tlast_line\tlocals\tmangled\tspan"]
    written = set()
    for f in (fns := m.game_functions(source_marker)):
        source = Path(f.source.replace("\\", "/")).name
        rows.append("\t".join([f.name, f"{m.rva(f.segment, f.offset):#x}", str(f.length), source,
                               str(f.first_line), str(f.last_line), ",".join(f.locals), f.mangled or "", span(f)]))
        folder = root / source
        folder.mkdir(exist_ok=True)
        target = folder / f"{_safe(f.name)}.asm"
        if target in written:
            target = folder / f"{_safe(f.name)}@{m.rva(f.segment, f.offset):x}.asm"
        written.add(target)
        target.write_text(m.disassemble(f), encoding="utf-8")
    index = f"functions.{_safe(source_marker)}.tsv" if source_marker else "functions.tsv"
    (root / index).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root, fns


@dataclass
class TraceRow:
    ms: int
    thread: int
    rva: int
    name: str
    source: str = ""
    length: int = 0
    span: str = None


def trace(dll, tsvs):
    """Traces written by the hook loader's trace mode (RecompTrace.tsv: ms, thread, rva of each function's first
    execution; one path or several), each function once, in the order the traces give, with its name, source
    file and span; and by source file, in order of first execution: (file, functions run, their bytes, first
    ms, distinct source spans, their bytes with each span counted once, at its largest function)."""
    m = Module(dll)
    by_rva = {}
    for f in m.functions:
        by_rva.setdefault(m.rva(f.segment, f.offset), f)
    rows, files, seen = [], {}, set()
    for tsv in [tsvs] if isinstance(tsvs, (str, Path)) else tsvs:
        for line in Path(tsv).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if line.startswith("#") or len(parts) < 3 or (rva := int(parts[2], 16)) in seen:
                continue
            seen.add(rva)
            ms, thread, f = int(parts[0]), int(parts[1]), by_rva.get(rva)
            source = Path(f.source.replace("\\", "/")).name if f and f.source else ""
            rows.append(TraceRow(ms, thread, rva, f.name if f else "?", source, f.length if f else 0,
                                 span(f) if f else None))
            if source:
                entry = files.setdefault(source, [0, 0, ms, {}])
                entry[0], entry[1] = entry[0] + 1, entry[1] + rows[-1].length
                key = rows[-1].span or rva
                entry[3][key] = max(entry[3].get(key, 0), rows[-1].length)
    ordered = sorted(files.items(), key=lambda kv: kv[1][2])
    return rows, [(name, n, size, first, len(spans), sum(spans.values())) for name, (n, size, first, spans) in ordered]


@dataclass
class Family:
    kind: str      # exact: the same code once numbers are masked; near: within 10 % in length, 90 % the same
    members: list  # (rva, length, names, "File:line"): one piece of code each, every function folded there
    bytes: int


NEAR_LENGTH, NEAR_SAME, NEAR_MIN = 1.10, 0.90, 10  # near: lengths, instructions the same, fewest instructions


def families(dll, tsvs=None, source_marker=None, near=True):
    """The game's functions (or those the traces ran) grouped by their code: each instruction's mnemonic and
    registers kept, every number (immediates, displacements, addresses, relocated operands) masked. Exact
    groups share all of it; near groups join functions of at least 10 instructions whose lengths in bytes are
    within 10 % and whose instructions are at least 90 % the same once aligned (compared only within that
    length window). Groups of two or more, largest in bytes first."""
    m = Module(dll)
    fns = m.game_functions(source_marker)
    if tsvs:
        ran = set()
        for tsv in [tsvs] if isinstance(tsvs, (str, Path)) else tsvs:
            for line in Path(tsv).read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("\t")
                if not line.startswith("#") and len(parts) >= 3:
                    ran.add(int(parts[2], 16))
        fns = [f for f in fns if m.rva(f.segment, f.offset) in ran]
    code = {}
    for f in fns:
        code.setdefault(m.rva(f.segment, f.offset), []).append(f)
    rvas = sorted(code)
    blobs = [m._bytes_at(rva, max(f.length for f in code[rva])) for rva in rvas]
    items = [(len(blob), tuple(_shape(t) for _, _, t in rows)) for blob, rows in zip(blobs, _disassemble(blobs))]
    out = []
    for group, kind in _groups(items, near):
        members = [(rvas[i], len(blobs[i]), tuple(f.name for f in code[rvas[i]]),
                    f"{_base(code[rvas[i]][0].source)}:{code[rvas[i]][0].first_line}") for i in sorted(group)]
        out.append(Family(kind, members, sum(m[1] for m in members)))
    return sorted(out, key=lambda g: (-g.bytes, g.members[0][0]))


def _shape(text):
    """An instruction with its numbers masked: "mov rax, qword ptr [rcx + #]"."""
    text = " ".join(re.sub(r"\s*<[^>]*>", "", text).partition(" # ")[0].split())
    return re.sub(r"0x[0-9a-f]+", "#", re.sub(r" - 0x", " + 0x", text))


def _groups(items, near=True):
    """[(indices, "exact" or "near")] for items [(length in bytes, instruction shapes)]: groups of two or more."""
    exact = {}
    for i, (_, shape) in enumerate(items):
        exact.setdefault(shape, []).append(i)
    parent = {members[0]: members[0] for members in exact.values()}

    def find(x):
        while parent[x] != x:
            parent[x] = x = parent[parent[x]]
        return x

    if near:
        reps = sorted((items[r][0], r) for r in parent if len(items[r][1]) >= NEAR_MIN)
        bags = {r: Counter(items[r][1]) for _, r in reps}
        for a, (la, ra) in enumerate(reps):
            for lb, rb in reps[a + 1:]:
                if lb > la * NEAR_LENGTH:
                    break
                sa, sb = items[ra][1], items[rb][1]
                need = NEAR_SAME * max(len(sa), len(sb))
                if min(len(sa), len(sb)) < need or find(ra) == find(rb):
                    continue
                if sum((bags[ra] & bags[rb]).values()) < need:  # the instructions shared, in any order
                    continue
                same = difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_matching_blocks()
                if sum(block.size for block in same) >= need:
                    parent[find(rb)] = find(ra)
    joined = {}
    for members in exact.values():
        joined.setdefault(find(members[0]), []).append(members)
    out = []
    for parts in joined.values():
        indices = [i for members in parts for i in members]
        if len(indices) >= 2:
            out.append((indices, "exact" if len(parts) == 1 else "near"))
    return out


@dataclass
class Match:
    name: str            # the object file's (decorated) name
    state: str           # matched, differs, missing
    shipped: str = ""    # the shipped function's name, from the PDB
    length: int = 0
    offset: int = None   # the first difference
    detail: str = ""
    score: int = None    # instructions not the same once aligned: 0 when matched, None when missing
    rows: list = field(default_factory=list)   # the alignment: (mark, (offset, shipped) or None, (offset, ours) or None)
    verdict: str = ""    # what the differences amount to, when the alignment tells (diff_listing)


def match_line(r):
    """One line per function of --match; it ends with `score <n>` (`score -` for a function the module lacks)."""
    shipped = f"  [{r.shipped}]" if r.shipped else ""
    return f"{r.state:<8} {r.name}{shipped}" + (f": {r.detail}" if r.detail else "") + \
        f"  score {'-' if r.score is None else r.score}"


def diff_listing(r):
    """The shipped and compiled disassembly side by side: `=` the same, `!` changed, `<` shipped only, `>` ours
    only; relocated operands as <reloc>, a call or tail call followed by the name of what it reaches
    (`; Name`). Ends with `verdict: ...` when the alignment tells what the differences
    amount to (_verdict), then `score <n>`."""
    out = [f"; {r.name}  [{r.shipped}]: {r.state}" + (f", {r.detail}" if r.detail else "")]
    for mark, left, right in r.rows:
        left = f"+{left[0]:05x}  {left[1]}" if left else ""
        out.append(f"{mark} {left:<59} {f'+{right[0]:05x}  {right[1]}' if right else ''}".rstrip())
    if r.verdict:
        out.append(f"verdict: {r.verdict}")
    out.append(f"score {r.score}")
    return "\n".join(out) + "\n"


def qualified_name(decorated):
    """"Scope::Name" from a plain MSVC decorated name (?Name@Scope@@...); None for operators,
    constructors, templates and undecorated names."""
    if not decorated.startswith("?") or decorated[1:2] in ("?", "$"):
        return None
    head, sep, _ = decorated[1:].partition("@@")
    parts = head.split("@")
    if not sep or not all(re.fullmatch(r"[A-Za-z_]\w*", p) for p in parts):
        return None
    return "::".join(reversed(parts))


SPECIAL = {"?R": "operator()", "?_G": "`scalar deleting destructor'", "?_E": "`vector deleting destructor'"}


def scoped_name(decorated):
    """The PDB's name of a function MSVC decorates inside another function's scope (a lambda's member, a
    local class's such as fgcheck's Impl) or of a dynamic initializer or atexit destructor:
    "`AFGGameMode::RebootSession'::`2'::<lambda_1>::operator()",
    "`dynamic initializer for 'AFGGameMode::MapOptionsToNeverSave''". None for anything else, and for what
    this reader does not know (templates, anonymous namespaces)."""
    for prefix, what in (("??__E", "dynamic initializer"), ("??__F", "dynamic atexit destructor")):
        if decorated.startswith(prefix):
            rest = decorated[len(prefix):]
            name = _undecorate(rest[1:] if rest.startswith("?") else rest)
            return f"`{what} for '{name}''" if name else None
    name = _undecorate(decorated[1:]) if decorated.startswith("?") else None
    return name if name and "`" in name else None


def _undecorate(s, depth=0):
    """The qualified name at the start of a decorated name (`s` without its leading '?'), or None."""
    member = None
    for code in ("?R", "?_G", "?_E", "?0", "?1"):
        if s.startswith(code):
            member, s = code, s[len(code):]
            break
    parts, memo = [], []
    while s and s[0] != "@" and depth < 6:
        if s[0].isdigit():  # a back reference to a name already read
            if int(s[0]) >= len(memo):
                return None
            parts.append(memo[int(s[0])])
            s = s[1:]
        elif hit := re.match(r"\?(\d|[A-P]+@)\?", s):  # a scope of the function whose decorated name follows
            outer = _undecorate(s[hit.end():][1:], depth + 1)
            if outer is None:
                return None
            parts.append(f"`{outer}'::`{_number(hit.group(1))}'")
            break
        else:
            name, sep, s = s.partition("@")
            if not sep or not re.fullmatch(r"[\w<>]+", name):
                return None  # a template, an anonymous namespace, a name this reader does not know
            parts.append(name)
            memo.append(name)
    if not parts or s == "" and not parts[-1].startswith("`"):
        return None
    if s.startswith("@") and s[1:2] in tuple("GHOPWX$"):
        return None  # an adjustor or vtordisp thunk: the PDB names it [thunk]:...
    if member in ("?0", "?1"):
        member = ("~" if member == "?1" else "") + parts[0]
    else:
        member = SPECIAL.get(member)
    return "::".join(reversed(parts)) + (f"::{member}" if member else "")


def _number(code):
    """A decorated name's number: a digit for 1 to 10, else hexadecimal digits A-P (0-15) and '@'."""
    if code.isdigit():
        return int(code) + 1
    return int("".join("0123456789abcdef"[ord(c) - ord("A")] for c in code[:-1]), 16)


def pair_scoped(names, functions, compiland=None):
    """{decorated name: shipped function} for compiled functions inside a function's scope, dynamic
    initializers and deleting destructors: the shipped function of that PDB name when only one has it, or
    only one compiled in `compiland` (the object's file name: a header's static has an initializer in every
    unity blob); else, the scopes numbered differently, the Nth of its kind in its enclosing function
    (lambda_N, the Nth Impl by scope), when both sides have as many and the order agrees with every pair of
    the same name. Left out when unsure."""
    scoped = {n: k for n in names if (k := scoped_name(n))}
    by_name, groups = {}, {}
    for f in functions:
        if "`" in f.name:
            by_name.setdefault(f.name, []).append(f)
    if compiland:
        for name, fs in by_name.items():
            if len(fs) > 1 and len(here := [f for f in fs if _base(f.compiland) == compiland]) == 1:
                by_name[name] = here
    for name, fs in by_name.items():
        if len(fs) == 1:
            groups.setdefault(_unnumbered(name), [[], []])[1].append(fs[0])
    out = {}
    for n, k in scoped.items():
        k = scoped[n] = next((x for x in _spellings(k) if len(by_name.get(x, ())) == 1), k)
        if len(by_name.get(k, ())) == 1:
            out[n] = by_name[k][0]
        groups.setdefault(_unnumbered(k), [[], []])[0].append(n)
    for ours, theirs in groups.values():
        if not ours or len(ours) != len(theirs):
            continue
        ours = sorted(ours, key=lambda n: _scopes(scoped[n]))
        theirs = sorted(theirs, key=lambda f: _scopes(f.name))
        if all(out.get(n, f) is f for n, f in zip(ours, theirs)):
            out.update(zip(ours, theirs))
    return out


def _spellings(name):
    """The names the PDB may give an initializer or atexit destructor: the qualified name inside (a class's
    static, "`dynamic initializer for 'FBuffer::Null''"), or a namespace outside, at each `::`
    ("OnlineMessages::`dynamic initializer for 'Kicked''"); the decorated name does not tell a namespace
    from a class. Any other name: itself."""
    hit = re.fullmatch(r"`(dynamic (?:initializer|atexit destructor)) for '(.+)''", name)
    if not hit:
        return [name]
    what, parts = hit.group(1), hit.group(2).split("::")
    return [name] + [f"{'::'.join(parts[:i])}::`{what} for '{'::'.join(parts[i:])}''"
                     for i in range(len(parts) - 1, 0, -1)]


def _unnumbered(name):
    return re.sub(r"`\d+'", "`#'", name)


def _scopes(name):
    return [int(x) for x in re.findall(r"`(\d+)'", name)]


def match(dll, obj, diff=(), only=None):
    """Each function of a compiled COFF object against the shipped function of the same decorated name
    (a public symbol), or else of the same qualified name when only one function has it, or else paired by
    scope (pair_scoped: lambdas, local classes, dynamic initializers). Bytes a relocation fills are masked
    on both sides: the shipped code has the linker's values there. Differing functions, and those named in
    `diff` (decorated or shipped name), are aligned instruction by instruction (needs objdump): rows and
    score. dll: a path, or a Module already read (the permuter's, once per run). only: the one function
    (decorated or shipped name) to compare and return; every function is still paired, for the names of
    what it calls."""
    try:
        o = coff.parse(Path(obj).read_bytes())
    except coff.NotCOFF as e:
        raise Fail(f"{obj}: {e}")
    m = dll if isinstance(dll, Module) else Module(dll)
    m.paired = {}
    by_mangled = {f.mangled: f for f in m.functions if f.mangled}
    by_name = {}
    for f in m.functions:
        by_name.setdefault(f.name, []).append(f)
    functions = o.functions()
    scoped = pair_scoped([fn.name for fn in functions if fn.name not in by_mangled], m.functions, Path(obj).name)
    results, pairs = [], []
    for fn in functions:
        shipped, how = by_mangled.get(fn.name), ""
        if shipped is None:
            candidates = by_name.get(qualified_name(fn.name), [])
            if len(candidates) == 1:
                shipped, how = candidates[0], "by name"
        if shipped is None and fn.name in scoped:
            shipped, how = scoped[fn.name], "by scope"
        if shipped is not None:
            m.paired[fn.name] = m.rva(shipped.segment, shipped.offset)
        if only is not None and only not in (fn.name, shipped and shipped.name):
            continue
        if shipped is None:
            results.append(Match(fn.name, "missing", detail="no function of that name in the shipped module"))
            continue
        results.append(_compare(m, fn, shipped, how))
        pairs.append((len(results) - 1, fn, shipped, how))
    differing = [x for x in pairs if results[x[0]].state == "differs"]
    for (i, fn, f, how), insn in zip(differing, _instructions_at(m, [(f, results[i].offset) for i, _, f, _ in differing])):
        results[i] = _compare(m, fn, f, how, insn)  # the shipped instruction where they first differ, named
    pairs = [(results[i], fn, f) for i, fn, f, _ in pairs]
    _align(m, [(r, fn, f) for r, fn, f in pairs if r.state == "differs" or r.name in diff or r.shipped in diff])
    return results


def _align(m, pairs):
    if not pairs or not objdump():
        return
    blobs = []
    for r, fn, f in pairs:
        blobs += [m._bytes_at(m.rva(f.segment, f.offset), f.length), fn.code]
    listings = _disassemble(blobs)
    for i, (r, fn, f) in enumerate(pairs):
        ours = [(o, *_normalize(t, o, n, len(fn.code), fn.masked)) for o, n, t in listings[2 * i + 1]]
        theirs = [(o, *_normalize(t, o, n, len(blobs[2 * i]), None)) for o, n, t in listings[2 * i]]
        calls = ([(o, x[1], _shipped_call(m, f, t, o, n, len(blobs[2 * i]))) for (o, n, t), x
                  in zip(listings[2 * i], theirs)],
                 [(o, x[1], _our_call(m, fn, t, o, n)) for (o, n, t), x in zip(listings[2 * i + 1], ours)])
        rows = []
        sm = difflib.SequenceMatcher(None, [k for *_, k in theirs], [k for *_, k in ours], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            left, right = [x[:2] for x in theirs[i1:i2]], [x[:2] for x in ours[j1:j2]]
            if tag == "equal":
                rows += [("=", a, b) for a, b in zip(left, right)]
                continue
            n = min(len(left), len(right))
            rows += [("!", a, b) for a, b in zip(left[:n], right[:n])]
            rows += [("<", a, None) for a in left[n:]] + [(">", None, b) for b in right[n:]]
        r.rows, r.score = _callees(rows, calls), sum(mark != "=" for mark, *_ in rows)
        if r.state == "matched":
            r.score = 0  # the bytes decide: a listing may show an operand only one side relocates
        elif not r.score:
            r.score = 1  # bytes differ where the text does not (another encoding of the same instruction)
        if r.state == "differs":
            r.verdict = _verdict(m, f, calls, rows)


def _callees(rows, calls):
    """The rows with the name of what each relocated call or tail call reaches after it, on both sides
    (`call <reloc>  ; FSoftObjectPath::ResolveObject`): shown only, the alignment is already made."""
    names = [{o: c[1] for o, _, c in side if c and c[1]} for side in calls]

    def named(x, k):
        return x and (x[0], f"{x[1]}  ; {names[k][x[0]]}" if x[0] in names[k] else x[1])
    return [(mark, named(left, 0), named(right, 1)) for mark, left, right in rows]


def _verdict(m, f, calls, rows):
    """One line on what the differences of an aligned pair amount to: "registers only", "stack slots only"
    (the same instructions once renamed consistently); "stack layout only (frame 0x188 vs 0x1c8)" (the same
    instructions, temporaries at other stack offsets); "register allocation only" (the same once copies,
    spills and nops are left out and registers masked); "inlined here, called there: X" or the reverse;
    else the first differing source line, the first difference register allocation does not explain, and
    how many instructions it does not explain. "" when unsure. calls: per side [(offset, shown, call)]."""
    theirs, ours = calls
    plain = [x[:2] for x in theirs], [x[:2] for x in ours]
    if renamed := _renamed(*plain):
        return renamed
    if layout := _stack_layout(rows):
        return layout
    same, offset, others = _allocation(*plain)
    if same:
        return "register allocation only"
    detail = m.details.detail(f) if f.module != 0xFFFF else None
    if inlined := _inlined(theirs, ours, detail.inlines if detail else ()):
        return inlined
    if offset is None:
        first = next((i for i, (mark, *_) in enumerate(rows) if mark != "="), None)
        offset = None if first is None else next((left[0] for _, left, _ in rows[first:] if left), None)
    line = _first_line(offset, detail, f.source)
    if line and others:
        line += f"; {others} instruction{'s differ' if others > 1 else ' differs'} besides register allocation"
    return line


def _register_table():
    out = {}
    for x in "abcd":
        out.update({f"r{x}x": (x, 64), f"e{x}x": (x, 32), f"{x}x": (x, 16), f"{x}l": (x, 8), f"{x}h": (x, 0)})
    for x in ("si", "di", "bp", "sp"):
        out.update({f"r{x}": (x, 64), f"e{x}": (x, 32), x: (x, 16), f"{x}l": (x, 8)})
    for n in range(8, 16):
        out.update({f"r{n}": (f"r{n}", 64), f"r{n}d": (f"r{n}", 32), f"r{n}w": (f"r{n}", 16), f"r{n}b": (f"r{n}", 8)})
    for n in range(32):
        out.update({f"xmm{n}": (f"v{n}", 128), f"ymm{n}": (f"v{n}", 256), f"zmm{n}": (f"v{n}", 512)})
    return out


FAMILY = _register_table()
SLOT = r"\[(?:rsp|rbp)(?: \+ 0x[0-9a-f]+)?\]"
TOKENS = re.compile(f"({SLOT}|\\b(?:{'|'.join(sorted(FAMILY, key=len, reverse=True))})\\b)")
LOCAL_BRANCH = re.compile(r"^(j\w+|call|loop\w*) \+([0-9a-f]{5})$")


def _renamed(theirs, ours):
    """"registers only", "stack slots only" or both when two listings [(offset, shown)] have the same
    instructions once registers (by family, the same width) or stack slots ([rsp + n], [rbp + n]) are renamed
    one to one; branches inside the function compare by the instruction they reach. None otherwise."""
    if len(theirs) != len(ours) or not theirs:
        return None
    a, b = _indexed(theirs), _indexed(ours)
    if a == b:
        return None
    for registers, slots, verdict in ((True, False, "registers only"), (False, True, "stack slots only"),
                                      (True, True, "registers and stack slots only")):
        if _same_renamed(a, b, registers, slots):
            return verdict
    return None


def _indexed(listing):
    at = {o: i for i, (o, _) in enumerate(listing)}
    out = []
    for _, text in listing:
        hit = LOCAL_BRANCH.match(text)
        out.append(f"{hit.group(1)} #{at.get(int(hit.group(2), 16), '?')}" if hit else text)
    return out


def _same_renamed(a, b, registers, slots):
    maps = ({}, {}), ({}, {})  # registers, slots: each one to one, both ways
    for x, y in zip(a, b):
        px, py = TOKENS.split(x), TOKENS.split(y)
        if len(px) != len(py):
            return False
        for k, (u, v) in enumerate(zip(px, py)):
            if k % 2 == 0 or u == v:
                if u != v:
                    return False
                if k % 2 and not _bind(maps[u.startswith("[")], u, v):  # the same token also binds
                    return False
                continue
            slot = u.startswith("[")
            if slot != v.startswith("[") or not (slots if slot else registers):
                return False
            if slot and u[:4] != v[:4]:
                return False  # rsp and rbp are not one another's
            if not slot:
                if FAMILY[u][1] != FAMILY[v][1]:
                    return False
                u, v = FAMILY[u][0], FAMILY[v][0]
            if not _bind(maps[slot], u, v):
                return False
    return True


def _bind(pair, u, v):
    if u in FAMILY and v in FAMILY:
        u, v = FAMILY[u][0], FAMILY[v][0]
    forward, back = pair
    if forward.setdefault(u, v) != v or back.setdefault(v, u) != u:
        return False
    return True


FRAME = re.compile(r"^(sub|add) rsp, (0x[0-9a-f]+|\d+)$")


def _stack_layout(rows):
    """"stack layout only (frame 0x188 vs 0x1c8)" when every differing row of an alignment pairs the same
    instruction once the displacements of [rsp + n] / [rbp + n] and the size of sub rsp / add rsp are left
    out: temporaries at other stack offsets, maybe in a frame of another size (the first sub rsp of each
    side; "(frame 0x188)" when the same, nothing when neither has one). None otherwise."""
    pairs = [(left, right) for mark, left, right in rows if mark != "="]
    if not pairs or any(left is None or right is None or _unstacked(left[1]) != _unstacked(right[1])
                        for left, right in pairs):
        return None
    frames = [next((int(hit.group(2), 0) for x in side if x and (hit := FRAME.match(x[1]))
                    and hit.group(1) == "sub"), None) for side in ([r[1] for r in rows], [r[2] for r in rows])]
    if None in frames:
        return "stack layout only"
    return "stack layout only (frame " + (f"{frames[0]:#x})" if frames[0] == frames[1] else
                                          f"{frames[0]:#x} vs {frames[1]:#x})")


def _unstacked(text):
    if hit := FRAME.match(text):
        return f"{hit.group(1)} rsp, #"
    return re.sub(r"\[(rsp|rbp)(?: [+-] 0x[0-9a-f]+)?\]", r"[\1 + #]", text)


MOVES = {"mov", "movaps", "movups", "movapd", "movupd", "movsd", "movss", "movq", "movd", "movdqa", "movdqu"}
STACK = re.compile(r"\[(?:rsp|rbp)(?: [+-] 0x[0-9a-f]+)?\]")


def _allocation(theirs, ours):
    """(same, first, others) for two listings [(offset, shown)] once what register allocation changes is left
    out: copies between registers, spills and reloads, nops; registers masked to their width, stack slots
    and branch targets masked, lea r, [r + n] read as add r, n. same: nothing else differs; first: the
    shipped offset of the first other difference; others: how many instructions differ besides."""
    a = [(o, x) for o, t in theirs if (x := _allocated(t))]
    b = [(o, x) for o, t in ours if (x := _allocated(t))]
    blocks = [op for op in difflib.SequenceMatcher(None, [x for _, x in a], [x for _, x in b],
                                                   autojunk=False).get_opcodes() if op[0] != "equal"]
    if not blocks:
        return True, None, 0
    _, i1, _, _, _ = blocks[0]
    first = a[i1][0] if i1 < len(a) else (a[-1][0] if a else None)
    return False, first, sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in blocks)


def _allocated(text):
    mnemonic, _, operands = text.partition(" ")
    if mnemonic.startswith("nop") or mnemonic == "int3":
        return None
    parts = [o.strip() for o in operands.split(",")] if operands else []
    if mnemonic in MOVES and len(parts) == 2:
        a, b = parts
        if (a in FAMILY or STACK.search(a)) and (b in FAMILY or STACK.search(b)) and (a in FAMILY or b in FAMILY):
            return None  # a copy, a spill or a reload
    if mnemonic == "lea" and (hit := re.fullmatch(r"(\w+), \[(\w+) \+ (0x[0-9a-f]+)\]", operands)) \
            and hit.group(2) in FAMILY:
        mnemonic, operands = "add", f"{hit.group(1)}, {hit.group(3)}"
    if LOCAL_BRANCH.match(text):
        return mnemonic + " #"
    operands = TOKENS.sub(lambda t: t.group(0) if t.group(0).startswith("[") else f"r{FAMILY[t.group(0)][1]}",
                          STACK.sub("[stack]", operands))
    return f"{mnemonic} {operands}"


def _inlined(theirs, ours, inlines=()):
    """"inlined here, called there: X" where the shipped code (theirs) calls X and ours, which never calls X,
    has code of its own there (calls aside, at least as many instructions; aligned with calls compared by
    their callee, differing blocks at most two instructions apart taken as one); "called here, inlined there: Y" where ours calls what
    the shipped code never calls, and the shipped code has an inline site there (the PDB's), named Y. Lists of
    (offset, shown, call): call is (key, name) or None. The compiler's helpers (__security_check_cookie,
    _guard_dispatch_icall...) are never inlined: a call to one is no evidence. None when neither."""
    called = [{x for *_, c in side if c for x in c if x} for side in (theirs, ours)]  # by key and by name
    theirs = [(o, t, c if c and not c[1].startswith(("__", "_guard")) else None) for o, t, c in theirs]
    ours = [(o, t, c if c and not c[1].startswith(("__", "_guard")) else None) for o, t, c in ours]
    here, there = [], []
    keys = [[(t, c and c[0]) for _, t, c in side] for side in (theirs, ours)]  # a call compares by its callee
    clusters = []  # differing blocks, joined when at most two equal instructions part them
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, *keys, autojunk=False).get_opcodes():
        if tag != "equal":
            if clusters and i1 - clusters[-1][1] <= 2:
                clusters[-1][1], clusters[-1][3] = i2, j2
            else:
                clusters.append([i1, i2, j1, j2])
    for i1, i2, j1, j2 in clusters:
        left, right = theirs[i1:i2], ours[j1:j2]
        plain = sum(not c for *_, c in left), sum(not c for *_, c in right)  # instructions other than calls
        unknown = any(c and isinstance(c[0], tuple) for *_, c in right)  # a symbol the module does not resolve
        if plain[1] >= max(1, plain[0]) and not unknown:
            here += [c[1] for *_, c in left if c and c[1] and c[0] not in called[1] and c[1] not in called[1]]
        if plain[0] >= max(1, plain[1]):
            site = _outermost(inlines, [o for o, _, c in left if not c])
            if site and any(c and not isinstance(c[0], tuple) and c[0] not in called[0] and c[1] not in called[0]
                            for *_, c in right):
                there.append(site.name)
    out = []
    if here:
        out.append("inlined here, called there: " + ", ".join(list(dict.fromkeys(here))[:3]))
    if there:
        out.append("called here, inlined there: " + ", ".join(list(dict.fromkeys(there))[:3]))
    return "; ".join(out) or None


def _outermost(inlines, offsets):
    sites = [s for s in inlines if any(a <= o < b for a, b, *_ in s.ranges for o in offsets)]
    return min(sites, key=lambda s: s.depth) if sites else None


def _first_line(offset, detail, source):
    """"first difference at File.cpp:<line>" for a shipped offset, from the function's line table (its own
    source file first), with the inline site the code is in; "" without line information."""
    if offset is None or detail is None:
        return ""
    lines = [x for x in detail.lines if x[1] == source and x[0] <= offset] or \
        [x for x in detail.lines if x[1] and x[0] <= offset]
    if not lines:
        return ""
    _, file, line = lines[-1]
    site = _outermost(detail.inlines, [offset])
    return f"first difference at {_base(file)}:{line}" + (f", in inlined {site.name}" if site else "")


CALL = re.compile(r"^(call|jmp) (?:qword ptr \[rip(?: ([+-]) (0x[0-9a-f]+))?\]|(0x[0-9a-f]+))$")


def _shipped_call(m, f, text, offset, length, size):
    """(RVA, name) of what a shipped instruction calls, or jumps to out of the function (a tail call)."""
    hit = CALL.match(" ".join(re.sub(r"\s*<[^>]*>", "", text).partition(" # ")[0].split()))
    if not hit:
        return None
    start = m.rva(f.segment, f.offset)
    if hit.group(4):
        target = int(hit.group(4), 16)
        target -= 1 << 64 if target >= 1 << 63 else 0  # a call backwards, from address 0
        if 0 <= target < size:
            return None
        rva = start + target
    else:
        rva = start + offset + length + (-1 if hit.group(2) == "-" else 1) * int(hit.group(3) or "0", 16)
    label = m.label(rva)
    return rva, _short(label) if label else None


def _our_call(m, fn, text, offset, length):
    """(RVA in the shipped module, or the symbol, and a name) of what a compiled instruction calls."""
    if not CALL.match(" ".join(re.sub(r"\s*<[^>]*>", "", text).partition(" # ")[0].split())):
        return None
    symbol = fn.targets.get(offset + length - 4)
    if symbol is None:
        return None
    rva = m.rva_of(symbol)
    if rva is None:
        return ("symbol", symbol), _short(symbol)
    return rva, _short(m.label(rva) or symbol)


def _short(name):
    """A callee's name for a verdict: an import without its DLL, a decorated name undecorated where it can be."""
    name = name.rpartition("!")[2].removeprefix("__imp_")
    if not name.startswith("?"):
        return name
    return qualified_name(name) or scoped_name(name) or _undecorate(name[1:]) or name


BRANCH = re.compile(r"^(j\w+|call|loop\w*) (0x[0-9a-f]+)$")
RIP = re.compile(r"\[rip(?: [+-] 0x[0-9a-f]+)?\]")


def _normalize(text, offset, length, size, masked):
    """(shown, compared) for an instruction of a function disassembled at address 0. Relocated operands
    become <reloc>: in the shipped code (masked None) every rip-relative operand and every branch out of the
    function, in ours those the relocations fill. A branch inside the function shows its target as +offset
    and compares by its displacement, so code that moved as a whole still aligns; instructions also compare
    by their length."""
    text = " ".join(re.sub(r"\s*<[^>]*>", "", text).partition(" # ")[0].split())
    relocated = masked is not None and any(o in masked for o in range(offset, offset + length))
    shown = key = text
    if hit := BRANCH.match(text):
        target = int(hit.group(2), 16)
        if relocated or not 0 <= target < size:
            shown = key = f"{hit.group(1)} <reloc>"
        else:
            shown, key = f"{hit.group(1)} +{target:05x}", f"{hit.group(1)} .{target - offset - length:+d}"
    elif "[rip" in text and (masked is None or relocated):
        shown = key = RIP.sub("[rip + <reloc>]", text)
    elif relocated:
        shown = key = re.sub(r"0x[0-9a-f]+(?!.*0x[0-9a-f])", "<reloc>", text)
    return shown, (key, length)


def _disassemble(blobs):
    """objdump over pieces of code, each at address 0 (one object file, a section each):
    [[(offset, length, text)]]."""
    w = coff.Writer()
    for i, code in enumerate(blobs):
        section = w.section(".text", coff.CODE_FLAGS)
        w.data(section, code)
        w.define(f"f{i}", section, 0, function=True)
    fd, path = tempfile.mkstemp(suffix=".obj")
    try:
        os.write(fd, w.build())
        os.close(fd)
        out = subprocess.run([objdump(), "-d", "--no-show-raw-insn", "--x86-asm-syntax=intel", path],
                             capture_output=True, text=True, check=False).stdout
    finally:
        os.unlink(path)
    listings, current = [[] for _ in blobs], None
    for raw in out.splitlines():
        if hit := re.match(r"^[0-9a-f]+ <f(\d+)>:$", raw):
            current = listings[int(hit.group(1))]
        elif current is not None and (hit := LINE.match(raw)):
            current.append([int(hit.group(1), 16), 0, hit.group(2)])
    for code, rows in zip(blobs, listings):
        for k, row in enumerate(rows):
            row[1] = (rows[k + 1][0] if k + 1 < len(rows) else len(code)) - row[0]
    return [[tuple(row) for row in rows] for rows in listings]


def _compare(m, fn, shipped, how, insn=None):
    ref = m._bytes_at(m.rva(shipped.segment, shipped.offset), shipped.length)
    result = Match(fn.name, "matched", shipped.name, len(ref))
    notes = [how] if how else []
    first = next((i for i in range(min(len(ref), len(fn.code))) if i not in fn.masked and ref[i] != fn.code[i]), None)
    if first is None and len(ref) != len(fn.code):
        first = min(len(ref), len(fn.code))
        notes.insert(0, f"length: shipped {len(ref):#x} bytes, compiled {len(fn.code):#x} bytes")
    if first is None:
        result.detail = ", ".join([f"{len(fn.masked)} relocated bytes masked"] + notes)
        result.score = 0
        return result

    def window(code):
        return " ".join("??" if first + i in fn.masked else f"{b:02x}" for i, b in enumerate(code[first:first + 8]))

    result.state, result.offset = "differs", first
    where = f"at +{first:#x}"
    if insn:
        where += f" (shipped: {insn})"
    result.detail = ", ".join([f"{where}: shipped {window(ref) or '(end)'}, compiled {window(fn.code) or '(end)'}"] + notes)
    return result


def _instructions_at(m, places):
    """The shipped instruction covering each (function, offset), written as --disasm writes it (a branch
    inside the function to +offset, one outside to its address): one objdump for them all (needs objdump)."""
    if not places or not objdump():
        return [None] * len(places)
    listings = _disassemble([m._bytes_at(m.rva(f.segment, f.offset), f.length) for f, _ in places])
    out = []
    for (f, offset), listing in zip(places, listings):
        best = None
        for at, _, text in listing:
            if at > offset:
                break
            best = text
        if best is not None:
            best = re.sub(r"\s*<[^>]*>", "", best).strip().partition(" # ")[0]
            if hit := re.fullmatch(r"(j\w+|call|loop\w*)(\s+)(0x[0-9a-f]+)", best):
                target = int(hit.group(3), 16)
                target -= 1 << 64 if target >= 1 << 63 else 0
                va = m.image.image_base + m.rva(f.segment, f.offset)
                best = hit.group(1) + hit.group(2) + (f"+{target:05x}" if 0 <= target < f.length
                                                      else f"{(va + target) % (1 << 64):#x}")
            best = re.sub(r"\s+", " ", best)
        out.append(best)
    return out
