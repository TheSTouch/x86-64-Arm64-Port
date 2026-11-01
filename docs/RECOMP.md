# Native recompilation: how a game's project is planned

A game's recompilation is planned after its Porting Kit phase, from what that phase measured. This
page lists what goes in, what each source gives, and the decisions the plan has to make.

## Why recompile at all

On a Mac, the Porting Kit is a translation layer: Rosetta 2 translates the game's x86-64
instructions into arm64 instructions as they are loaded, and the CPU runs those. The translated
code is not native quality: it emulates x86 flags and memory ordering, and calls into Rosetta's
runtime for indirect branches. Wine and D3DMetal in the toolkit are x86-64 too, so the whole
process is translated. The case for a native build is:

- **iOS and iPadOS**: no JIT is allowed, so no Rosetta, and there is no Wine and no D3DMetal. On
  iOS, the code, the Windows layer and the graphics all have to be native.
- **On the Mac**: compiled arm64 instead of translated code, lower memory use (no Wine, no
  Rosetta), native Metal instead of translated
  DirectX, and no dependency on a toolkit Apple ships for evaluating ports.

## Inputs, in the order they come

| Source | Gives | Command |
|---|---|---|
| Static inspection | Engine and version, graphics API, middleware, anti-cheat, code protection, code size | `./port inspect <game>` |
| Porting Kit benchmark | The numbers a native build has to beat: frame rate, frame times, memory, CPU | `./port bench <game>` |
| Porting Kit trace | Windows DLLs Wine stands in for, the game's own DLLs, unimplemented and failing calls | `./port trace <game>` |
| Relay trace | Every Windows API function called and from where: the runtime's surface, plus call sites that seed code discovery | `./port trace <game> --relay` |
| Coverage on the Windows PC | Every code address that runs, indirect jump and call targets included: ground truth for code discovery | DynamoRIO `drcov`, on the PC the game was bought on (to set up) |
| Vulkan shaders and pipelines | For a game on its Vulkan renderer (Satisfactory): every SPIR-V module it creates, and the layouts of pipelines MoltenVK refuses, which SPIRV-Cross turns into Metal offline | `IMAGELOG_DUMP` with [layers/imagelog](../layers/imagelog) ([docs/SATISFACTORY.md](SATISFACTORY.md)) |
| Shaders | DXIL shaders converted offline to Metal with Apple's Metal Shader Converter, the converter D3DMetal itself uses (`libmetalirconverter`). Checked against an Xcode GPU frame capture of the Porting Kit run | (to set up) |

Rosetta's output is not an input. Its arm64 code is the x86-64 code with x86 registers pinned to
arm64 registers and the flags emulated, and calls into Rosetta's runtime for indirect branches,
exceptions and system calls. It carries no types or structure the x86-64 code lacks, its format is
undocumented, and reverse-engineering Rosetta conflicts with the macOS license.

## Go / no-go

The plan stops here if any of these hold:

- **The code is protected**: Denuvo, Arxan, VMProtect and similar tools encrypt the code at rest
  (`inspect` flags code sections with entropy near 8). Recompiling would need the decrypted code,
  and getting it means circumventing the protection, which this project does not do.
- **Kernel anti-cheat is required to play**: online modes are out of reach either way. Single-player
  is possible only if the game starts without it.
- **The work dwarfs the gain**: a modern engine with hundreds of MB of code is a multi-year
  project. The plan has to name a smaller first target, such as the main menu rendering natively.

## Decisions the plan makes

1. **Code discovery**: start from the PE's exports, the `.pdata` unwind table (x64 PE files list
   every function's start and end there), relocations, relay call sites and Windows coverage.
   Decide how unresolved indirect jumps are handled: a fallback interpreter, or a failure that is
   reported.
2. **Translation**: lifting to C or to LLVM IR, how x86 flags and memory ordering are kept, and
   how SSE/AVX maps to NEON.
3. **Windows layer**: which of the traced APIs are reimplemented and which are stubbed. The Mac
   build can borrow from Wine's code (LGPL). iOS needs its own layer.
4. **Graphics**: DirectX 12 → Metal, using Metal Shader Converter for the shaders and a native
   implementation of the D3D12 calls the trace shows.
5. **Engine knowledge**: if the engine has an official Mac or iOS target (Unreal does), the
   engine's own Metal backend and platform layer show what the game expects.
6. **First milestone**: the smallest part that proves the pipeline end to end.
