// Force-included into every file of the hook DLL (FactoryGameRecomp.Build.cs). Not Coffee Stain's code.
//
// The recovered FactoryGame functions compile in a module of their own, against FactoryGame's headers:
// - matching build (RECOMP_HOOKS=0): FACTORYGAME_API is dllexport, as inside FactoryGame, so every call and data
//   access compiles as in the shipped module. It is not neutral: MSVC inlines an exported function less readily than
//   a plain one, and with FACTORYGAME_API empty seven functions differed that match with dllexport;
// - hook build (RECOMP_HOOKS=1): FACTORYGAME_API is dllimport, so what the recovered code takes from FactoryGame goes
//   through __imp_ pointers, the slots `./port hooks` generates and the loader fills. Defining members of a dllimport
//   class is only a warning (C4273), turned off here.

#pragma once

#ifndef RECOMP_HOOKS
#define RECOMP_HOOKS 0
#endif

#undef FACTORYGAME_API
#if RECOMP_HOOKS
#define FACTORYGAME_API __declspec( dllimport )
#pragma warning( disable : 4273 )
#else
#define FACTORYGAME_API __declspec( dllexport )
#endif

// A mark the hook build adds to a string literal it shows (RECOMP_MARK next to the literal, on the same line),
// to see on screen that our function ran: " (recomp)" when hooking, nothing when matching.
#if RECOMP_HOOKS
#define RECOMP_MARK TEXT( " (recomp)" )
#else
#define RECOMP_MARK
#endif
