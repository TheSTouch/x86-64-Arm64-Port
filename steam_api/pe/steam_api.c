/*
 * steam_api64.dll for Wine: the Steamworks API a Windows game imports, without Windows Steam.
 *
 * This file is the part every build shares: the exported functions, the interface objects the
 * game calls through their vtables (laid out by vtables.h, generated from the SDK headers), the
 * callback registry, and a log of every interface and method the game uses. Each vtable slot
 * starts as a stub that logs its first call and answers "nothing" (0, false, NULL, an empty
 * struct); bridge.c replaces the slots that answer for real.
 *
 * Two builds (portkit/kettle.py):
 *  - the bridge (bridge.c): SteamAPI_Init signs in through Kettle on the Mac side (a Wine unixlib,
 *    steam_api/unix) and checks the license; it fails without a sign-in or a license;
 *  - the inventory (-DSTEAM_API_INVENTORY): no Steam at all. SteamAPI_Init succeeds so the game
 *    asks for everything it uses, every answer is "nothing" (no SteamID, not logged on, no
 *    license), and the log is the list of what a bridge has to provide.
 *
 * Microsoft x64 ABI: `this` comes first, in rcx; a method that returns a struct writes it through
 * a hidden pointer that follows `this` and returns that pointer.
 */
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "steam_api.h"

#define EXPORT __declspec(dllexport)

static void stub_called(int iface, int slot);
#define SA_STUB_INT(i, s) \
    static uint64_t sa_stub_##i##_##s(void *self) { (void)self; stub_called(i, s); return 0; }
#define SA_STUB_STRING(i, s) \
    static const char *sa_stub_##i##_##s(void *self) { (void)self; stub_called(i, s); return ""; }
#define SA_STUB_FLOAT(i, s) \
    static float sa_stub_##i##_##s(void *self) { (void)self; stub_called(i, s); return 0; }
#define SA_STUB_DOUBLE(i, s) \
    static double sa_stub_##i##_##s(void *self) { (void)self; stub_called(i, s); return 0; }
#define SA_STUB_RECORD(i, s) \
    static void *sa_stub_##i##_##s(void *self, void *ret) \
    { (void)self; stub_called(i, s); memset(ret, 0, sa_interfaces[i].methods[s].size); return ret; }
#include "vtables.h"

volatile bool sa_inited;

/* --- the log ------------------------------------------------------------------------------- */

static CRITICAL_SECTION log_lock;
static HANDLE log_out = INVALID_HANDLE_VALUE;

static void open_log(void)
{
    char path[MAX_PATH];
    DWORD n = GetEnvironmentVariableA("PORTKIT_STEAM_LOG", path, sizeof path);
    if (n && n < sizeof path)
        log_out = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, NULL);
    if (log_out == INVALID_HANDLE_VALUE) log_out = GetStdHandle(STD_ERROR_HANDLE);
}

void sa_log(const char *format, ...)
{
    char line[1024];
    int n = snprintf(line, sizeof line, "[steam_api] ");
    va_list ap;
    va_start(ap, format);
    int m = vsnprintf(line + n, sizeof line - n - 1, format, ap);
    va_end(ap);
    n = m < 0 ? n : (n + m > (int)sizeof line - 2 ? (int)sizeof line - 2 : n + m);
    line[n++] = '\n';
    DWORD written;
    EnterCriticalSection(&log_lock);
    if (log_out && log_out != INVALID_HANDLE_VALUE) WriteFile(log_out, line, n, &written, NULL);
    LeaveCriticalSection(&log_lock);
}

const char *sa_callback_name(int ident)
{
    for (int i = 0; i < SA_CALLBACK_COUNT; i++)
        if (sa_callbacks[i].id == ident) return sa_callbacks[i].name;
    return "?";
}

/* --- interfaces --------------------------------------------------------------------------- */

static void *vtables[SA_INTERFACE_COUNT][SA_MAX_SLOTS];
static struct { void **vtbl; } objects[SA_INTERFACE_COUNT];
static volatile LONG calls[SA_INTERFACE_COUNT][SA_MAX_SLOTS];
static volatile LONG asked[SA_INTERFACE_COUNT];
static bool overridden[SA_INTERFACE_COUNT][SA_MAX_SLOTS];

void sa_seen(int iface, int slot)
{
    if (InterlockedIncrement(&calls[iface][slot]) == 1)
        sa_log("call %s::%s (%s slot %d)%s", sa_interfaces[iface].klass, sa_interfaces[iface].methods[slot].name,
               sa_interfaces[iface].version, slot, overridden[iface][slot] ? "" : ", stub");
}

static void stub_called(int iface, int slot) { sa_seen(iface, slot); }

