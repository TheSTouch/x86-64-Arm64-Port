import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import config, kettle
from portkit.util import Fail

from .test_pe import build_pe

spec = importlib.util.spec_from_file_location("gen_vtables", config.ROOT / "steam_api/gen_vtables.py")
gen_vtables = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_vtables)

LOG = """\
[steam_api] inventory build: no Steam, every method answers nothing
[steam_api] export SteamAPI_Init
[steam_api] SteamAPI_Init: ok
[steam_api] export SteamInternal_FindOrCreateUserInterface
[steam_api] interface SteamUtils010 (FindOrCreateUserInterface)
[steam_api] interface SteamUtils010 (FindOrCreateGameServerInterface, user 0): no game server
[steam_api] call ISteamUtils::IsSteamRunningOnSteamDeck (SteamUtils010 slot 34), stub
[steam_api] call ISteamUser::BLoggedOn (SteamUser023 slot 1)
[steam_api] register callback 304 PersonaStateChange_t
[steam_api] export SteamAPI_Init
[steam_api] summary: methods called (count):
[steam_api]   ISteamUser::BLoggedOn 12
"""


class InventoryTest(unittest.TestCase):
    def test_parse_log(self):
        f = kettle.parse_log(LOG)
        self.assertEqual(f["exports"], ["SteamAPI_Init", "SteamInternal_FindOrCreateUserInterface"])
        self.assertEqual(f["interfaces"], [("SteamUtils010", "FindOrCreateUserInterface")])
        self.assertEqual(f["missing"], [("SteamUtils010", "FindOrCreateGameServerInterface, user 0", "no game server")])
        self.assertEqual(f["methods"][("ISteamUtils", "IsSteamRunningOnSteamDeck")], ("SteamUtils010", 34))
        self.assertEqual(f["callbacks"], {304: "PersonaStateChange_t"})
        self.assertEqual(f["counts"], {("ISteamUser", "BLoggedOn"): 12})

    def test_report(self):
        md = kettle.inventory_report(LOG, "Game")
        self.assertIn("| 1 | `BLoggedOn` | 12 |", md)
        self.assertIn("| 34 | `IsSteamRunningOnSteamDeck` |  |", md)
        self.assertIn("| 304 | `PersonaStateChange_t` |", md)


class BuiltinTest(unittest.TestCase):
    def test_mark_wine_builtin(self):
        # lld (Zig) puts the PE header at 0x78, after a DOS stub the signature replaces.
        data = bytearray(0x200)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x78)
        data[0x78:0x7C] = b"PE\0\0"
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d, "steam_api64.dll")
            dll.write_bytes(bytes(data))
            self.assertFalse(kettle.is_wine_builtin(dll))
            kettle.mark_wine_builtin(dll)
            self.assertTrue(kettle.is_wine_builtin(dll))
            marked = dll.read_bytes()
            self.assertEqual(marked[0x40:0x51], b"Wine builtin DLL\0")
            self.assertEqual(marked[0x78:0x7C], b"PE\0\0")

    def test_no_room_for_the_signature(self):
        with tempfile.TemporaryDirectory() as d:
            dll = Path(d, "steam_api64.dll")
            dll.write_bytes(build_pe(b"\xc3" * 16))   # PE header right after the DOS header
            with self.assertRaises(Fail):
                kettle.mark_wine_builtin(dll)


class VtableTest(unittest.TestCase):
    BODY = """
        virtual bool BLoggedOn() = 0;
        virtual CSteamID GetSteamID() = 0;
        STEAM_FLAT_NAME( GetStatInt32 )
        virtual bool GetStat( const char *pchName, int32 *pData ) = 0;
        STEAM_FLAT_NAME( GetStatFloat )
        virtual bool GetStat( const char *pchName, float *pData ) = 0;
        STEAM_CALL_RESULT( UserStatsReceived_t )
        virtual SteamAPICall_t RequestUserStats( CSteamID steamIDUser ) = 0;
        STEAM_PRIVATE_API( virtual void RunFrame() = 0; )
        virtual float GetLocalPingLocation( SteamNetworkPingLocation_t &result ) = 0;
        virtual const char *GetPersonaName() = 0;
    """

    def test_msvc_layout(self):
        ms = gen_vtables.methods(self.BODY, {"SteamAPICall_t"})
        self.assertEqual([m["flat"] for m in ms], ["BLoggedOn", "GetSteamID", "GetStatFloat", "GetStatInt32",
                                                  "RequestUserStats", "RunFrame", "GetLocalPingLocation",
                                                  "GetPersonaName"])
        kinds = {m["flat"]: (m["kind"], m["size"]) for m in ms}
        self.assertEqual(kinds["GetSteamID"], (gen_vtables.RECORD, 8))   # through a hidden pointer
        self.assertEqual(kinds["GetLocalPingLocation"][0], gen_vtables.FLOAT)
        self.assertEqual(kinds["GetPersonaName"][0], gen_vtables.STRING)
        self.assertEqual(kinds["RunFrame"][0], gen_vtables.VOID)

    def test_generated_tables_up_to_date(self):
        if not (gen_vtables.SDK_DIR / "isteamuser.h").exists():
            self.skipTest("SDK headers not fetched (steam_api/gen_vtables.py --fetch)")
        self.assertEqual(gen_vtables.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
