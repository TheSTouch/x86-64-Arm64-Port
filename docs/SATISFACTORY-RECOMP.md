# Satisfactory: the native recompilation plan

This plan follows [RECOMP.md](RECOMP.md): the go/no-go, what makes the work smaller, a recommendation
for each of the six decisions, and a first milestone. It is based on the static inspection of build
CL 513885 (`./port inspect satisfactory`, 2026-11-01), on the game's PDBs and on the Porting Kit runs in
[SATISFACTORY.md](SATISFACTORY.md). Steps that need the game running come at the end, under "To run
later".

Nothing taken from the game is in this repository. The reports and PDB measurements stay in `reports/`
(ignored by git); recovered code stays on the owner's own disks (see "Where recovered code lives").


## Verdict: go

| Condition | Measured | Result |
|---|---|---|
| Code protection | All 509 `.exe`/`.dll` files sampled. The highest code-section entropy is 6.95 (`PlatformCryptoContext`); encrypted code sits near 8. No SteamStub `.bind` section. The cooked content (IoStore) is compressed and signed, but not encrypted, so reading it needs no key | none |
| Anti-cheat | No Easy Anti-Cheat or BattlEye files. The game's modules (`FactoryGame`, `OnlineIntegration`) never mention anti-cheat; only the EOS SDK carries its generic interface. Single-player reaches the main menu under the Porting Kit with no anti-cheat process | none |
| Size | 276 MB of x86-64 code in 509 files, 231 MB of it loaded at the main menu (444 modules). That is a multi-year job if all of it is translated. The game's own code is 20.6 MB, and the hand-written part of the main game module is 6.9 MB | go, if the plan works on the game's own code and rebuilds the engine from source |

## What makes the work smaller

### A modular build with every PDB

Satisfactory is a modular Unreal build: one DLL per module, named `FactoryGameSteam-<Module>-Win64-Shipping.dll`.
The main executable is a 0.1 MB stub. `inspect` groups the files by where they come from:

