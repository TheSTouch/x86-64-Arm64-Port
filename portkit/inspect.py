"""Static inspection of a Windows game: what it is built with and what each route faces.

Reads files only; nothing is run. The verdicts are hints for planning, not
promises: the Porting Kit run is what settles them.
"""

import mmap
import re
from pathlib import Path

from . import games, pe

# File (or folder) name patterns -> component. Matched against names in the game folder.
COMPONENTS = [
    (r"^steam_api64\.dll$|^steam_api\.dll$", "Steamworks"),
    (r"^eossdk-win64-shipping\.dll$", "Epic Online Services"),
    (r"easyanticheat", "Easy Anti-Cheat"),
    (r"^battleye$|^beservice|_be\.exe$", "BattlEye"),
    (r"^d3d12core\.dll$", "DirectX 12 Agility SDK"),
    (r"^dstorage(core)?\.dll$", "DirectStorage"),
    (r"^nvngx_dlss\.dll$", "NVIDIA DLSS"),
    (r"^nvngx_dlssg\.dll$", "NVIDIA DLSS Frame Generation"),
    (r"^sl\.interposer\.dll$", "NVIDIA Streamline"),
    (r"^gfsdk_aftermath", "NVIDIA Aftermath"),
    (r"amd_fidelityfx|^ffx_", "AMD FidelityFX (FSR)"),
    (r"^amd_ags_x64\.dll$", "AMD AGS"),
    (r"^libxess\.dll$", "Intel XeSS"),
    (r"^bink2w64\.dll$", "Bink video"),
    (r"^fmod(studio)?\.dll$", "FMOD"),
    (r"aksoundengine|wwise", "Wwise"),
    (r"^oo2core_\d+_win64\.dll$", "Oodle"),
    (r"physx", "PhysX"),
    (r"^unityplayer\.dll$", "Unity player"),
    (r"^gameassembly\.dll$", "Unity IL2CPP"),
]
COMPONENTS = [(re.compile(p, re.I), name) for p, name in COMPONENTS]

ANTI_CHEAT = {"Easy Anti-Cheat", "BattlEye"}

GRAPHICS_DLLS = {
    "d3d12.dll": "DirectX 12",
    "d3d11.dll": "DirectX 11",
    "d3d9.dll": "DirectX 9",
    "vulkan-1.dll": "Vulkan",
    "opengl32.dll": "OpenGL",
}

# Unreal 5 modules carry a small .uedbg code section; Microsoft's own D3D12Core and XAudio2 redistributables
# have .text_hf and .no_bbt. All have ordinary entropy.
STANDARD_CODE_SECTIONS = {".text", ".textbss", "text", "code", ".init", "init", "page", ".itext",
                          ".uedbg", ".text_hf", ".no_bbt"}
PACKED_ENTROPY = 7.2  # machine code sits around 6 to 6.9; encrypted or compressed code near 8
PE_SUFFIXES = {".exe", ".dll"}

# Unreal's build version: "5.6.1-44394996+++UE5+Release-5.6" in stock builds, "5.6.1-++Studio+branch-CL-123"
# on a studio's own engine branch, or only the branch ("++UE4+Release-4.27"). Shipping builds keep it as
# UTF-16 (TEXT()) in the BuildSettings module, older monolithic ones as ASCII in the executable.
UE_VERSION = re.compile(r"(?:(\d+\.\d+\.\d+)-(?:(\d+)\+)?)?(\+\+\w+\+[\w.+-]+)")


def unreal_version(path, limit=512 * 1024 * 1024, max_hits=20000):
    """{"version", "branch", "changelist"} from an Unreal binary's build string, or None."""
    with open(path, "rb") as f:
        try:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            return None
    with m:
        end = min(len(m), limit)
        for encoding, needle in (("utf-16-le", "++".encode("utf-16-le")), ("latin-1", b"++")):
            i, hits = m.find(needle, 0, end), 0
            while i >= 0 and hits < max_hits:
                hits += 1
                start = max(i - 64, i % 2) if encoding == "utf-16-le" else max(i - 32, 0)
                text = m[start:min(i + 256, end)].decode(encoding, "replace")
                if hit := UE_VERSION.search(text):
                    return _version_fields(*hit.groups())
                i = m.find(needle, i + 2, end)
    return None


