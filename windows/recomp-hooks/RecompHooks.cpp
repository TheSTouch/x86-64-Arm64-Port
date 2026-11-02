// The hook loader (docs/SATISFACTORY-RECOMP.md, playable step 2). Not Coffee Stain's code.
//
// Built into the hook DLL (the FactoryGameRecomp module) with the recovered FactoryGame functions and the tables
// `./port hooks` generates (portkit/hooks.py). FactoryPreEarlyLoadingScreen, the first game module to load, loads
// this DLL and calls RecompInstall (RecompHooksClient.h). From then on, as each module of the tables is mapped
// (a loader notification, before the module's own code runs):
// - its pointer slots get the module's base plus the RVA from the PDB: what the recovered code takes from FactoryGame
//   and the game's plugins, which have no import library;
// - for the hooked module (FactoryGame), each recovered function's shipped entry gets a jump to ours, once its
//   first bytes are checked against the shipped build's;
// - in a trace build (tables made with --trace), every other function entry gets an int3; the first execution of
//   each traps once, is written to RecompTrace.tsv, and the original byte goes back.
// Everything is logged to Recomp.log, next to the game's own log.

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"
#include "Windows/WindowsHWrapper.h"
#include "Windows/AllowWindowsPlatformTypes.h"

#include <intrin.h>
#include <stdio.h>

IMPLEMENT_MODULE( FDefaultModuleImpl, FactoryGameRecomp );

extern "C"
{
	struct FRecompModule { const char* Name; uint32 TimeDateStamp; uint32 SizeOfImage; uint8* Base; };
	struct FRecompImport { void** Slot; const char* Symbol; uint32 Module; uint32 Rva; };
	struct FRecompHook { void* Ours; const char* Name; uint32 Rva; uint32 Length; uint8 Expect[ 8 ]; };

	extern FRecompModule RecompModules[];
	extern const uint32 RecompModuleCount;
	extern const FRecompImport RecompImports[];
	extern const uint32 RecompImportCount;
	extern const FRecompHook RecompHooks[];
	extern const uint32 RecompHookCount;
	extern const uint32 RecompTraceRvas[];
	extern const uint32 RecompTraceCount;
}

namespace
{
	// ntdll's DLL notifications (documented for LdrRegisterDllNotification).
	struct FUnicodeString { USHORT Length; USHORT MaximumLength; PWSTR Buffer; };
	struct FDllNotificationData { ULONG Flags; const FUnicodeString* FullDllName; const FUnicodeString* BaseDllName; void* DllBase; ULONG SizeOfImage; };
	typedef VOID( CALLBACK* FDllNotification )( ULONG Reason, const FDllNotificationData* Data, void* Context );
	typedef LONG( NTAPI* FLdrRegisterDllNotification )( ULONG Flags, FDllNotification Callback, void* Context, void** Cookie );
	constexpr ULONG DllLoaded = 1;

	HANDLE GRecompLog = INVALID_HANDLE_VALUE;
	wchar_t GRecompLogDir[ MAX_PATH ];
	SRWLOCK GRecompLogLock = SRWLOCK_INIT;
	volatile LONG GApplied = 0;
	bool GModuleBad[ 64 ];

	// Safe under the loader lock: no allocation, no module loads.
	void RecompLog( const char* Format, ... )
	{
		if( GRecompLog == INVALID_HANDLE_VALUE )
		{
			return;
		}
		char Line[ 1024 ];
		SYSTEMTIME Now;
		GetLocalTime( &Now );
		int N = _snprintf_s( Line, _TRUNCATE, "%02d:%02d:%02d.%03d ", Now.wHour, Now.wMinute, Now.wSecond, Now.wMilliseconds );
		va_list Args;
		va_start( Args, Format );
		int M = _vsnprintf_s( Line + N, sizeof( Line ) - N - 2, _TRUNCATE, Format, Args );
		va_end( Args );
		N += M < 0 ? (int)strlen( Line + N ) : M;
		Line[ N++ ] = '\r';
		Line[ N++ ] = '\n';
		DWORD Written;
		AcquireSRWLockExclusive( &GRecompLogLock );
		WriteFile( GRecompLog, Line, N, &Written, nullptr );
		ReleaseSRWLockExclusive( &GRecompLogLock );
	}

	HANDLE OpenInLogDir( const wchar_t* Name )
	{
		wchar_t Path[ MAX_PATH ];
		_snwprintf_s( Path, _TRUNCATE, L"%s\\%s", GRecompLogDir, Name );
		return CreateFileW( Path, GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr );
	}

	// --- trace mode ---

