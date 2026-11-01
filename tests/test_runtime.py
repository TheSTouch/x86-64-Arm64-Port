import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portkit import config, runtime

MEMBER = "MoltenVK/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib"


def fake_release(path, payload):
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo(MEMBER)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MoltenVKTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        archive = root / "MoltenVK-macos.tar"
        sha = fake_release(archive, b"new moltenvk")
        spec = {"version": "1", "about": "test Wine", "app": "Wine.app", "wine": "wine", "moltenvk": "9.9"}
        patches = [
            mock.patch.object(config, "RUNTIME_DIR", root / "runtime"),
            mock.patch.object(config, "DOWNLOADS", root / "downloads"),
            mock.patch.dict(config.RUNTIMES, {"test": spec}),
            mock.patch.dict(config.MOLTENVK, {"9.9": {"url": archive.as_uri(), "sha256": sha, "member": MEMBER}}),
            mock.patch.object(runtime, "run"),  # codesign
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.lib = runtime.wine_dir("test") / "lib"
        self.lib.mkdir(parents=True)
        (self.lib / "libMoltenVK.dylib").write_bytes(b"bundled moltenvk")

    def tearDown(self):
        self.tmp.cleanup()

    def test_replaces_bundled_and_keeps_it(self):
        self.assertIsNone(runtime.moltenvk_version("test"))
        runtime.install_moltenvk("test")
        self.assertEqual((self.lib / "libMoltenVK.dylib").read_bytes(), b"new moltenvk")
        self.assertEqual((self.lib / "libMoltenVK.dylib.bundled").read_bytes(), b"bundled moltenvk")
        self.assertEqual(runtime.moltenvk_version("test"), "9.9")

    def test_second_install_keeps_the_bundled_copy(self):
        runtime.install_moltenvk("test")
        runtime.install_moltenvk("test")
        self.assertEqual((self.lib / "libMoltenVK.dylib.bundled").read_bytes(), b"bundled moltenvk")

    def test_runtime_without_moltenvk_is_left_alone(self):
        config.RUNTIMES["test"].pop("moltenvk")
        runtime.install_moltenvk("test")
        self.assertEqual((self.lib / "libMoltenVK.dylib").read_bytes(), b"bundled moltenvk")


if __name__ == "__main__":
    unittest.main()