def _version_fields(version, changelist, branch):
    if cl := re.search(r"-CL-(\d+)$", branch):
        branch, changelist = branch[:cl.start()], changelist or cl.group(1)
    if not version and (release := re.search(r"Release-(\d+\.\d+)", branch)):
        version = release.group(1)
    return {"version": version, "branch": branch, "changelist": changelist}


def _section_report(binary):
    out = []
    for s in binary.sections:
        if not s.executable:
            continue
        flags = []
        if s.entropy is not None and s.entropy > PACKED_ENTROPY:
            flags.append("encrypted or packed at rest")
        if s.name.lower() not in STANDARD_CODE_SECTIONS:
            flags.append("non-standard code section")
        out.append({"name": s.name, "size_mb": round(s.vsize / 2**20, 1), "entropy": s.entropy, "flags": flags})
    return out


def inspect_file(path):
    binary = pe.parse(path, with_entropy=True)
    names = [s.name for s in binary.sections]
    sections = _section_report(binary)
    protection = []
    if ".bind" in names:
        protection.append("SteamStub DRM wrapper (.bind section)")
    if any("encrypted or packed at rest" in s["flags"] for s in sections):
        protection.append("code encrypted or packed at rest: a protector decrypts it at run time "
                          "(Denuvo, Arxan, VMProtect, Themida...)")
    if len(names) != len(set(names)):
        protection.append("duplicate section names: typical of a protector")
    ue = unreal_version(path) or {}
    return {
        "path": str(path),
        "size_mb": round(binary.size / 2**20, 1),
        "machine": binary.machine,
        "subsystem": binary.subsystem,
        "dll": binary.is_dll,
        "code_mb": round(binary.code_size / 2**20, 1),
        "sections": sections,
        "tls_callbacks": binary.tls_callbacks,
        "pdb": binary.pdb,
        "imports": {dll: len(fns) for dll, fns in binary.imports.items()},
        "delay_imports": {dll: len(fns) for dll, fns in binary.delay_imports.items()},
        "exports": binary.exports,
        "unreal_version": ue.get("version"),
        "unreal_branch": ue.get("branch"),
        "unreal_changelist": ue.get("changelist"),
        "protection": protection,
    }


def inspect_folder(root, max_pe=600):
    root = Path(root)
    files = [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    names = {p.name for p in files} | {p.name for p in root.rglob("*") if p.is_dir()}
    components = sorted({label for rx, label in COMPONENTS for n in names if rx.search(n)})

    exts = {}
    for p in files:
        exts[p.suffix.lower()] = exts.get(p.suffix.lower(), 0) + 1

    engine = None
    if exts.get(".pak") or exts.get(".utoc") or any(p.name.lower().endswith("-shipping.exe") for p in files):
        engine = "Unreal Engine"
    elif "Unity player" in components:
        engine = "Unity (IL2CPP)" if "Unity IL2CPP" in components else "Unity (Mono)"

    main = games.guess_exe(root)
    pe_files = sorted((p for p in files if p.suffix.lower() in PE_SUFFIXES), key=lambda p: -p.stat().st_size)
    graphics, x86_32, parsed, code = {}, [], 0, 0
    # In a modular build the main executable is a small stub: protection is judged on every code file.
    protection = {"files": 0, "max_entropy": 0.0, "max_entropy_file": None, "flagged": [], "steamstub": []}
    for p in pe_files[:max_pe]:
        try:
            b = pe.parse(p, with_entropy=True)
        except (pe.NotPE, OSError, ValueError):
            continue
        parsed += 1
        code += b.code_size
        rel = p.relative_to(root).as_posix()
        if entropies := [s.entropy for s in b.sections if s.executable and s.entropy is not None]:
            protection["files"] += 1
            if max(entropies) > protection["max_entropy"]:
                protection["max_entropy"], protection["max_entropy_file"] = max(entropies), rel
            if max(entropies) > PACKED_ENTROPY:
                protection["flagged"].append(rel)
        if any(s.name == ".bind" for s in b.sections):
            protection["steamstub"].append(rel)
        if b.machine == "x86":
            x86_32.append(p.name)
        for dll in b.imported_dlls():
            if dll in GRAPHICS_DLLS:
                graphics.setdefault(GRAPHICS_DLLS[dll], []).append(str(p.relative_to(root)))

    report = {
        "root": str(root),
        "size_gb": round(total / 1e9, 1),
        "files": len(files),
        "top_extensions": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:12]),
        "engine": engine,
        "components": components,
        "graphics": sorted(graphics, key=lambda api: -len(graphics[api])),
        "graphics_importers": graphics,
        "pe_files": len(pe_files),
        "pe_parsed": parsed,
        "code_mb": round(code / 2**20, 1),
        "x86_32_binaries": x86_32[:20],
        "code_protection": protection,
        "main": inspect_file(main) if main else None,
    }
    if engine == "Unreal Engine" and report["main"] and not report["main"]["unreal_version"]:
        # Modular builds keep the version string in the BuildSettings module (older ones in Core).
        for module in ("buildsettings", "core"):
            found = [p for p in pe_files if re.search(rf"-{module}-win64-shipping\.dll$", p.name, re.I)]
            if found and (ue := unreal_version(found[0])):
                report["main"].update(unreal_version=ue["version"], unreal_branch=ue["branch"],
                                      unreal_changelist=ue["changelist"])
                break
    report["routes"] = routes(report)
    return report


