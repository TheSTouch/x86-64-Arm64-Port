# RecompSteam (copied)

The Steamworks API that [Mac To iOS Recomp](https://github.com/TheSTouch/Mac-To-iOS-Recomp) serves
on top of Kettle's Steam client core, copied unchanged from its `recomp/native/RecompSteam`:

| File | What the bridge uses it for |
|---|---|
| `RecompSteamFlat.h` | the shared declarations: interfaces, callbacks, ownership |
| `RecompSteamFlat.m` | the callback and call-result queue, the player's SteamID, the flat getters (persona, server time) |
| `RecompSteamApps.m` | the ownership ticket (owned DLC) and "playing" on Steam |
| `RecompSteamUser.m` | auth session tickets, encrypted app tickets, DLC names |

Source: Mac To iOS Recomp at commit `78523ffd470a4effe1848eab35f57ce9467eea39` (2026-10-30; the folder
last changed in `4fdfeea`). Same owner and same license (PolyForm Noncommercial 1.0.0) as this
repository. `RecompSteamStart.m` is left out on purpose: the bridge starts the session itself, with
a real license check first (`steam_api/unix/bridge.m`).

These files are not edited here. To update them, copy them again from a newer commit of Mac To iOS
Recomp, change the commit above, and rebuild. Fixes to the Steam client core underneath (the
session, the sign-in, the protobuf codec) go to [Kettle](https://github.com/TheSTouch/kettle) first,
then `vendor/kettle` moves to the new tag; the files under `vendor/kettle` are never patched in place.
