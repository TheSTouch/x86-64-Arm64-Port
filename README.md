# x86-64 → Arm64 Port

Play the Windows games you own on an Apple Silicon Mac, and later on iPad and iPhone.

There are two routes, and each game uses the best one it has:

| Route | What it is | Where | Status |
|---|---|---|---|
| **Porting Kit** | Runs the Windows game as it is, translated at run time: Wine for Windows, a graphics translator to Metal, Rosetta 2 for x86-64 → arm64. Also the benchmark and the observation post for the recompilation. | Mac | Steam runs; first game reaches its main menu |
| **Native recompilation** | A per-game project: the game's x86-64 code translated ahead of time into a native arm64 binary, with its own stand-ins for Windows and DirectX. The only route to iOS, which forbids the run-time translation Rosetta does. | Mac, iOS | Planned per game, after the Porting Kit benchmark |

> **Your games, your devices.** This repository holds tools only. Game files, recompiled
> binaries, prefixes and reports stay on your own disk (`runtime/`, `prefixes/` and `reports/`
> are ignored by git) and are never shared.

## Games

| Game | Porting Kit | Native recompilation |
|---|---|---|
| Satisfactory (Steam, Unreal Engine 5.6) | `wine11`, Vulkan on MoltenVK 1.4.2: reaches the main menu; the menu's 3D scene does not render correctly yet, and some runs stop on a GPU timeout. Details: [docs/SATISFACTORY.md](docs/SATISFACTORY.md) | Go: no protection, no anti-cheat, every PDB shipped. Plan: Coffee Stain's engine built natively, the game's own code recovered as C++; first milestone, the early loading screen ([docs/SATISFACTORY-RECOMP.md](docs/SATISFACTORY-RECOMP.md)) |
| STAR WARS: Galactic Racer (Steam, 2026) | Waiting for a Steam copy of the game | To plan after the benchmark ([docs/RECOMP.md](docs/RECOMP.md)) |

Each game has a file in [games/](games/). Fields left empty are filled from the Steam library once
the game is installed there.

## Requirements

- An Apple Silicon Mac running macOS 14 or later, with Rosetta 2. 16 GB of memory or more: Wine,
  Rosetta and the game all share it.
- Xcode, for the Instruments traces the benchmark records.
- Python 3.11 or later (the tool uses the standard library only).
- Tens of GB free where the repository lives, for the toolkit, the Wine prefix and shader caches.
  Set `PORTKIT_HOME` to keep them somewhere else.
- The game in a Windows Steam library folder (here `/Volumes/SMART/SteamLibraryWindows`, copied
  from the Windows PC).

## Runtimes

Each runtime is a Wine build with its own prefix (`prefixes/<runtime>`); a game file names the one
it uses. Downloads are pinned by checksum in [portkit/config.py](portkit/config.py).