def routes(r):
    main = r.get("main") or {}
    comps = set(r["components"])
    gptk, recomp = [], []

    if blockers := comps & ANTI_CHEAT:
        gptk.append(("blocker", f"{', '.join(sorted(blockers))}: kernel anti-cheat does not run under "
                                 "Wine on macOS; single-player may still work if the game allows starting without it"))
    if "DirectX 12" in r["graphics"] or "DirectX 11" in r["graphics"]:
        gptk.append(("ok", "DirectX 11/12: translated to Metal by D3DMetal"))
    if "Vulkan" in r["graphics"] and not ({"DirectX 12", "DirectX 11"} & set(r["graphics"])):
        gptk.append(("risk", "Vulkan only: D3DMetal translates DirectX, not Vulkan"))
    if comps & {"NVIDIA DLSS", "NVIDIA DLSS Frame Generation"}:
        gptk.append(("info", "DLSS needs an NVIDIA GPU: pick FSR/XeSS or native resolution in the game"))
    if "DirectStorage" in comps:
        gptk.append(("risk", "DirectStorage: Wine's support is partial; loading may be slow or fail"))
    if main:
        gptk.append(("info", "x86-64 code runs through Rosetta 2 (AVX advertised by default)"))

    cp = r.get("code_protection")
    target = _unreal_target(main.get("path"))
    own = [f for f in (cp or {}).get("flagged", []) + (cp or {}).get("steamstub", [])
           if target and Path(f).name.lower().startswith(target.lower() + "-")]
    if main.get("protection") or own:
        recomp.append(("blocker", "the game's own code looks protected ("
                                  + (", ".join(own[:5]) if own else "main executable, see the hints above")
                                  + "): static recompilation needs the decrypted code, and removing the "
                                  "protection is not something this project does"))
    elif cp and main:
        recomp.append(("ok", f"code is plain in all {cp['files']} code files (highest code-section entropy "
                             f"{cp['max_entropy']}, in {Path(cp['max_entropy_file'] or '-').name}); "
                             f"{r['code_mb']} MB of x86 code across all .exe/.dll files"))
    elif main:
        recomp.append(("ok", f"main executable code is plain; {main['code_mb']} MB of x86 code"))
    if cp and (other := [f for f in cp["flagged"] + cp["steamstub"] if f not in own]):
        recomp.append(("risk", f"{len(other)} other file(s) look packed or wrapped ({', '.join(other[:5])}): "
                               "they can only be replaced or dropped, not recompiled"))
    if r["engine"] == "Unreal Engine":
        recomp.append(("info", f"Unreal Engine {_unreal_label(main) or '(version unknown)'}: the engine "
                               "has a Mac target, so engine-level knowledge transfers"))
        if main.get("unreal_branch") and not main["unreal_branch"].startswith("++UE"):
            recomp.append(("risk", f"the engine is the studio's own branch ({main['unreal_branch']}), not Epic's "
                                   "release: a native engine has to be built from that branch"))
    if r["graphics"]:
        recomp.append(("work", f"graphics: {', '.join(r['graphics'])} calls and shaders must map to Metal"))
    if comps & ANTI_CHEAT:
        recomp.append(("risk", "anti-cheat: online modes stay out of reach"))
    return {"porting_kit": gptk, "recomp": recomp}


