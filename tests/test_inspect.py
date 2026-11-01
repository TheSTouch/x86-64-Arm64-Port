import os
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import inspect, pe
from tests.test_pe import build_pe

GUID = bytes(range(16))


def with_pdb(image, name, guid=GUID, age=1):
    """`image` from build_pe, with a CodeView (RSDS) debug record naming `name`."""
    buf = bytearray(image)
    struct.pack_into("<II", buf, 0x58 + 112 + 6 * 8, 0x2100, 28)  # debug directory in .idata
    raw = 0x1400 + 0x120  # file offset of RVA 0x2120
    struct.pack_into("<IIHHIIII", buf, 0x1400 + 0x100, 0, 0, 0, 0, 2, 24 + len(name) + 1, 0x2120, raw)
    buf[raw:raw + 24] = b"RSDS" + guid + struct.pack("<I", age)
    buf[raw + 24:raw + 25 + len(name)] = name.encode() + b"\0"
    return bytes(buf)


def build_msf(guid=GUID, age=1):
    """A minimal PDB (MSF 7.00): the directory and stream 1, the PDB info stream."""
    bs = 0x200
    buf = bytearray(bs * 6)
    buf[:32] = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"
    struct.pack_into("<6I", buf, 32, bs, 1, 6, 16, 0, 3)
    struct.pack_into("<I", buf, 3 * bs, 4)                 # block map: the directory is in block 4
    struct.pack_into("<4I", buf, 4 * bs, 2, 0, 28, 5)      # 2 streams, sizes 0 and 28, stream 1 in block 5
    struct.pack_into("<III", buf, 5 * bs, 20000404, 0, age)
    buf[5 * bs + 12:5 * bs + 28] = guid
    return bytes(buf)


class InspectTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)

    def put(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def unreal_game(self):
        """A modular Unreal 5 build laid out like Satisfactory's."""
        self.put("Engine/Binaries/Win64/Game-Win64-Shipping.exe", build_pe(b"\xc3" * 0x100))
        self.put("Engine/Binaries/Win64/Game-Core-Win64-Shipping.dll", build_pe(b"\x48\x89\xe5\xc3" * 0x400))
        self.put("Engine/Binaries/Win64/Game-BuildSettings-Win64-Shipping.dll",
                 build_pe("5.6.1-++Game+rel-main-2026-CL-513885\0".encode("utf-16-le")))
        self.put("Engine/Binaries/Win64/Game-VulkanRHI-Win64-Shipping.dll", build_pe(b"\xc3" * 0x100))
        self.put("Engine/Plugins/FX/Niagara/Binaries/Win64/Game-Niagara-Win64-Shipping.dll", build_pe(b"\xc3" * 0x100))
        self.put("Engine/Binaries/ThirdParty/Ogg/libogg_64.dll", build_pe(b"\xc3" * 0x100))
        self.put("Engine/Binaries/Win64/CrashReportClient.exe", build_pe(b"\xc3" * 0x100))
        self.put("Game/Binaries/Win64/Game-Game-Win64-Shipping.dll",
                 with_pdb(build_pe(b"\x48\x89\xe5\xc3" * 0x400), "Game-Game-Win64-Shipping.pdb"))
        self.put("Game/Binaries/Win64/Game-Game-Win64-Shipping.pdb", build_msf())
        self.put("Game/Plugins/Rain/Binaries/Win64/Game-Rain-Win64-Shipping.dll", build_pe(b"\xc3" * 0x100))
        self.put("Game/Content/Paks/Game-Windows.utoc", b"")
        self.put("CommunityResources/Headers.zip", b"PK")

    # A-1: modular builds keep the version in BuildSettings, as UTF-16, on a studio's own branch.
    def test_unreal_version_from_utf16_build_settings(self):
        self.unreal_game()
        r = inspect.inspect_folder(self.root)
        main = r["main"]
        self.assertIn(("risk", "the engine is the studio's own branch (++Game+rel-main-2026), not Epic's release: "
                               "a native engine has to be built from that branch"), r["routes"]["recomp"])
        self.assertEqual(main["unreal_version"], "5.6.1")
        self.assertEqual(main["unreal_branch"], "++Game+rel-main-2026")
        self.assertEqual(main["unreal_changelist"], "513885")

    def test_unreal_version_from_stock_ascii_string(self):
        p = self.put("Game-Win64-Shipping.exe", build_pe(b"junk ++ 5.4.2-34092967+++UE5+Release-5.4\0"))
        self.assertEqual(inspect.unreal_version(p),
                         {"version": "5.4.2", "branch": "++UE5+Release-5.4", "changelist": "34092967"})
        p = self.put("Old-Win64-Shipping.exe", build_pe(b"++UE4+Release-4.27\0"))
        self.assertEqual(inspect.unreal_version(p)["version"], "4.27")


if __name__ == "__main__":
    unittest.main()
