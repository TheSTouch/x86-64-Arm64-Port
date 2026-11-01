import tempfile
import unittest
from pathlib import Path

from portkit import games

from .test_pe import build_pe


class GamesTest(unittest.TestCase):
    def test_resolve_from_library(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            (lib / "steamapps/common/Racer/Racer/Binaries/Win64").mkdir(parents=True)
            (lib / "steamapps/appmanifest_42.acf").write_text(
                '"AppState" { "appid" "42" "name" "STAR WARS: Galactic Racer\u2122" "installdir" "Racer" }')
            root = lib / "steamapps/common/Racer"
            (root / "Racer.exe").write_bytes(build_pe(b"\xc3" * 16))
            (root / "UnrealCEFSubProcess.exe").write_bytes(b"MZ" * 10)
            (root / "Racer/Binaries/Win64/Racer-Win64-Shipping.exe").write_bytes(build_pe(b"\xc3" * 4096))
            g = games.Game(id="racer", name="STAR WARS: Galactic Racer", library=str(lib))
            self.assertEqual(g.resolve(), [])
            self.assertEqual(g.steam_appid, 42)
            self.assertEqual(g.install_dir, "Racer")
            self.assertEqual(g.exe, "Racer/Binaries/Win64/Racer-Win64-Shipping.exe")
            self.assertEqual(g.process, "Racer-Win64-Shipping.exe")

    def test_missing_library(self):
        g = games.Game(id="x", name="Nothing", library="/nonexistent")
        self.assertEqual(g.resolve(), ["steam_appid", "install_dir"])


if __name__ == "__main__":
    unittest.main()