def _unreal_target(main_path):
    """"FactoryGameSteam" from ".../FactoryGameSteam-Win64-Shipping.exe": the prefix of its module DLLs."""
    hit = re.match(r"(.+)-Win64-(?:Shipping|Test|Development|DebugGame)\.exe$", Path(main_path or "").name, re.I)
    return hit.group(1) if hit else None


def _unreal_label(m):
    if not m.get("unreal_version"):
        return None
    extra = ", ".join(x for x in (m.get("unreal_branch"), m.get("unreal_changelist") and f"CL {m['unreal_changelist']}") if x)
    return f"{m['unreal_version']} ({extra})" if extra else m["unreal_version"]


def inspect_path(path):
    path = Path(path)
    return inspect_folder(path) if path.is_dir() else {"main": inspect_file(path)}


def format_report(r):
    lines = []
    if "root" in r:
        lines += [
            f"Folder      {r['root']}",
            f"Size        {r['size_gb']} GB in {r['files']} files ({r['pe_files']} .exe/.dll)",
            f"Engine      {r['engine'] or 'not recognised'}",
            "Graphics    " + (", ".join(f"{api} ({len(r['graphics_importers'][api])} files)" for api in r["graphics"])
                             or "none found in imports"),
            f"Code        {r['code_mb']} MB of machine code in {r['pe_parsed']} parsed .exe/.dll",
            f"Components  {', '.join(r['components']) or 'none recognised'}",
        ]
        if cp := r.get("code_protection"):
            found = cp["flagged"] + cp["steamstub"]
            lines.append(f"Protection  {len(found)} of {cp['files']} code files look packed or wrapped"
                         + (f": {', '.join(found[:5])}" if found else
                            f" (highest code-section entropy {cp['max_entropy']})"))
        if r["x86_32_binaries"]:
            lines.append(f"32-bit x86  {', '.join(r['x86_32_binaries'])}")
    m = r.get("main")
    if m:
        lines += [
            "",
            f"Main executable  {m['path']}",
            f"  {m['machine']} {m['subsystem']}, {m['size_mb']} MB file, {m['code_mb']} MB code"
            + (f", Unreal {_unreal_label(m)}" if m["unreal_version"] else ""),
            f"  PDB path   {m['pdb'] or 'none'}",
            f"  TLS callbacks {m['tls_callbacks']}, exports {m['exports']}",
            "  Code sections:",
        ]
        for s in m["sections"]:
            lines.append(f"    {s['name']:<10} {s['size_mb']:>7} MB  entropy {s['entropy']}"
                         + (f"  ({'; '.join(s['flags'])})" if s["flags"] else ""))
        imports = sorted(m["imports"].items(), key=lambda kv: kv[0].lower())
        lines.append(f"  Imports from {len(imports)} DLLs ({sum(n for _, n in imports)} functions), "
                     f"{len(m['delay_imports'])} delay-loaded")
        lines.append("    " + ", ".join(f"{d} ({n})" for d, n in imports))
        if m["protection"]:
            lines.append("  Protection hints:")
            lines += [f"    - {p}" for p in m["protection"]]
    for route, title in (("porting_kit", "Porting Kit route"), ("recomp", "Native recompilation route")):
        if r.get("routes", {}).get(route):
            lines += ["", title]
            lines += [f"  [{level}] {text}" for level, text in r["routes"][route]]
    return "\n".join(lines)
