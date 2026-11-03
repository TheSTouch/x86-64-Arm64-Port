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
        self._types = self._details = None

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


def match_line(r):
    """One line per function of --match; it ends with `score <n>` (`score -` for a function the module lacks)."""
    shipped = f"  [{r.shipped}]" if r.shipped else ""
    return f"{r.state:<8} {r.name}{shipped}" + (f": {r.detail}" if r.detail else "") + \
        f"  score {'-' if r.score is None else r.score}"


def diff_listing(r):
    """The shipped and compiled disassembly side by side: `=` the same, `!` changed, `<` shipped only, `>` ours
    only; relocated operands as <reloc>. Ends with `score <n>`."""
    out = [f"; {r.name}  [{r.shipped}]: {r.state}" + (f", {r.detail}" if r.detail else "")]
    for mark, left, right in r.rows:
        left = f"+{left[0]:05x}  {left[1]}" if left else ""
        out.append(f"{mark} {left:<59} {f'+{right[0]:05x}  {right[1]}' if right else ''}".rstrip())
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


def match(dll, obj, diff=()):
    """Each function of a compiled COFF object against the shipped function of the same decorated name
    (a public symbol), or else of the same qualified name when only one function has it. Bytes a
    relocation fills are masked on both sides: the shipped code has the linker's values there. Differing
    functions, and those named in `diff` (decorated or shipped name), are aligned instruction by instruction
    (needs objdump): rows and score."""
    try:
        o = coff.parse(Path(obj).read_bytes())
    except coff.NotCOFF as e:
        raise Fail(f"{obj}: {e}")
    m = Module(dll)
    by_mangled = {f.mangled: f for f in m.functions if f.mangled}
    by_name = {}
    for f in m.functions:
        by_name.setdefault(f.name, []).append(f)
    results, pairs = [], []
    for fn in o.functions():
        shipped, how = by_mangled.get(fn.name), ""
        if shipped is None:
            candidates = by_name.get(qualified_name(fn.name), [])
            if len(candidates) == 1:
                shipped, how = candidates[0], "by name"
        if shipped is None:
            results.append(Match(fn.name, "missing", detail="no function of that name in the shipped module"))
            continue
        results.append(_compare(m, fn, shipped, how))
        pairs.append((results[-1], fn, shipped))
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
