import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portkit import config, games, kettle, session

from .test_pe import build_pe


def game(root, **kw):
    g = games.Game(id="g", name="Satisfactory", library=str(root), steam_appid=526870, install_dir="Sat",
                   exe="Engine/Binaries/Win64/FactoryGameSteam-Win64-Shipping.exe", launch="kettle", **kw)
    return g


class KettleLaunchTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.lib = Path(d.name)
        self.root = self.lib / "steamapps/common/Sat"
        (self.root / "Engine/Binaries/Win64").mkdir(parents=True)
        (self.root / "Engine/Binaries/Win64/FactoryGameSteam-Win64-Shipping.exe").write_bytes(build_pe(b"\xc3"))
        (self.root / "FactoryGameSteam.exe").write_bytes(build_pe(b"\xc3"))
        (self.lib / "steamapps/appmanifest_526870.acf").write_text(
            '"AppState" { "appid" "526870" "name" "Satisfactory" "installdir" "Sat" "buildid" "12345" }')

    def test_environment(self):
        g = game(self.lib)
        bridge = Path("/x/runtime/steam-api/bridge")
        env = kettle.environment(g, bridge, log=Path("/r/steam_api.log"),
                                 base={"WINEDLLPATH": "/other", "WINEDLLOVERRIDES": "dxgi=n;steam_api64=n"})
        self.assertEqual(env["WINEDLLPATH"], "/x/runtime/steam-api/bridge:/other")
        self.assertEqual(env["WINEDLLOVERRIDES"], "steam_api64=b;dxgi=n")   # ours wins, the game's others stay
        self.assertEqual(env["SteamAppId"], "526870")
        self.assertEqual(env["SteamGameId"], "526870")
        self.assertEqual(env["PORTKIT_STEAM_LOG"], "Z:\\r\\steam_api.log")
        self.assertEqual(env["PORTKIT_GAME_DIR"], "Z:" + str(self.root).replace("/", "\\"))
        self.assertEqual(env["PORTKIT_GAME_BUILDID"], "12345")
        self.assertEqual(env["PORTKIT_GAME_NAME"], "Satisfactory")

    def test_launcher_is_what_steam_starts(self):
        self.assertEqual(game(self.lib).launcher_path, self.root / "FactoryGameSteam.exe")

    def test_session_starts_the_game_without_windows_steam(self):
        g = game(self.lib, env={"DXVK_HUD": "1"})
        bridge = config.RUNTIME_DIR / "steam-api/bridge"
        with mock.patch.object(session.steam, "stop") as stop, \
             mock.patch.object(session.steam, "start") as steam_start, \
             mock.patch.object(session.kettle, "build_bridge", return_value=bridge), \
             mock.patch.object(session.wine, "spawn") as spawn:
            session.start(g, extra_env={"MTL_HUD_ENABLED": "1"}, log=Path("/r/wine.log"))
        stop.assert_called_once_with(g.prefix)        # Windows Steam is stopped, not started
        steam_start.assert_not_called()
        (prefix, args, debug, env, log), kw = spawn.call_args
        self.assertEqual(args[0], self.root / "FactoryGameSteam.exe")
        self.assertEqual(kw["cwd"], self.root)
        self.assertEqual(env["WINEDLLOVERRIDES"], "steam_api64=b")
        self.assertEqual(env["WINEDLLPATH"], str(bridge))
        self.assertEqual(env["PORTKIT_STEAM_LOG"], "Z:\\r\\steam_api.log")
        self.assertEqual((env["DXVK_HUD"], env["MTL_HUD_ENABLED"]), ("1", "1"))
        self.assertTrue(str(bridge).startswith(str(config.RUNTIME_DIR)))   # never the game folder

    def test_unknown_launch_mode(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "GAMES_DIR", Path(d)):
            Path(d, "x.toml").write_text('name = "X"\nlaunch = "steamless"\n')
            with self.assertRaises(games.Fail):
                games.load("x")

    def test_kettle_needs_an_app_id(self):
        g = games.Game(id="x", name="Nothing", library="/nonexistent", launch="kettle")
        self.assertIn("steam_appid", g.resolve())


if __name__ == "__main__":
    unittest.main()
