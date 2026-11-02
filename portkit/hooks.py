"""The tables a hook DLL links in, to replace a shipped module's functions with recovered ones.

The hook DLL (windows/recomp-hooks) holds the recovered functions, compiled against the game's headers, and
is loaded into the game before the module it hooks. What it cannot get from an import library comes from
the object `emit` writes:

- a hook per recovered function: the shipped function's RVA, length and first bytes (to check the build),
  and a pointer to ours. The loader writes a jump over the shipped entry once the module is mapped;
- a pointer slot per symbol the recovered code takes from a project module (the hooked module itself and the
  game's plugins, which have no import library): `__imp_<name>` for what the headers declare with the
  module's _API macro, or a jump thunk named after a function referenced directly. The loader fills each
  slot with the module's base plus the RVA the PDB gives, once that module is mapped;
- with `trace`, every function of the hooked module, for the loader's trace mode (first execution of each).

Engine modules are left to their import libraries. Only symbols and RVAs come from the game here; the
generated object stays with the build, outside this repository.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import coff, config, pdb, pe
from .decomp import Module
from .util import Fail

MIN_HOOK = 5   # a rel32 jump to the loader's trampoline


class Shipped:
    """A shipped module and its PDB: {decorated name: (rva, is_code)}, {decorated name: (rva, length,
    source)}, every function's RVA, and its bytes."""

    def __init__(self, dll):
        self.path = Path(dll)
        self.name = self.path.name
        pdb_path = Module.pdb_of(dll)
        image = pe.parse(dll)
        self.data = self.path.read_bytes()
        self.timestamp = image.timestamp
        lfanew = struct.unpack_from("<I", self.data, 0x3C)[0]
        self.size_of_image = struct.unpack_from("<I", self.data, lfanew + 24 + 56)[0]
        self.sections = image.sections
        self.symbols = {}
        for name, seg, off, flags in pdb.public_symbols(pdb_path):
            if 0 < seg <= len(self.sections):
                s = self.sections[seg - 1]
                self.symbols.setdefault(name, (s.vaddr + off, bool(flags & 3) or s.executable))
        self.functions, rvas = {}, set()
        for f in pdb.functions(pdb_path):
            if not 0 < f.segment <= len(self.sections) or not self.sections[f.segment - 1].executable:
                continue
            rva = self.sections[f.segment - 1].vaddr + f.offset
            rvas.add(rva)
            if f.mangled:
                self.functions.setdefault(f.mangled, (rva, f.length, f.source or ""))
        self.all_function_rvas = sorted(rvas)

    def bytes_at(self, rva, n):
        for s in self.sections:
            if s.vaddr <= rva < s.vaddr + s.raw_size:
                start = s.raw_ptr + rva - s.vaddr
                return self.data[start:start + n]
        return b""


@dataclass
class Hook:
    name: str
    rva: int
    length: int
    expect: bytes


@dataclass
class Slot:
    symbol: str     # the name the recovered code references
    module: int     # index in the module list
    rva: int
    thunk: bool     # a direct call: a jump through the slot, under the function's own name


@dataclass
class Plan:
    hooks: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    left: list = field(default_factory=list)      # for the linker: engine import libraries, the CRT
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # (name, why): recovered but not hooked


def _externals(objects):
    defined, undefined = set(), set()
    for o in objects:
        for _, name, value, number, _, storage in o.symbols:
            if storage != 2:
                continue
            if number > 0:
                defined.add(name)
            elif number == 0 and value == 0:
                undefined.add(name)
    return defined, undefined


def plan(recovered, others, modules):
    """recovered, others: parsed objects of the hook DLL (the recovered sources, the rest); modules: the hooked
    module first, then the other project modules its code may reference."""
    p = Plan()
    defined, undefined = _externals(recovered + others)
    index = {}
    for i, m in enumerate(modules):
        for name, (rva, code) in m.symbols.items():
            index.setdefault(name, (i, rva, code))
    for name in sorted(undefined - defined):
        target = name[6:] if name.startswith("__imp_") else name
        if not target.startswith("?") or target not in index:   # C names: the CRT's, for our own CRT
            p.left.append(name)
            continue
        i, rva, code = index[target]
        if target != name:
            p.slots.append(Slot(name, i, rva, False))
        elif code:
            p.slots.append(Slot(name, i, rva, True))
        else:
            p.errors.append(f"{name}: data in {modules[i].name} referenced directly "
                            "(declare it with its module's _API macro)")
    game, seen = modules[0], set()
    for o in recovered:
        for _, name, _, number, kind, storage in o.symbols:
            if storage != 2 or number <= 0 or kind != coff.DTYPE_FUNCTION or name in seen:
                continue
            if not o.sections[number - 1].code or name not in game.functions:
                continue
            seen.add(name)
            rva, length, source = game.functions[name]
            if name.startswith(("??__E", "??__F")):
                p.skipped.append((name, "a dynamic initializer or atexit: the file's statics in the hook DLL are its "
                                        "own copies, the game's would stay uninitialized"))
            elif not source.lower().endswith(".cpp"):
                p.skipped.append((name, f"defined in a header ({Path(source.replace(chr(92), '/')).name}), "
                                        "inlined copies would stay"))
            elif length < MIN_HOOK:
                p.skipped.append((name, f"shorter than {MIN_HOOK} bytes ({length})"))
            elif any(h.rva == rva for h in p.hooks):
                p.skipped.append((name, f"folded with {next(h.name for h in p.hooks if h.rva == rva)}: hooked once"))
            else:
                p.hooks.append(Hook(name, rva, length, game.bytes_at(rva, 8).ljust(8, b"\0")))
    return p