	struct FTraceHit { uint32 Rva; uint32 Thread; int64 Ticks; volatile LONG Ready; };
	uint8* GTraceOriginal = nullptr;    // the byte under each int3
	volatile char* GTraceSeen = nullptr;
	FTraceHit* GTraceHits = nullptr;
	volatile LONG GTraceCount = 0;
	LONG GTraceFlushed = 0;
	HANDLE GTraceFile = INVALID_HANDLE_VALUE;
	int64 GTraceStart = 0, GTraceFrequency = 1;

	int32 TraceIndex( uint32 Rva )
	{
		uint32 Lo = 0, Hi = RecompTraceCount;
		while( Lo < Hi )
		{
			uint32 Mid = ( Lo + Hi ) / 2;
			if( RecompTraceRvas[ Mid ] < Rva ) Lo = Mid + 1; else Hi = Mid;
		}
		return Lo < RecompTraceCount && RecompTraceRvas[ Lo ] == Rva ? (int32)Lo : -1;
	}

	LONG CALLBACK TraceHandler( EXCEPTION_POINTERS* Info )
	{
		if( Info->ExceptionRecord->ExceptionCode != EXCEPTION_BREAKPOINT || !GTraceSeen )
		{
			return EXCEPTION_CONTINUE_SEARCH;
		}
		uint8* Base = RecompModules[ 0 ].Base;
		uint8* At = (uint8*)Info->ExceptionRecord->ExceptionAddress;
		if( !Base || At < Base || At >= Base + RecompModules[ 0 ].SizeOfImage )
		{
			return EXCEPTION_CONTINUE_SEARCH;
		}
		int32 Index = TraceIndex( (uint32)( At - Base ) );
		if( Index < 0 || GTraceOriginal[ Index ] == 0xCC )
		{
			return EXCEPTION_CONTINUE_SEARCH;
		}
		if( _InterlockedExchange8( (volatile char*)&GTraceSeen[ Index ], 1 ) == 0 )
		{
			*At = GTraceOriginal[ Index ];
			FlushInstructionCache( GetCurrentProcess(), At, 1 );
			LARGE_INTEGER Now;
			QueryPerformanceCounter( &Now );
			FTraceHit& Hit = GTraceHits[ _InterlockedIncrement( &GTraceCount ) - 1 ];
			Hit.Rva = (uint32)( At - Base );
			Hit.Thread = GetCurrentThreadId();
			Hit.Ticks = Now.QuadPart;
			_InterlockedExchange( &Hit.Ready, 1 );
		}
		else
		{
			while( *(volatile uint8*)At == 0xCC )   // another thread is putting the byte back
			{
				YieldProcessor();
			}
		}
		Info->ContextRecord->Rip = (DWORD64)At;
		return EXCEPTION_CONTINUE_EXECUTION;
	}

	void TraceFlush()
	{
		if( GTraceFile == INVALID_HANDLE_VALUE )
		{
			return;
		}
		char Buffer[ 64 * 1024 ];
		int N = 0;
		DWORD Written;
		while( GTraceFlushed < GTraceCount && GTraceHits[ GTraceFlushed ].Ready )
		{
			const FTraceHit& Hit = GTraceHits[ GTraceFlushed++ ];
			N += _snprintf_s( Buffer + N, sizeof( Buffer ) - N, _TRUNCATE, "%lld\t%u\t%08x\n",
				( Hit.Ticks - GTraceStart ) * 1000 / GTraceFrequency, Hit.Thread, Hit.Rva );
			if( N > (int)sizeof( Buffer ) - 64 )
			{
				WriteFile( GTraceFile, Buffer, N, &Written, nullptr );
				N = 0;
			}
		}
		if( N )
		{
			WriteFile( GTraceFile, Buffer, N, &Written, nullptr );
		}
		FlushFileBuffers( GTraceFile );
	}

	DWORD WINAPI TraceFlusher( void* )
	{
		for( ;; )
		{
			Sleep( 1000 );
			TraceFlush();
		}
	}

	struct FTraceAtExit { ~FTraceAtExit() { TraceFlush(); } } GTraceAtExit;