| Group | Files | Code | PDBs |
|---|---:|---:|---:|
| Engine modules (`Engine/Binaries`) | 182 | 118.3 MB | 182 |
| Engine plugins (`Engine/Plugins`, Audiokinetic's Wwise integration included) | 201 | 72.9 MB | 201 |
| Project modules (`FactoryGame/Binaries`) | 4 | 17.3 MB | 4 |
| Project plugins (`FactoryGame/Plugins`) | 55 | 3.3 MB | 55 |
| Third-party binaries | 62 | 44.9 MB | 0 |
| Tools and redistributables | 5 | 19.3 MB | 1 |

**Every Unreal-built module ships with its PDB**: 443 files (18.3 GB), each matching its binary's GUID
and age. They are full private PDBs. FactoryGame's has 1.75 million type records (112 MB), per-object
section contributions, line tables, and, for every function, its named locals with their registers or
stack slots and its inlined call sites. The compiler was MSVC 14.38.33130, with Windows SDK 10.0.22621.

### Coffee Stain's code, vendor code and engine code

| Part | Files | Code | What it is |
|---|---:|---:|---|
| `FactoryGame` | 1 | 14.9 MB | The game |
| `FactoryDedicatedClient` | 1 | 2.3 MB | Server management in the client; 89 % of it is statically linked OpenSSL and nghttp2 |
| `FactoryPreEarlyLoadingScreen`, `FactoryMessagingTypes` | 2 | 0.06 MB | The early loading screen; message types |
| Coffee Stain's plugins | 13 | 1.7 MB | `OnlineIntegration` and `OnlineServicesSteamLimited` (1.2 MB), `ReliableMessaging`, `AbstractInstance`, `SignificanceISPC`, `GameplayEvents`, `RainRendering`, `SteamDeckConfig` |
| Vendor plugins in the project | 42 | 1.6 MB | Unreal glue for DLSS, FSR, XeSS, Streamline and Sentry |
| Vendor SDK binaries | 17 | 12.2 MB | `libxess*`, `nvngx_*`, `sl.*`, FidelityFX for DirectX 12 |
| Engine modules and plugins | 383 | 191.2 MB | Coffee Stain's Unreal 5.6.1, Wwise integration included |
| Other third-party binaries | 45 | 32.7 MB | EOS SDK (14.2 MB), DirectX 12 Agility SDK, Aftermath, MsQuic, dbghelp, Wwise's plugin DLLs, Steamworks 1.57, Vorbis and Ogg, XAudio2 |

On a Mac, the vendor upscalers and their SDKs are dropped: they need NVIDIA GPUs, DirectX 12 or Intel's
libraries, and XeSS already fails under the Porting Kit. Sentry is crash reporting and can be dropped
too. **What has to be recovered is the 19 MB in Coffee Stain's modules and plugins.**

### Inside FactoryGame.dll

The PDB's line tables attribute each byte of code to the source file it was compiled from:

| Source | Code | Share | Files |
|---|---:|---:|---:|
| Game source (`FactoryGame/Source`) | 6.88 MB | 49.5 % | 1,805 |
| Engine headers and templates inlined (`TSet`, `UClass`, delegates…) | 3.15 MB | 22.7 % | 363 |
| UHT-generated reflection code (`*.gen.cpp`) | 2.27 MB | 16.3 % | 932 |
| Statically linked libraries (OpenSSL 1.1.1t, the Wwise 2025.1.6 SDK) | 1.60 MB | 11.5 % | 452 |

Only the first row is hand-written game code. UHT regenerates the generated code from the headers; the
inlined templates come with the engine source; OpenSSL and Wwise come from their vendors. The module
exports 50,796 symbols and imports 6,244 functions from 60 engine modules. Its direct Windows imports
(Kernel32, HID, SetupAPI, WinMM, Crypt32…) come mostly from the static libraries.

### Headers and the engine fork

- `CommunityResources/Headers.zip` ships with the game for modders: 1,144 files, including FactoryGame's
  978 public headers, the headers of the dedicated server and client modules and of Coffee Stain's plugins,
  and the `Build.cs` and `Target.cs` files. Next to it are `FactoryGame.usmap` (property layouts of
  the cooked assets), `CustomVersions.json` and the dedicated server API documentation.
- **The engine is not stock Unreal.** BuildSettings reads `5.6.1-++FactoryGame+rel-main-anniversary-2026-CL-513885`:
  Coffee Stain's own branch of 5.6.1. The Satisfactory Modding community gives modders that engine as source
  (the `5.6.1-CSS` branch of `satisfactorymodding/UnrealEngine`) and as prebuilt Windows and Linux editors, to
  anyone whose GitHub account is linked to Epic
  ([required software](https://docs.ficsit.app/satisfactory-modding/latest/Development/BeginnersGuide/dependencies.html),
  [Linux setup](https://docs.ficsit.app/satisfactory-modding/latest/Development/Linux/LinuxSetup.html)).
  C++ mods built with it load into the shipped game, so its headers match the shipped engine wherever mods
  touch it. Whether its tag is exactly CL 513885, and how far it differs from Epic's 5.6.1, are still
  to check.
- The modding starter project (`SatisfactoryUnrealProject`) builds FactoryGame from these headers with stub
  function bodies. It shows the project compiles with only the headers.

## Can stock Unreal be rebuilt natively, with only the game modules recompiled?

**Not by mixing binaries, and not with stock Unreal.** It works if Coffee Stain's engine is built from
source, and the game modules are rebuilt from C++ recovered with the PDBs.

- **The engine has to be Coffee Stain's fork**, not Epic's 5.6.1: the game modules are compiled against its
  headers.
- **Translated game code cannot call a natively compiled engine.** The game modules use the MSVC x64 ABI:
  Microsoft's calling convention, MSVC's vtable order (one deleting destructor slot instead of the Itanium
  ABI's two), its member-function pointers and its layout rules. A clang build for macOS or iOS uses arm64's
  calling convention and the Itanium C++ ABI. The boundary is wide: 6,244 imported engine functions,
  every engine virtual function the game overrides or calls, and 3.15 MB of engine templates already inlined
  into the game, with MSVC layouts baked in as constant offsets. A binary translator sees
  `call [rax+0x1F8]` and `mov rax, [rcx+0x1A8]`, not the class and field behind them. Mapping each one
  to the native layout needs the type of every pointer at every instruction, which is a decompiler's job.
- **Recovering the source is tractable here.** The PDBs give every function's name, signature,
  locals, inlined calls, source file and lines; the headers give every class. The result is ordinary C++
  that clang compiles for arm64 against the native engine, with no translated code and no ABI bridge.
- **The renderer stays Vulkan.** The cooked content (`FactoryGame-Windows`) holds shaders for the Windows
  renderers only; the Porting Kit runs load its Vulkan SM5 shaders on MoltenVK. Unreal's Metal renderer
  would need Metal shaders cooked from the uncooked project, which is not available. A native engine
  therefore uses its Vulkan renderer on a native MoltenVK, with what the Porting Kit phase learnt (MoltenVK
  1.4.1 or later, `r.Shadow.CacheWholeSceneShadows=0`, XeSS off).

**The alternative**, translating the whole process (231 MB of engine, game and third-party code) and
keeping the MSVC ABI throughout, avoids recovering source but costs more:

- twelve times as much code to translate;
- a Windows layer for everything the Windows engine calls, standing in for the 83 DLLs Wine provides today;
- x86 flags and memory ordering emulated everywhere;
- on the Mac, no clear gain over Rosetta.

It stays the fallback if the engine's source cannot be had.

## Decisions

1. **Code discovery.** Use the PDBs. For the game modules, each function's start, size, source file and
   lines come from the PDB, and `.pdata` cross-checks the list (about 84,000 entries in FactoryGame). Work
   is planned per source file, 1,805 for FactoryGame. Unresolved indirect jumps do not arise, since nothing
   is translated as binary. Engine modules are not discovered: they are built from source.
2. **Translation.** Recover C++ for the hand-written code, and **verify it by matching**: compile each
   recovered function with MSVC 14.38.33130 against Coffee Stain's engine for Win64, and compare it with
   the shipped function, before compiling the same source with clang for arm64. UHT regenerates the
   reflection code. OpenSSL is built from source. The Wwise SDK 2025.1.6 for Mac and iOS comes from
   Audiokinetic (a licence to check). `SignificanceISPC`'s kernels are recovered and recompiled with ISPC for
   NEON. Since nothing is lifted, x86 flags, memory ordering and SSE/AVX to NEON do not arise. Until a
   function is recovered, it stays a stub, as in the modding starter project.
3. **Windows layer.** None for the game: the engine's own Mac and iOS platform layers replace the 83 Wine
   builtins. The relay trace (below) will list the Windows calls made from the game's modules rather than
   the engine; the static imports say they come mostly from OpenSSL and Wwise, which have Mac builds.
   Steam goes through Steamworks for Mac or the Kettle bridge in [steam_api/](../steam_api), and Epic
   Online Services through the EOS SDK for Mac and iOS.
4. **Graphics.** Unreal's own Vulkan renderer on a native arm64 MoltenVK (iOS has MoltenVK too), loading
   the shipped Vulkan SM5 shaders. No DirectX translation and no shader conversion. Still to check: how
   much Unreal 5.6 supports VulkanRHI on Mac, and the BC texture formats on iOS (Apple GPUs on macOS
   support them).
5. **Engine knowledge.** Coffee Stain's fork itself, with Epic's Mac and iOS targets. The modding docs
   name a clang 18 cross-compile toolchain for Linux, so the fork already builds with clang. The
   Porting Kit phase's settings and MoltenVK findings carry over unchanged, because the renderer is the same.
6. **First milestone**: below, as step 3 of the playable steps; steps 1 and 2 come first, in the shipped game.

## Playable steps

Each step is something the owner runs and sees, so the recovery shows progress long before the native
build. The first ones run in the shipped game under the Porting Kit, swapping Coffee Stain's code for
the recovered code a piece at a time, the way OpenRCT2 replaced RollerCoaster Tycoon's functions one by
one and the way SML already hooks FactoryGame's functions. Nothing in the game's folder changes: the
game runs from an overlay folder on SMART (its own `Binaries`, everything else linked to the install),
started with the Kettle launch mode:

```sh
./port overlay satisfactory /Volumes/SMART/TheSTouch/satisfactory-overlay \
    --dll <build>/FactoryGameSteam-FactoryPreEarlyLoadingScreen-Win64-Shipping.dll
./port run satisfactory --overlay /Volumes/SMART/TheSTouch/satisfactory-overlay --kettle
```

`FactoryGame/`, `FactoryGame/Binaries/` and `FactoryGame/Binaries/Win64/` are real folders there; the rest
of the install, `Engine/` and the paks included, is linked. Unreal finds the project from the launcher's
own path, and Wine should report the path it was started from (the overlay's), not the link's target:
not yet seen with the game. A first run with the shipped DLL copied in (`--dll` a copy
of Coffee Stain's own) checks the overlay itself before any recovered code is involved.

| Step | What you see | Needs |
|---|---|---|
| 0 | The shipped game on the Porting Kit, and `STATUS.md` in the recovered-code repository: functions written and matched, by module | done |
| 1 | The game starts with **our** `FactoryPreEarlyLoadingScreen` DLL, built from recovered source for Win64, in place of Coffee Stain's. A `RECOMP_WATERMARK` build (off when matching) prints "recomp: N modules, M functions" on the loading screen | `5.6.1-CSS` built for Win64 (import libraries), MSVC 14.38.33130 (msvc-wine on the Mac, or the Windows PC), `./port run --overlay` |
| 2 | More of the game is ours each time: whole small modules swapped (FactoryMessagingTypes, then Coffee Stain's plugins), and inside FactoryGame, each recovered function hooked over the shipped one by a loader DLL. The watermark's counters grow; the game must keep playing | A hook loader (SML's approach), the matching harness |
| 3 | The native milestone below: the Mac build of the engine draws the early loading screen from recovered code | Step 1's module, the engine built for macOS arm64 |
| 4 | Native main menu, with the rest of FactoryGame still stubs | The menu's C++ classes recovered |
| 5 | Native: load a save and walk around | Most of FactoryGame recovered |

### Step 1: where it stands (2026-11-01)

Done: `FactoryPreEarlyLoadingScreen` is written in full (24 functions, 11,333 bytes, each on its PDB
lines) with the `RECOMP_WATERMARK` switch; `./port overlay` and `./port run --overlay` work on the real
install (an overlay with the shipped DLL copied in is at `/Volumes/SMART/TheSTouch/satisfactory-overlay`,
ready for the baseline run); `./port decomp --match` compares objects. Nothing is compiled yet. Missing,
in order:

1. **Engine access** (the owner). Neither GitHub account (TheSTouch, stan69b) can read
   `EpicGames/UnrealEngine` or `satisfactorymodding/UnrealEngine` ("Repository not found", checked
   2026-11-01). Link one account to an Epic Games account (Epic's account settings, Apps and Accounts,
   GitHub), accept the EpicGames organisation's invitation, then follow the modding docs' engine page.
   The modding docs also offer a prebuilt `5.6.1-CSS` editor for Windows; an installed engine carries the
   headers and Win64 import libraries a game module needs, which may be enough for step 1 without
   building the engine.
2. **The compiler** (the Windows PC). Visual Studio 2022 with the "MSVC v143 x64/x86 build tools
   (v14.38-17.8)" component and Windows SDK 10.0.22621; UBT pinned to it in `BuildConfiguration.xml`
   (`<WindowsPlatform><CompilerVersion>14.38.33130</CompilerVersion></WindowsPlatform>`). msvc-wine on the
   Mac was not tried: without the engine's headers it has nothing of ours to compile.
3. **The build** (the Windows PC). A project with FactoryGame's headers, such as the modding starter
   project, with `FactoryPreEarlyLoadingScreen` assembled as the recovered-code repository's `STATUS.md`
   says; target `FactoryGameSteam`, Win64, Shipping, so the DLL's name and its imports
   (`FactoryGameSteam-Core-Win64-Shipping.dll`...) are the game's. Two builds: plain, for matching, and
   with `RECOMP_WATERMARK=1`. Bring back to SMART (never to a remote) both DLLs and the plain build's
   objects (`Intermediate/Build/Win64/x64/FactoryGameSteam/Shipping/FactoryPreEarlyLoadingScreen/*.obj`).
4. **On the Mac.** `./port decomp <shipped dll> --match <obj>` for each of the three objects, to fix what
   differs; `./port overlay satisfactory /Volumes/SMART/TheSTouch/satisfactory-overlay --dll <watermark
   dll>`; the owner starts `./port run satisfactory --overlay /Volumes/SMART/TheSTouch/satisfactory-overlay
   --kettle` (after `./port steam signin` if Kettle is not signed in): first once as the overlay stands
   now, with the shipped DLL, then with ours.

## First milestone: the early loading screen, native

`FactoryPreEarlyLoadingScreen` is the smallest game module and runs first: three source files with
about 11 KB of Coffee Stain's code (`FGSlateMinimalLoadingScreen.cpp`, `FGPreLoadScreen.cpp`, the module
file), the rest being inlined Slate templates. The milestone is done when:

1. Coffee Stain's engine (the `5.6.1-CSS` branch) builds as a native macOS arm64 game target;
2. FactoryGame builds natively from `Headers.zip`, with stub bodies and UHT's regenerated code;
3. `FactoryPreEarlyLoadingScreen`'s three files are recovered, every function matches the shipped DLL when
   compiled with MSVC 14.38, and the same files compile with clang for arm64;
4. the native build mounts the shipped `FactoryGame-Windows` content, creates its Vulkan device on native
   MoltenVK, and draws the early loading screen from the recovered code. A screenshot and the log show both.

This proves every stage once: engine, headers, recovery, matching, native compile, the shipped content
and the renderer. It also measures how fast functions are recovered, which turns 6.9 MB of game
code into a schedule. The next milestones are the main menu (the C++ classes behind its widgets), then
loading a save.

Before starting, the owner has to act:

- **Engine access.** Link the owner's GitHub account to Epic and follow the modding docs' steps to the
  `5.6.1-CSS` repository (not done on 2026-11-01: see "Step 1: where it stands"). The engine source stays
  outside this repository.
- **Recovered code lives on the owner's disks only** (decided 2026-11-01): see below.

## Where recovered code lives

Recovered C++ is Coffee Stain's code in another form. The decompilation exception for interoperability is
narrow and forbids passing the result to third parties, so it never leaves the owner's own disks: no
GitHub, private or public, and no cloud storage.

- **Working copy**: a git repository with no remote, `/Volumes/SMART/TheSTouch/satisfactory-recomp/`.
- **Backup**: a bare repository on the Mac's internal disk, `~/Backups/satisfactory-recomp.git`, pushed to
  after each session (`git push backup`). It holds only text: recovered sources, notes, work lists.
- **Engine source and builds** (Coffee Stain's fork, tens of GB) stay on SMART only, outside this
  repository and the recovered-code repository.
- **When the work ends**, both copies are archived to another drive, and the internal-disk copy is deleted.

This repository holds the tools (PDB reading, disassembly, the matching harness) and never game-derived
output; `.gitignore` also refuses a `satisfactory-recomp/` folder copied in by mistake.

## To run later (the game running)

The Mac is short on memory, so these wait:

```sh
# Every Windows call and its call site, at the main menu: which calls come from the game's modules
# rather than the engine (decision 3)
./port trace satisfactory --relay

# The baseline a native build has to beat: load a save by hand, lowest settings set in the game's options
./port run satisfactory --hud
./port bench satisfactory --attach --label vulkan-low --note "<scene>"

# Every SPIR-V module and failing pipeline at the menu, to replay on a native MoltenVK outside the game
layers/imagelog/build.sh
./port run satisfactory --env VK_ADD_LAYER_PATH=$PWD/runtime/layers/imagelog \
    --env VK_INSTANCE_LAYERS=VK_LAYER_PORTKIT_imagelog \
    --env IMAGELOG_DUMP=$PWD/reports/satisfactory/imagelog-dump
```

## To do next without the game

- Recovery has started with the tools only: `./port decomp <dll> --out /Volumes/SMART/TheSTouch/satisfactory-recomp/work`
  lists a module's game functions from its PDB and writes each one's annotated disassembly to the
  recovered-code repository.

- Diff `5.6.1-CSS` against Epic's 5.6.1 tag (which engine modules Coffee Stain changed), and check that
  its tag matches CL 513885.
- Build the fork for macOS arm64, and check that VulkanRHI is available there.
- The matching harness compares, it does not compile: `./port decomp <dll> --match <file.obj>` pairs each
  function of a compiled object with the shipped one of the same decorated name (the PDB's public symbols;
  a static function by its qualified name when only one has it), masks the bytes relocations fill, and
  says matched or where the first difference is, with the shipped instruction there. Still to set up:
  MSVC 14.38.33130 with the flags UBT passes for `5.6.1-CSS` (on the Windows PC; msvc-wine on the Mac for
  single files).
