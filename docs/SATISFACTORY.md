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
