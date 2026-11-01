/*
 * steam-check.exe: what a game sees of our steam_api64.dll, without the game. Run under Wine with
 * the bridge as a builtin (./port steam check <game>): it loads steam_api64.dll as the game does,
 * calls SteamAPI_Init and a few methods through the interfaces' vtables, as C++ code would, and
 * prints the answers. Exit code 0 when Steam signed the game's ownership ticket, 1 otherwise.
 */
#include <windows.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "../pe/slots.h"

typedef bool (*Init)(void);
typedef void (*Simple)(void);
typedef void *(*FindOrCreate)(int32_t user, const char *version);

/* A method in vtable slot `slot` of interface object `iface`. */
#define METHOD(iface, slot, type) ((type)(*(void ***)(iface))[slot])

/* argv[1]: the game's own steam_api64.dll, loaded by its path as Unreal does; Wine's load order
 * (steam_api64=b) puts our builtin in its place. Wine only loads a builtin that has a file on the
 * search path or is named by path, outside its own system DLLs. */
int main(int argc, char **argv)
{
    HMODULE dll = LoadLibraryA(argc > 1 ? argv[1] : "steam_api64.dll");
    if (!dll) {
        printf("steam_api64.dll did not load (error %lu)\n", GetLastError());
        return 1;
    }
    Init init = (Init)(void *)GetProcAddress(dll, "SteamAPI_Init");
    Simple run_callbacks = (Simple)(void *)GetProcAddress(dll, "SteamAPI_RunCallbacks");
    Simple shutdown = (Simple)(void *)GetProcAddress(dll, "SteamAPI_Shutdown");
    FindOrCreate find = (FindOrCreate)(void *)GetProcAddress(dll, "SteamInternal_FindOrCreateUserInterface");
    if (!init || !run_callbacks || !shutdown || !find) {
        printf("steam_api64.dll lacks the Steamworks exports\n");
        return 1;
    }
    if (!init()) {
        printf("SteamAPI_Init: failed (no sign-in, no license, or Steam not reached; see the log)\n");
        return 1;
    }
    printf("SteamAPI_Init: ok\n");
    void *user = find(0, "SteamUser023");
    void *apps = find(0, "STEAMAPPS_INTERFACE_VERSION008");
    void *utils = find(0, "SteamUtils010");
    void *friends = find(0, "SteamFriends017");
    if (!user || !apps || !utils || !friends) {
        printf("interfaces missing\n");
        return 1;
    }
    /* GetSteamID returns a CSteamID through a hidden pointer, as MSVC does for member functions. */
    uint64_t steamid = 0;
    METHOD(user, SA_SteamUser023_GetSteamID, uint64_t *(*)(void *, uint64_t *))(user, &steamid);
    bool logged_on = METHOD(user, SA_SteamUser023_BLoggedOn, bool (*)(void *))(user);
    bool subscribed = METHOD(apps, SA_STEAMAPPS_INTERFACE_VERSION008_BIsSubscribed, bool (*)(void *))(apps);
    uint32_t app = METHOD(utils, SA_SteamUtils010_GetAppID, uint32_t (*)(void *))(utils);
    const char *name = METHOD(friends, SA_SteamFriends017_GetPersonaName, const char *(*)(void *))(friends);
    for (int i = 0; i < 20; i++) {
        run_callbacks();
        Sleep(100);
    }
    printf("ISteamUser::GetSteamID: %llu\nISteamUser::BLoggedOn: %d\nISteamApps::BIsSubscribed: %d\n"
           "ISteamUtils::GetAppID: %u\nISteamFriends::GetPersonaName: %s\n",
           (unsigned long long)steamid, logged_on, subscribed, app, name);
    shutdown();
    Sleep(1000);   /* lets the logoff go out */
    return subscribed && steamid ? 0 : 1;
}
