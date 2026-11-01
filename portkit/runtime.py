"""Wine runtimes: download, verify, unpack, sign, locate.

Each runtime has its own Wine prefix of the same name (prefixes/<runtime>).
"""

import plistlib
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import config
from .util import Fail, download, run, step


def spec(name):
    if name not in config.RUNTIMES:
        raise Fail(f"unknown runtime {name!r} (known: {', '.join(config.RUNTIMES)})")
    return config.RUNTIMES[name]


def install_dir(name):
    return config.RUNTIME_DIR / f"{name}-{spec(name)['version']}"


def app_path(name):
    return install_dir(name) / spec(name)["app"]


def wine_dir(name):
    return app_path(name) / "Contents/Resources/wine"


def wine_bin(name):
    return wine_dir(name) / "bin" / spec(name)["wine"]


def wineserver_bin(name):
    return wine_dir(name) / "bin/wineserver"


def installed(name):
    return wine_bin(name).exists()


def d3dmetal_version(name="gptk"):
    res = wine_dir(name) / "lib/external/D3DMetal.framework/Versions/A/Resources"
    for p in (res / "version.plist", res / "Info.plist"):
        if p.exists():
            with open(p, "rb") as f:
                data = plistlib.load(f)
            for key in ("CFBundleShortVersionString", "CFBundleVersion", "SourceVersion"):
                if key in data:
                    return str(data[key])
    return None


def moltenvk_path(name):
    return wine_dir(name) / "lib/libMoltenVK.dylib"


def moltenvk_version(name):
    """The MoltenVK release laid over the runtime's own, or None for the bundled one."""
    stamp = moltenvk_path(name).with_suffix(".version")
    return stamp.read_text().strip() if stamp.exists() else None


def install_moltenvk(name):
    """Replace the runtime's bundled MoltenVK with the release its spec names.

    The bundled library is kept next to it as libMoltenVK.dylib.bundled.
    """
    version = spec(name).get("moltenvk")
    if not version or moltenvk_version(name) == version:
        return
    release = config.MOLTENVK[version]
    archive = download(release["url"], config.DOWNLOADS / f"MoltenVK-macos-v{version}.tar", release["sha256"])
    target = moltenvk_path(name)
    bundled = target.with_suffix(".dylib.bundled")
    if target.exists() and not bundled.exists():
        target.rename(bundled)
    step(f"Installing MoltenVK {version} into {name}")
    with tarfile.open(archive) as tar, tar.extractfile(release["member"]) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    target.chmod(0o755)
    run(["codesign", "--force", "-s", "-", target], capture_output=True)
    target.with_suffix(".version").write_text(version + "\n")


def describe(name):
    s = spec(name)
    parts = [f"{name} {s['version']}: {s['about']}"]
    if name == "gptk" and installed(name):
        parts.append(f"D3DMetal {d3dmetal_version(name) or '?'}")
    if installed(name) and (mvk := moltenvk_version(name)):
        parts.append(f"MoltenVK {mvk}")
    return ", ".join(parts)


def install(name, force=False):
    s = spec(name)
    target = install_dir(name)
    if installed(name) and not force:
        step(f"{name} {s['version']} already installed at {target}")
        install_moltenvk(name)
        return
    archive = download(s["url"], config.DOWNLOADS / Path(s["url"]).name, s["sha256"])
    step(f"Unpacking into {target}")
    with tempfile.TemporaryDirectory(dir=config.RUNTIME_DIR) as tmp:
        run(["tar", "-xf", archive, "-C", tmp])
        unpacked = Path(tmp) / s["app"]
        if not unpacked.exists():
            raise Fail(f"{s['app']} not found in {archive}")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        unpacked.rename(target / s["app"])
    # Same postflight as Gcenx's casks: the archive is downloaded, so it is
    # quarantined, and Wine's binaries need a (local, ad-hoc) signature.
    step("Clearing quarantine and signing ad hoc")
    run(["xattr", "-drs", "com.apple.quarantine", app_path(name)])
    run(["codesign", "--force", "--deep", "-s", "-", app_path(name)], capture_output=True)
    install_moltenvk(name)
    step(f"Installed {describe(name)}")
