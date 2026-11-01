/*
 * The bridge build of steam_api64.dll: the methods that answer for real, through the Mac side
 * (steam_api/unix/bridge.m, a Wine unixlib) where Kettle signs in and checks the license.
 *
 * Real: SteamAPI_Init (sign-in and license), callbacks and call results, ISteamUser (SteamID,
 * logged on, auth and encrypted app tickets), ISteamApps (ownership from the signed ticket, DLC),
 * ISteamUtils (app id, Steam's time, call results), and the player's own name in ISteamFriends.
 * Everything else is steam_api.c's stub: "nothing", logged at the first call.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "steam_api.h"
#include "../bridge.h"
#include "slots.h"

typedef LONG NTSTATUS;
typedef NTSTATUS (WINAPI *unix_dispatcher)(uint64_t handle, unsigned int code, void *args);
typedef NTSTATUS (WINAPI *query_memory)(HANDLE, void *, int, void *, SIZE_T, SIZE_T *);
enum { MemoryWineUnixFuncs = 1000 };

static uint64_t unix_handle;
static unix_dispatcher unix_call_dispatcher;
static uint32_t app_id;
static ULONGLONG started_at;

static NTSTATUS unix_call(enum sa_unix_call code, void *args)
{
    return unix_call_dispatcher ? unix_call_dispatcher(unix_handle, code, args) : (NTSTATUS)0xC0000135;
}

static uint64_t call0(enum sa_unix_call code)
{
    sa_args a = {0};
    unix_call(code, &a);
    return a.ret;
}

static uint64_t call(enum sa_unix_call code, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
{
    sa_args a = {{a0, a1, a2, a3, a4}, 0};
    unix_call(code, &a);
    return a.ret;
}

/* Wine gives a builtin DLL its unixlib through NtQueryVirtualMemory(MemoryWineUnixFuncs), as
 * winecrt0's __wine_init_unix_call does; the calls go through ntdll's __wine_unix_call_dispatcher. */
static bool find_unix_side(void)
{
    HMODULE ntdll = GetModuleHandleA("ntdll.dll");
    query_memory query = (query_memory)(void *)GetProcAddress(ntdll, "NtQueryVirtualMemory");
    void **dispatcher = (void **)(void *)GetProcAddress(ntdll, "__wine_unix_call_dispatcher");
    HMODULE self = NULL;
    GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (const char *)(void *)find_unix_side, &self);
    if (!query || !dispatcher || !self) return false;
    if (query(GetCurrentProcess(), self, MemoryWineUnixFuncs, &unix_handle, sizeof unix_handle, NULL)) return false;
    unix_call_dispatcher = (unix_dispatcher)*dispatcher;
    return unix_handle != 0;
}

/* $SteamAppId (what Steam and ./port set), else steam_appid.txt in the working folder. */
static uint32_t find_app_id(void)
{
    char buf[32];
    DWORD n = GetEnvironmentVariableA("SteamAppId", buf, sizeof buf);
    if (n && n < sizeof buf && atoi(buf) > 0) return (uint32_t)atoi(buf);
    FILE *f = fopen("steam_appid.txt", "r");
    unsigned app = 0;
    if (f) {
        if (fscanf(f, "%u", &app) != 1) app = 0;
        fclose(f);
    }
    return app;
}

static void bind_methods(void);

void sa_bridge_load(void)
{
    bind_methods();
    if (!find_unix_side()) sa_log("the Mac side (x86_64-unix/steam_api64.so) did not load: SteamAPI_Init will fail");
}

