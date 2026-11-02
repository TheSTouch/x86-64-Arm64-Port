import struct
import tempfile
import unittest
from pathlib import Path

from portkit import coff, hooks, pdb
from portkit.util import Fail
from tests.test_decomp import build_dll
from tests.test_pdb import build_pdb

CONSTRUCT = "?Construct@FGScreen@@QEAAXAEBUFArguments@1@@Z"


def obj(defined=(), undefined=()):
    """A compiled object: each defined function a `ret` in its own .text$mn section, and references
    (REL32 relocations from a call) to each undefined symbol."""
    w = coff.Writer()
    for name in defined:
        text = w.section(".text$mn", coff.CODE_FLAGS)
        code = b"\xe8\0\0\0\0" * len(undefined) + b"\xc3"
        w.data(text, code)
        w.define(name, text, 0, function=True)
        for i, ref in enumerate(undefined):
            w.relocate(text, 5 * i + 1, coff.REL32, w.external(ref))
    return w.build()


class ModuleFixture:
    """A shipped module as `hooks` sees it, without a PDB: symbols {name: (rva, is_code)}, functions
    {decorated name: (rva, length, source)}, code bytes."""

    def __init__(self, name, symbols, functions=None, code=b""):
        self.name, self.symbols, self.functions = name, symbols, functions or {}
        self.timestamp, self.size_of_image, self.code = 0x1234, 0x8000, code
        self.all_function_rvas = sorted(r for r, _, _ in self.functions.values())

    def bytes_at(self, rva, n):
        return self.code[:n]


GAME = ModuleFixture("Game-FactoryGame-Win64-Shipping.dll",
                     {CONSTRUCT: (0x1010, True), "?Helper@@YAXXZ": (0x1100, True), "?GData@@3HA": (0x5000, False),
                      "?Inline@FGScreen@@QEAAHXZ": (0x1200, True), "?Tiny@@YAXXZ": (0x1300, True),
                      "__security_cookie": (0x6000, False), "memcpy": (0x1400, True),
                      "??__EGName@@YAXXZ": (0x1500, True)},
                     {CONSTRUCT: (0x1010, 0x40, "D:\\Game\\Source\\Game\\Private\\Screen.cpp"),
                      "?Helper@@YAXXZ": (0x1100, 0x20, "D:\\Game\\Source\\Game\\Private\\Helper.cpp"),
                      "?Inline@FGScreen@@QEAAHXZ": (0x1200, 0x10, "D:\\Game\\Source\\Game\\Public\\Screen.h"),
                      "?Tiny@@YAXXZ": (0x1300, 3, "D:\\Game\\Source\\Game\\Private\\Tiny.cpp"),
                      "??__EGName@@YAXXZ": (0x1500, 0x20, "D:\\Game\\Source\\Game\\Private\\Screen.cpp")},
                     code=bytes(range(0x40, 0x50)))
PLUGIN = ModuleFixture("Game-OnlineIntegration-Win64-Shipping.dll", {"?Login@@YAXXZ": (0x2000, True)})


class PlanTest(unittest.TestCase):
    def plan(self, recovered, others=()):
        return hooks.plan([coff.parse(o) for o in recovered], [coff.parse(o) for o in others], [GAME, PLUGIN])

    def test_recovered_functions_from_a_cpp_are_hooked(self):
        p = self.plan([obj(defined=[CONSTRUCT, "?Inline@FGScreen@@QEAAHXZ", "?Tiny@@YAXXZ"])])
        self.assertEqual([(h.name, h.rva, h.length) for h in p.hooks], [(CONSTRUCT, 0x1010, 0x40)])
        self.assertEqual(p.hooks[0].expect, bytes(range(0x40, 0x48)))
        skipped = dict(p.skipped)
        self.assertIn("header", skipped["?Inline@FGScreen@@QEAAHXZ"])
        self.assertIn("shorter than", skipped["?Tiny@@YAXXZ"])

    def test_dynamic_initializers_are_not_hooked(self):
        # they initialize the file's statics: in the hook DLL those are its own copies, the game's would stay empty
        p = self.plan([obj(defined=[CONSTRUCT, "??__EGName@@YAXXZ"])])
        self.assertEqual([h.name for h in p.hooks], [CONSTRUCT])
        self.assertIn("initializer", dict(p.skipped)["??__EGName@@YAXXZ"])

    def test_imports_from_project_modules_become_slots(self):
        p = self.plan([obj(defined=[CONSTRUCT], undefined=["__imp_?GData@@3HA", "__imp_?Login@@YAXXZ",
                                                            "__imp_?Engine@@YAXXZ"])])
        self.assertEqual({(s.symbol, s.module, s.rva, s.thunk) for s in p.slots},
                         {("__imp_?GData@@3HA", 0, 0x5000, False), ("__imp_?Login@@YAXXZ", 1, 0x2000, False)})
        self.assertIn("__imp_?Engine@@YAXXZ", p.left)

    def test_direct_calls_get_a_thunk_and_direct_data_is_an_error(self):
        p = self.plan([obj(defined=[CONSTRUCT], undefined=["?Helper@@YAXXZ", "?GData@@3HA"])])
        self.assertEqual([(s.symbol, s.thunk) for s in p.slots], [("?Helper@@YAXXZ", True)])
        self.assertEqual(p.errors, ["?GData@@3HA: data in Game-FactoryGame-Win64-Shipping.dll referenced "
                                    "directly (declare it with its module's _API macro)"])

    def test_c_names_stay_with_the_linker(self):
        # the CRT's statically linked pieces are public in the shipped module too; ours come from our own CRT
        p = self.plan([obj(defined=[CONSTRUCT], undefined=["__security_cookie", "memcpy", "__imp_memcpy"])])
        self.assertEqual((p.slots, p.errors), ([], []))
        self.assertEqual(p.left, ["__imp_memcpy", "__security_cookie", "memcpy"])

    def test_symbols_defined_in_another_object_are_not_imported(self):
        p = self.plan([obj(defined=[CONSTRUCT], undefined=["?Helper@@YAXXZ"])], [obj(defined=["?Helper@@YAXXZ"])])
        self.assertEqual(p.slots, [])


