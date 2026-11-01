import tempfile
import unittest

from portkit import trace

LOG = r'''0020:0024:trace:loaddll:build_module Loaded L"C:\\windows\\system32\\kernelbase.dll" at 00006FFFFFB80000: builtin
0020:0024:trace:loaddll:load_native_dll Loaded L"C:\\Games\\Racer\\Racer-Win64-Shipping.exe" at 0000000140000000: native
0020:0024:trace:loaddll:load_native_dll Loaded L"C:\\Games\\Racer\\steam_api64.dll" at 0000000180000000: native
0020:0024:trace:loaddll:load_native_dll Loaded L"C:\\Program Files (x86)\\Steam\\gameoverlayrenderer64.dll" at 0000000190000000: native
0020:0028:fixme:dxgi:DXGIFactory_CheckFeatureSupport Unsupported feature 0x1 at 000000001234ABCD
0020:0028:fixme:dxgi:DXGIFactory_CheckFeatureSupport Unsupported feature 0x1 at 000000001234ABCE
0020:0024:Call KERNEL32.CreateFileW(00001234,80000000,00000001,00000000,00000003,00000080,00000000) ret=14001a2b3
0020:0024:Call KERNEL32.CreateFileW(00001234,80000000,00000001,00000000,00000003,00000080,00000000) ret=14001a2b3
0020:0024:Call USER32.PeekMessageW(0021fd40,00000000,00000000,00000000,00000001) ret=1400ff000
0030:0034:trace:loaddll:build_module Loaded L"C:\\Program Files (x86)\\Steam\\steam.exe" at 0000000000400000: native
D3DM: Created device "Apple M4"
'''


class TraceTest(unittest.TestCase):
    def test_analyse_splits_processes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(LOG)
        result, d3dm = trace.analyse(f.name, "Racer-Win64-Shipping.exe")
        g = result["game"]
        self.assertEqual(g["pid"], "0020")
        self.assertEqual(g["builtin"], ["C:\\windows\\system32\\kernelbase.dll"])
        self.assertEqual(len(g["native"]), 3)  # no game folder given: every native DLL
        self.assertEqual(g["problems"][0][1], 2)  # the two fixmes differ only by address
        self.assertEqual(g["api_calls"], {"KERNEL32.CreateFileW": 2, "USER32.PeekMessageW": 1})
        self.assertEqual(g["call_sites"], ["0x14001a2b3", "0x1400ff000"])
        self.assertEqual(len(result["processes"]), 2)
        self.assertEqual(d3dm, ['D3DM: Created device "Apple M4"'])

    def test_native_dlls_outside_the_game_folder_are_listed_apart(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(LOG)
        g = trace.analyse(f.name, "Racer-Win64-Shipping.exe", game_root="c:\\games\\racer")[0]["game"]
        self.assertEqual(len(g["native"]), 2)
        self.assertEqual(g["other_native"], ["C:\\Program Files (x86)\\Steam\\gameoverlayrenderer64.dll"])


if __name__ == "__main__":
    unittest.main()
