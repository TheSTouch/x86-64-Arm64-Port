// The Mac side of steam_api64.dll: a Wine unixlib. Wine loads it next to the Windows DLL
// (x86_64-unix/steam_api64.so beside x86_64-windows/steam_api64.dll) inside the game's process,
// which is x86-64 under Rosetta, and the DLL calls the functions in __wine_unix_call_funcs.
//
// The Steam client is Kettle's (vendor/kettle): a logon as the player with the sign-in
// ./port steam signin stored, and the game's ownership ticket, which Steam signs only for an
// account that owns the game or borrows it through Family Sharing. SteamAPI_Init waits for both:
// no sign-in, a refused one, no answer or no license, and the game is told there is no Steam.
// The Steamworks answers on top come from RecompSteam (steam_api/recompsteam), as in Mac To iOS
// Recomp's ports.
//
// Arguments live in the game's memory, which this side shares (steam_api/bridge.h).
#import <Foundation/Foundation.h>
#include "bridge.h"
#include "RecompSteamAuth.h"
#include "RecompSteamFlat.h"
#include "RecompSteamInternal.h"

#define RLOG(...) RecompSteam_Log(__VA_ARGS__)

typedef int32_t NTSTATUS;
typedef NTSTATUS (*unixlib_entry_t)(void *args);
#define STATUS_SUCCESS 0
#define STATUS_NO_MORE_ENTRIES ((NTSTATUS)0x8000001A)

static const double kLogonTimeout = 30, kTicketTimeout = 20;

// RecompSteam's flat API (RecompSteamFlat.m, RecompSteamUser.m), called with no interface object.
#pragma pack(push, 4)
typedef struct { int32_t user, ident; uint8_t *param; int32_t size; } CallbackMsg_t;
typedef struct { uint64_t call; int32_t ident; uint32_t size; } SteamAPICallCompleted_t;
#pragma pack(pop)
bool SteamAPI_ManualDispatch_GetNextCallback(int32_t pipe, CallbackMsg_t *msg);
bool SteamAPI_ManualDispatch_GetAPICallResult(int32_t pipe, uint64_t call, void *out, int size, int expected, bool *failed);
void SteamAPI_ManualDispatch_FreeLastCallback(int32_t pipe);
bool SteamAPI_ISteamUtils_IsAPICallCompleted(void *self, uint64_t call, bool *failed);
uint32_t SteamAPI_ISteamUser_GetAuthSessionTicket(void *self, void *ticket, int max, uint32_t *length, const void *identity);
void SteamAPI_ISteamUser_CancelAuthTicket(void *self, uint32_t handle);
int SteamAPI_ISteamUser_BeginAuthSession(void *self, const void *ticket, int length, uint64_t steamid);
void SteamAPI_ISteamUser_EndAuthSession(void *self, uint64_t steamid);
int SteamAPI_ISteamUser_UserHasLicenseForApp(void *self, uint64_t steamid, uint32_t app);
uint64_t SteamAPI_ISteamUser_RequestEncryptedAppTicket(void *self, void *data, int length);
bool SteamAPI_ISteamUser_GetEncryptedAppTicket(void *self, void *ticket, int max, uint32_t *length);
int SteamAPI_ISteamApps_GetDLCCount(void *self);
bool SteamAPI_ISteamApps_BGetDLCDataByIndex(void *self, int index, uint32_t *app, bool *available, char *name, int size);

// --- what RecompSteamStart.m gives a port ------------------------------------------------------

static uint32_t gApp;
static bool gStarted;

// "Playing" on Steam (and the ticket's DLC, RecompSteamApps.m) from the first callback frame, not
// from SteamAPI_Init: a launcher stub (Unreal's FactoryGameSteam.exe) calls SteamAPI_Init and stays
// alive while the game runs, with a Kettle session of its own. Had it claimed the game, Steam would
// log the game's own session off (LoggedInElsewhere) when it claims it in turn. A launcher does not
// run callbacks; the game does, every frame.
static bool gPlaying;

uint32_t RS_GameAppID(void) { return gApp; }
// The session is started by init below, after the checks; never from elsewhere.
bool RecompSteam_StartStored(void) { return gStarted; }

// --- SteamAPI_Init: sign-in and license ------------------------------------------------------------

static void fail(sa_init_args *a, int result, int32_t eresult, NSString *message)
{
    a->result = result;
    a->eresult = eresult;
    snprintf(a->message, sizeof a->message, "%s", message.UTF8String);
    RLOG("SteamAPI_Init: %s", a->message);
}