void sa_override(int iface, int slot, void *fn)
{
    vtables[iface][slot] = fn;
    overridden[iface][slot] = true;
}

static void build_interfaces(void)
{
    for (int i = 0; i < SA_INTERFACE_COUNT; i++) {
        for (int s = 0; s < sa_interfaces[i].count; s++) vtables[i][s] = sa_stubs[i][s];
        objects[i].vtbl = vtables[i];
    }
}

void *sa_interface_for(const char *version, const char *how)
{
    for (int i = 0; i < SA_INTERFACE_COUNT; i++)
        if (version && !strcmp(sa_interfaces[i].version, version)) {
            if (InterlockedIncrement(&asked[i]) == 1) sa_log("interface %s (%s)", version, how);
            return &objects[i];
        }
    sa_log("interface %s (%s): not provided", version ? version : "(null)", how);
    return NULL;
}

/* --- callbacks ---------------------------------------------------------------------------- */

/* CCallbackBase as MSVC lays it out: the two Run overloads are in reverse order of declaration. */
typedef struct {
    void **vtbl;                  /* [0] Run(param, io_failure, call), [1] Run(param), [2] GetCallbackSizeBytes() */
    uint8_t flags;                /* 1 registered, 2 game server */
    int32_t ident;
} CCallbackBase;
enum { FLAG_REGISTERED = 1, FLAG_GAMESERVER = 2 };
typedef void (*RunCallback)(CCallbackBase *self, void *param);
typedef void (*RunCallResult)(CCallbackBase *self, void *param, bool io_failure, uint64_t call);

#define MAX_REGISTERED 1024
static CRITICAL_SECTION cb_lock;
static CCallbackBase *registered[MAX_REGISTERED];
static int nregistered;
static struct { CCallbackBase *cb; uint64_t call; } results[MAX_REGISTERED];
static int nresults;

EXPORT void SteamAPI_RegisterCallback(CCallbackBase *cb, int ident)
{
    if (!cb) return;
    sa_log("register callback %d %s%s", ident, sa_callback_name(ident), cb->flags & FLAG_GAMESERVER ? " (game server)" : "");
    EnterCriticalSection(&cb_lock);
    cb->flags |= FLAG_REGISTERED;
    cb->ident = ident;
    if (nregistered < MAX_REGISTERED) registered[nregistered++] = cb;
    LeaveCriticalSection(&cb_lock);
}

EXPORT void SteamAPI_UnregisterCallback(CCallbackBase *cb)
{
    EnterCriticalSection(&cb_lock);
    for (int i = 0; i < nregistered; i++)
        if (registered[i] == cb) registered[i--] = registered[--nregistered];
    if (cb) cb->flags &= ~FLAG_REGISTERED;
    LeaveCriticalSection(&cb_lock);
}

EXPORT void SteamAPI_RegisterCallResult(CCallbackBase *cb, uint64_t call)
{
    if (!cb || !call) return;
    EnterCriticalSection(&cb_lock);
    if (nresults < MAX_REGISTERED) {
        results[nresults].cb = cb;
        results[nresults++].call = call;
    }
    LeaveCriticalSection(&cb_lock);
}

EXPORT void SteamAPI_UnregisterCallResult(CCallbackBase *cb, uint64_t call)
{
    EnterCriticalSection(&cb_lock);
    for (int i = 0; i < nresults; i++)
        if (results[i].cb == cb && results[i].call == call) results[i--] = results[--nresults];
    LeaveCriticalSection(&cb_lock);
}

/* On the game's thread. The game may register or unregister from inside a callback, so the
 * objects to run are copied first, and each is checked again before it runs. */
void sa_dispatch(int ident, void *data, int size, uint64_t call, bool io_failure)
{
    (void)size;
    static CCallbackBase *run[MAX_REGISTERED];
    int n = 0;
    EnterCriticalSection(&cb_lock);
    if (call) {
        for (int i = 0; i < nresults; i++)
            if (results[i].call == call) {
                run[n++] = results[i].cb;
                results[i--] = results[--nresults];   /* a call result runs once */
            }
        LeaveCriticalSection(&cb_lock);
        for (int i = 0; i < n; i++) ((RunCallResult)run[i]->vtbl[0])(run[i], data, io_failure, call);
        return;
    }
    for (int i = 0; i < nregistered; i++)
        if (registered[i]->ident == ident && !(registered[i]->flags & FLAG_GAMESERVER)) run[n++] = registered[i];
    LeaveCriticalSection(&cb_lock);
    for (int i = 0; i < n; i++) {
        bool still = false;
        EnterCriticalSection(&cb_lock);
        for (int j = 0; j < nregistered && !still; j++) still = registered[j] == run[i];
        LeaveCriticalSection(&cb_lock);
        if (still) ((RunCallback)run[i]->vtbl[1])(run[i], data);
    }
}

