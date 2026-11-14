import locale
import os
import tempfile
import unittest

import metafiles  # noqa: F401
from metafiles import fileutil
from metafiles.bookmarks import Bookmarks, parse, serialize
from metafiles.clipboard import parse_gnome_copied, parse_uri_list
from metafiles.fileview import SizeCalculator


class FileUtilTest(unittest.TestCase):
    def test_copy_names_like_nautilus(self):
        from metafiles.i18n import _

        copy = _("copy")
        self.assertEqual(fileutil.copy_name("plik.txt", set()), f"plik ({copy}).txt")
        self.assertEqual(fileutil.copy_name("plik.txt", {f"plik ({copy}).txt"}), f"plik ({copy} 2).txt")
        self.assertEqual(fileutil.copy_name(f"plik ({copy}).txt", {f"plik ({copy} 2).txt"}), f"plik ({copy} 3).txt")
        self.assertEqual(fileutil.copy_name("archiwum.tar.gz", set()), f"archiwum ({copy}).tar.gz")

    def test_natural_sort(self):
        locale.setlocale(locale.LC_ALL, "")
        # The same collation as Nautilus (g_utf8_collate_key_for_filename): numbers compare as numbers.
        names = ["plik10", "plik2", "plik1", "ala"]
        self.assertEqual(sorted(names, key=fileutil.natural_key), ["ala", "plik1", "plik2", "plik10"])
        self.assertEqual(sorted(names, key=fileutil.natural_key_py), ["ala", "plik1", "plik2", "plik10"])

    def test_validate_filename(self):
        self.assertIsNone(fileutil.validate_filename("ok.txt", set()))
        self.assertIsNotNone(fileutil.validate_filename("a/b", set()))
        self.assertIsNotNone(fileutil.validate_filename("..", set(), is_folder=True))
        self.assertIsNotNone(fileutil.validate_filename("x", {"x"}))
        self.assertIsNotNone(fileutil.validate_filename("x" * 300, set()))

    def test_relative_label(self):
        self.assertEqual(fileutil.relative_label("/p", "/p"), ".")
        self.assertEqual(fileutil.relative_label("/p/src/lib", "/p"), "src/lib")
        self.assertTrue(fileutil.relative_label("/etc", "/p").startswith("/etc"))

    def test_clipboard_formats(self):
        self.assertEqual(parse_gnome_copied(b"cut\nfile:///a\nfile:///b"), (True, ["file:///a", "file:///b"]))
        self.assertEqual(parse_gnome_copied(b"copy\r\nfile:///a\r\n"), (False, ["file:///a"]))
        self.assertEqual(parse_gnome_copied(b"garbage"), (False, []))
        self.assertEqual(parse_uri_list(b"# comment\r\nfile:///a\r\nfile:///b\r\n"), ["file:///a", "file:///b"])


class BookmarksTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gtk-3.0", "bookmarks")
            marks = Bookmarks(path, watch=False)
            marks.add("file:///home/a%20b", "Moje")
            marks.add("file:///srv")
            marks.add("file:///srv")
            marks.move("file:///srv", 0)
            marks.rename("file:///home/a%20b", "Inne")
            self.assertEqual(Bookmarks(path, watch=False).entries, [("file:///srv", ""), ("file:///home/a%20b", "Inne")])
            marks.remove("file:///srv")
            self.assertEqual(parse(open(path).read()), [("file:///home/a%20b", "Inne")])
        self.assertEqual(serialize([("u", "")]), "u\n")


class SizeTest(unittest.TestCase):
    def test_hardlinks_counted_once_symlinks_not_followed(self):
        from gi.repository import GLib

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "d", "sub"))
            with open(os.path.join(tmp, "d", "a"), "wb") as f:
                f.write(b"x" * 1000)
            os.link(os.path.join(tmp, "d", "a"), os.path.join(tmp, "d", "sub", "hard"))
            with open(os.path.join(tmp, "big"), "wb") as f:
                f.write(b"y" * 50000)
            os.symlink(os.path.join(tmp, "big"), os.path.join(tmp, "d", "link"))
            calc = SizeCalculator()
            result = []
            loop = GLib.MainLoop()
            calc.compute(os.path.join(tmp, "d"), lambda total: (result.append(total), loop.quit()))
            GLib.timeout_add(5000, loop.quit)
            loop.run()
            link_size = os.lstat(os.path.join(tmp, "d", "link")).st_size
            dir_size = os.lstat(os.path.join(tmp, "d", "sub")).st_size
            self.assertEqual(result, [1000 + link_size + dir_size])


if __name__ == "__main__":
    unittest.main()