| Runtime | What | Status |
|---|---|---|
| `wine11` (default) | [Gcenx's Wine Staging 11.21](https://github.com/Gcenx/macOS_Wine_builds), Direct3D through wined3d on Vulkan, with Khronos's [MoltenVK 1.4.2](https://github.com/KhronosGroup/MoltenVK/releases/tag/v1.4.2) in place of the bundled 1.4.0 | Today's Windows Steam runs, with the fixes below. Unreal 5 games run on Vulkan. DXMT cannot load yet: this Wine's Mac driver does not export the Metal view functions DXMT needs |
| `gptk` | [Gcenx's build](https://github.com/Gcenx/game-porting-toolkit) of Apple's Game Porting Toolkit 3.0: Wine 7.7 + D3DMetal (DirectX 11/12) | Today's Windows Steam crashes on it (Wine 7.7's bcrypt does not know `BCRYPT_RNG_ALG_HANDLE`), so Steam games cannot start. D3DMetal is Apple's, under [Apple's license](https://developer.apple.com/games) |

Windows Steam on `wine11` needs three things, all applied by `./port steam start`:

- Direct3D on Vulkan (`WINE_D3D_CONFIG=renderer=vulkan`) instead of OpenGL;
- [wrapper/steamwebhelper.c](wrapper/steamwebhelper.c) in front of Steam's web helper, which runs
  it as one process with the GPU off; otherwise every Steam window stays black. It is
  cross-compiled with Zig (installed from pip into `runtime/tools`), and put back after Steam
  updates replace it. The fix comes from
  [play-windows-steam-on-mac](https://github.com/ramiabih/play-windows-steam-on-mac);
- `-noverifyfiles -norepairfiles`, so Steam does not restore its own helper over the wrapper.

`./port setup` lays the MoltenVK release a runtime names (`"moltenvk"` in
[portkit/config.py](portkit/config.py)) over the one Wine bundles, and keeps the bundled library as
`libMoltenVK.dylib.bundled`; re-running it on an installed runtime does only that. 1.4.1 and later
support the uncompressed views of compressed textures that Unreal 5's runtime virtual textures
need. Khronos has since deprecated MoltenVK in favour of KosmicKrisp on Apple Silicon; Wine's Unix
side runs as x86-64 under Rosetta, so a driver for it has to be built for x86-64.

Wine's windows show up as "wine" in the Dock and do not come to the front by themselves.

## Games without Windows Steam: the Kettle launch mode

A Steam game can also start under Wine without Windows Steam: it then loads our own
`steam_api64.dll`, which reaches Steam through [Kettle](https://github.com/TheSTouch/kettle) (signed
in as you, with Steam signing the game's ownership ticket). It is not a Steam emulator: without a
sign-in or a license the game is told there is no Steam.

```sh
./port steam signin                  # once: a QR code in the terminal (or ./port steam signin <account>)
./port steam status                  # the sign-in Kettle holds
./port steam check satisfactory      # sign-in and license through the DLL, without the game
./port run satisfactory --kettle     # the game, no Windows Steam (or launch = "kettle" in its file)
```

Wine loads the DLL as a builtin in place of the game's own; the game's folder is never changed.
Cloud saves, achievements, stats and networking answer "nothing" for now.
[steam_api/README.md](steam_api/README.md) describes the bridge, and
[steam_api/satisfactory-interfaces.md](steam_api/satisfactory-interfaces.md) lists what
Satisfactory asks Steam for (`./port steam inventory <game>` records it for any game).

## Getting started

```sh
./port doctor            # what this Mac has and what is missing
./port setup             # Wine 11 + its prefix + Windows Steam (--runtime gptk for the toolkit)
./port steam start       # sign in to Steam once
./port steam stop        # asks Steam to quit cleanly (keeps the sign-in)
./port steam library /Volumes/SMART/SteamLibraryWindows
./port games             # what the tool knows about each game
./port run satisfactory --hud
```

Every run through Steam restarts the prefix's Steam, because the game inherits Steam's environment
and each run sets its own variables. `--keep-steam` skips the restart. `--arg` replaces the game
file's arguments for one run. Each run's Wine, Steam and game output goes to
`reports/<game>/<time>-run/wine.log`.

Unreal's shipping builds ignore `-dpcvars=`, but apply `-ini:Engine:[SystemSettings]:<cvar>=<value>`
on the command line, so a game file's `args` can carry console variables (Satisfactory's does). To
see what a game asks of Vulkan, [layers/imagelog](layers/imagelog) is a Vulkan layer that logs
refused images and dumps failing pipelines with a tool to replay them
([docs/SATISFACTORY.md](docs/SATISFACTORY.md) shows it in use).

`ROSETTA_ADVERTISE_AVX=1` is set for every game, because most recent games need AVX2 and Rosetta
hides it otherwise. A game's `[env]` table and `--env KEY=VALUE` add or override variables.

## Commands

| Command | What it does |
|---|---|
| `./port doctor` | Checks the Mac, each runtime, its prefix and Steam, and each game |
| `./port setup` | Installs a runtime, creates its prefix, installs Windows Steam |
| `./port steam start\|stop\|install\|library <folder>` | Manages Windows Steam in the prefix |
| `./port steam signin [account]\|status` | Signs in to Steam through Kettle, for the Kettle launch mode, and shows the sign-in |
| `./port steam check\|inventory <game>` | Checks a game's sign-in and license through our `steam_api64.dll`; runs a game with a logging `steam_api64.dll` and lists what it asks Steam for |
| `./port wine -- <program>` | Runs a program in the prefix (`winecfg`, `regedit`, an installer) |
| `./port games [id]` | Shows each game's resolved settings and whether it is ready |
| `./port inspect <game\|folder\|file>` | Static report: engine and version, graphics API, middleware, anti-cheat, code protection in every code file, for Unreal builds the modules by origin and the PDBs shipped, and what each route faces. Reads files only |
| `./port decomp <dll>` | A module's game functions from its PDB (`--disasm NAME`: one function's disassembly, naming imports, functions and strings; `--out DIR`: a work folder per module, refused inside this repository). Reads files only |
| `./port run <game>` | Plays the game under the Porting Kit (`--kettle`: without Windows Steam) |
| `./port bench <game>` | Benchmarks a scene (`--attach` measures the game already running) |
| `./port trace <game>` | Watches a run for the recompilation plan |

## Benchmarking

```sh
./port bench star-wars-galactic-racer --label 1080p-medium --note "Tatooine track, lap 1"
```

The tool starts the game and waits for the game process. Shader compilation on the first start can
take minutes. You then get the warm-up time (`--warmup`, 45 s) to reach the scene. After that it
records for `--duration` seconds (120 s):

- the game's memory and CPU, the Mac's swap and free memory, once a second (`samples.csv`);
- an Instruments trace with the Game Performance template (`run.trace`), for frame rate, frame
  times and GPU time;
- the Metal HUD on screen (turn it off with `--no-hud`).

Each run writes `reports/<game>/<time>-bench-<label>/` with `report.json` and `summary.md`. The
report includes the machine, the toolkit and D3DMetal versions, and every variable that was set.
Benchmark the same scene with the same settings so runs compare. The Porting Kit numbers are the
baseline that a native build has to beat.

## Watching a run (for the recompilation)

```sh
./port trace star-wars-galactic-racer                  # DLLs loaded, failing/unimplemented calls
./port trace star-wars-galactic-racer --relay          # + every Windows API call and its call site (slow)
./port trace star-wars-galactic-racer --sample 10      # + call-stack samples after 60 s
./port trace star-wars-galactic-racer --dump-metal     # + Metal libraries/pipelines (experimental)
```

Wine is the part of the Porting Kit that can be watched: it is open source and logs at the boundary
between the game and Windows. A trace lists:

- the Windows DLLs Wine had to stand in for: what a native port must provide itself;
- the game's own DLLs: what must be recompiled or replaced;
- the Windows calls Wine reports as unimplemented or failing, grouped and counted.

With `--relay`, the trace also lists every API function called, how often, and the address in the
game's code it was called from. That is the exact API surface a native or iOS runtime must cover,
and real call sites to seed the recompiler's code search. Results go to `reports/<game>/<time>-trace/`
(`summary.md`, `modules.json`, `d3dm.log`, `wine.log`).

Rosetta's own arm64 output is not harvested: it carries no more information than the x86-64 code
it comes from, it depends on Rosetta's runtime, and its format is undocumented.
[docs/RECOMP.md](docs/RECOMP.md) says where the recompilation gets its run-time ground truth
instead.

## Layout

- `port`: the entry point.
- `portkit/`: the tool.
  - `runtime.py`: the Wine runtimes.
  - `wine.py`: prefixes, environment and processes.
  - `steam.py`: Windows Steam and its libraries.
  - `games.py`: game files and how they are resolved.
  - `pe.py`: a read-only parser for Windows executables.
  - `pdb.py`: a read-only PDB reader: its signature, functions (source file, lines, locals) and public symbols.
  - `decomp.py`: work material for recovering a module's source: its game functions and their annotated disassembly.
  - `inspect.py`: the static report.
  - `session.py`, `bench.py`, `trace.py`: running a game, benchmarking it and watching it.
  - `kettle.py`: builds the Kettle bridge and runs games through it.
- `games/`: one TOML file per game.
- `wrapper/`: the Steam web helper wrapper (Windows C, built with Zig).
- `steam_api/`: our `steam_api64.dll` and its Mac side, on Kettle (`vendor/kettle`, a submodule).
- `layers/imagelog/`: a Vulkan layer and tools for what a game asks of the driver.
- `docs/RECOMP.md`: how the native recompilation is planned.
- `docs/SATISFACTORY.md`: Satisfactory on the Porting Kit, what stopped it and what fixed it.
- `docs/SATISFACTORY-RECOMP.md`: Satisfactory's recompilation plan: go/no-go, what makes it smaller, the decisions, the first milestone.
- `tests/`: `python3 -m unittest discover -s tests -t .`
- `runtime/`, `prefixes/`, `reports/`: local only, ignored by git.

## License

[PolyForm Noncommercial 1.0.0](LICENSE), as [Mac To iOS Recomp](https://github.com/TheSTouch/Mac-To-iOS-Recomp):
free to use, change and share for any noncommercial purpose. The runtimes and tools it downloads
(Wine, Apple's Game Porting Toolkit, MoltenVK, DXMT, Zig, Steam, Kettle) keep their own licenses.

## Disclaimer

For games you bought, on your own devices. Decompiling a program to make it run on another platform
is generally allowed for interoperability, but laws differ by country. This project does not remove
or bypass copy protection or anti-cheat: a game that depends on either either works through its own
mechanisms or is not supported.