	void PlaceTrace( uint8* Base )
	{
		// The code sections stay writable: each first execution puts its byte back.
		IMAGE_NT_HEADERS* Nt = (IMAGE_NT_HEADERS*)( Base + ( (IMAGE_DOS_HEADER*)Base )->e_lfanew );
		IMAGE_SECTION_HEADER* Section = IMAGE_FIRST_SECTION( Nt );
		for( WORD i = 0; i < Nt->FileHeader.NumberOfSections; ++i, ++Section )
		{
			if( Section->Characteristics & IMAGE_SCN_MEM_EXECUTE )
			{
				DWORD Old;
				VirtualProtect( Base + Section->VirtualAddress, Section->Misc.VirtualSize, PAGE_EXECUTE_READWRITE, &Old );
			}
		}
		uint32 Placed = 0;
		for( uint32 i = 0; i < RecompTraceCount; ++i )
		{
			uint8* At = Base + RecompTraceRvas[ i ];
			GTraceOriginal[ i ] = *At;
			if( GTraceSeen[ i ] )   // a hook's jump: leave it
			{
				continue;
			}
			*At = 0xCC;
			++Placed;
		}
		FlushInstructionCache( GetCurrentProcess(), nullptr, 0 );
		RecompLog( "trace: %u function entries armed", Placed );
	}

	// --- hooks ---

	uint8* AllocateNear( uint8* Base, SIZE_T Size )
	{
		SYSTEM_INFO System;
		GetSystemInfo( &System );
		const uintptr_t Step = System.dwAllocationGranularity;
		for( uintptr_t Distance = Step; Distance < 0x70000000; Distance += Step )
		{
			uintptr_t Below = ( (uintptr_t)Base - Distance ) & ~( Step - 1 );
			if( void* P = VirtualAlloc( (void*)Below, Size, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE ) )
			{
				return (uint8*)P;
			}
		}
		return nullptr;
	}

	void ApplyHooks( uint8* Base )
	{
		if( !RecompHookCount )
		{
			return;
		}
		uint8* Trampolines = AllocateNear( Base, RecompHookCount * 16 );
		if( !Trampolines )
		{
			RecompLog( "error: no memory within 2 GB of %s for the trampolines; nothing hooked", RecompModules[ 0 ].Name );
			return;
		}
		for( uint32 i = 0; i < RecompHookCount; ++i )
		{
			const FRecompHook& Hook = RecompHooks[ i ];
			uint8* Entry = Base + Hook.Rva;
			if( memcmp( Entry, Hook.Expect, Hook.Length < 8 ? Hook.Length : 8 ) != 0 )
			{
				RecompLog( "skipped %s: its first bytes are not the shipped build's (%02x %02x %02x %02x)", Hook.Name, Entry[ 0 ], Entry[ 1 ], Entry[ 2 ], Entry[ 3 ] );
				continue;
			}
			uint8* Trampoline = Trampolines + 16 * i;   // jmp [rip+0]; dq ours
			Trampoline[ 0 ] = 0xFF;
			Trampoline[ 1 ] = 0x25;
			*(uint32*)( Trampoline + 2 ) = 0;
			*(void**)( Trampoline + 6 ) = Hook.Ours;
			DWORD Old;
			VirtualProtect( Entry, 5, PAGE_EXECUTE_READWRITE, &Old );
			Entry[ 0 ] = 0xE9;
			*(int32*)( Entry + 1 ) = (int32)( Trampoline - ( Entry + 5 ) );
			VirtualProtect( Entry, 5, Old, &Old );
			if( GTraceSeen )
			{
				int32 Index = TraceIndex( Hook.Rva );
				if( Index >= 0 )
				{
					GTraceSeen[ Index ] = 1;   // not traced: ours runs there
				}
			}
			_InterlockedIncrement( &GApplied );
			RecompLog( "hooked %s (+%08x, %u bytes)", Hook.Name, Hook.Rva, Hook.Length );
		}
		FlushInstructionCache( GetCurrentProcess(), nullptr, 0 );
		RecompLog( "%ld of %u FactoryGame functions hooked", GApplied, RecompHookCount );
	}

	void OnMapped( uint32 Module, uint8* Base )
	{
		FRecompModule& M = RecompModules[ Module ];
		if( M.Base || GModuleBad[ Module ] )
		{
			return;
		}
		IMAGE_NT_HEADERS* Nt = (IMAGE_NT_HEADERS*)( Base + ( (IMAGE_DOS_HEADER*)Base )->e_lfanew );
		if( Nt->FileHeader.TimeDateStamp != M.TimeDateStamp || Nt->OptionalHeader.SizeOfImage != M.SizeOfImage )
		{
			GModuleBad[ Module ] = true;
			RecompLog( "error: %s is not the build the tables were made from (timestamp %08x, expected %08x); its slots stay empty%s",
				M.Name, Nt->FileHeader.TimeDateStamp, M.TimeDateStamp, Module == 0 ? " and nothing is hooked" : "" );
			return;
		}
		M.Base = Base;
		uint32 Resolved = 0;
		for( uint32 i = 0; i < RecompImportCount; ++i )
		{
			if( RecompImports[ i ].Module == Module )
			{
				*RecompImports[ i ].Slot = Base + RecompImports[ i ].Rva;
				++Resolved;
			}
		}
		RecompLog( "%s mapped at %p: %u slots filled", M.Name, Base, Resolved );
		if( Module == 0 )
		{
			ApplyHooks( Base );
			if( GTraceSeen )
			{
				PlaceTrace( Base );
			}
		}
	}