bool sa_bridge_init(void)
{
    if (!unix_call_dispatcher) return false;
    sa_init_args a = {0};
    a.app = app_id = find_app_id();
    char game[64];
    DWORD n = GetEnvironmentVariableA("PORTKIT_GAME_NAME", game, sizeof game);
    snprintf(a.game, sizeof a.game, "%s", n && n < sizeof game ? game : "");
    unix_call(SA_CALL_INIT, &a);
    if (a.result != SA_INIT_OK) {
        sa_log("SteamAPI_Init: %s", a.message);
        char quiet[4];
        if (!GetEnvironmentVariableA("PORTKIT_STEAM_NO_DIALOG", quiet, sizeof quiet))
            MessageBoxA(NULL, a.message, "Steam (Porting Kit)", MB_OK | MB_ICONWARNING | MB_SETFOREGROUND);
        return false;
    }
    started_at = GetTickCount64();
    sa_log("SteamAPI_Init: signed in, app %u owned", app_id);
    return true;
}

void sa_bridge_shutdown(void) { unix_call(SA_CALL_SHUTDOWN, NULL); }

void sa_bridge_run_callbacks(void)
{
    static uint8_t data[4096];
    for (int i = 0; i < 64; i++) {   /* a frame's worth; the rest waits for the next frame */
        sa_callback_args c = {(uint64_t)(uintptr_t)data, sizeof data, 0, 0, 0, 0};
        if (unix_call(SA_CALL_NEXT_CALLBACK, &c)) break;
        sa_dispatch(c.ident, data, (int)c.length, c.call, c.io_failure != 0);
    }
}

/* --- ISteamUser ----------------------------------------------------------------------------- */

#define USER(name) sa_seen(SA_IF_SteamUser023, SA_SteamUser023_##name)
static int32_t user_GetHSteamUser(void *self) { (void)self; USER(GetHSteamUser); return 1; }
static bool user_BLoggedOn(void *self) { (void)self; USER(BLoggedOn); return call0(SA_CALL_LOGGED_ON) != 0; }
static uint64_t *user_GetSteamID(void *self, uint64_t *ret) { (void)self; USER(GetSteamID); *ret = call0(SA_CALL_STEAMID); return ret; }
static bool user_BIsBehindNAT(void *self) { (void)self; USER(BIsBehindNAT); return true; }
static uint32_t user_GetAuthSessionTicket(void *self, void *ticket, int max, uint32_t *length, const void *identity)
{
    (void)self; (void)identity;
    USER(GetAuthSessionTicket);
    uint32_t n = 0;
    uint32_t h = (uint32_t)call(SA_CALL_AUTH_TICKET, (uintptr_t)ticket, (uint64_t)(max > 0 ? max : 0), (uintptr_t)&n, 0, 0);
    if (length) *length = n;
    return h;
}
static void user_CancelAuthTicket(void *self, uint32_t h) { (void)self; USER(CancelAuthTicket); call(SA_CALL_CANCEL_TICKET, h, 0, 0, 0, 0); }
static int user_BeginAuthSession(void *self, const void *ticket, int length, uint64_t steamid)
{
    (void)self;
    USER(BeginAuthSession);
    return (int)call(SA_CALL_BEGIN_AUTH, (uintptr_t)ticket, (uint64_t)(length > 0 ? length : 0), steamid, 0, 0);
}
static void user_EndAuthSession(void *self, uint64_t steamid) { (void)self; USER(EndAuthSession); call(SA_CALL_END_AUTH, steamid, 0, 0, 0, 0); }
static int user_UserHasLicenseForApp(void *self, uint64_t steamid, uint32_t app)
{
    (void)self;
    USER(UserHasLicenseForApp);
    return (int)call(SA_CALL_HAS_LICENSE, steamid, app, 0, 0, 0);
}
static uint64_t user_RequestEncryptedAppTicket(void *self, void *data, int length)
{
    (void)self;
    USER(RequestEncryptedAppTicket);
    return call(SA_CALL_REQUEST_APP_TICKET, (uintptr_t)data, (uint64_t)(length > 0 ? length : 0), 0, 0, 0);
}
static bool user_GetEncryptedAppTicket(void *self, void *ticket, int max, uint32_t *length)
{
    (void)self;
    USER(GetEncryptedAppTicket);
    return call(SA_CALL_GET_APP_TICKET, (uintptr_t)ticket, (uint64_t)(max > 0 ? max : 0), (uintptr_t)length, 0, 0) != 0;
}

