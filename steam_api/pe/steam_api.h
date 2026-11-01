/*
 * The Windows side of steam_api64.dll, shared between steam_api.c (exports, interfaces,
 * callbacks, the call log) and bridge.c (the methods that answer for real, through the unix side).
 */
#ifndef SA_STEAM_API_H
#define SA_STEAM_API_H

#include <stdbool.h>
#include <stdint.h>

enum { RET_VOID, RET_INT, RET_STRING, RET_FLOAT, RET_DOUBLE, RET_RECORD };
typedef struct { const char *name; unsigned char ret, size; } sa_method;
typedef struct { const char *version, *klass; int count; const sa_method *methods; } sa_interface;
typedef struct { int id; const char *name; } sa_callback;

#define SA_MAX_SLOTS 96

/* "[steam_api] ..." to $PORTKIT_STEAM_LOG (a Windows path), else to stderr. */
void sa_log(const char *format, ...) __attribute__((format(printf, 1, 2)));
/* Counts a call of an interface method; the first one is logged. Every method calls it. */
void sa_seen(int iface, int slot);
/* Puts fn in the interface's vtable in place of the logging stub (at DLL load, before any use). */
void sa_override(int iface, int slot, void *fn);
/* The interface object for a version string, or NULL (logged) when the bridge does not have it. */
void *sa_interface_for(const char *version, const char *how);
/* Runs the game's callbacks registered for ident (CCallbackBase objects), or the call result's. */
void sa_dispatch(int ident, void *data, int size, uint64_t call, bool io_failure);
const char *sa_callback_name(int ident);
extern volatile bool sa_inited;

/* bridge.c: the real side. sa_bridge_init runs at DLL load (overrides), the rest from the exports. */
void sa_bridge_load(void);
bool sa_bridge_init(void);
void sa_bridge_shutdown(void);
void sa_bridge_run_callbacks(void);

#endif
