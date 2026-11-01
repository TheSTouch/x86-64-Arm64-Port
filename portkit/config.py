"""Paths and pinned downloads.

Everything heavy (the toolkit, Wine prefixes, reports) lives under PORTKIT_HOME,
which defaults to the repository itself. All of it is ignored by git.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("PORTKIT_HOME", ROOT))

RUNTIME_DIR = HOME / "runtime"
DOWNLOADS = RUNTIME_DIR / "downloads"
PREFIXES = HOME / "prefixes"
REPORTS = HOME / "reports"
GAMES_DIR = ROOT / "games"

# Wine runtimes. Each gets its own prefix, prefixes/<name>.
RUNTIMES = {
    # Gcenx's binary distribution of Apple's Game Porting Toolkit: Wine 7.7 +
    # D3DMetal (DirectX 11/12). sha256 as in Gcenx's Homebrew cask. Today's
    # Windows Steam client crashes on this Wine (bcrypt pseudo-handles).
    "gptk": {
        "version": "3.0-3",
        "about": "Apple's Game Porting Toolkit (Wine 7.7 + D3DMetal)",
        "url": "https://github.com/Gcenx/game-porting-toolkit/releases/download/"
        "Game-Porting-Toolkit-3.0-3/game-porting-toolkit-3.0-3.tar.xz",
        "sha256": "d377683937340f914823dbb2e1252b329cbf834ff58907d0293db8cebf0e392e",
        "app": "Game Porting Toolkit.app",
        "wine": "wine64",
    },
    # Gcenx's build of Wine Staging. sha256 pinned on first download (2026-11-01).
    # Stock Wine: its Mac driver does not export the Metal view functions DXMT
    # needs, so DirectX 11 goes through wined3d (OpenGL) until that is patched.
    "wine11": {
        "version": "11.21",
        "about": "Wine Staging 11.21",
        "url": "https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.21/wine-staging-11.21-osx64.tar.xz",
        "sha256": "d7bcd5abf4df27aae397a7898aef074fea4a2e0b51e0a55aa9659517bbf9d452",
        "app": "Wine Staging.app",
        "wine": "wine",
        # Steam's browser draws through D3D11 -> wined3d -> OpenGL ES 2, which is
        # too old for it (it wants ES 3): software drawing from the start.
        # -noverifyfiles/-norepairfiles: Steam would otherwise restore its own
        # web helper over the wrapper below at every start.
        "steam_args": ["-cef-disable-gpu", "-cef-disable-gpu-compositing", "-noverifyfiles", "-norepairfiles"],
        # wined3d's Vulkan renderer (on the bundled MoltenVK) instead of OpenGL:
        # Steam's window compositor draws through D3D11 and is black on OpenGL.
        "env": {"WINE_D3D_CONFIG": "renderer=vulkan"},
        # Steam's browser windows stay black unless it runs as a single process:
        # wrapper/steamwebhelper.c takes the helper's place.
        "webhelper_wrapper": True,
    },
}
DEFAULT_RUNTIME = "wine11"

# DXMT (DirectX 10/11 -> Metal), builtin-DLL build. sha256 pinned on first download.
DXMT = {
    "version": "0.80",
    "url": "https://github.com/3Shain/dxmt/releases/download/v0.80/dxmt-v0.80-builtin.tar.gz",
    "sha256": "8f260e36b5739e68f3bad613381441385c4dc7b85b78ba8de653d5a6a264529d",
}

STEAM_SETUP_URL = "https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe"