/* --- ISteamApps ------------------------------------------------------------------------------ */

#define APPS(name) sa_seen(SA_IF_STEAMAPPS_INTERFACE_VERSION008, SA_STEAMAPPS_INTERFACE_VERSION008_##name)
static bool owns(uint32_t app) { return call(SA_CALL_OWNS_APP, app, 0, 0, 0, 0) != 0; }
static bool apps_BIsSubscribed(void *self) { (void)self; APPS(BIsSubscribed); return owns(app_id); }
static bool apps_BIsSubscribedApp(void *self, uint32_t app) { (void)self; APPS(BIsSubscribedApp); return owns(app); }
static bool apps_BIsDlcInstalled(void *self, uint32_t app) { (void)self; APPS(BIsDlcInstalled); return owns(app); }
static bool apps_BIsAppInstalled(void *self, uint32_t app) { (void)self; APPS(BIsAppInstalled); return app == app_id; }
static const char *apps_GetCurrentGameLanguage(void *self) { (void)self; APPS(GetCurrentGameLanguage); return "english"; }
static const char *apps_GetAvailableGameLanguages(void *self) { (void)self; APPS(GetAvailableGameLanguages); return "english"; }
static int apps_GetDLCCount(void *self) { (void)self; APPS(GetDLCCount); return (int)call0(SA_CALL_DLC_COUNT); }
static bool apps_BGetDLCDataByIndex(void *self, int index, uint32_t *app, bool *available, char *name, int size)
{
    (void)self;
    APPS(BGetDLCDataByIndex);
    return call(SA_CALL_DLC_BY_INDEX, (uint64_t)index, (uintptr_t)app, (uintptr_t)available, (uintptr_t)name,
                (uint64_t)(size > 0 ? size : 0)) != 0;
}
static uint64_t *apps_GetAppOwner(void *self, uint64_t *ret) { (void)self; APPS(GetAppOwner); *ret = call0(SA_CALL_APP_OWNER); return ret; }
static bool apps_BIsSubscribedFromFamilySharing(void *self)
{
    (void)self;
    APPS(BIsSubscribedFromFamilySharing);
    uint64_t owner = call0(SA_CALL_APP_OWNER);
    return owner && owner != call0(SA_CALL_STEAMID);
}
/* The game's folder ($PORTKIT_GAME_DIR, a Windows path set by ./port), for the game itself. */
static uint32_t apps_GetAppInstallDir(void *self, uint32_t app, char *folder, uint32_t size)
{
    (void)self;
    APPS(GetAppInstallDir);
    char dir[MAX_PATH];
    DWORD n = app == app_id ? GetEnvironmentVariableA("PORTKIT_GAME_DIR", dir, sizeof dir) : 0;
    if (!n || n >= sizeof dir) return 0;
    if (folder && size) snprintf(folder, size, "%s", dir);
    return n;
}
static int apps_GetAppBuildId(void *self)
{
    (void)self;
    APPS(GetAppBuildId);
    char buf[16];
    DWORD n = GetEnvironmentVariableA("PORTKIT_GAME_BUILDID", buf, sizeof buf);
    return n && n < sizeof buf ? atoi(buf) : 0;
}

/* --- ISteamUtils ----------------------------------------------------------------------------- */

