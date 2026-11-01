#!/usr/bin/env python3
"""Generate steam_api/pe/vtables.h and slots.h: the vtable of every Steamworks interface the bridge hands out.

The game calls Steam through C++ interface objects (ISteamUser, ISteamApps...), so what matters is
each interface version's vtable: which method sits in which slot, and how it returns its value
under the Microsoft x64 ABI. This reads the Steamworks SDK headers of the SDK the game was built
with and writes, for each interface version in VERSIONS:

- the methods in vtable order. MSVC groups a method's overloads at the slot of its first
  declaration, in reverse order of declaration (as Proton's lsteamclient/gen_wrapper.py lays them out);
- each method's name (its flat name where the SDK gives one: GetStatInt32, GetStatFloat);
- how it returns: nothing, an integer or pointer (rax), a string (a stub answers ""), a float or double (xmm0), or a struct.
  A member function that returns a struct (CSteamID, SteamIPAddress_t...) does it through a hidden
  pointer passed after `this`, and returns that pointer.

The headers are Valve's; this repository keeps only the generated tables (names, order, return
kinds), which the bridge needs to interoperate. --fetch downloads the SDK 1.57 headers Valve
publishes in Proton (lsteamclient/steamworks_sdk_157) at a pinned commit into runtime/.

    python3 steam_api/gen_vtables.py --fetch
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "steam_api/pe/vtables.h"
SLOTS = ROOT / "steam_api/pe/slots.h"
PROTON_COMMIT = "2c620c0fd7c2c424f6bd91898602bdc4d4260468"  # proton_9.0, 2026-11-01
SDK = "157"
SDK_DIR = ROOT / "runtime" / f"steamworks-sdk-{SDK}"
SDK_URL = (f"https://raw.githubusercontent.com/ValveSoftware/Proton/{PROTON_COMMIT}"
           f"/lsteamclient/steamworks_sdk_{SDK}/{{}}")

# Interface version -> (class, header). Satisfactory asks for these (steam_api/satisfactory-interfaces.md).
VERSIONS = {
    "SteamClient020": ("ISteamClient", "isteamclient.h"),
    "SteamUser023": ("ISteamUser", "isteamuser.h"),
    "SteamFriends017": ("ISteamFriends", "isteamfriends.h"),
    "SteamUtils010": ("ISteamUtils", "isteamutils.h"),
    "STEAMAPPS_INTERFACE_VERSION008": ("ISteamApps", "isteamapps.h"),
    "STEAMUSERSTATS_INTERFACE_VERSION012": ("ISteamUserStats", "isteamuserstats.h"),
    "STEAMREMOTESTORAGE_INTERFACE_VERSION016": ("ISteamRemoteStorage", "isteamremotestorage.h"),
    "SteamMatchMaking009": ("ISteamMatchmaking", "isteammatchmaking.h"),
    "SteamMatchMakingServers002": ("ISteamMatchmakingServers", "isteammatchmaking.h"),
    "SteamNetworking006": ("ISteamNetworking", "isteamnetworking.h"),
    "SteamNetworkingSockets012": ("ISteamNetworkingSockets", "isteamnetworkingsockets.h"),
    "SteamNetworkingUtils004": ("ISteamNetworkingUtils", "isteamnetworkingutils.h"),
    "SteamGameServer015": ("ISteamGameServer", "isteamgameserver.h"),
    "SteamGameServerStats001": ("ISteamGameServerStats", "isteamgameserverstats.h"),
    "SteamInput006": ("ISteamInput", "isteaminput.h"),
}
HEADERS = sorted({h for _, h in VERSIONS.values()})

# Structs returned by value, with their size under the Windows SDK's packing. A struct return the
# table does not know stops the generator: zeroing the wrong size would write past the caller's buffer.
RECORDS = {
    "CSteamID": 8,
    "CGameID": 8,
    "SteamIPAddress_t": 20,             # uint8 ipv6[16] (union with uint32 ipv4), ESteamIPType
    "InputDigitalActionData_t": 2,      # bool bState, bool bActive (pack 1)
    "InputAnalogActionData_t": 13,      # EInputSourceMode, float x, float y, bool bActive (pack 1)
    "InputMotionData_t": 40,            # 10 floats
}
SCALARS = {"bool", "char", "int", "short", "long", "unsigned", "uint8", "int8", "uint16", "int16", "uint32",
           "int32", "uint64", "int64", "intp", "uintp", "size_t"}

# Where each interface's callback ids start (Steamworks' k_i...Callbacks; Proton's copies of the
# headers leave the constants out). SteamServersConnected_t is k_iSteamUserCallbacks + 1 = 101.
CALLBACK_BASES = {
    "k_iSteamUserCallbacks": 100, "k_iSteamGameServerCallbacks": 200, "k_iSteamFriendsCallbacks": 300,
    "k_iSteamMatchmakingCallbacks": 500, "k_iSteamUtilsCallbacks": 700, "k_iSteamAppsCallbacks": 1000,
    "k_iSteamUserStatsCallbacks": 1100, "k_iSteamNetworkingCallbacks": 1200,
    "k_iSteamNetworkingSocketsCallbacks": 1220, "k_iSteamNetworkingMessagesCallbacks": 1250,
    "k_iSteamNetworkingUtilsCallbacks": 1280, "k_iSteamRemoteStorageCallbacks": 1300,
    "k_iSteamGameServerStatsCallbacks": 1800, "k_iSteamControllerCallbacks": 2800,
    "k_iSteamGameSearchCallbacks": 5200, "k_iSteamPartiesCallbacks": 5300,
}

VOID, INT, STRING, FLOAT, DOUBLE, RECORD = "RET_VOID", "RET_INT", "RET_STRING", "RET_FLOAT", "RET_DOUBLE", "RET_RECORD"


def fetch():
    SDK_DIR.mkdir(parents=True, exist_ok=True)
    for name in HEADERS + ["steamclientpublic.h", "steamtypes.h", "steamnetworkingtypes.h", "steam_api_common.h",
                           "matchmakingtypes.h", "isteamcontroller.h", "steamuniverse.h",
                           "steamhttpenums.h"]:
        dest = SDK_DIR / name
        if not dest.exists():
            print(f"fetching {name}", file=sys.stderr)
            with urllib.request.urlopen(SDK_URL.format(name)) as r:
                dest.write_bytes(r.read())


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def class_body(text, klass):
    m = re.search(r"\bclass\s+" + klass + r"\b[^;{]*\{", text)
    if not m:
        raise SystemExit(f"class {klass} not found")
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def scalar_typedefs(texts):
    """Typedef names that stand for integers or enums (HSteamUser, SteamAPICall_t, EResult...)."""
    names = set()
    for t in texts:
        names |= set(re.findall(r"\benum\s+(\w+)", t))
        for target, name in re.findall(r"\btypedef\s+([\w\s]+?)\s+(\w+)\s*;", t):
            if target.split()[-1] in SCALARS or target.startswith("enum"):
                names.add(name)
        names |= set(re.findall(r"\btypedef\s+[\w\s]+\*\s*(\w+)\s*;", t))   # pointer handles
    # Typedefs of typedefs (typedef uint32 AppId_t; typedef AppId_t DepotId_t;).
    for _ in range(3):
        for t in texts:
            for target, name in re.findall(r"\btypedef\s+(\w+)\s+(\w+)\s*;", t):
                if target in names:
                    names.add(name)
    return names


def return_kind(rtype, scalars):
    rtype = re.sub(r"\b(const|virtual|struct|class|enum)\b", " ", rtype).strip()
    if re.fullmatch(r"char\s*\*", rtype):
        return STRING, 0      # a stub answers "", not NULL: games compare these strings
    if rtype.endswith(("*", "&")):
        return INT, 0
    rtype = " ".join(rtype.split())
    if rtype == "void":
        return VOID, 0
    if rtype == "float":
        return FLOAT, 0
    if rtype == "double":
        return DOUBLE, 0
    if rtype in RECORDS:
        return RECORD, RECORDS[rtype]
    if rtype.split()[-1] in SCALARS or rtype in scalars:
        return INT, 0
    raise SystemExit(f"unknown return type {rtype!r}: add it to RECORDS or SCALARS")


METHOD = re.compile(r"(?:STEAM_FLAT_NAME\(\s*(\w+)\s*\)\s*)?virtual\s+([^;(]*?)\s*\b(\w+)\s*\(([^;]*?)\)\s*=\s*0\s*;", re.S)


def unwrap_private(body):
    """STEAM_PRIVATE_API( ... ) only makes its methods protected for game code: the slots stay."""
    while (i := body.find("STEAM_PRIVATE_API")) >= 0:
        start = body.index("(", i)
        depth, j = 1, start + 1
        while depth:
            depth += {"(": 1, ")": -1}.get(body[j], 0)
            j += 1
        body = body[:i] + body[start + 1:j - 1] + body[j:]
    return body


def methods(body, scalars):
    body = unwrap_private(body)
    body = re.sub(r"STEAM_(?!FLAT_NAME)\w+\s*\((?:[^()]|\([^()]*\))*\)", " ", body)   # other annotations
    body = re.sub(r"\bSTEAM_(?!FLAT_NAME)\w+\b", " ", body)
    decls = []
    for flat, rtype, name, _ in METHOD.findall(body):
        kind, size = return_kind(rtype, scalars)
        decls.append({"name": name, "flat": flat or name, "kind": kind, "size": size})
    # MSVC: overloads share the first declaration's slot, later declarations first.
    first = {}
    for i, d in enumerate(decls):
        d["slot"], n = first.get(d["name"], (i, 0))
        first[d["name"]] = (d["slot"], n + 1)
        d["order"] = -n
    return sorted(decls, key=lambda d: (d["slot"], d["order"]))


def callbacks(texts):
    """{id: struct name} for every callback struct the headers declare."""
    found = {}
    pattern = re.compile(r"\bstruct\s+(\w+)\s*\{\s*enum\s*\{\s*k_iCallback\s*=\s*(\w+)\s*\+\s*(\d+)\s*\}"
                         r"|STEAM_CALLBACK_BEGIN\(\s*(\w+)\s*,\s*(\w+)\s*\+\s*(\d+)\s*\)")
    for t in texts:
        for m in pattern.finditer(t):
            name, base, n = (m.group(1), m.group(2), m.group(3)) if m.group(1) else m.group(4, 5, 6)
            if base in CALLBACK_BASES:
                found[CALLBACK_BASES[base] + int(n)] = name
    return found


def generate(sdk_dir):
    texts = {p.name: strip_comments(p.read_text(encoding="latin-1")) for p in Path(sdk_dir).glob("*.h")}
    missing = [h for h in HEADERS if h not in texts]
    if missing:
        raise SystemExit(f"{sdk_dir}: missing {', '.join(missing)} (run with --fetch)")
    scalars = scalar_typedefs(texts.values())
    out = [
        "/* Generated by steam_api/gen_vtables.py from the Steamworks SDK " + SDK[0] + "." + SDK[1:] + " headers",
        f" * (Proton lsteamclient/steamworks_sdk_{SDK}, commit {PROTON_COMMIT[:12]}). Do not edit.",
        " *",
        " * For each interface version: its methods in MSVC vtable order, their names (flat names for",
        " * overloads) and how each returns. Included once, by steam_api/pe/steam_api.c. */",
        "",
        '#include "slots.h"',
        "",
    ]
    slots = [
        "/* Generated by steam_api/gen_vtables.py with vtables.h. Do not edit.",
        " * SA_IF_<version>: an interface's index; SA_<version>_<method>: a method's vtable slot. */",
        "#ifndef SA_SLOTS_H",
        "#define SA_SLOTS_H",
    ]
    stubs = []
    for vi, (version, (klass, header)) in enumerate(VERSIONS.items()):
        ms = methods(class_body(texts[header], klass), scalars)
        names = [m["flat"] for m in ms]
        if len(set(names)) != len(names):
            raise SystemExit(f"{version}: two methods share a name")
        # SA_IF_<version> is the interface's index, SA_<version>_<method> a method's slot.
        slots.append(f"enum {{ SA_IF_{version} = {vi} }};")
        slots.append(f"enum {{ " + ", ".join(f"SA_{version}_{n} = {i}" for i, n in enumerate(names)) + " };")
        out.append(f"static const sa_method methods_{version}[{len(ms)}] = {{")
        for m in ms:
            out.append(f'    {{"{m["flat"]}", {m["kind"]}, {m["size"]}}},')
        out.append("};")
        stubs.append((vi, version, klass, ms))
    out.append("")
    out.append("#define SA_INTERFACE_COUNT %d" % len(VERSIONS))
    out.append("static const sa_interface sa_interfaces[SA_INTERFACE_COUNT] = {")
    for vi, version, klass, ms in stubs:
        out.append(f'    {{"{version}", "{klass}", {len(ms)}, methods_{version}}},')
    out.append("};")
    out.append("")
    cbs = callbacks(texts.values())
    out.append(f"#define SA_CALLBACK_COUNT {len(cbs)}")
    out.append("static const sa_callback sa_callbacks[SA_CALLBACK_COUNT] = {")
    out.extend(f'    {{{i}, "{n}"}},' for i, n in sorted(cbs.items()))
    out.append("};")
    out.append("")
    out.append("/* One stub per slot, so a call says which method it was. SA_STUB_<kind>(interface, slot). */")
    for vi, version, klass, ms in stubs:
        kinds = {INT: "INT", VOID: "INT", STRING: "STRING", FLOAT: "FLOAT", DOUBLE: "DOUBLE", RECORD: "RECORD"}
        out.append(" ".join(f"SA_STUB_{kinds[m['kind']]}({vi}, {i})" for i, m in enumerate(ms)))
    out.append("")
    for vi, version, klass, ms in stubs:
        out.append(f"static void *const stubs_{vi}[{len(ms)}] = {{")
        out.append("    " + ", ".join(f"(void *)sa_stub_{vi}_{i}" for i in range(len(ms))) + ",")
        out.append("};")
    out.append("static void *const *const sa_stubs[SA_INTERFACE_COUNT] = {")
    out.append("    " + ", ".join(f"stubs_{vi}" for vi in range(len(stubs))) + ",")
    out.append("};")
    slots.append("#endif")
    return "\n".join(out) + "\n", "\n".join(slots) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sdk", type=Path, default=SDK_DIR, help="folder with the SDK headers (default: %(default)s)")
    p.add_argument("--fetch", action="store_true", help="download the SDK headers from Proton first")
    p.add_argument("--check", action="store_true", help="fail if vtables.h is not up to date")
    a = p.parse_args(argv)
    if a.fetch:
        fetch()
    files = dict(zip((OUT, SLOTS), generate(a.sdk)))
    if a.check:
        for path, text in files.items():
            if not path.exists() or path.read_text() != text:
                raise SystemExit(f"{path} is out of date: run {Path(__file__).name}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
