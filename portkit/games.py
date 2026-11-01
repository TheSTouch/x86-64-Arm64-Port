"""Per-game settings in games/<id>.toml.

Fields left empty are filled from the Steam library when the game is there:
the app id and install folder from its appmanifest, the executable and the
process to watch from the files.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, steam
from .util import Fail

# Executables that ship next to games but are not the game.
NOT_THE_GAME = re.compile(
    r"crash|report|redist|vc_?redist|dxsetup|directx|setup|install|unins|"
    r"easyanticheat|start_protected|beservice|battleye|launcher|helper|uploader|"
    r"prereq|ue4prereq|ueprereq|dotnet|cefprocess|webhelper",
    re.I,
)


# How a game starts: "steam" through Windows Steam in the prefix (-applaunch); "kettle" its own
# executable with our steam_api64.dll, Steam reached through Kettle (portkit/kettle.py), no Windows
# Steam; "exe" its executable alone, for games without Steam.
LAUNCH_MODES = ("steam", "kettle", "exe")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class Game:
    id: str
    name: str
    library: str = ""
    steam_appid: int = 0
    install_dir: str = ""
    exe: str = ""
    process: str = ""
    launch: str = "steam"
    args: list = field(default_factory=list)
    runtime: str = config.DEFAULT_RUNTIME
    env: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def prefix(self):
        return self.runtime

    @property
    def root(self):
        if not (self.library and self.install_dir):
            return None
        return Path(self.library) / "steamapps/common" / self.install_dir

    @property
    def exe_path(self):
        return self.root / self.exe if self.root and self.exe else None

    @property
    def launcher_path(self):
        """What Steam starts: Unreal's launcher stub at the top (FactoryGameSteam.exe), which starts
        Binaries/Win64/<Name>-Win64-Shipping.exe with the project; else the executable itself.
        Started directly, the shipping exe looks for <Name>.uproject and quits."""
        exe = self.exe_path
        if exe and self.root and exe.name.lower().endswith("-win64-shipping.exe"):
            stub = self.root / (exe.name[:-len("-Win64-Shipping.exe")] + ".exe")
            if stub.exists():
                return stub
        return exe

    @property
    def available(self):
        return bool(self.exe_path and self.exe_path.exists())

    def resolve(self):
        """Fill empty fields from the Steam library; return what was missing."""
        missing = []
        if self.library and Path(self.library, "steamapps").is_dir():
            if not self.steam_appid or not self.install_dir:
                for appid, state in steam.manifests(self.library).items():
                    if _norm(self.name) in _norm(state.get("name", "")):
                        self.steam_appid = self.steam_appid or int(appid)
                        self.install_dir = self.install_dir or state.get("installdir", "")
                        break
        if self.root and self.root.is_dir() and not self.exe:
            if exe := guess_exe(self.root):
                self.exe = str(exe.relative_to(self.root))
        if self.exe and not self.process:
            # Unreal ships a small launcher stub at the top that starts
            # Binaries/Win64/<Name>-Win64-Shipping.exe: watch the real one.
            self.process = Path(self.exe).name
            if self.root and not self.exe.lower().endswith("-shipping.exe"):
                stem = Path(self.exe).stem
                shipping = sorted(self.root.glob(f"*/Binaries/Win64/{stem}-Win64-Shipping.exe"))
                if shipping:
                    self.process = shipping[0].name
        if not self.steam_appid and self.launch in ("steam", "kettle"):
            missing.append("steam_appid")
        if not self.root or not self.root.is_dir():
            missing.append("install_dir")
        elif not self.available:
            missing.append("exe")
        return missing


def guess_exe(root):
    """The likeliest game executable: a *-Shipping.exe, else the largest .exe."""
    root = Path(root)
    exes = [p for p in root.rglob("*.exe") if not NOT_THE_GAME.search(p.name)]
    shipping = [p for p in exes if p.name.lower().endswith("-shipping.exe")]
    if shipping:
        return max(shipping, key=lambda p: p.stat().st_size)
    top = [p for p in exes if p.parent == root]
    pool = top or exes
    return max(pool, key=lambda p: p.stat().st_size) if pool else None


def load(game_id):
    path = config.GAMES_DIR / f"{game_id}.toml"
    if not path.exists():
        known = ", ".join(all_ids()) or "none"
        raise Fail(f"no game {game_id!r} (known: {known})")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("id", game_id)
    data["env"] = {k: str(v) for k, v in data.get("env", {}).items()}
    if data.get("launch", "steam") not in LAUNCH_MODES:
        raise Fail(f"games/{game_id}.toml: launch = {data['launch']!r}, expected one of {', '.join(LAUNCH_MODES)}")
    game = Game(**data)
    game.resolve()
    return game


def all_ids():
    return sorted(p.stem for p in config.GAMES_DIR.glob("*.toml"))
