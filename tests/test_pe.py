import os
import struct
import tempfile
import unittest
from pathlib import Path

from portkit import inspect, pe


def build_pe(text):
    """A minimal PE32+ exe: .text holding `text`, .idata importing KERNEL32!ExitProcess and #5."""
    buf = bytearray(0x1600)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x40)
    buf[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, 0x44, 0x8664, 2, 0, 0, 0, 240, 0x22)
    opt = 0x58
    struct.pack_into("<H", buf, opt, 0x20B)
    struct.pack_into("<I", buf, opt + 16, 0x1000)
    struct.pack_into("<Q", buf, opt + 24, 0x140000000)
    struct.pack_into("<H", buf, opt + 68, 2)
    struct.pack_into("<I", buf, opt + 108, 16)
    struct.pack_into("<II", buf, opt + 112 + 8, 0x2000, 40)  # import directory
    sec = opt + 240
    struct.pack_into("<8sIIII", buf, sec, b".text", 0x1000, 0x1000, 0x1000, 0x400)
    struct.pack_into("<I", buf, sec + 36, 0x60000020)
    struct.pack_into("<8sIIII", buf, sec + 40, b".idata", 0x200, 0x2000, 0x200, 0x1400)
    struct.pack_into("<I", buf, sec + 76, 0xC0000040)
    buf[0x400:0x400 + len(text)] = text
    idata = 0x1400 - 0x2000  # file offset of an .idata RVA
    struct.pack_into("<IIIII", buf, idata + 0x2000, 0x2040, 0, 0, 0x2060, 0x2040)
    struct.pack_into("<QQQ", buf, idata + 0x2040, 0x2070, (1 << 63) | 5, 0)
    buf[idata + 0x2060:idata + 0x206D] = b"KERNEL32.dll\0"
    buf[idata + 0x2072:idata + 0x207E] = b"ExitProcess\0"
    return bytes(buf)


class PETest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_headers_sections_imports(self):
        b = pe.parse(self.write(build_pe(b"\x90" * 0x1000)), with_entropy=True)
        self.assertEqual(b.machine, "x86-64")
        self.assertTrue(b.pe32_plus)
        self.assertEqual(b.subsystem, "windows-gui")
        self.assertEqual(b.entry, 0x1000)
        self.assertEqual([s.name for s in b.sections], [".text", ".idata"])
        self.assertTrue(b.sections[0].executable)
        self.assertEqual(b.sections[0].entropy, 0.0)
        self.assertEqual(b.imports, {"KERNEL32.dll": ["ExitProcess", "#5"]})
        self.assertEqual(b.code_size, 0x1000)

    def test_not_pe(self):
        with self.assertRaises(pe.NotPE):
            pe.parse(self.write(b"not a pe file at all" * 10))

    def test_random_code_is_flagged_as_protected(self):
        report = inspect.inspect_file(self.write(build_pe(os.urandom(0x1000))))
        self.assertIn("encrypted or packed at rest", report["sections"][0]["flags"])
        self.assertTrue(any("encrypted" in p for p in report["protection"]))
        plain = inspect.inspect_file(self.write(build_pe(b"\x48\x89\xe5\xc3" * 0x400)))
        self.assertEqual(plain["protection"], [])

    def test_folder_routes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Game.exe").write_bytes(build_pe(b"\xc3" * 0x1000))
            (root / "EasyAntiCheat").mkdir()
            (root / "steam_api64.dll").write_bytes(b"MZ")
            r = inspect.inspect_folder(root)
            self.assertIn("Easy Anti-Cheat", r["components"])
            self.assertIn("Steamworks", r["components"])
            self.assertTrue(r["main"]["path"].endswith("Game.exe"))
            self.assertEqual(r["routes"]["porting_kit"][0][0], "blocker")


if __name__ == "__main__":
    unittest.main()
