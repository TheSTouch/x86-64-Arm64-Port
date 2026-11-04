# Matching decompilation of other games: what a game needs, and the plan to automate it

Satisfactory's recompilation ([SATISFACTORY-RECOMP.md](SATISFACTORY-RECOMP.md)) recovers the game's own code as
C++ that compiles to the shipped bytes, function by function, and proves each one in the shipped game on Windows
before anything is built for the Mac. This page says which other games the same method can work on, what stays
private, and what the tools still lack before a new game can be started with one command and an AI session that
reads this repository.

Status: the tools exist and are used on Satisfactory every day; the check and the setup below are **planned**, not
written yet. They are to be built after Satisfactory's menu milestone.

## The method in one paragraph

The shipped PDB gives every function's name, source file, line numbers, locals and types. The game's own code is
written back as C++, laid out on its PDB lines, and compiled with the exact compiler and flags against the engine's
source; `./port msvc match` compares each compiled function with the shipped one, relocated operands masked, and
only an exact match counts. A hook DLL (`windows/recomp-hooks`) then replaces the shipped functions by the matched
ones in the running game on Windows, and a trace mode logs which functions a milestone needs (for Satisfactory: the
main menu and every menu screen). Recovery runs in parallel AI sessions, one unity blob each, with a coordinator that
builds and play-tests on the PC.

## What a game needs

Hard requirements: without any one of them the method does not apply.

| Requirement | Why | Satisfactory |
|---|---|---|
| **The PDBs of the shipped build**, matching its binaries (same GUID and age) | Names, source files and lines, locals and types: without them there is nothing to lay code out on and nothing to pair compiled functions with. Almost no commercial game ships them; this is the main limit. | Every module's PDB ships with the game |
| **No code protection** (Denuvo, Arxan, VMProtect, ...) | The code must be readable as shipped. This project never circumvents a protection. `./port inspect` flags encrypted code sections. | None |
| **No anti-cheat that stops a modified single-player game** | Recovered functions are proved by hooking them into the running game. | None |
| **The engine's source, at or near the shipped version** | The game's code compiles against the engine's headers; class layouts and inlines must agree. The closer the version, the fewer functions differ through engine drift. | Unreal 5.6 (Coffee Stain's fork, public through Epic's GitHub access); some drift remains |
| **The exact compiler and flags** | Matching is byte for byte. The PDB's compiland records name the compiler version; the build's flags come from the engine's build rules. | MSVC 14.38.33130, Coffee Stain's target rules, checks in Shipping |
| **A copy the user owns, and a Windows PC to run it** | Game files never leave the user's disks; the proof is the game running on Windows. | The owner's Steam copy |

Helpful, not required:

- **The game's headers** (a modding SDK): Satisfactory's are public for modding and save months. Without them, the
  headers are rebuilt from the PDB's types first.
- **Middleware stubs**: closed SDKs (Wwise, Sentry, platform SDKs) are not compiled; a stub with the PDB's
  declarations and sizes lets the game's code that calls them compile.
- **Size**: Satisfactory's own code is about 8.4 MB in 59 modules (14,570 functions in FactoryGame's .cpp files);
  its menu milestone is 1,989 functions.

## What stays private, what is public

- **Public (this repository)**: the tools only: `./port`, the hook loader, the watcher, the docs and the generic
  workflow.
- **Private, on the user's disks only**: the recovered code, decompiled listings, game files, builds, traces and
  the per-game workspace. Recovered code is derived from a commercial product: it is never pushed to a public host.
- **Never**: circumventing a protection, contacting the game's developer or modding community on the user's behalf,
  using a copy the user does not own.

## Planned: making a new game a one-command start

1. **`./port check <game>`**: a go/no-go report, with the reason for each line.
   - PDBs present for which modules, and whether they match the binaries (GUID, age);
   - the compiler version from the compilands, and whether that MSVC is installed (Mac mirror or PC);
   - the engine and its version, and where its source would come from;
   - protection and anti-cheat (from `./port inspect`);
   - middleware from the imports and the PDB's compilands (what will need a stub);
   - the game's own code size per module, against the engine's;
   - headers: a known SDK, or rebuilt from the PDB;
   - the verdict: go, go with limits (named), or no-go.
2. **`./port init <game> --workspace <private dir>`**: sets up the private workspace outside this repository.
   - a git repository with a local backup remote and no public one;
   - the headers-only module, so the engine's header tool runs without the game's code compiling;
   - stubs for the middleware, generated from the PDBs;
   - the staging, matching and hook-build scripts;
   - STATUS.md with its counters, and the first work list in unity-blob order.
3. **The generic pieces moved into the tools.** They live today in Satisfactory's private workspace but contain no
   recovered code:
   - laying a spec out on its PDB lines (`place.py`);
   - staging into unity blobs (`stage.sh`);
   - the rebuild-and-match and hook-build scripts;
   - the work-list tools (menu path from traces, blob order);
   - the match helpers (background match, `--diff` view, the Mac counter keyed by decorated name).
4. **An agent guide at the root (AGENTS.md)**, so an AI session opened on a clone knows the workflow without the
   conversation that built it:
   - the per-function loop: spec on PDB lines, background match, one `--diff` read, at most one try, queue the rest;
   - the generic matching rules learned on Satisfactory (MSVC and Unreal ones: check macros, `/fp:fast`, cast and
     ternary order, unity-blob inlining, `TObjectPtr` temporaries, ...);
   - the batch and coordinator prompt templates for parallel sessions: one unity blob per session, prompts in a file,
     one start line per session, don't stop for context, write down any wait;
   - the leftovers pass after a milestone;
   - the private-code rules above.
5. **Trace-driven milestones from the start**: the hook loader's trace mode on the user's PC gives the first
   milestone's work list (a main menu), so the order is measured, not guessed.

## What to expect

Even with all of the above, a game is weeks of sessions, not one run. On Satisfactory, one session matched about
37 KB of code an hour, and four parallel sessions 60-80 KB an hour; about one function in ten doesn't match at once
(inlining, registers, stack layout, engine drift) and waits for a dedicated pass after the milestone.