	VOID CALLBACK OnDllNotification( ULONG Reason, const FDllNotificationData* Data, void* )
	{
		if( Reason != DllLoaded || !Data->BaseDllName )
		{
			return;
		}
		for( uint32 i = 0; i < RecompModuleCount && i < 64; ++i )
		{
			const char* Name = RecompModules[ i ].Name;
			const FUnicodeString& Loaded = *Data->BaseDllName;
			USHORT Length = Loaded.Length / sizeof( wchar_t );
			bool Same = strlen( Name ) == Length;
			for( USHORT c = 0; Same && c < Length; ++c )
			{
				Same = towlower( Loaded.Buffer[ c ] ) == (wchar_t)tolower( (unsigned char)Name[ c ] );
			}
			if( Same )
			{
				OnMapped( i, (uint8*)Data->DllBase );
			}
		}
	}
}

// Called once, by FactoryPreEarlyLoadingScreen. Returns the number of FactoryGame functions to hook.
extern "C" __declspec( dllexport ) int32 RecompInstall()
{
	DWORD N = GetEnvironmentVariableW( L"LOCALAPPDATA", GRecompLogDir, MAX_PATH );
	if( N && N < MAX_PATH - 40 )
	{
		wcscat_s( GRecompLogDir, L"\\FactoryGame\\Saved\\Logs" );
		CreateDirectoryW( GRecompLogDir, nullptr );
		GRecompLog = OpenInLogDir( L"Recomp.log" );
	}
	RecompLog( "recomp hook loader: %u modules, %u import slots, %u hooks, trace table of %u functions",
		RecompModuleCount, RecompImportCount, RecompHookCount, RecompTraceCount );

	if( RecompTraceCount )
	{
		GTraceOriginal = (uint8*)VirtualAlloc( nullptr, RecompTraceCount, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE );
		GTraceHits = (FTraceHit*)VirtualAlloc( nullptr, sizeof( FTraceHit ) * RecompTraceCount, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE );
		volatile char* Seen = (volatile char*)VirtualAlloc( nullptr, RecompTraceCount, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE );
		LARGE_INTEGER Value;
		QueryPerformanceFrequency( &Value );
		GTraceFrequency = Value.QuadPart;
		QueryPerformanceCounter( &Value );
		GTraceStart = Value.QuadPart;
		GTraceFile = OpenInLogDir( L"RecompTrace.tsv" );
		if( GTraceOriginal && GTraceHits && Seen && GTraceFile != INVALID_HANDLE_VALUE )
		{
			char Header[ 256 ];
			DWORD Written;
			int H = _snprintf_s( Header, _TRUNCATE, "# first execution of each function of %s (timestamp %08x): ms since install, thread, rva\n",
				RecompModules[ 0 ].Name, RecompModules[ 0 ].TimeDateStamp );
			WriteFile( GTraceFile, Header, H, &Written, nullptr );
			GTraceSeen = Seen;
			AddVectoredExceptionHandler( 1, TraceHandler );
			CloseHandle( CreateThread( nullptr, 0, TraceFlusher, nullptr, 0, nullptr ) );
			RecompLog( "trace: writing %ls\\RecompTrace.tsv", GRecompLogDir );
		}
		else
		{
			RecompLog( "error: trace mode could not start" );
		}
	}

	auto Register = (FLdrRegisterDllNotification)(void*)GetProcAddress( GetModuleHandleW( L"ntdll.dll" ), "LdrRegisterDllNotification" );
	void* Cookie = nullptr;
	if( !Register || Register( 0, OnDllNotification, nullptr, &Cookie ) != 0 )
	{
		RecompLog( "error: LdrRegisterDllNotification unavailable; nothing will be hooked" );
		return 0;
	}
	for( uint32 i = 0; i < RecompModuleCount && i < 64; ++i )   // modules mapped already
	{
		if( HMODULE Already = GetModuleHandleA( RecompModules[ i ].Name ) )
		{
			OnMapped( i, (uint8*)Already );
		}
	}
	return (int32)RecompHookCount;
}

extern "C" __declspec( dllexport ) int32 RecompTraceFunctions()
{
	return GTraceSeen ? (int32)RecompTraceCount : 0;
}

#include "Windows/HideWindowsPlatformTypes.h"