class EmitTest(unittest.TestCase):
    def test_tables_object(self):
        p = hooks.plan([coff.parse(obj(defined=[CONSTRUCT], undefined=["__imp_?GData@@3HA", "?Helper@@YAXXZ"]))],
                       [], [GAME, PLUGIN])
        o = coff.parse(hooks.emit(p, [GAME, PLUGIN], trace=True))
        names = {name: (number, value) for _, name, value, number, _, storage in o.symbols if storage == 2}
        for sym in ("RecompModules", "RecompModuleCount", "RecompImports", "RecompImportCount", "RecompHooks",
                    "RecompHookCount", "RecompTraceRvas", "RecompTraceCount", "__imp_?GData@@3HA", "?Helper@@YAXXZ"):
            self.assertIn(sym, names)
        self.assertEqual(names[CONSTRUCT][0], 0)               # the hook's target: ours, undefined here
        text = o.sections[names["?Helper@@YAXXZ"][0] - 1]
        self.assertTrue(text.code)
        self.assertEqual(text.data[:2], b"\xff\x25")            # jmp [rip+slot]
        self.assertEqual([k for _, _, k in text.relocations], [coff.REL32])

        def u32(sym, i=0):
            number, value = names[sym]
            return struct.unpack_from("<I", o.sections[number - 1].data, value + 4 * i)[0]
        self.assertEqual(u32("RecompHookCount"), 1)
        self.assertEqual(u32("RecompImportCount"), 2)
        self.assertEqual(u32("RecompModuleCount"), 2)
        self.assertEqual(u32("RecompTraceCount"), 5)
        self.assertEqual([u32("RecompTraceRvas", i) for i in range(5)], [0x1010, 0x1100, 0x1200, 0x1300, 0x1500])


class WriterTest(unittest.TestCase):
    def test_round_trip(self):
        w = coff.Writer()
        data = w.section(".data", coff.DATA_FLAGS)
        w.data(data, b"\0" * 16)
        w.define("Slot", data, 8)
        w.relocate(data, 0, coff.ADDR64, w.external("?Target@@YAXXZ", function=True))
        o = coff.parse(w.build())
        self.assertEqual(o.sections[0].name, ".data")
        self.assertEqual(o.sections[0].relocations, [(0, 1, coff.ADDR64)])
        self.assertEqual([(n, v, num) for _, n, v, num, _, _ in o.symbols], [("Slot", 8, 1), ("?Target@@YAXXZ", 0, 0)])


class PublicSymbolsTest(unittest.TestCase):
    def test_every_public_with_its_flags(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.pdb"
            p.write_bytes(build_pdb())
            self.assertEqual(pdb.public_symbols(p), [(CONSTRUCT, 1, 0x10, 2)])

    def test_shipped_module_from_a_dll_and_its_pdb(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d) / "Game-Screen-Win64-Shipping.dll"
            dll.write_bytes(build_dll())
            dll.with_suffix(".pdb").write_bytes(build_pdb())
            m = hooks.Shipped(dll)
            self.assertEqual(m.symbols[CONSTRUCT], (0x1010, True))
            self.assertEqual(m.functions[CONSTRUCT][:2], (0x1010, 0x40))
            self.assertEqual(len(m.bytes_at(0x1010, 8)), 8)

    def test_missing_pdb(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d) / "Game-Screen-Win64-Shipping.dll"
            dll.write_bytes(build_dll())
            with self.assertRaises(Fail):
                hooks.Shipped(dll)
