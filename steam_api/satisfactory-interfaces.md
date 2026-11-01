# Satisfactory: the Steamworks API it uses

What Satisfactory (Steam app 526870, Unreal Engine 5.6, Steamworks SDK 1.57) asks of
`steam_api64.dll`, recorded by the inventory build of our DLL (`./port steam inventory satisfactory`,
2026-11-01, `wine11`). That build has no Steam behind it: `SteamAPI_Init` succeeds so the game asks
for what it uses, and every method answers "nothing" (no SteamID, not logged on, not subscribed).

The game imports 17 functions from `steam_api64.dll`, all delay-loaded, in seven modules
(`FactoryGameSteam.exe` and the FactoryGame, OnlineSubsystemSteam, OnlineServicesSteamLimited,
SteamShared, SteamSockets and SteamController modules), and names 15 interface versions:
`SteamClient020`, `SteamUser023`, `SteamFriends017`, `SteamUtils010`, `STEAMAPPS_INTERFACE_VERSION008`,
`STEAMUSERSTATS_INTERFACE_VERSION012`, `STEAMREMOTESTORAGE_INTERFACE_VERSION016`, `SteamMatchMaking009`,
`SteamMatchMakingServers002`, `SteamNetworking006`, `SteamNetworkingSockets012`,
`SteamNetworkingUtils004`, `SteamGameServer015`, `SteamGameServerStats001`, `SteamInput006`.
Their vtables are in `steam_api/pe/vtables.h`, generated from the SDK 1.57 headers.

How the run went: the launcher (`FactoryGameSteam.exe`) and the game both call `SteamAPI_Init`; the
game's modules ask for interfaces with a user handle of 0 (they never import
`SteamAPI_GetHSteamUser`). The Steam online backend asks `ISteamApps::BIsSubscribed`, gets false and
gives up ("Steam User is NOT subscribed, exiting"; "Failed to initialize 'STEAM' online backend"),
then the game goes on with its Epic backend. So this list stops where an unlicensed Steam stops the
game; what it asks once Steam answers for real needs a run with the bridge. The game was stopped after five minutes (no call counts: it did not quit by itself).

## Exports called

- `SteamAPI_Init`
- `SteamInternal_ContextInit`
- `SteamInternal_FindOrCreateUserInterface`
- `SteamGameServer_GetHSteamUser`
- `SteamInternal_FindOrCreateGameServerInterface`
- `SteamAPI_GetHSteamUser`
- `SteamAPI_RestartAppIfNecessary`

## Interfaces handed out

| Version | Asked through |
|---|---|
| `SteamUtils010` | FindOrCreateUserInterface |
| `SteamNetworkingUtils004` | FindOrCreateUserInterface |
| `SteamNetworkingSockets012` | FindOrCreateUserInterface |
| `SteamUser023` | FindOrCreateUserInterface |
| `SteamFriends017` | FindOrCreateUserInterface |
| `STEAMREMOTESTORAGE_INTERFACE_VERSION016` | FindOrCreateUserInterface |
| `STEAMUSERSTATS_INTERFACE_VERSION012` | FindOrCreateUserInterface |
| `SteamMatchMakingServers002` | FindOrCreateUserInterface |
| `STEAMAPPS_INTERFACE_VERSION008` | FindOrCreateUserInterface |
| `SteamNetworking006` | FindOrCreateUserInterface |
| `SteamMatchMaking009` | FindOrCreateUserInterface |

Asked for and not handed out:

- `SteamNetworkingSockets012` (FindOrCreateGameServerInterface, user 0): no game server

## Methods called

Counts are calls until the game quit (blank: the game did not quit cleanly).

### ISteamApps (`STEAMAPPS_INTERFACE_VERSION008`)

| Slot | Method | Calls |
|---|---|---|
| 0 | `BIsSubscribed` |  |
| 15 | `GetCurrentBetaName` |  |

### ISteamNetworkingSockets (`SteamNetworkingSockets012`)

| Slot | Method | Calls |
|---|---|---|
| 22 | `InitAuthentication` |  |

### ISteamNetworkingUtils (`SteamNetworkingUtils004`)

| Slot | Method | Calls |
|---|---|---|
| 7 | `CheckPingDataUpToDate` |  |
| 13 | `SetDebugOutputFunction` |  |

### ISteamUtils (`SteamUtils010`)

| Slot | Method | Calls |
|---|---|---|
| 34 | `IsSteamRunningOnSteamDeck` |  |
| 36 | `SetGameLauncherMode` |  |

## Callbacks registered

| Id | Struct |
|---|---|
| 1221 | `SteamNetConnectionStatusChangedCallback_t (game server)` |

