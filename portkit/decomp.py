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

import difflib
import os
from collections import Counter
import re
import shutil
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
        self._types = self._details = self._symbols = None
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

    def label(self, rva):
        if rva in self.image.iat:
            return self.image.iat[rva]
        if rva in self.names and not self.names[rva].startswith("??_C@"):  # ??_C@: a string literal's symbol
            return self.names[rva]
        return self.string_at(rva)

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
                note = self.label(rva) or (f"[{rva:#x}]" if "rip" in insn else None)
                if note and note not in notes:
                    notes.append(note)
            rows.append((addr - start, insn, notes))
        detail = self.details.detail(f) if f.module != 0xFFFF else None
        source, fields = _Source(detail), _Fields(self.types, detail, f, rows)
        for i, (offset, insn, notes) in enumerate(rows):
            after = rows[i + 1][0] if i + 1 < len(rows) else f.length
            notes += [n for n in fields.notes(offset, insn, after) if n not in notes]
            lines += source.at(offset)
            lines.append(f"  +{offset:05x}  {insn:<56}" + (f" ; {'; '.join(notes)}" if notes else ""))
        return "\n".join(lines) + "\n"


class _Fields:
    """Names what a memory operand [reg + disp] reaches, where the PDB says what reg holds: `this->mFoo`,
    `Local.mBar`, `&this->mFoo` for a lea. A store into a variable's memory is named only if the variable
    still lives there after it. Besides the PDB's ranges, `this` is in rcx at entry: up to the first
    instruction that writes rcx or calls (included: it reads its operands first), and not past where a jump
    from further on lands."""

    def __init__(self, types, detail, f, rows):
        self.types, self.variables = types, detail.variables if detail else []
        this = f.local_types.get("this")
        self.this = types.pointee(this) if this is not None else None
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
                    if path := t.field_path(target, disp, size, mask):
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
        if reg == REGISTERS["rcx"] and offset <= self.entry and (path := t.field_path(self.this, disp, size, mask)):
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
    """One folder per module, holding functions.tsv and one .asm per game function, by source file."""
    if config.inside_repository(out):
        raise Fail(f"{out} is inside this repository; game-derived material stays outside it")
    m = Module(dll)
    module = re.sub(r"-Win64-(Shipping|Test|Development)$", "", Path(dll).stem)
    root = Path(out) / module
    root.mkdir(parents=True, exist_ok=True)
    rows = ["name\trva\tlength\tsource\tfirst_line\tlast_line\tlocals\tmangled\tspan"]
    for f in (fns := m.game_functions(source_marker)):
        source = Path(f.source.replace("\\", "/")).name
        rows.append("\t".join([f.name, f"{m.rva(f.segment, f.offset):#x}", str(f.length), source,
                               str(f.first_line), str(f.last_line), ",".join(f.locals), f.mangled or "", span(f)]))
        folder = root / source
        folder.mkdir(exist_ok=True)
        target = folder / f"{_safe(f.name)}.asm"
        if target.exists():
            target = folder / f"{_safe(f.name)}@{m.rva(f.segment, f.offset):x}.asm"
        target.write_text(m.disassemble(f), encoding="utf-8")
    (root / "functions.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
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
    only; relocated operands as <reloc>. Ends with `verdict: ...` when the alignment tells what the differences
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


def _unnumbered(name):
    return re.sub(r"`\d+'", "`#'", name)


def _scopes(name):
    return [int(x) for x in re.findall(r"`(\d+)'", name)]


def match(dll, obj, diff=()):
    """Each function of a compiled COFF object against the shipped function of the same decorated name
    (a public symbol), or else of the same qualified name when only one function has it, or else paired by
    scope (pair_scoped: lambdas, local classes, dynamic initializers). Bytes a relocation fills are masked
    on both sides: the shipped code has the linker's values there. Differing functions, and those named in
    `diff` (decorated or shipped name), are aligned instruction by instruction (needs objdump): rows and
    score."""
    try:
        o = coff.parse(Path(obj).read_bytes())
    except coff.NotCOFF as e:
        raise Fail(f"{obj}: {e}")
    m = Module(dll)
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
        if shipped is None:
            results.append(Match(fn.name, "missing", detail="no function of that name in the shipped module"))
            continue
        results.append(_compare(m, fn, shipped, how))
        pairs.append((results[-1], fn, shipped))
        m.paired[fn.name] = m.rva(shipped.segment, shipped.offset)
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
        r.rows, r.score = rows, sum(mark != "=" for mark, *_ in rows)
        if r.state == "matched":
            r.score = 0  # the bytes decide: a listing may show an operand only one side relocates
        elif not r.score:
            r.score = 1  # bytes differ where the text does not (another encoding of the same instruction)
        if r.state == "differs":
            calls = ([(o, x[1], _shipped_call(m, f, t, o, n, len(blobs[2 * i]))) for (o, n, t), x
                      in zip(listings[2 * i], theirs)],
                     [(o, x[1], _our_call(m, fn, t, o, n)) for (o, n, t), x in zip(listings[2 * i + 1], ours)])
            r.verdict = _verdict(m, f, calls, rows)


def _verdict(m, f, calls, rows):
    """One line on what the differences of an aligned pair amount to: "registers only", "stack slots only"
    (the same instructions once renamed consistently); "register allocation only" (the same once copies,
    spills and nops are left out and registers masked); "inlined here, called there: X" or the reverse;
    else the first differing source line, the first difference register allocation does not explain, and
    how many instructions it does not explain. "" when unsure. calls: per side [(offset, shown, call)]."""
    theirs, ours = calls
    plain = [x[:2] for x in theirs], [x[:2] for x in ours]
    if renamed := _renamed(*plain):
        return renamed
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
    relocated = masked is not None and any(offset <= o < offset + length for o in masked)
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


def _compare(m, fn, shipped, how):
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
    if insn := _instruction_at(m, shipped, first):
        where += f" (shipped: {insn})"
    result.detail = ", ".join([f"{where}: shipped {window(ref) or '(end)'}, compiled {window(fn.code) or '(end)'}"] + notes)
    return result


def _instruction_at(m, f, offset):
    """The shipped instruction covering offset, from the annotated disassembly (needs objdump)."""
    if not objdump():
        return None
    best = None
    for line in m.disassemble(f).splitlines():
        if (hit := re.match(r"^\s+\+([0-9a-f]{5})\s+(.*?)\s*(;.*)?$", line)) and int(hit.group(1), 16) <= offset:
            best = re.sub(r"\s+", " ", hit.group(2))
    return best
