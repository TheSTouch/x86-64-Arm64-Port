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
| 1 | The game starts with **our** `FactoryPreEarlyLoadingScreen` DLL, built from recovered source for Win64, in place of Coffee Stain's. A `RECOMP_WATERMARK` build (off when matching) prints "recomp: N modules, M functions" on the loading screen | done 2026-11-02, on the owner's Windows PC (`windows/overlay.ps1`): the menu reached with the watermark on the loading screen |
| 2 | More of the game is ours each time: whole small modules swapped (FactoryMessagingTypes, then Coffee Stain's plugins), and inside FactoryGame, each recovered function hooked over the shipped one by a loader DLL. The watermark's counters grow; the game must keep playing | started 2026-11-02: the hook loader runs in the shipped game on Windows ("Step 2" below); the menu path is measured by trace mode |
| 3 | The native milestone below: the Mac build of the engine draws the early loading screen from recovered code | Step 1's module, the engine built for macOS arm64 |
| 4 | Native main menu, with the rest of FactoryGame still stubs | The menu's C++ classes recovered |
| 5 | Native: load a save and walk around | Most of FactoryGame recovered |

**Order of work (the owner, 2026-11-02).** Windows first, up to the main menu with complete menu navigation
running recovered code in the shipped game (steps 1, 2 and the menu part of 4, validated on Windows); then one
Mac build to check that everything holds natively; only then the game proper (step 5). The Mac engine build is
paused until the menu milestone.

### Step 1: where it stands (2026-11-02)

Each milestone is validated on Windows first (matched against the shipped module, then run in the shipped
game), and only then built natively on the Mac and compared: the two tracks run side by side on the same
engine tag, `5.6.1-css-83`.

- **Windows (the owner's PC, `ssh windows`).** MSVC 14.38.33130 and Windows SDK 10.0.22621 in Visual Studio
  2022, the modding community's prebuilt `5.6.1-css-83` editor (which carries the `FactoryGameSteam` Win64
  Shipping libraries), and a minimal project holding only the recovered modules with Coffee Stain's target
  rules. `FactoryPreEarlyLoadingScreen` builds, and 22 of its 24 functions (10,258 of 11,333 bytes) match the
  shipped DLL byte for byte, `SFGMinimalLoadingScreen::Construct` (6,069 bytes) included. The two that differ
  do so in inlined engine code (register allocation in FString path building, the load order of a vector's
  halves); pure engine functions differ too, because the public engine is older than the game's.
- **The game's build settings.** The game was built with checks in Shipping (`-UseChecksInShipping`, so
  `DO_CHECK=1`): delegates then carry the multithreading access detector and every `TAttribute` is 8 bytes
  bigger. A module built without it does not have the game's ABI. UBT pins the compiler with
  `-CompilerVersion=14.38.33130`, and needs `-NoUBTMakefiles` when only an environment switch changed.
- **Mac.** The engine source (`5.6.1-css-83`) is on SMART with its Mac dependencies. Unreal 5.6 accepts
  Xcode 15.2 to 16.9; with Xcode 26.6 the local clone raises `MaxVersion` in `Apple_SDK.json` and turns off
  the new clang warnings 5.6 code trips (`AppleToolChain.cs`). The first target is `UnrealGame` for macOS
  arm64.
- **Step 1 done (2026-11-02), on Windows.** A copy of the install on the PC (`D:\SatisfactoryRecomp\Game`,
  without PDBs) and an overlay of it made by `windows/overlay.ps1` (junctions and hard links, a real
  `FactoryGame\Binaries\Win64` holding our watermarked DLL). The shipped game ran from the overlay to the main
  menu with "recomp: 1 modules, 24 functions" on the early loading screen; the log shows the overlay as its
  base directory and no error. The game copy needs `steam_appid.txt` next to the shipping executable, and Steam
  running. The Mac overlays (`/Volumes/SMART/TheSTouch/satisfactory-overlay`, `-recomp`) are ready for the
  same check under the Porting Kit.

### Step 2: the hook loader (2026-11-02)

FactoryGame.dll (15 MB) cannot be swapped whole, so its functions are replaced one at a time, in the shipped game:

