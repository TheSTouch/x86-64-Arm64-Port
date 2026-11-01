/*
 * The calls between the two halves of steam_api64.dll: the Windows DLL (steam_api/pe, MS x64 ABI)
 * calls its Mac side (steam_api/unix, a Wine unixlib) through Wine's __wine_unix_call_dispatcher.
 * Both halves see the same memory, so arguments are plain structs passed by address: fixed-width
 * types only, since Windows' long is 32 bits and the Mac's 64.
 */
#ifndef SA_BRIDGE_H
#define SA_BRIDGE_H

#include <stdint.h>

enum sa_unix_call {
    SA_CALL_INIT,             /* sa_init_args */
    SA_CALL_SHUTDOWN,
    SA_CALL_NEXT_CALLBACK,    /* sa_callback_args: the next callback or call result, 0 when there is one */
    SA_CALL_STEAMID,          /* ret: the player's SteamID64 */
    SA_CALL_LOGGED_ON,        /* ret: 1 when the session is logged on */
    SA_CALL_OWNS_APP,         /* arg[0] app; ret: 1 when the ownership ticket grants it */
    SA_CALL_APP_OWNER,        /* ret: SteamID64 whose license the game is played on */
    SA_CALL_SERVER_TIME,      /* ret: Steam's clock, Unix seconds */
    SA_CALL_PERSONA_NAME,     /* arg[0] buffer, arg[1] size */
    SA_CALL_DLC_COUNT,        /* ret */
    SA_CALL_DLC_BY_INDEX,     /* arg[0] index, arg[1] AppId_t *, arg[2] bool *, arg[3] name buffer, arg[4] size; ret */
    SA_CALL_AUTH_TICKET,      /* arg[0] buffer, arg[1] size, arg[2] uint32 *length; ret: HAuthTicket */
    SA_CALL_CANCEL_TICKET,    /* arg[0] HAuthTicket */
    SA_CALL_BEGIN_AUTH,       /* arg[0] ticket, arg[1] length, arg[2] SteamID64; ret: EBeginAuthSessionResult */
    SA_CALL_END_AUTH,         /* arg[0] SteamID64 */
    SA_CALL_HAS_LICENSE,      /* arg[0] SteamID64, arg[1] app; ret: EUserHasLicenseForAppResult */
    SA_CALL_REQUEST_APP_TICKET, /* arg[0] data, arg[1] length; ret: SteamAPICall_t */
    SA_CALL_GET_APP_TICKET,   /* arg[0] buffer, arg[1] size, arg[2] uint32 *length; ret: 1 when copied */
    SA_CALL_CALL_COMPLETED,   /* arg[0] SteamAPICall_t; ret: 1 when its result waits */
    SA_CALL_COUNT
};

/* Most calls: up to five arguments and a result, all 64-bit. */
typedef struct {
    uint64_t arg[5];
    uint64_t ret;
} sa_args;

/* What SteamAPI_Init found. */
enum sa_init_result {
    SA_INIT_OK,
    SA_INIT_NO_SIGNIN,        /* no Steam client sign-in stored on this Mac */
    SA_INIT_DENIED,           /* Steam refused the sign-in (revoked or expired) */
    SA_INIT_OFFLINE,          /* Steam not reached in time */
    SA_INIT_NOT_OWNED,        /* Steam refused to sign the game's ownership ticket */
    SA_INIT_NO_APP,           /* no app id */
};

typedef struct {
    uint32_t app;             /* in */
    char game[64];            /* in: how Steam lists the sign-in, "Porting Kit: <game> on <Mac>" */
    int32_t result;           /* out: sa_init_result */
    int32_t eresult;          /* out: Steam's, when it answered */
    uint64_t steamid;         /* out */
    char message[256];        /* out: for the player */
} sa_init_args;

typedef struct {
    uint64_t buffer;          /* in: where the callback's struct goes, in the Windows layout */
    uint32_t size;            /* in */
    int32_t ident;            /* out: k_iCallback */
    uint32_t length;          /* out */
    uint32_t io_failure;      /* out */
    uint64_t call;            /* out: the SteamAPICall_t of a call result, else 0 */
} sa_callback_args;

#endif
