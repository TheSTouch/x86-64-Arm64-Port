import struct
import tempfile
import unittest
from pathlib import Path

from portkit import kettle, pe

from .test_pe import build_pe

# What Satisfactory imports from steam_api64.dll (steam_api/satisfactory-interfaces.md).
SATISFACTORY_IMPORTS = {
    "SteamAPI_Init", "SteamAPI_Shutdown", "SteamAPI_RunCallbacks", "SteamAPI_RestartAppIfNecessary",
    "SteamAPI_GetHSteamUser", "SteamAPI_RegisterCallback", "SteamAPI_UnregisterCallback",
    "SteamAPI_RegisterCallResult", "SteamAPI_UnregisterCallResult", "SteamInternal_ContextInit",
    "SteamInternal_CreateInterface", "SteamInternal_FindOrCreateUserInterface",
    "SteamInternal_FindOrCreateGameServerInterface", "SteamInternal_GameServer_Init",
    "SteamGameServer_GetHSteamUser", "SteamGameServer_RunCallbacks", "SteamGameServer_Shutdown",
}


def with_exports(names, dll=b"test.dll"):
    """build_pe's exe with an export directory in the .idata section naming `names`."""
    buf = bytearray(build_pe(b"\xc3" * 16))
    opt, idata = 0x58, 0x1400 - 0x2000           # file offset of an .idata RVA
    struct.pack_into("<II", buf, opt + 112, 0x2100, 40)
    exp = 0x2100
    name_rvas, at = [], 0x2180
    for n in [dll, *(x.encode() for x in names)]:
        buf[idata + at:idata + at + len(n) + 1] = n + b"\0"
        name_rvas.append(at)
        at += len(n) + 1
    struct.pack_into("<IIHHIIIIIII", buf, idata + exp, 0, 0, 0, 0, name_rvas[0], 1, len(names), len(names),
                     0, 0x2140, 0)
    for i, rva in enumerate(name_rvas[1:]):
        struct.pack_into("<I", buf, idata + 0x2140 + 4 * i, rva)
    return bytes(buf)


class ExportTableTest(unittest.TestCase):
    def test_export_names(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "x.dll")
            path.write_bytes(with_exports(["SteamAPI_Init", "SteamAPI_Shutdown"]))
            p = pe.parse(path)
            self.assertEqual(p.exports, 2)
            self.assertEqual(p.export_names, ["SteamAPI_Init", "SteamAPI_Shutdown"])
            self.assertEqual(p.export_dll, "test.dll")

    def test_built_dll(self):
        """The bridge's steam_api64.dll exports everything Satisfactory imports and is a Wine builtin."""
        dll = kettle.build_root("bridge") / "x86_64-windows" / kettle.DLL
        if not dll.exists():
            self.skipTest("bridge not built (./port steam check <game> builds it)")
        p = pe.parse(dll)
        self.assertEqual(p.machine, "x86-64")
        self.assertTrue(p.is_dll)
        self.assertEqual(p.export_dll, kettle.DLL)
        self.assertLessEqual(SATISFACTORY_IMPORTS, set(p.export_names))
        self.assertTrue(kettle.is_wine_builtin(dll))
        so = kettle.build_root("bridge") / "x86_64-unix" / "steam_api64.so"
        self.assertTrue(so.exists())

    def test_every_import_of_the_installed_game(self):
        """Every steam_api64 function any module of Satisfactory imports, read from the game itself."""
        from portkit import games
        dll = kettle.build_root("bridge") / "x86_64-windows" / kettle.DLL
        g = games.load("satisfactory")
        if not dll.exists() or not g.root or not g.root.is_dir():
            self.skipTest("bridge not built or Satisfactory not installed")
        wanted = set()
        for f in [*g.root.rglob("*.exe"), *g.root.rglob("*-Shipping.dll")]:
            try:
                p = pe.parse(f)
            except pe.NotPE:
                continue
            for name, funcs in {**p.imports, **p.delay_imports}.items():
                if name.lower() == kettle.DLL:
                    wanted |= set(funcs)
        self.assertEqual(wanted, SATISFACTORY_IMPORTS)
        self.assertLessEqual(wanted, set(pe.parse(dll).export_names))


if __name__ == "__main__":
    unittest.main()