- **Compiling recovered FactoryGame code.** The build project holds FactoryGame as a headers-only module (`Headers.zip`,
  with Coffee Stain's UHT plugin and a declarations-only Wwise stub whose sizes come from the PDB), so UHT runs on its
  978 headers and nothing compiles FactoryGame itself. The recovered functions compile in a module of their own,
  `FactoryGameRecomp`, with FactoryGame's definitions. In a matching build `FACTORYGAME_API` is
  `__declspec( dllexport )`, as inside FactoryGame, and the objects match the shipped functions byte for byte
  (`./port decomp --match`). It is not neutral: MSVC inlines an exported function less readily than a plain one, and
  with the macro empty seven functions differed that match with dllexport (batch Pace 4: `ShouldShowAlphaInfoScreen`,
  `ToggleFullscreenMode`...).
- **The hook DLL** ([windows/recomp-hooks](../windows/recomp-hooks)). The same sources built with `RECOMP_HOOKS=1`:
  `FACTORYGAME_API` becomes dllimport, so what the recovered code takes from FactoryGame and the game's plugins goes
  through `__imp_` pointers. `./port hooks` reads the compiled objects and the PDBs and writes the tables object the
  DLL links: one slot per such symbol (module and RVA), one hook per recovered function (RVA, length, first bytes).
  The RVAs come from the PDB's public symbols, so a function FactoryGame does not export gets its slot too; a name
  no PDB has is left to the linker, which fails naming it.
  Engine modules link through their import libraries; those not yet loaded when the hook DLL loads are delay-loaded.
- **Loading.** `FactoryPreEarlyLoadingScreen`, the first game module to load, loads the hook DLL (`RecompHooksClient.h`,
  one macro on a line that had only a brace, so its matches stay). The DLL registers for DLL load notifications; as
  each module of its tables is mapped, before its own code runs, it fills that module's slots, and for FactoryGame it
  checks each hooked function's first bytes and writes a jump to ours. `Recomp.log`, next to the game's log, lists it.
- **Proof (2026-11-02).** `UFGVersionFunctionLibrary::GetVersionString`, recovered and matched, hooked over the shipped
  one: the main menu reads "Version: 1.2.4.1 CL#513885 (recomp)", the mark the hook build adds after the literal.
- **Recovering FactoryGame (2026-11-02).** Functions compile in the shipped build's unity blob (the PDB names each
  function's `Module.FactoryGame.<n>.cpp`): what the shipped build inlined from another file of its blob is only inlined
  here if that file's function is recovered too. Coffee Stain's checks are `fgcheck` (the CSS engine's FG_CHECK_IMPL).
  By the morning of 2026-11-02: 143 FactoryGame functions hooked, every one matching byte for byte (game instance,
  user settings, local player, game engine and game mode, game session, the menus' own widgets (menu base, widget
  switcher, button hints, base UI and their constructors), player input, save system, settings apply types...).
  Each batch ran in the shipped game through the watcher: the full menu navigation (every Options tab, New Game's
  panels, Load with its saves listed, Credits, Exit), no error. The counters live in the recovered-code repository's
  STATUS.md.
- **Only matching functions are hooked.** One that differs may use other field offsets than the game: css-83's
  UInstancedStaticMeshComponent is laid out differently from the game's engine, so a recovered
  UFGFoliageInstancedSMC::ShouldCreatePhysicsState reads its flag at +0xbb0 where the game has it at +0xb68.
  `./port hooks --skip` takes the functions the last matching run reported as differing.
- **What does not match** is recorded with its evidence: inlining decisions of a smaller translation unit (an engine
  template or a function of the same blob that the shipped build called, inlined here), and engine drift (FName ==
  compares the two halves separately in the shipped build; a class laid out differently).
- **Trace mode.** Tables made with `--trace` also list every FactoryGame function; the loader puts an int3 on each
  entry, and the first execution of each traps once, is written to `RecompTrace.tsv` and runs on (74,064 entries armed
  in 2 s; the menu loads as fast). `./port decomp <dll> --trace <tsv>` names them. Three traces, launch to the main menu
  and through every menu screen, give the menu milestone's work list: 1,989 functions from FactoryGame's own .cpp
  files (745 KB), most of them startup (static initializers, default objects, game instance, the menu map); the menu
  screens themselves add 160, being mostly UMG and Blueprint.
- **Running it while away.** `windows/watcher.ps1`, started by the owner in their own session, runs the desktop
  launchers on request from ssh (which cannot show windows), takes screenshots, clicks and types.

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
  `5.6.1-CSS` repository (done on 2026-11-02 for TheSTouch, with the modding community's linker). The engine
  source stays outside this repository.
- **Recovered code lives on the owner's disks only** (decided 2026-11-01): see below.

## Where recovered code lives

Recovered C++ is Coffee Stain's code in another form. The decompilation exception for interoperability is
narrow and forbids passing the result to third parties, so it never leaves the owner's own disks: no
GitHub, private or public, and no cloud storage.

- **Working copy**: a git repository with no remote, `/Volumes/SMART/TheSTouch/satisfactory-recomp/`.
- **Backup**: a bare repository on the Mac's internal disk, `~/Backups/satisfactory-recomp.git`, pushed to
  after each session (`git push backup`). It holds only text: recovered sources, notes, work lists.
- **Hub** (2026-11-02): a bare repository on the owner's Windows PC, `D:\SatisfactoryRecomp\recomp.git`, over ssh on
  the local network, so recovery sessions on the Mac and on the PC share work. Still the owner's own disks.
- **Engine source and builds** (Coffee Stain's fork, tens of GB) stay on SMART only, outside this
  repository and the recovered-code repository.
- **When the work ends**, both copies are archived to another drive, and the internal-disk copy is deleted.

This repository holds the tools (PDB reading, disassembly, the matching harness) and never game-derived
output; `.gitignore` also refuses a `satisfactory-recomp/` folder copied in by mistake.

### Sharing the work with other players (2026-11-02)

Other players own the game too, but owning it does not change who may receive recovered code. What this
repository may hold:

- **Generic tools**: PDB reading, disassembly, matching, the hook loader, the build project and its scripts
  without recovered sources.
- **Pipelines that run on the user's own copy**: they read the user's install and write their output on the
  user's machine. Function names, lines and types come from the user's own PDBs at run time, never from here.

What it may not hold, in any encoding: recovered C++, a diff or patch against the shipped binary that rebuilds
it, per-function snippets, or hint files detailed enough to regenerate the recovered code exactly (those are
the code in another form). A script that "decompiles it for you" in a few days without AI would need exactly
that knowledge, so it is not an option. A tool that redoes the recovery on the user's machine (a decompiler
draft and an AI loop, checked by the matcher) can be published; its output is that user's own recovery.

Existing recompilation projects (N64Recomp and Zelda64Recomp, XenonRecomp) ship a tool and a config and
generate code from the user's own game, but they translate machine code mechanically, and for this game that
route is ruled out above (the ABI boundary). Handing out the recovered source itself would need the rights
holder's permission.

## Making recovery faster (2026-11-02)

Measured on the night of 2026-11-02: 143 FactoryGame functions (9,172 bytes) recovered and matched in about
3.5 hours, in 24 batches of about ten minutes. Compiling, linking and the hook tables take from seconds to two
minutes of a batch; the rest is reading the disassembly and writing the C++. Those were the small functions,
64 bytes on average; the rest of the menu path averages about 400 bytes (736 KB in 1,846 functions). At the
same rate in bytes that is a few hundred hours for the menu milestone alone, so the gains are in the writing,
not the building.

| # | Change | What it saves |
|---|---|---|
| 1 | **Compile and match one file on the Mac.** UBT writes a response file for each compile; replaying it with `cl.exe` 14.38.33130 under msvc-wine compiles one unity blob without UBT or the PC | the PC round trip; matching goes on while the PC is out of reach (running the game still needs it) |
| 2 | **Every difference, side by side.** `--match` prints the whole function aligned against the shipped one instead of the first difference, or objdiff reads the objects | a rebuild for each next difference |
| 3 | **A decompiler draft.** Ghidra (free, reads PDBs) gives typed pseudo-C to turn into Unreal C++ | reading assembly by hand, above all for the 1,154 load-phase functions, mostly constructors setting defaults |
| 4 | **Skeletons from the PDB.** Each function's signature, first line, locals and callees written into an empty `.cpp`, already on its lines | line bookkeeping |
| 5 | **Systematic differences fixed once.** Whether FName `==` as two 32-bit compares comes from an engine setting or from the engine's version; class layouts compared between the game's PDB and css-83, so drifting classes are known before their functions are written | thousands of functions that compare FNames; work on functions that can never match |
| 6 | **Parallel work by unity blob.** Files in different blobs do not change each other's inlining, so several people or sessions work at once and share one build queue | wall time |

What `./port decomp` writes in each `.asm` today: the signature, the first and last line, the locals, and
called functions and string literals by name. The PDB has more that `pdb.py` reads past: the line of every
instruction, every inlined call (`S_INLINESITE`: its range and inlinee), and which register holds which local
where (`S_DEFRANGE_*`). The next changes use that, and the running game:

| # | Change | What it saves |
|---|---|---|
| 7 | **A source line on every instruction, and inlined calls by name.** The C13 line table for each address, and each inline site with its range and its inlinee ("lines 45-47: `TArray::Add` inlined"; "`UFGFoo::GetBar` inlined from its header", whose body is already in `Headers.zip`) | working out the structure from the assembly: the C++ is written line by line |
| 8 | **Field names instead of offsets.** `[rcx + 0x408]` becomes `this->mCachedOwner`: the type of `this`, the class layouts (`--type`), and the registers the PDB says hold each typed local, over their ranges | looking up offsets by hand, with no type guessing |
| 9 | **Constructors drafted from the running game.** The hook loader walks each FactoryGame class's default object through reflection and dumps every property's name and value, turned into `mFoo = 5.0f;` lines; only members reflection does not see need the assembly | most of the load phase (1,154 functions, 287 KB, about 39 % of the menu path's bytes), mostly constructors and static initializers |
| 10 | **Families recovered once.** Each function's code hashed with its addresses and offsets masked; identical or near-identical groups (`UFGUserSettingApplyType` subclasses, the `Get( WorldContext )` subsystem getters, option rows) recovered once and the pattern carried to the rest. To measure first | repeating the same recovery |
| 11 | **A permuter for the last differences.** As decomp-permuter does for the N64 decompilations: small rewrites of the C++ (statement order, temporaries, casts) kept when they score closer; needs a numeric diff score instead of the first difference, and 1 | the register-allocation and ordering differences that take the longest by hand; runs unattended |
| 12 | **Work counted in source, not functions.** Functions from the same lines of a game file (template instances, copies of one inline) counted once, templates first | a truer size of the job; one source, several matches |
| 13 | **Later milestones: stubs that log.** In the native build an unrecovered function is a stub that logs "not recovered: X", and traces of real play pick the work; debug commands, the cheat manager and server-only code may never need recovering | everything that never runs |

### Done (2026-11-02, batch "Speed")

Items 1, 2, 7, 8, 9, 10, 11 and 12, in three sessions at once: A on the PDB and the disassembly, B on the
compiler and the running game, C recovering code with them.

| # | Command | Measured |
|---|---|---|
| 1 | `./port msvc cl`, `flags <obj>`, `mirror`, `match <staged project> <blob \| File.cpp>`, `compare <obj> <obj>`. MSVC 14.38.33130 (cl 19.38.33145) and SDK 10.0.22621, copied from the PC, run under `runtime/wine11-11.21` with a prefix of their own, no msvc-wine. The flags are UBT's response files, checked against the command line each `/Z7` object records (LF_BUILDINFO); UHT's generated headers and UBT's files are mirrored from the PC at their PC paths (`msvc-mirror/pc`); the engine is the Mac clone, checked by hashing the 1,786 engine headers the PC's compiles read (all identical). The shared PCH is built on the Mac too: with its header only forced, `FKey::operator=` stops being inlined | all 14 blobs of a PC build (c4-b24): the same functions with the same code (1,584, relocations masked) and the same match output (515 matched, 23 differs). A blob in 2 to 31 s (median 11 s), the PCH once in 30 s, all 14 with matching in 2 min 8 s. On the PC, UBT's compile of the same module (`-NoLink`, every blob) took 2 min 31 s, before syncing and fetching |
| 2 | `./port decomp <dll> --match <obj>` ends each line with a score (instructions not the same once both are aligned; 0 when matched); `--diff NAME` prints the function side by side | one module load (about 3 s for FactoryGame) per run |
| 7 | `--disasm` shows `; File:line` where the line changes, and `; > inlined X (file:line)` / `; < X` around each inline site | the details of all 38,989 FactoryGame functions in 9 s (438,227 inline sites) |
| 8 | memory operands named from the PDB: `; this->mCachedOwner`, `; inArgs->_LoadingText.TextData`, `; this->mSlots[1]`, where the PDB says which register holds which typed local | |
| 9 | `RecompDump.txt` next to the hook DLL: 20 s after the main menu, every property of the default object of each FactoryGame class goes to `RecompCDO.tsv`; `./port cdo <tsv> --class UFGFoo` prints the constructor lines (`mFadeOutTime = 0.25f;`, `PrimaryActorTick.bCanEverTick = true;`), zero defaults left out | one run: 1,203 classes, 7,536 properties (840 KB). The drafts of UFGMenuBase and UFGWidgetSwitcher are C's matched constructors; UFGBaseUI's `mCanDisplayPopup` is no UPROPERTY, so reflection does not see it: such members still come from the disassembly |
| 10 | `./port decomp <dll> --families [--trace TSV...]`: functions grouped by their code with every number masked, exact and near (90 % of the instructions) | see below; 3 s on the menu path, 25 s on all 41,504 game functions |
| 11 | `./port permute <staged project> <File.cpp> <Scope::Function> [--tries N] [--minutes M]`: textual rewrites (independent statements swapped, a temporary introduced or removed, an `if` initializer split, an early return or an if/else inverted, a condition, cast or `const` written another way), each compiled with item 1 and scored with item 2; the best five written beside the staged file. Never in the recovered-code repository | 24 to 28 s per candidate. On c4-b24: `UFGUserSetting::ShouldUseCVar` 2 -> 0 with the first candidate (`return UseCVar != 0;`); `GetEffectiveFrameRateLimit` 8 and `GetFGGameInstance` 16 unchanged after 30 minutes each (what differs there is what they inline, or the stack slots of two temporaries) |
| 12 | `--trace` counts distinct source spans (`file:first-last`, template instances and copies of one inline counted once); `functions.tsv` gains a `span` column | the three menu traces: FactoryGame's own sources 11,181 functions (1,013,268 bytes) in 3,475 spans (786,405 bytes); its `.cpp` files 2,015 functions in 2,012 spans (746,458 bytes), its headers 9,166 functions in 1,463 spans (40,014 bytes) |

**Families (item 10, measured).** Of the 11,181 menu-path game functions (1,013,268 bytes), 10,604 functions and
352,190 bytes fall in 71 families of 3 or more (139 families of 2 or more: 10,740 functions, 378,359 bytes). Most of
it is code written once and repeated by the compiler: 8,000 dynamic initializers (272,971 bytes, `FactoryGame.h`'s
FName and console-variable globals once per unity blob, 6,343 of them one exact family), UHT's `__DefaultConstructor`
(614) and `StaticClass` (1,216). Inside the game's own `.cpp` files, initializers aside, families hold 235 functions
(22,883 bytes), the buildable constructors among them. So families save little on the menu path beyond what the
span count (item 12) already folds; they matter for the buildables later. All 41,504 game functions: 489 families of
3 or more, 17,388 functions, 958,751 bytes.

**The order of recovery work (session C, 2026-11-02).** Whole unity blobs, whole files: `Project/blob-order.py` in the
recovered-code repository ranks FactoryGame's blobs by the menu-path bytes not yet recovered (blob 107 first: game
user settings, game mode, gameplay tags, game instance; then 101, then 95), and every function of the next file of
the top blob is recovered, traced or not, constructors included (drafted with item 9). Files of one blob change each
other's inlining, so a half-done file keeps differing (`LoadSettings` matched only once the rest of
`FGGameUserSettings.cpp` was there). The game runs every fifth batch, through the hook build on the PC and the
watcher; between runs a batch can be compiled and matched on the Mac (item 1), and the permuter takes what is left
differing, unattended.

### Done (2026-11-03, batch "Tools")

What session C's recovery of blob 107 asked of the tools, by session A of batch "Tools" while session B kept
recovering:

| # | Command | Measured on blob 107 |
|---|---|---|
| A-1 | `RecompApi.h`: the matching build defines `FACTORYGAME_API` as dllexport (above); `Project/stage.sh` greps that exact line | |
| A-2 | `--disasm` names a bitfield access by the bits it reaches, with each `LF_BITFIELD`'s position from the PDB: `test byte ptr [rbx + 0x2b], 0x20 ; ar->ArIsSaveGame` (test, or, xor: the mask's bits; and: the bits it clears; bt*: the bit numbered) | |
| A-3 | `--match` pairs what is decorated inside a function's scope with the PDB's undecorated name, "by scope": lambdas (nested too), fgcheck's `Impl` and other local classes, dynamic initializers and atexit destructors, deleting destructors; by exact name, else in the object's own blob (a header's static has an initializer in every blob), else the Nth of its kind in its function when both sides have as many | 251 more functions paired, 247 of them matched; unpaired 1,622 -> 1,371 (left: templates instantiated on a lambda) |
| A-4 | `--diff` ends with `verdict:` before `score`: `registers only`, `stack slots only` (renamed one to one), `register allocation only` (the same once copies, spills and nops are left out and registers masked), `inlined here, called there: X` / `called here, inlined there: Y` (calls compared by callee; the reverse needs the PDB's inline site), else `first difference at File.cpp:<line>[, in inlined Z]; N instructions differ besides register allocation`. Nothing when unsure | of 98 differing functions: 2 register allocation only, ~40 inline verdicts (`OnRep_CheatNoPower`: `CastChecked<IFGOptionInterface,UFGAdvancedGameSettings>`), the rest a line. `FindInactivePlayer` (7 other instructions) and `GetClosestUnOccupiedRespawnPoint` (5) are more than registers in this object |
| A-5 | `--disasm` reads an `FStaticBasicLogRecord` operand: `log Warning, line 859: u"No story tutorial intro manager created when calling NotifyPlayerAdded"`; and names a global by the dynamic initializer that constructs it (`VIDEO_QUALITY_SECTION_NAME`, `+0x8` inside an FString) | 9,012 FactoryGame globals named, read on first need (2 s) |
| A-6 | `./port decomp <dll> --callers NAME`: the functions that call (or tail-jump to) a function, or an imported one through the import address table, with "same blob" or the caller's blob; a site counts only where the caller's disassembly starts an instruction there | 2 s |
| A-7 | `./port decomp <dll> --by-line NAME`: per line of the function's own source file (from the PDB), in address order, the calls, inlined calls, strings, log records, globals, fields and compared constants | |
| A-8 | `./port permute` also swaps the operands of `==`, `!=`, `+`, `*`, `&`, `\|`, `^` (only a whole operand, so precedence holds), the arguments of `Dist`, `DistSquared`, `Dist2D`, `DotProduct`, `FMath::Min`/`Max`, and moves a local's declaration up or down one statement | ChoosePlayerStart's `Dist( a, b )` -> `Dist( b, a )` is now among its candidates |
| A-9 | `./port msvc match` names the shipped instruction at each first difference from one objdump (it ran a full `--disasm` per differing function), and matching compiles (`msvc match`, the permuter) leave out `/Z7 /Zo`: the same code (5,742 of 5,742 functions), the back end 10-12 s -> 3.3 s; `--debug-info` keeps them | 81.6 s -> about 40 s quiet (match 37.7 s -> 4.9 s, compile 36-40 s -> 32-34 s), the output identical line for line. Not under 30 s: the compiler's front end alone takes 24-29 s |

### Done (2026-11-03, batch "Finish")

What session B's recovery asked of the tools, by session A of batch "Finish":

| # | Command | What it changed |
|---|---|---|
| A-1 | `./port decomp <dll> --match <obj> --diff NAME` | each relocated call or tail call shows its callee after it, on both sides: the shipped one from the PDB and the import table (as `--disasm`), ours from the object's relocation symbol: `> +00006  call <reloc>  ; FSoftObjectPath::ResolveObject`. Shown only: the alignment, score and verdict are unchanged. Session B's extra `TSoftClassPtr::Get` in `TryGiveStartingRecipes` took four rounds to find without it |
| A-2 | `./port decomp <dll> --disasm AFGGameState::PostLoadGame_Implementation` | a virtual overriding one of a base at a non-zero offset runs with `this` on that base; the PDB records it as the member function type's this adjustment (its `this` local is still typed as the class). The function's own `this` (not an inlinee's) then names members by their offset in the object, and the listing says once `; this is AFGGameState's IFGSaveInterface base (+0x328)`: `[r14 + 0x370]` reads `this->mBuildableLightColorSlots.ArrayNum` (it read a member 0x328 bytes too low), `[rcx - 0x58]` `this->PlayerArray.ArrayNum` (unnamed before) |
| A-3 | `./port decomp <dll> --match <obj> --diff NAME` | `verdict: stack layout only (frame 0x188 vs 0x1c8)` when every differing instruction is the same but for its `[rsp/rbp + n]` and the frame's size: temporaries at other stack offsets (session B's `DeserializeGameModeSetting`, its two FVariant at `[rsp + 0x30]` / `[rsp + 0x48]`). After `stack slots only` (a one-to-one renaming), before `register allocation only` |
| A-4 | `./port hooks <dll> --objects DIR --recovered FGGameState.cpp --out FILE` | nothing to change: slots already come from the PDB's public symbols, not the export table, so `UFGDownloadPlayerAvatarImageTask::DownloadAvatarImage` (not exported, rva 0x77fbd0) gets an `__imp_` slot or a thunk. Now tested; a name no PDB has is left to the linker, whose unresolved external names it (no stub) |

Still to do: 3 (a decompiler draft), 4 (skeletons from the PDB), 5 (systematic differences fixed once) and 13 (stubs
that log, with the native milestones).

## Recovery by someone else, without AI

The matcher makes it possible: a function is right when it reproduces the shipped bytes, and that takes no
judgement. Someone else needs:

- their own copy of the game (the PDBs and `Headers.zip` ship with it);
- Coffee Stain's engine, `5.6.1-css-83` (a GitHub account linked to Epic, then the modding docs' steps);
- Windows with Visual Studio 2022, MSVC 14.38.33130 and Windows SDK 10.0.22621;
- this repository.

Their recovered code stays on their own disks, as ours does: they start from zero (see "Sharing the work"
above). Missing before that works:

- **The build project and its scripts move here.** `stage.sh`, `place.py`, `pcbuild.sh`,
  `rebuild-and-match.sh`, `build-hooks.sh`, the headers-only FactoryGame module and the Wwise stub live in the
  recovered-code repository today. They are tools: they come here without recovered sources, with the owner's
  paths and hosts (`/Volumes/SMART`, `ssh windows`, `D:\SatisfactoryRecomp`) turned into settings, and what is
  derived from the game (the Wwise stub's sizes, `compilands.tsv`) generated from the user's own install.
- **A how-to from zero**: the setup, then a first function, such as `UFGMenuBase::OnMenuEnterDone` (24 bytes,
  six lines of C++).
- **Plain `.cpp` files edited in an IDE.** Today the C++ sits in Python strings (`Specs/<File>.py`, laid out by
  `place.py`), which suits AI sessions. For people the `.cpp` becomes the source, and `place --check` /
  `--fix` moves each function onto its PDB lines.
- **A patterns cookbook**: what the disassembly shows and the C++ that produces it. The rules learnt so far
  are in the recovered-code repository's STATUS.md (`fgcheck`, the `TObjectPtr` GC barrier, `/fp:fast`, statics
  that must not be hooked, why only matching functions are hooked).
- **A work list to pick from**: the menu path by file and size, regenerated from the user's own traces, with
  a way to claim a file.
- **One command per try**: compile one file and show its differences: `./port msvc match` and `--diff` (items 1
  and 2 above, done). Without a PC, the toolchain and the mirror still come from a Windows install once.

Someone sitting at the PC does not need the watcher: the desktop launchers do the same.

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
- The matching harness: `./port decomp <dll> --match <file.obj>` pairs each function of a compiled object
  with the shipped one of the same decorated name (the PDB's public symbols; a static function by its
  qualified name when only one has it; lambdas, local classes and dynamic initializers by scope), masks the
  bytes relocations fill, and says matched or where the
  first difference is, with a score. `./port msvc match` compiles a blob on the Mac first, with MSVC
  14.38.33130 under Wine and UBT's own flags ("Making recovery faster", item 1).
