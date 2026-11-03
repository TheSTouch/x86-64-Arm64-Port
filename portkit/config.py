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


def inside_repository(path):
    """Whether path is in this repository, where nothing derived from a game may be written."""
    return Path(path).resolve().is_relative_to(ROOT)

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
        # The bundled MoltenVK 1.4.0 refuses images with uncompressed views of
        # compressed formats (VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT),
        # which Unreal 5 creates for runtime virtual textures; 1.4.1 and later
        # make those views by aliasing heap memory on Apple GPUs.
        "moltenvk": "1.4.2",
    },
}
DEFAULT_RUNTIME = "wine11"

# MoltenVK releases a runtime can use instead of the one it bundles ("moltenvk"
# above). Khronos's own macOS build, universal (Wine's Unix side is x86_64).
MOLTENVK = {
    "1.4.2": {
        "url": "https://github.com/KhronosGroup/MoltenVK/releases/download/v1.4.2/MoltenVK-macos.tar",
        "sha256": "f95765a6229cb7b915990a2890ce12ebe36a730b021545d3d52ae69ce4c4024e",
        "member": "MoltenVK/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib",
    },
}

# DXMT (DirectX 10/11 -> Metal), builtin-DLL build. sha256 pinned on first download.
DXMT = {
    "version": "0.80",
    "url": "https://github.com/3Shain/dxmt/releases/download/v0.80/dxmt-v0.80-builtin.tar.gz",
    "sha256": "8f260e36b5739e68f3bad613381441385c4dc7b85b78ba8de653d5a6a264529d",
}

STEAM_SETUP_URL = "https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe"


def _setting(name, default):
    """A path setting: PORTKIT_<NAME> in the environment, else today's value on the owner's machines."""
    return os.environ.get(f"PORTKIT_{name}", str(default))


# The owner's large disk: toolchains, mirrors and builds, never in this repository.
SMART = Path(_setting("SMART", "/Volumes/SMART/TheSTouch"))

# MSVC on the Mac (portkit/msvc.py): the game's compiler and SDK, copied from the owner's Windows PC (the
# same licence and bits), run under the default runtime's Wine with a prefix of its own.
MSVC_VERSION = _setting("MSVC_VERSION", "14.38.33130")
WINSDK_VERSION = _setting("WINSDK_VERSION", "10.0.22621.0")
MSVC_DIR = Path(_setting("MSVC_DIR", SMART / "msvc"))            # MSVC/<version>/, sdk/Include, sdk/Lib
MSVC_PREFIX = Path(_setting("MSVC_PREFIX", MSVC_DIR / "prefix"))
# What exists only on the PC (UHT's generated headers, UBT's response files and definitions), each file at
# its PC path under pc/<drive>/; work/ holds the rewritten response files of the compiles.
MSVC_MIRROR = Path(_setting("MSVC_MIRROR", SMART / "msvc-mirror"))
MSVC_BUILDS = Path(_setting("MSVC_BUILDS", SMART / "satisfactory-builds" / "mac"))
# The recovered-code repository: tools read it, never write into it (`permute` works on a staged copy).
RECOVERED_REPO = Path(_setting("RECOVERED_REPO", SMART / "satisfactory-recomp"))
# The engine on the Mac: a clone of the tag the PC's engine was installed from.
ENGINE_DIR = Path(_setting("ENGINE_DIR", SMART / "custom-repos" / "UnrealEngine-CSS"))
# What pdb.py keeps of a large PDB (its functions and public symbols), to read it in a fraction of the time.
PDB_CACHE = Path(_setting("PDB_CACHE", SMART / "pdb-cache"))
# The shipped modules matched against, each with its PDB.
GAME_BINARIES = Path(_setting("GAME_BINARIES", "/Volumes/SMART/SteamLibraryWindows/steamapps/common/Satisfactory/"
                                               "FactoryGame/Binaries/Win64"))
# The Windows PC: its ssh host, and where things are there.
PC_HOST = _setting("PC_HOST", "windows")
PC_ENGINE = _setting("PC_ENGINE", "D:/UnrealEngine-CSS")
PC_PROJECT = _setting("PC_PROJECT", "D:/SatisfactoryRecomp/Project")
PC_MSVC = _setting("PC_MSVC", f"D:/Programs/Visual Studio/VC/Tools/MSVC/{MSVC_VERSION}")
PC_WINSDK = _setting("PC_WINSDK", "C:/Program Files (x86)/Windows Kits/10")
PC_TARGET = _setting("PC_TARGET", "FactoryGameSteam")
