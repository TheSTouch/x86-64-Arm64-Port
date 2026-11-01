"""Work material for recovering a module's source: its functions from the PDB, and their disassembly.

The disassembly names what each instruction reaches: imported functions (through
the import address table), the module's own functions (from the PDB) and string
literals. What `dump` writes is derived from the game, so it refuses to write
inside this repository (docs/SATISFACTORY-RECOMP.md: recovered code stays on the
owner's disks).
"""

import re
import shutil
import subprocess
from pathlib import Path

from . import config, pdb, pe
from .util import Fail

ADDRESS = re.compile(r"0x([0-9a-f]{6,16})")
LINE = re.compile(r"^\s*([0-9a-f]+):\s+(.*)$")


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
        if not shutil.which("objdump"):
            raise Fail("objdump not found: install Xcode's command line tools")
        base = self.image.image_base
        start = base + self.rva(f.segment, f.offset)
        out = subprocess.run(["objdump", "-d", "--no-show-raw-insn", "--x86-asm-syntax=intel",
                              f"--start-address={start:#x}", f"--stop-address={start + (length or f.length):#x}",
                              str(self.dll)], capture_output=True, text=True, check=False)
        lines = [f"; {f.name}",
                 f"; {Path(f.source.replace(chr(92), '/')).name}:{f.first_line}-{f.last_line}" if f.source else "; no line info",
                 f"; rva {self.rva(f.segment, f.offset):#x}, {f.length} bytes, compiland {f.compiland}"]
        if f.mangled:
            lines.append(f"; {f.mangled}")
        if f.locals:
            lines.append(f"; locals: {', '.join(f.locals)}")
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
            lines.append(f"  +{addr - start:05x}  {insn:<56}" + (f" ; {'; '.join(notes)}" if notes else ""))
        return "\n".join(lines) + "\n"


def _is_game_source(path):
    p = path.replace("/", "\\").lower()
    return "\\source\\" in p and not any(x in p for x in ("\\engine\\", "\\intermediate\\", "\\thirdparty\\"))


def _inside_repository(path):
    try:
        Path(path).resolve().relative_to(config.ROOT)
        return True
    except ValueError:
        return False


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.replace("::", "__")).strip("_")[:120] or "anonymous"


def dump(dll, out, source_marker=None):
    """One folder per module, holding functions.tsv and one .asm per game function, by source file."""
    if _inside_repository(out):
        raise Fail(f"{out} is inside this repository; game-derived material stays outside it")
    m = Module(dll)
    module = re.sub(r"-Win64-(Shipping|Test|Development)$", "", Path(dll).stem)
    root = Path(out) / module
    root.mkdir(parents=True, exist_ok=True)
    rows = ["name\trva\tlength\tsource\tfirst_line\tlast_line\tlocals\tmangled"]
    for f in (fns := m.game_functions(source_marker)):
        source = Path(f.source.replace("\\", "/")).name
        rows.append("\t".join([f.name, f"{m.rva(f.segment, f.offset):#x}", str(f.length), source,
                               str(f.first_line), str(f.last_line), ",".join(f.locals), f.mangled or ""]))
        folder = root / source
        folder.mkdir(exist_ok=True)
        target = folder / f"{_safe(f.name)}.asm"
        if target.exists():
            target = folder / f"{_safe(f.name)}@{m.rva(f.segment, f.offset):x}.asm"
        target.write_text(m.disassemble(f), encoding="utf-8")
    (root / "functions.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root, fns
