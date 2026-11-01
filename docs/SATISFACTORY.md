# Satisfactory on the Porting Kit: Vulkan on MoltenVK

Satisfactory (Unreal Engine 5.6, Windows, Steam) runs under `wine11` with `-vulkan`: Unreal's
Vulkan renderer on MoltenVK. Unreal falls back from SM6 to SM5, because MoltenVK has no mesh
shaders, 64-bit atomics or mutable descriptors. This page records what stopped the game, how each
cause was found, and what fixed it.

Logs of a run:

- the game's own log: `prefixes/wine11/drive_c/users/<you>/AppData/Local/FactoryGame/Saved/Logs/FactoryGame.log`.
  The shipping build logs little: applied console variables don't show up there;
- Wine, MoltenVK and the tools below: `reports/satisfactory/<time>-run/wine.log`. Add
  `--env MVK_CONFIG_LOG_LEVEL=2` for MoltenVK's warnings and errors, `3` for its information lines.

## Seeing what the game asks Vulkan for

Wine's `win32u.so` loads the standard Vulkan loader bundled with the runtime
(`libvulkan.1.dylib`), so ordinary Vulkan layers work. [layers/imagelog](../layers/imagelog) is a
small one that logs every image the driver refuses, and every image created to be viewed in another
format, with its format, size, flags, usage and allowed view formats:

```sh
layers/imagelog/build.sh            # x86_64 layer into runtime/layers/imagelog
./port run satisfactory --env VK_ADD_LAYER_PATH=$PWD/runtime/layers/imagelog \
    --env VK_INSTANCE_LAYERS=VK_LAYER_PORTKIT_imagelog
grep imagelog: reports/satisfactory/<time>-run/wine.log
```

`IMAGELOG=all` logs every image. `IMAGELOG_STRIP_BLOCK_TEXEL=1` removes
`VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT` before the driver sees it, to test what happens
next. `layers/imagelog/build.sh --test` runs [blocktexel.c](../layers/imagelog/blocktexel.c), which
asks the runtime's MoltenVK for the same kind of image outside the game. Set `VK_DRIVER_FILES` to
another driver's ICD manifest to test that driver instead.

## The refused image (A-1)

The game quit loading the menu map with `vkCreateImage failed VK_ERROR_FEATURE_NOT_PRESENT`
(VulkanTexture.cpp:1480); MoltenVK said "Metal does not allow uncompressed views of compressed
images". The layer shows the image:

```
vkCreateImage -> -8 type=2D format=BC5_UNORM_BLOCK extent=7208x7208x1 mips=1 layers=1
  flags=MUTABLE_FORMAT|BLOCK_TEXEL_VIEW_COMPATIBLE|EXTENDED_USAGE
  usage=TRANSFER_SRC|TRANSFER_DST|SAMPLED|STORAGE view_formats=BC5_UNORM_BLOCK,R32G32B32A32_UINT
```

- **What it is**: a runtime virtual texture (RVT) physical texture. 7208 = 53 tiles of 136 pixels
  (128 plus a 4-pixel border on each side). BC5 is the normal layer; right after it comes the base
  colour layer, `BC1_RGB_UNORM` 8160×8160 with an `R32G32_UINT` view. Unreal compresses RVT tiles
  on the GPU (`VirtualTextureCompress.usf`) and writes the blocks through an uncompressed UAV of
  the same texture: one 64- or 128-bit texel per 4×4 block.
- **Metal has no such view.** `newTextureViewWithPixelFormat:` refuses BC1 → RG32Uint and BC5 →
  RGBA32Uint on an M4 with macOS 26.5, with or without API validation
  ([bcview.m](../layers/imagelog/bcview.m)).