def emit(p, modules, trace=False):
    """The tables object. Layouts, read by windows/recomp-hooks/RecompHooks.cpp:
    RecompModules[RecompModuleCount]: {const char* name; uint32 timestamp, size_of_image; void* base}
    RecompImports[RecompImportCount]: {void** slot; const char* symbol; uint32 module, rva}
    RecompHooks[RecompHookCount]: {void* ours; const char* name; uint32 rva, length; uint8 expect[8]}
    RecompTraceRvas[RecompTraceCount]: uint32, the hooked module's functions, sorted."""
    w = coff.Writer()
    rdata = w.section(".rdata", coff.RDATA_FLAGS)
    data = w.section(".data", coff.DATA_FLAGS)
    text = w.section(".text", coff.CODE_FLAGS) if any(s.thunk for s in p.slots) else None
    rdata_sym, data_sym = w.local(".rdata", rdata, 0), w.local(".data", data, 0)

    def string(s):
        return w.data(rdata, s.encode() + b"\0")

    def pointer(section, offset, symbol, addend=0):
        w.relocate(section, offset, coff.ADDR64, symbol)
        buf = w.sections[section - 1][2]
        buf[offset:offset + 8] = struct.pack("<Q", addend)

    def count(name, n):
        w.define(name, rdata, w.data(rdata, struct.pack("<I", n), 4))

    names = [string(m.name) for m in modules]
    at = w.data(data, b"", 8)
    w.define("RecompModules", data, at)
    for m, n in zip(modules, names):
        o = w.data(data, b"\0" * 8 + struct.pack("<II", m.timestamp, m.size_of_image) + b"\0" * 8)
        pointer(data, o, rdata_sym, n)
    count("RecompModuleCount", len(modules))

    slots = []
    for s in p.slots:
        o = w.data(data, b"\0" * 8, 8)
        if s.thunk:
            w.local(f"$slot{len(slots)}", data, o)
            t = w.data(text, b"\xff\x25\0\0\0\0\xcc\xcc", 8)   # jmp [rip+slot]
            w.define(s.symbol, text, t, function=True)
            w.relocate(text, t + 2, coff.REL32, data_sym)
            w.sections[text - 1][2][t + 2:t + 6] = struct.pack("<i", o)
        else:
            w.define(s.symbol, data, o)
        slots.append(o)
    at = w.data(data, b"", 8)
    w.define("RecompImports", data, at)
    for s, slot in zip(p.slots, slots):
        o = w.data(data, b"\0" * 16 + struct.pack("<II", s.module, s.rva))
        pointer(data, o, data_sym, slot)
        pointer(data, o + 8, rdata_sym, string(s.symbol))
    count("RecompImportCount", len(p.slots))

    entries = [(h, string(h.name)) for h in p.hooks]
    at = w.data(rdata, b"", 8)
    w.define("RecompHooks", rdata, at)
    for h, n in entries:
        o = w.data(rdata, b"\0" * 16 + struct.pack("<II", h.rva, h.length) + h.expect[:8].ljust(8, b"\0"))
        pointer(rdata, o, w.external(h.name, function=True))
        pointer(rdata, o + 8, rdata_sym, n)
    count("RecompHookCount", len(p.hooks))

    rvas = modules[0].all_function_rvas if trace else []
    w.define("RecompTraceRvas", rdata, w.data(rdata, struct.pack(f"<{len(rvas)}I", *rvas), 4))
    count("RecompTraceCount", len(rvas))
    return w.build()


def generate(dlls, objects, recovered, out, trace=False):
    """dlls: the hooked module first, then the project modules; objects: every object of the hook DLL;
    recovered: those compiled from recovered sources. Writes the tables object; returns the plan."""
    if config.inside_repository(out):
        raise Fail(f"{out} is inside this repository; game-derived material stays outside it")
    modules = [Shipped(d) for d in dlls]
    rec_set = {Path(r).resolve() for r in recovered}
    parsed_rec, parsed_other = [], []
    for o in objects:
        try:
            parsed = coff.parse(Path(o).read_bytes())
        except coff.NotCOFF as e:
            raise Fail(f"{o}: {e}")
        (parsed_rec if Path(o).resolve() in rec_set else parsed_other).append(parsed)
    p = plan(parsed_rec, parsed_other, modules)
    if p.errors:
        raise Fail("cannot link these references:\n  " + "\n  ".join(p.errors))
    Path(out).write_bytes(emit(p, modules, trace))
    return p
