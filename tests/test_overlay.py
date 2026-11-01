import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portkit import config, games, overlay, session
from portkit.util import Fail

from .test_pe import build_pe

MODULE = "FactoryGameSteam-FactoryPreEarlyLoadingScreen-Win64-Shipping"


def snapshot(root):
    """Every path under root with its kind, size and modification time: what "untouched" means."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            s = p.lstat()
            out[str(p.relative_to(root))] = (stat.S_IFMT(s.st_mode), s.st_size, s.st_mtime_ns)
    return out


def set_writable(root, writable):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames + [""]:
            p = Path(dirpath, name)
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class Fixture(unittest.TestCase):
    """A small game folder, read-only while overlays are built from it, and a DLL built from recovered source."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name).resolve()   # /var is /private/var on macOS; the overlay resolves its paths
        self.lib = self.tmp / "lib"
        self.game = self.lib / "steamapps/common/Satisfactory"
        for folder in ("Engine/Binaries/Win64", "FactoryGame/Binaries/Win64", "FactoryGame/Content/Paks",
                       "CommunityResources"):
            (self.game / folder).mkdir(parents=True)
        (self.game / "FactoryGameSteam.exe").write_bytes(build_pe(b"\xc3"))
        (self.game / "Engine/Binaries/Win64/FactoryGameSteam-Win64-Shipping.exe").write_bytes(build_pe(b"\xc3"))
        (self.game / f"FactoryGame/Binaries/Win64/{MODULE}.dll").write_bytes(b"coffee stain's")
        (self.game / f"FactoryGame/Binaries/Win64/{MODULE}.pdb").write_bytes(b"coffee stain's pdb")
        (self.game / "FactoryGame/Binaries/Win64/FactoryGameSteam-FactoryGame-Win64-Shipping.dll").write_bytes(b"game")
        (self.game / "FactoryGame/Binaries/Win64/FactoryGameSteam-Win64-Shipping.modules").write_text("{}")
        (self.game / "FactoryGame/Content/Paks/FactoryGame-Windows.pak").write_bytes(b"pak")
        (self.lib / "steamapps/appmanifest_526870.acf").write_text(
            '"AppState" { "appid" "526870" "name" "Satisfactory" "installdir" "Satisfactory" "buildid" "1" }')
        self.built = self.tmp / "build" / f"{MODULE}.dll"
        self.built.parent.mkdir()
        self.built.write_bytes(b"ours")
        self.out = self.tmp / "overlay"
        set_writable(self.game, False)
        self.addCleanup(set_writable, self.game, True)

    def build(self, *dlls):
        before = snapshot(self.game)
        overlay.build(self.game, self.out, list(dlls))
        self.assertEqual(snapshot(self.game), before)   # the game folder is never written to