#define UTILS(name) sa_seen(SA_IF_SteamUtils010, SA_SteamUtils010_##name)
static uint32_t utils_GetSecondsSinceAppActive(void *self)
{
    (void)self;
    UTILS(GetSecondsSinceAppActive);
    return started_at ? (uint32_t)((GetTickCount64() - started_at) / 1000) : 0;
}
static int utils_GetConnectedUniverse(void *self) { (void)self; UTILS(GetConnectedUniverse); return 1; }   /* public */
static uint32_t utils_GetServerRealTime(void *self) { (void)self; UTILS(GetServerRealTime); return (uint32_t)call0(SA_CALL_SERVER_TIME); }
static uint8_t utils_GetCurrentBatteryPower(void *self) { (void)self; UTILS(GetCurrentBatteryPower); return 255; }   /* on AC */
static uint32_t utils_GetAppID(void *self) { (void)self; UTILS(GetAppID); return app_id; }
static const char *utils_GetSteamUILanguage(void *self) { (void)self; UTILS(GetSteamUILanguage); return "english"; }
static int utils_GetAPICallFailureReason(void *self, uint64_t c) { (void)self; (void)c; UTILS(GetAPICallFailureReason); return -1; }
static bool utils_IsAPICallCompleted(void *self, uint64_t c, bool *failed)
{
    (void)self;
    UTILS(IsAPICallCompleted);
    if (failed) *failed = false;
    return call(SA_CALL_CALL_COMPLETED, c, 0, 0, 0, 0) != 0;
}

/* --- ISteamFriends: the player's own name ----------------------------------------------------- */

#define FRIENDS(name) sa_seen(SA_IF_SteamFriends017, SA_SteamFriends017_##name)
static const char *friends_GetPersonaName(void *self)
{
    (void)self;
    FRIENDS(GetPersonaName);
    static char names[2][128];
    static volatile LONG which;
    char *buf = names[InterlockedIncrement(&which) & 1];   /* valid until the next call but one */
    if (!call(SA_CALL_PERSONA_NAME, (uintptr_t)buf, sizeof names[0], 0, 0, 0)) snprintf(buf, sizeof names[0], "Player");
    return buf;
}
static int friends_GetPersonaState(void *self) { (void)self; FRIENDS(GetPersonaState); return call0(SA_CALL_LOGGED_ON) ? 1 : 0; }

/* Bound at load (DllMain, after steam_api.c laid out the stubs), before the game sees any interface. */
static void bind_methods(void)
{
#define BIND(iface, prefix, name) sa_override(SA_IF_##iface, SA_##iface##_##name, (void *)prefix##_##name)
    BIND(SteamUser023, user, GetHSteamUser); BIND(SteamUser023, user, BLoggedOn); BIND(SteamUser023, user, GetSteamID);
    BIND(SteamUser023, user, BIsBehindNAT); BIND(SteamUser023, user, GetAuthSessionTicket);
    BIND(SteamUser023, user, CancelAuthTicket); BIND(SteamUser023, user, BeginAuthSession);
    BIND(SteamUser023, user, EndAuthSession); BIND(SteamUser023, user, UserHasLicenseForApp);
    BIND(SteamUser023, user, RequestEncryptedAppTicket); BIND(SteamUser023, user, GetEncryptedAppTicket);
#define APP(name) BIND(STEAMAPPS_INTERFACE_VERSION008, apps, name)
    APP(BIsSubscribed); APP(BIsSubscribedApp); APP(BIsDlcInstalled); APP(BIsAppInstalled);
    APP(GetCurrentGameLanguage); APP(GetAvailableGameLanguages); APP(GetDLCCount); APP(BGetDLCDataByIndex);
    APP(GetAppOwner); APP(BIsSubscribedFromFamilySharing); APP(GetAppInstallDir); APP(GetAppBuildId);
#undef APP
    BIND(SteamUtils010, utils, GetSecondsSinceAppActive); BIND(SteamUtils010, utils, GetConnectedUniverse);
    BIND(SteamUtils010, utils, GetServerRealTime); BIND(SteamUtils010, utils, GetCurrentBatteryPower);
    BIND(SteamUtils010, utils, GetAppID); BIND(SteamUtils010, utils, GetSteamUILanguage);
    BIND(SteamUtils010, utils, GetAPICallFailureReason); BIND(SteamUtils010, utils, IsAPICallCompleted);
    BIND(SteamFriends017, friends, GetPersonaName); BIND(SteamFriends017, friends, GetPersonaState);
#undef BIND
}