- **No Unreal setting turns it off.** `r.VT.RVT.DirectCompress` exists in this build
  (`FactoryGameSteam-Renderer-Win64-Shipping.dll`: "Compress texture data direct to the physical
  texture on platforms that support it"), but it only chooses whether the compute shader writes
  through the alias or into a temporary texture that is then copied. The texture appears to get its
  alias whenever the RHI supports UAV format aliasing (inferred: the engine source was not
  available). With `r.VT.RVT.DirectCompress=0` in the
  user `Engine.ini` under `[SystemSettings]` (which the game keeps), the image was still created
  with the alias. `-dpcvars=` is ignored by this shipping build: nothing in the log shows it applied.
- **The game does create the views.** With `IMAGELOG_STRIP_BLOCK_TEXEL=1`, MoltenVK 1.4.0 accepted
  the image, Unreal created the `R32G32B32A32_UINT` and `R32G32_UINT` views, and the menu map
  loaded. A driver that accepted the image but refused the views would only move the failure.

## Getting past it: MoltenVK 1.4.2 (A-2)

MoltenVK 1.4.1 added `VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT` on Apple GPUs (commit
`744d3ea3`): the image lives in a placement heap (`MVK_CONFIG_USE_MTLHEAP`, on by default on
Apple GPUs), and an uncompressed view is a second texture of the view's format and block size over
the same memory. The `wine11` runtime bundles 1.4.0, so `./port setup` now lays Khronos's 1.4.2
macOS build over it, pinned by checksum in [portkit/config.py](../portkit/config.py) (`MOLTENVK`,
and `"moltenvk"` in the runtime). The bundled library stays next to it as
`libMoltenVK.dylib.bundled`, and `./port doctor` shows the version in use. No patch was needed.

With 1.4.2 the layer shows both RVT physical textures created, and the menu map loads (31 s).
`-dpcvars=r.VT.RVT.DirectCompress=0` is gone from the game file: the shipping build ignores it.
`r.VT.RVT.DirectCompress=0` is kept, through `-ini:` (A-3 below), as the cautious choice: the one
run with the direct path (which writes the blocks through MoltenVK's aliased views) hung in the
menu's first frames ("GameThread timed out waiting for RenderThread after 120 seconds", then
"Application Hang Detected"). That does not prove the direct path is at fault: a later run with it
off lost the device to a GPU timeout during start-up (see "GPU timeouts" below).

## Failing pipelines (A-3)

`IMAGELOG_DUMP=<folder>` makes the layer write every shader module once (`<hash>.spv`) and, for
each pipeline the driver fails to create, a record (`pipeline-<n>-<kind>.txt`: stages, entry
points, specialization, descriptor set and pipeline layouts). `runtime/layers/imagelog/replay
<records>` rebuilds compute pipelines from those records on the runtime's MoltenVK (or the driver
`VK_DRIVER_FILES` names), outside the game. On MoltenVK 1.4.2 a run fails 17 pipelines:

- **16 compute pipelines are Intel XeSS's** (`XeSS_i10_pipeline_state` …, compiled from DXIL:
  `RootConstants`, `registers`, `SSBO`). Every binding of their layouts is
  `VK_DESCRIPTOR_TYPE_MUTABLE_EXT`, from `VK_EXT_mutable_descriptor_type`, which MoltenVK does not
  offer (Unreal's own log lists it as unsupported). With argument buffers, SPIRV-Cross pads unused
  slots (set 0 binding 1 here) using each binding's resource type, and a mutable binding has none:
  "Argument buffer resource base type could not be determined". With
  `MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS=0` they fail in Metal instead. MoltenVK 1.4.2 still fails
  them, and `replay` reproduces all 16. SPIRV-Cross is not at fault: the fix is MoltenVK supporting
  mutable descriptors (feasible with Metal 3 argument buffers, which hold any resource ID; SPIRV-Cross
  already aliases descriptors on Metal 3). It is not needed to play: XeSS initialises at start, the
  failure is not fatal, and XeSS stays unavailable.
- **The fatal one is a graphics pipeline with a geometry shader.** Vertex `DrawRectangle`, a
  geometry shader that emits 18 vertices with `gl_Layer` (the six faces of a cube) and a fragment
  shader that samples `ShadowDepthCubeTexture` and writes depth: `FCopyShadowMapsCubeGS` /
  `FCopyShadowMapsCubePS` (`CopyShadowMaps.usf`), which copy a point light's cached static shadow
  depths into its shadow cube. Metal has no geometry shaders and MoltenVK reports
  `geometryShader = false` (Unreal logs "Geometry 0"), but Unreal's `VULKAN_SM5` platform still
  compiles and uses this one. MoltenVK drops the stage, so the vertex shader's outputs meet the
  fragment shader's inputs and Metal refuses: "Fragment input(s) `user(locn1)` mismatching vertex
  shader output type(s) or not written by vertex shader". Unreal stops on it
  (VulkanPipeline.cpp:1666) in the menu's first frames.

**Settings that reach the game.** The shipping build ignores `-dpcvars=`, but applies Unreal's
command-line ini override, and the game file now passes both settings that way:

```
-ini:Engine:[SystemSettings]:r.VT.RVT.DirectCompress=0,[SystemSettings]:r.Shadow.CacheWholeSceneShadows=0
```

`r.Shadow.CacheWholeSceneShadows=0` turns off the point-light shadow cache, so the copy with the
geometry shader never runs. The same lines under `[SystemSettings]` in the prefix's user
`Engine.ini` work too (the game keeps that section), but the command line keeps everything in the
game file. With both, the game reaches the main menu on MoltenVK 1.4.2, and the only failing
pipelines left are XeSS's.

## The pink screen (A-4)

Pink, for this page, is the share of strongly magenta pixels in captures of the game's window,
taken every two seconds (`screencapture -l <window>` works while the window is behind others).

- **At start-up it is the game window before its first frame.** In the run that reached the menu
  at 18:28 (MoltenVK 1.4.2, the A-3 settings), the splash window came first, then the game window
  was entirely magenta for about 25 seconds: from the Vulkan device's creation (18:27:20) until
  Unreal drew its loading screen (18:28:00). Every capture after that had no magenta. It is the
  content of a Wine window whose Metal layer has not been presented to yet, not something the game
  draws. A-3's fixes do not change it, and it does no harm.
- **Once, in the menu**: one run (17:33, cached shadows off through `Engine.ini`) showed blocky
  magenta areas in the 3D scene behind the menu, 10 % of the window. Later runs with the same
  settings through `-ini:` showed none; the 3D scene was black with scattered coloured specks
  instead. That, not the start-up pink, is the rendering problem to chase next, with an Xcode GPU
  frame capture of the menu.
- **XeSS is not involved**: its 16 pipelines fail in every run, with and without magenta.

## GPU timeouts

Two of seven runs on MoltenVK 1.4.2 stopped on the GPU, at different moments: once at the menu's
second frame (render thread waiting 120 s, with `DirectCompress` on), once during start-up
(MoltenVK: "Lost VkDevice after MTLCommandBuffer … execution failed: Caused GPU Timeout Error";
Unreal: `TerminateOnGPUCrash`, with `DirectCompress` off). A run that stops this way can simply be
started again; finding the command buffer needs a capture of a failing run.

## First runs of `bench` and `trace` (A-5)

The benchmark in a world, at the lowest settings, is still to do: it needs a save loaded by hand
(`./port run satisfactory --hud`, load a save, then
`./port bench satisfactory --attach --label vulkan-low --note "<scene>"`). Set the lowest
settings in the game's own options: the game rewrites `GameUserSettings.ini` at start, and most
of the `sg.*` values set to 0 there were back at 3.

What the first runs, attached to the main menu, showed and changed:

- **Memory**: `ps`'s resident size said 354 MB while Activity Monitor and the Metal HUD said 4.3 GB;
  under Wine and Rosetta most of the game's memory is compressed or swapped. `bench` now samples
  the physical footprint (`proc_pid_rusage`), and still records the resident size in `samples.csv`.
- **Instruments**: saving a 20-second Game Performance recording of the game took 7 minutes
  (161 MB). `bench` killed `xctrace` after 3, which left a document Instruments cannot open. It now
  waits up to 15 minutes, stops it with Ctrl-C (which still saves) after that, and lists the trace
  in the report only when `xctrace` says it saved it.
- **Ctrl-C** during sampling kept nothing; the report now covers what was sampled.
- **Menu numbers** (M4, 16 GB, menu scene, with the HUD): about 8 to 9 frames a second with 70 ms
  of GPU time a frame, 1.2 to 1.5 cores of CPU, a 4.3 GB footprint, and swap growing. The menu's 3D scene
  is not drawn correctly (A-4), so these are not a benchmark.
- **`trace`** (menu, `--sample 10`): 83 Wine builtin DLLs and 444 of the game's own in the game's
  process. Steam's overlay and client DLLs, loaded from Steam's folder, were counted as the game's;
  they are now listed apart. The most frequent Wine message is Vulkan's
  `VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_EXT` (4058 times, Unreal's GPU timing). With `+loaddll`
  set, Windows Steam took 10 minutes to start, because Steam inherits the trace's debug channels.

## Also seen

- **Sound**: during one run (around 17:10) the game's sound was white noise. Not looked into yet
  (Wwise on Wine's XAudio2 and Core Audio).