/* --- ISteamClient: plumbing, the same in every build ---------------------------------------- */

#define CLIENT(name) sa_seen(SA_IF_SteamClient020, SA_SteamClient020_##name)
static int32_t client_CreateSteamPipe(void *self) { (void)self; CLIENT(CreateSteamPipe); return 1; }
static bool client_BReleaseSteamPipe(void *self, int32_t pipe) { (void)self; (void)pipe; CLIENT(BReleaseSteamPipe); return true; }
static int32_t client_ConnectToGlobalUser(void *self, int32_t pipe) { (void)self; (void)pipe; CLIENT(ConnectToGlobalUser); return 1; }
static void client_ReleaseUser(void *self, int32_t pipe, int32_t user) { (void)self; (void)pipe; (void)user; CLIENT(ReleaseUser); }
static uint32_t client_GetIPCCallCount(void *self) { (void)self; CLIENT(GetIPCCallCount); return 0; }
static bool client_BShutdownIfAllPipesClosed(void *self) { (void)self; CLIENT(BShutdownIfAllPipesClosed); return false; }

/* Every GetISteamX(user, pipe, version) of ISteamClient: the interface the version names. */
#define CLIENT_GETTER(name) \
    static void *client_##name(void *self, int32_t user, int32_t pipe, const char *version) \
    { (void)self; (void)user; (void)pipe; CLIENT(name); return sa_inited ? sa_interface_for(version, "ISteamClient::" #name) : NULL; }
CLIENT_GETTER(GetISteamUser)
CLIENT_GETTER(GetISteamGameServer)
CLIENT_GETTER(GetISteamFriends)
CLIENT_GETTER(GetISteamMatchmaking)
CLIENT_GETTER(GetISteamMatchmakingServers)
CLIENT_GETTER(GetISteamGenericInterface)
CLIENT_GETTER(GetISteamUserStats)
CLIENT_GETTER(GetISteamGameServerStats)
CLIENT_GETTER(GetISteamApps)
CLIENT_GETTER(GetISteamNetworking)
CLIENT_GETTER(GetISteamRemoteStorage)
CLIENT_GETTER(GetISteamInput)
/* GetISteamUtils(pipe, version) has no user. */
static void *client_GetISteamUtils(void *self, int32_t pipe, const char *version)
{
    (void)self; (void)pipe; CLIENT(GetISteamUtils);
    return sa_inited ? sa_interface_for(version, "ISteamClient::GetISteamUtils") : NULL;
}

static void override_client(void)
{
#define SET(name) sa_override(SA_IF_SteamClient020, SA_SteamClient020_##name, (void *)client_##name)
    SET(CreateSteamPipe); SET(BReleaseSteamPipe); SET(ConnectToGlobalUser); SET(ReleaseUser);
    SET(GetIPCCallCount); SET(BShutdownIfAllPipesClosed);
    SET(GetISteamUser); SET(GetISteamGameServer); SET(GetISteamFriends); SET(GetISteamUtils);
    SET(GetISteamMatchmaking); SET(GetISteamMatchmakingServers); SET(GetISteamGenericInterface);
    SET(GetISteamUserStats); SET(GetISteamGameServerStats); SET(GetISteamApps); SET(GetISteamNetworking);
    SET(GetISteamRemoteStorage); SET(GetISteamInput);
#undef SET
}

/* --- the exports -------------------------------------------------------------------------- */

static volatile LONG generation = 1;   /* bumped at Init and Shutdown: accessors fetch again */
static volatile LONG exported[32];

static void seen_export(int which, const char *name)
{
    if (InterlockedIncrement(&exported[which]) == 1) sa_log("export %s", name);
}
#define SEEN(which) seen_export(which, __func__)

EXPORT bool SteamAPI_Init(void)
{
    SEEN(0);
    if (sa_inited) return true;
    if (!sa_bridge_init()) {
        sa_log("SteamAPI_Init: failed");
        return false;
    }
    sa_inited = true;
    InterlockedIncrement(&generation);
    sa_log("SteamAPI_Init: ok");
    return true;
}
EXPORT bool SteamAPI_InitSafe(void) { SEEN(1); return SteamAPI_Init(); }
/* ESteamAPIInitResult: 0 OK, 1 failed generic, 2 no Steam client. */
EXPORT int SteamInternal_SteamAPI_Init(const char *versions, char *message)
{
    (void)versions;
    SEEN(2);
    bool ok = SteamAPI_Init();
    if (message) snprintf(message, 1024, "%s", ok ? "" : "No Steam sign-in or no license for this game (./port steam signin)");
    return ok ? 0 : 1;
}

EXPORT void SteamAPI_Shutdown(void)
{
    SEEN(3);
    if (!sa_inited) return;
    sa_bridge_shutdown();
    sa_inited = false;
    InterlockedIncrement(&generation);
}

EXPORT bool SteamAPI_RestartAppIfNecessary(uint32_t app)
{
    SEEN(4);
    sa_log("SteamAPI_RestartAppIfNecessary(%u): no", app);
    return false;
}
EXPORT bool SteamAPI_IsSteamRunning(void) { SEEN(5); return sa_inited; }

EXPORT void SteamAPI_RunCallbacks(void)
{
    SEEN(6);
    if (sa_inited) sa_bridge_run_callbacks();
}

EXPORT int32_t SteamAPI_GetHSteamUser(void) { SEEN(7); return sa_inited ? 1 : 0; }
EXPORT int32_t SteamAPI_GetHSteamPipe(void) { SEEN(8); return sa_inited ? 1 : 0; }
EXPORT void SteamAPI_ReleaseCurrentThreadMemory(void) { SEEN(9); }

/* The SDK's inline accessors keep { init function, counter, context } and want the context back,
 * filled by the init function (which calls the FindOrCreate functions below) when stale. */
struct context_init { void (*init)(void *ctx); uintptr_t counter; void *ctx; };
EXPORT void *SteamInternal_ContextInit(struct context_init *data)
{
    SEEN(10);
    uintptr_t g = (uintptr_t)generation;
    if (data->counter != g) {
        data->init(&data->ctx);
        data->counter = g;
    }
    return &data->ctx;
}

EXPORT void *SteamInternal_CreateInterface(const char *version)
{
    SEEN(11);
    return sa_interface_for(version, "SteamInternal_CreateInterface");
}

EXPORT void *SteamInternal_FindOrCreateUserInterface(int32_t user, const char *version)
{
    SEEN(12);
    /* The user handle is not checked: Satisfactory's modules pass 0 (they never import
     * SteamAPI_GetHSteamUser), and Valve's DLL serves them anyway. */
    (void)user;
    if (!sa_inited) {
        sa_log("interface %s (FindOrCreateUserInterface): Steam not initialized", version ? version : "(null)");
        return NULL;
    }
    return sa_interface_for(version, "FindOrCreateUserInterface");
}

/* Game servers (hosting through Steam) come later: no game server, ever. */
EXPORT void *SteamInternal_FindOrCreateGameServerInterface(int32_t user, const char *version)
{
    SEEN(13);
    sa_log("interface %s (FindOrCreateGameServerInterface, user %d): no game server", version ? version : "(null)", user);
    return NULL;
}
EXPORT bool SteamInternal_GameServer_Init(uint32_t ip, uint16_t port, uint16_t game_port, uint16_t query_port,
                                         int mode, const char *version)
{
    (void)ip; (void)port;
    SEEN(14);
    sa_log("SteamInternal_GameServer_Init(game port %u, query port %u, mode %d, version %s): not provided",
           game_port, query_port, mode, version ? version : "");
    return false;
}
EXPORT int32_t SteamGameServer_GetHSteamUser(void) { SEEN(15); return 0; }
EXPORT int32_t SteamGameServer_GetHSteamPipe(void) { SEEN(16); return 0; }
EXPORT void SteamGameServer_RunCallbacks(void) { SEEN(17); }
EXPORT void SteamGameServer_Shutdown(void) { SEEN(18); }

/* --- loading ------------------------------------------------------------------------------ */

static void summary(void)
{
    sa_log("summary: methods called (count):");
    for (int i = 0; i < SA_INTERFACE_COUNT; i++)
        for (int s = 0; s < sa_interfaces[i].count; s++)
            if (calls[i][s])
                sa_log("  %s::%s %ld%s", sa_interfaces[i].klass, sa_interfaces[i].methods[s].name, calls[i][s],
                       overridden[i][s] ? "" : " (stub)");
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, void *reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        InitializeCriticalSection(&log_lock);
        InitializeCriticalSection(&cb_lock);
        open_log();
        build_interfaces();
        override_client();
        sa_bridge_load();
    } else if (reason == DLL_PROCESS_DETACH) {
        summary();
    }
    return TRUE;
}

#ifdef STEAM_API_INVENTORY
/* No Steam: Init succeeds so the game asks for what it uses; every method answers "nothing". */
void sa_bridge_load(void) { sa_log("inventory build: no Steam, every method answers nothing"); }
bool sa_bridge_init(void) { return true; }
void sa_bridge_shutdown(void) {}
void sa_bridge_run_callbacks(void) {}
#endif
