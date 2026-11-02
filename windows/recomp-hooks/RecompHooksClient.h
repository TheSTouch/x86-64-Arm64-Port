// The hook loader's client side, compiled into FactoryPreEarlyLoadingScreen (docs/SATISFACTORY-RECOMP.md, playable
// step 2). Not Coffee Stain's code. With RECOMP_HOOKS=1, RECOMP_INSTALL_HOOKS() loads the hook DLL from the project's
// binaries folder, next to this module (the executable's name with -FactoryGameRecomp inserted: FactoryGameSteam-
// FactoryGameRecomp-Win64-Shipping.dll), and calls RecompInstall. With RECOMP_HOOKS=0 it is empty, and the matching build is unchanged.
// No windows.h here: the module's other files include this header for the counters, and windows.h macros
// (LoadImage...) would rename engine functions they call.

#pragma once

#ifndef RECOMP_HOOKS
#define RECOMP_HOOKS 0
#endif

#if RECOMP_HOOKS
#include "CoreMinimal.h"
#include "HAL/PlatformProcess.h"
#include "Misc/Paths.h"

namespace RecompHooks
{
	inline int32 GHooked = -1;   // FactoryGame functions to hook; -1: the hook DLL did not load
	inline int32 GTraced = 0;    // functions traced (a trace build)

	inline void Install()
	{
		const FString File = FPaths::ConvertRelativePathToFull( FPaths::Combine( FPaths::ProjectDir(), TEXT( "Binaries" ), FPlatformProcess::GetBinariesSubdirectory(),
			FString( FPlatformProcess::ExecutableName( true ) ).Replace( TEXT( "-Win64-" ), TEXT( "-FactoryGameRecomp-Win64-" ) ) + TEXT( ".dll" ) ) );
		void* Hooks = FPlatformProcess::GetDllHandle( *File );
		if( !Hooks )
		{
			UE_LOG( LogTemp, Error, TEXT( "recomp: could not load %s" ), *File );
			return;
		}
		if( auto Start = (int32( * )())FPlatformProcess::GetDllExport( Hooks, TEXT( "RecompInstall" ) ) )
		{
			GHooked = Start();
		}
		if( auto Traced = (int32( * )())FPlatformProcess::GetDllExport( Hooks, TEXT( "RecompTraceFunctions" ) ) )
		{
			GTraced = Traced();
		}
		UE_LOG( LogTemp, Display, TEXT( "recomp: %s loaded, %d FactoryGame functions to hook, %d traced (Recomp.log)" ), *File, GHooked, GTraced );
	}
}

#define RECOMP_INSTALL_HOOKS() RecompHooks::Install()
#else
#define RECOMP_INSTALL_HOOKS()
#endif