static NTSTATUS init(void *p)
{
    sa_init_args *a = p;
    a->steamid = 0;
    if (gStarted) {
        a->result = SA_INIT_OK;
        a->steamid = RS_SteamID();
        return STATUS_SUCCESS;
    }
    if (!a->app) {
        fail(a, SA_INIT_NO_APP, 0, @"No Steam app id for this game.");
        return STATUS_SUCCESS;
    }
    gApp = a->app;
    RS_SetAppID(a->app);
    char *token = RecompSteamAuth_StoredToken(a->app);
    if (!token) {
        fail(a, SA_INIT_NO_SIGNIN, 0, @"This Mac is not signed in to Steam. Run ./port steam signin, then start the game again.");
        return STATUS_SUCCESS;
    }
    NSString *host = NSProcessInfo.processInfo.hostName;
    if ([host hasSuffix:@".local"]) host = [host substringToIndex:host.length - 6];
    a->game[sizeof a->game - 1] = 0;
    NSString *machine = [NSString stringWithFormat:@"Porting Kit: %s on %@", a->game[0] ? a->game : "game", host];
    RLOG("signing in for app %u as \"%s\"", a->app, machine.UTF8String);
    RecompSteam_Start(token, a->app, machine.UTF8String);
    free(token);

    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block bool loggedOn = false;
    RecompSteam_WhenLoggedOn(kLogonTimeout, ^(bool ok) {
        loggedOn = ok;
        dispatch_semaphore_signal(done);
    });
    dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)((kLogonTimeout + 5) * NSEC_PER_SEC)));
    if (!loggedOn) {
        bool denied = RecompSteam_State() == RecompSteamDenied;
        RecompSteam_Stop();
        if (denied) fail(a, SA_INIT_DENIED, 0, @"Steam refused this Mac's sign-in (signed out or expired). Run ./port steam signin again.");
        else fail(a, SA_INIT_OFFLINE, RecompSteamResultTimeout, @"Steam did not answer: check the network, then start the game again.");
        return STATUS_SUCCESS;
    }

    // The license check: Steam signs the ownership ticket only for an account that owns the game.
    __block int32_t eresult = RecompSteamResultTimeout;
    RecompSteam_AppOwnershipTicket(a->app, ^(int32_t er, const uint8_t *ticket, size_t length) {
        (void)ticket; (void)length;
        eresult = er;
        dispatch_semaphore_signal(done);
    });
    dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(kTicketTimeout * NSEC_PER_SEC)));
    if (eresult != RecompSteamResultOK) {
        RecompSteam_Stop();
        if (eresult == RecompSteamResultTimeout)
            fail(a, SA_INIT_OFFLINE, eresult, @"Steam did not answer the license check: start the game again.");
        else
            fail(a, SA_INIT_NOT_OWNED, eresult, [NSString stringWithFormat:
                 @"The Steam account signed in on this Mac does not own this game (app %u, EResult %d).", a->app, eresult]);
        return STATUS_SUCCESS;
    }
    gStarted = true;
    a->result = SA_INIT_OK;
    a->steamid = RecompSteam_SteamID();
    RLOG("SteamAPI_Init: signed in as %llu, app %u owned", a->steamid, a->app);
    return STATUS_SUCCESS;
}

static NTSTATUS shutdown_(void *p)
{
    (void)p;
    if (!gStarted) return STATUS_SUCCESS;
    if (gPlaying) RS_PresenceStop();
    gPlaying = false;
    RecompSteam_Stop();
    gStarted = false;
    return STATUS_SUCCESS;
}

// --- callbacks -----------------------------------------------------------------------------------

// Steam packs callback structs to 4 bytes on the Mac and to 8 on Windows. Copies one into the
// game's buffer in the Windows layout; returns its Windows size.
static uint32_t to_windows(int ident, const uint8_t *mac, uint32_t length, uint8_t *out, uint32_t size)
{
    uint8_t win[4096];
    uint32_t n = length;
    switch (ident) {
    case 143:   // ValidateAuthTicketResponse_t { CSteamID; EAuthSessionResponse; CSteamID owner }
        memset(win, 0, 24);
        memcpy(win, mac, 12);
        memcpy(win + 16, mac + 12, 8);
        n = 24;
        break;
    default:    // every other struct RecompSteam posts lays out the same on both
        memcpy(win, mac, MIN(length, sizeof win));
        break;
    }
    memcpy(out, win, MIN(n, size));
    return MIN(n, size);
}

static NTSTATUS next_callback(void *p)
{
    sa_callback_args *a = p;
    if (gStarted && !gPlaying) {
        gPlaying = true;
        RS_PresenceStart();
        RLOG("playing app %u (first callback frame)", gApp);
    }
    RS_Frame();
    CallbackMsg_t msg;
    if (!SteamAPI_ManualDispatch_GetNextCallback(1, &msg)) return STATUS_NO_MORE_ENTRIES;
    uint8_t data[4096];
    uint32_t length;
    bool failed = false;
    a->call = 0;
    if (msg.ident == 703) {   // a call result is announced as SteamAPICallCompleted_t
        SteamAPICallCompleted_t c = *(SteamAPICallCompleted_t *)msg.param;
        a->call = c.call;
        a->ident = c.ident;
        length = MIN(c.size, (uint32_t)sizeof data);
        SteamAPI_ManualDispatch_GetAPICallResult(1, c.call, data, (int)length, c.ident, &failed);
    } else {
        a->ident = msg.ident;
        length = MIN((uint32_t)msg.size, (uint32_t)sizeof data);
        memcpy(data, msg.param, length);
    }
    SteamAPI_ManualDispatch_FreeLastCallback(1);
    a->io_failure = failed;
    a->length = to_windows(a->ident, data, length, (uint8_t *)(uintptr_t)a->buffer, a->size);
    return STATUS_SUCCESS;
}

