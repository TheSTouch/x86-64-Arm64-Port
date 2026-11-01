"""An overlay game folder: the install seen through symlinks, with some of its DLLs replaced.

Playable step 1 (docs/SATISFACTORY-RECOMP.md) runs the shipped game with modules built from
recovered source. The game's folder is never written to: the overlay has real folders only on the
way to a replaced file (FactoryGame/Binaries/Win64 for a FactoryGame module), and every other entry
is a symlink to the install. A replaced DLL is copied in, with the .pdb built next to it if there is
one; otherwise the shipped .pdb, which no longer matches, is left out. The overlay holds DLLs built
from recovered code, so it stays outside this repository (on SMART).
"""

import json
import os
import shutil
from pathlib import Path

from . import config
from .util import Fail

MARKER = ".portkit-overlay.json"


def build(game_root, out, dlls):
    """Build (or rebuild) the overlay of game_root in out, with each file of dlls replacing the game's
    file of the same name. Returns {path in the game: source}."""
    game_root, out = Path(game_root).resolve(), Path(out).resolve()
    if out.is_relative_to(game_root) or game_root.is_relative_to(out):
        raise Fail(f"{out} is inside the game folder or holds it; an overlay goes beside it")
    if config.inside_repository(out):
        raise Fail(f"{out} is inside this repository; an overlay holds game-derived DLLs and stays outside it")
    replaced = _locate(game_root, [Path(d).resolve() for d in dlls])
    if out.is_dir() and not out.is_symlink() and not any(out.iterdir()):
        out.rmdir()
    elif out.exists() or out.is_symlink():
        _remove(out)
    out.mkdir(parents=True)
    _link_children(game_root, out)
    for rel, src in replaced.items():
        folder = _materialise(game_root, out, rel.parent)
        _put(folder / rel.name, src)
        pdb = folder / Path(rel.name).with_suffix(".pdb")
        if src.with_suffix(".pdb").is_file():
            _put(pdb, src.with_suffix(".pdb"))
        elif pdb.is_symlink():
            pdb.unlink()
    marker = {"game": str(game_root), "replaced": {str(rel): str(src) for rel, src in replaced.items()}}
    (out / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return replaced


def read(out):
    """The marker of the overlay in out: {"game": install folder, "replaced": {path: source}}."""
    marker = Path(out) / MARKER
    if Path(out).is_symlink() or not marker.is_file():
        raise Fail(f"{out} is not an overlay (no {MARKER}): build it with ./port overlay")
    return json.loads(marker.read_text(encoding="utf-8"))


def use(game, out):
    """Make this run of game start from the overlay in out."""
    if game.launch == "steam":
        raise Fail("Steam starts the install itself, not an overlay: add --kettle (or set launch = \"exe\")")
    built_from = Path(read(out)["game"])
    if not game.root or built_from != game.root.resolve():
        raise Fail(f"{out} is an overlay of {built_from}, not of {game.root}")
    game.overlay = str(Path(out).resolve())
    return game


def _locate(game_root, dlls):
    """{path in the game: source} for each built file, by file name (Windows: any case)."""
    wanted = {d.name.lower(): d for d in dlls}
    for d in dlls:
        if not d.is_file():
            raise Fail(f"{d}: no such file")
    found = {name: [] for name in wanted}
    for folder, _, files in os.walk(game_root):
        for f in files:
            if f.lower() in found:
                found[f.lower()].append(Path(folder, f).relative_to(game_root))
    out = {}
    for name, paths in found.items():
        if not paths:
            raise Fail(f"{wanted[name].name}: the game ships no such file, so there is nothing to replace")
        if len(paths) > 1:
            raise Fail(f"{wanted[name].name} is in several places in the game: {', '.join(map(str, paths))}")
        out[paths[0]] = wanted[name]
    return out


def _link_children(source, folder):
    for entry in sorted(os.listdir(source)):
        os.symlink(source / entry, folder / entry)


def _materialise(game_root, out, rel):
    """Turn each linked folder from out down to out/rel into a real folder of links; return out/rel."""
    folder = out
    for part in rel.parts:
        folder = folder / part
        if folder.is_symlink():
            folder.unlink()
            folder.mkdir()
            _link_children(game_root / folder.relative_to(out), folder)
        elif not folder.is_dir():
            raise Fail(f"{folder}: expected a folder of the game")
    return folder


def _put(target, src):
    """Copy src to target, where the overlay had a link to the game's file. A link is removed, never followed."""
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        raise Fail(f"{target} is not a link to the game; refusing to overwrite it")
    shutil.copyfile(src, target)


def _remove(out):
    """Delete a previous overlay: its links (not what they point to), copies and folders."""
    read(out)

    def clear(folder):
        for entry in os.scandir(folder):
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.path)
            else:
                clear(entry.path)
                os.rmdir(entry.path)

    clear(out)
    os.rmdir(out)
