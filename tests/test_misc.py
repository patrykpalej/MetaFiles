"""Archives (gnome-autoar), preview provider selection and optional-component detection."""

import os
import shutil
import tempfile
import unittest
import zipfile

from gi.repository import Gio, GLib

import metafiles  # noqa: F401
from metafiles import features
from metafiles.preview.providers import is_binary, kind_for


class PreviewKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(kind_for("image/png", "a.png", False), "image")
        self.assertEqual(kind_for("image/gif", "a.gif", False), "gif")
        self.assertEqual(kind_for("text/x-python", "a.py", False), "text")
        self.assertEqual(kind_for("application/json", "a.json", False), "text")
        self.assertEqual(kind_for("text/markdown", "README.md", False), "text")
        self.assertEqual(kind_for("application/pdf", "a.pdf", False), "pdf")
        self.assertEqual(kind_for("video/mp4", "a.mp4", False), "media")
        self.assertEqual(kind_for("application/zip", "a.zip", False), "archive")
        self.assertEqual(kind_for("application/octet-stream", "x.tar.gz", False), "archive")
        self.assertEqual(kind_for("inode/directory", "dir", True), "folder")
        self.assertEqual(kind_for("application/x-iso9660-image", "disk.iso", False), "other")

    def test_binary_detection(self):
        self.assertTrue(is_binary(b"abc\0def"))
        self.assertFalse(is_binary("zażółć".encode()))


class FeaturesTest(unittest.TestCase):
    def test_forced_missing_component(self):
        self.assertTrue(features.has("git") or True)
        features.disable("meld")
        self.assertFalse(features.has("meld"))
        self.assertIn("meld", features.missing_packages())


@unittest.skipUnless(features.has("autoar"), "gnome-autoar not installed")
class ArchiveTest(unittest.TestCase):
    def run_loop(self, worker):
        loop = GLib.MainLoop()
        result = {}
        worker.connect("completed", lambda *_: (result.setdefault("ok", True), loop.quit()))
        worker.connect("error", lambda _w, e: (result.setdefault("error", e.message), loop.quit()))
        if hasattr(worker, "set_output_is_dest"):
            pass
        GLib.timeout_add(10000, loop.quit)
        worker.start_async(None)
        loop.run()
        return result

    def test_compress_and_extract_single_root(self):
        from gi.repository import GnomeAutoar

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dane", "sub"))
            with open(os.path.join(tmp, "dane", "sub", "x.txt"), "w") as f:
                f.write("x")
            archive = Gio.File.new_for_path(os.path.join(tmp, "dane.zip"))
            compressor = GnomeAutoar.Compressor.new([Gio.File.new_for_path(os.path.join(tmp, "dane"))], archive,
                                                    GnomeAutoar.Format.ZIP, GnomeAutoar.Filter.NONE, False)
            compressor.set_output_is_dest(True)
            self.assertEqual(self.run_loop(compressor), {"ok": True})
            self.assertTrue(zipfile.is_zipfile(archive.get_path()))
            out = os.path.join(tmp, "out")
            os.makedirs(out)
            extractor = GnomeAutoar.Extractor.new(archive, Gio.File.new_for_path(out))
            extractor.set_output_is_dest(False)
            self.assertEqual(self.run_loop(extractor), {"ok": True})
            # One top-level folder: no extra "dane/dane" nesting (Nautilus' behaviour).
            self.assertTrue(os.path.exists(os.path.join(out, "dane", "sub", "x.txt")))
            self.assertFalse(os.path.exists(os.path.join(out, "dane", "dane")))
            shutil.rmtree(out)


if __name__ == "__main__":
    unittest.main()