// --- the rest ------------------------------------------------------------------------------------

#define ARGS sa_args *a = p
#define PTR(i) ((void *)(uintptr_t)a->arg[i])

static NTSTATUS steamid(void *p) { ARGS; a->ret = RS_SteamID(); return 0; }
static NTSTATUS logged_on(void *p) { ARGS; a->ret = RecompSteam_State() == RecompSteamLoggedOn; return 0; }
static NTSTATUS owns_app(void *p) { ARGS; a->ret = gStarted && RS_OwnsApp((uint32_t)a->arg[0]); return 0; }
static NTSTATUS app_owner(void *p) { ARGS; a->ret = RS_AppOwner(); return 0; }
static NTSTATUS server_time(void *p) { ARGS; a->ret = RecompSteam_ServerTime(); return 0; }
static NTSTATUS persona_name(void *p)
{
    ARGS;
    if (!a->arg[1]) return 0;
    a->ret = RecompSteam_PersonaName(PTR(0), (size_t)a->arg[1]);
    return 0;
}
static NTSTATUS dlc_count(void *p) { ARGS; a->ret = (uint64_t)SteamAPI_ISteamApps_GetDLCCount(NULL); return 0; }
static NTSTATUS dlc_by_index(void *p)
{
    ARGS;
    a->ret = SteamAPI_ISteamApps_BGetDLCDataByIndex(NULL, (int)a->arg[0], PTR(1), PTR(2), PTR(3), (int)a->arg[4]);
    return 0;
}
static NTSTATUS auth_ticket(void *p)
{
    ARGS;
    a->ret = SteamAPI_ISteamUser_GetAuthSessionTicket(NULL, PTR(0), (int)a->arg[1], PTR(2), NULL);
    return 0;
}
static NTSTATUS cancel_ticket(void *p) { ARGS; SteamAPI_ISteamUser_CancelAuthTicket(NULL, (uint32_t)a->arg[0]); return 0; }
static NTSTATUS begin_auth(void *p)
{
    ARGS;
    a->ret = (uint64_t)(int64_t)SteamAPI_ISteamUser_BeginAuthSession(NULL, PTR(0), (int)a->arg[1], a->arg[2]);
    return 0;
}
static NTSTATUS end_auth(void *p) { ARGS; SteamAPI_ISteamUser_EndAuthSession(NULL, a->arg[0]); return 0; }
static NTSTATUS has_license(void *p)
{
    ARGS;
    a->ret = (uint64_t)SteamAPI_ISteamUser_UserHasLicenseForApp(NULL, a->arg[0], (uint32_t)a->arg[1]);
    return 0;
}
static NTSTATUS request_app_ticket(void *p)
{
    ARGS;
    a->ret = SteamAPI_ISteamUser_RequestEncryptedAppTicket(NULL, PTR(0), (int)a->arg[1]);
    return 0;
}
static NTSTATUS get_app_ticket(void *p)
{
    ARGS;
    a->ret = SteamAPI_ISteamUser_GetEncryptedAppTicket(NULL, PTR(0), (int)a->arg[1], PTR(2));
    return 0;
}
static NTSTATUS call_completed(void *p)
{
    ARGS;
    a->ret = SteamAPI_ISteamUtils_IsAPICallCompleted(NULL, a->arg[0], NULL);
    return 0;
}

__attribute__((visibility("default"))) const unixlib_entry_t __wine_unix_call_funcs[SA_CALL_COUNT] = {
    [SA_CALL_INIT] = init,
    [SA_CALL_SHUTDOWN] = shutdown_,
    [SA_CALL_NEXT_CALLBACK] = next_callback,
    [SA_CALL_STEAMID] = steamid,
    [SA_CALL_LOGGED_ON] = logged_on,
    [SA_CALL_OWNS_APP] = owns_app,
    [SA_CALL_APP_OWNER] = app_owner,
    [SA_CALL_SERVER_TIME] = server_time,
    [SA_CALL_PERSONA_NAME] = persona_name,
    [SA_CALL_DLC_COUNT] = dlc_count,
    [SA_CALL_DLC_BY_INDEX] = dlc_by_index,
    [SA_CALL_AUTH_TICKET] = auth_ticket,
    [SA_CALL_CANCEL_TICKET] = cancel_ticket,
    [SA_CALL_BEGIN_AUTH] = begin_auth,
    [SA_CALL_END_AUTH] = end_auth,
    [SA_CALL_HAS_LICENSE] = has_license,
    [SA_CALL_REQUEST_APP_TICKET] = request_app_ticket,
    [SA_CALL_GET_APP_TICKET] = get_app_ticket,
    [SA_CALL_CALL_COMPLETED] = call_completed,
};