class OverlayTest(Fixture):
    def test_replaced_dll_sits_in_real_folders_and_everything_else_is_linked(self):
        self.build(self.built)
        win64 = self.out / "FactoryGame/Binaries/Win64"
        for real in (self.out, self.out / "FactoryGame", self.out / "FactoryGame/Binaries", win64):
            self.assertTrue(real.is_dir() and not real.is_symlink(), real)
        dll = win64 / f"{MODULE}.dll"
        self.assertFalse(dll.is_symlink())
        self.assertEqual(dll.read_bytes(), b"ours")
        for linked in ("Engine", "FactoryGameSteam.exe", "CommunityResources", "FactoryGame/Content",
                       "FactoryGame/Binaries/Win64/FactoryGameSteam-FactoryGame-Win64-Shipping.dll",
                       "FactoryGame/Binaries/Win64/FactoryGameSteam-Win64-Shipping.modules"):
            p = self.out / linked
            self.assertTrue(p.is_symlink(), linked)
            self.assertEqual(Path(os.readlink(p)), self.game / linked)
        self.assertEqual((self.out / "FactoryGame/Content/Paks/FactoryGame-Windows.pak").read_bytes(), b"pak")
        self.assertEqual((self.game / f"FactoryGame/Binaries/Win64/{MODULE}.dll").read_bytes(), b"coffee stain's")

    def test_a_pdb_next_to_the_built_dll_replaces_the_shipped_one(self):
        self.built.with_suffix(".pdb").write_bytes(b"our pdb")
        self.build(self.built)
        pdb = self.out / f"FactoryGame/Binaries/Win64/{MODULE}.pdb"
        self.assertFalse(pdb.is_symlink())
        self.assertEqual(pdb.read_bytes(), b"our pdb")

    def test_without_a_pdb_the_shipped_one_is_left_out(self):
        self.build(self.built)
        self.assertFalse(os.path.lexists(self.out / f"FactoryGame/Binaries/Win64/{MODULE}.pdb"))

    def test_no_replacement_links_the_top_level(self):
        self.build()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted([overlay.MARKER, "CommunityResources", "Engine", "FactoryGame", "FactoryGameSteam.exe"]))
        self.assertTrue((self.out / "FactoryGame").is_symlink())

    def test_rebuild_replaces_the_previous_overlay(self):
        self.build(self.built)
        self.build()
        self.assertTrue((self.out / "FactoryGame").is_symlink())
        self.assertEqual(overlay.read(self.out)["replaced"], {})

    def test_refuses_a_folder_that_is_not_an_overlay(self):
        self.out.mkdir()
        (self.out / "notes.txt").write_text("mine")
        with self.assertRaises(Fail):
            overlay.build(self.game, self.out, [])
        self.assertEqual((self.out / "notes.txt").read_text(), "mine")

    def test_refuses_to_build_inside_the_game_or_around_it(self):
        set_writable(self.game, True)   # so that a wrong build would be seen, not stopped by permissions
        before = snapshot(self.game)
        for out in (self.game / "overlay", self.game / "FactoryGame/Binaries", self.game, self.lib):
            with self.assertRaises(Fail, msg=out):
                overlay.build(self.game, out, [])
        self.assertEqual(snapshot(self.game), before)

    def test_refuses_to_build_inside_this_repository(self):
        with self.assertRaises(Fail):
            overlay.build(self.game, config.ROOT / "runtime" / "overlay-test", [])
        self.assertFalse((config.ROOT / "runtime" / "overlay-test").exists())

    def test_a_dll_the_game_does_not_ship_is_refused(self):
        other = self.tmp / "build" / "Unknown-Win64-Shipping.dll"
        other.write_bytes(b"?")
        with self.assertRaises(Fail):
            self.build(other)

    def test_an_ambiguous_name_is_refused(self):
        set_writable(self.game, True)
        (self.game / f"Engine/Binaries/Win64/{MODULE}.dll").write_bytes(b"twin")
        set_writable(self.game, False)
        with self.assertRaises(Fail):
            self.build(self.built)


class OverlayRunTest(Fixture):
    def satisfactory(self):
        return games.Game(id="satisfactory", name="Satisfactory", library=str(self.lib), steam_appid=526870,
                          install_dir="Satisfactory", exe="Engine/Binaries/Win64/FactoryGameSteam-Win64-Shipping.exe",
                          launch="kettle")

    def test_kettle_starts_the_game_from_the_overlay(self):
        self.build(self.built)
        g = self.satisfactory()
        overlay.use(g, self.out)
        bridge = config.RUNTIME_DIR / "steam-api/bridge"
        with mock.patch.object(session.steam, "stop"), \
             mock.patch.object(session.kettle, "build_bridge", return_value=bridge), \
             mock.patch.object(session.wine, "spawn") as spawn:
            session.start(g, log=Path("/r/wine.log"))
        (prefix, args, debug, env, log), kw = spawn.call_args
        self.assertEqual(args[0], self.out / "FactoryGameSteam.exe")
        self.assertEqual(kw["cwd"], self.out)
        self.assertEqual(env["PORTKIT_GAME_DIR"], "Z:" + str(self.out).replace("/", "\\"))  # where the game runs
        self.assertEqual(env["PORTKIT_GAME_BUILDID"], "1")                                  # still the install's

    def test_an_overlay_of_another_game_folder_is_refused(self):
        other = self.tmp / "other"
        (other / "Engine").mkdir(parents=True)
        overlay.build(other, self.out, [])
        with self.assertRaises(Fail):
            overlay.use(self.satisfactory(), self.out)

    def test_a_folder_that_is_not_an_overlay_is_refused(self):
        self.out.mkdir()
        with self.assertRaises(Fail):
            overlay.use(self.satisfactory(), self.out)

    def test_steam_launch_cannot_use_an_overlay(self):
        self.build()
        g = self.satisfactory()
        g.launch = "steam"
        with self.assertRaises(Fail):   # Steam starts the install itself
            overlay.use(g, self.out)


if __name__ == "__main__":
    unittest.main()
