"""Local (GIO) file operations, used when Nautilus' service is unavailable, with undo."""

import os
import shutil
import tempfile
import unittest

from gi.repository import Gio, GLib

import metafiles  # noqa: F401
from metafiles.operations import FileOperations


class StubUI:
    def __init__(self):
        self.errors = []
        self.conflict_answer = ("skip", "", False)
        self.moves = []

    def error(self, title, detail=""):
        self.errors.append((title, detail))

    def confirm_async(self, title, body, accept, callback):
        callback(True)

    def ask_conflict(self, src, dst, done):
        done(self.conflict_answer)

    def file_moved(self, old, new):
        self.moves.append((old, new))


def wait(ops, timeout=5):
    """Run the main loop until no job is running."""
    loop = GLib.MainLoop()
    deadline = GLib.get_monotonic_time() + timeout * 1_000_000

    def check():
        if not ops.jobs or GLib.get_monotonic_time() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(20, check)
    loop.run()
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class LocalOperationsTest(unittest.TestCase):
    def setUp(self):
        # Under the home filesystem so that the trash works (tmpfs has none).
        base = os.path.join(os.path.expanduser("~"), ".cache")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="metafiles-test-", dir=base)
        self.ui = StubUI()
        self.ops = FileOperations(self.ui, use_nautilus=False)
        os.makedirs(self.p("src"))
        os.makedirs(self.p("dst"))
        with open(self.p("src", "a.txt"), "w") as f:
            f.write("new")
        with open(self.p("dst", "a.txt"), "w") as f:
            f.write("old")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self._purge_trash()

    def _purge_trash(self):
        trash = Gio.File.new_for_uri("trash:///")
        for info in trash.enumerate_children("standard::name,trash::orig-path", Gio.FileQueryInfoFlags.NONE, None):
            orig = info.get_attribute_byte_string("trash::orig-path") or ""
            if orig.startswith(self.dir):
                trash.get_child(info.get_name()).delete(None)

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def u(self, *parts):
        return Gio.File.new_for_path(self.p(*parts)).get_uri()

    def read(self, *parts):
        with open(self.p(*parts)) as f:
            return f.read()

    def test_copy_conflict_skip_and_replace(self):
        self.ops.copy([self.u("src", "a.txt")], self.u("dst"))
        wait(self.ops)
        self.assertEqual(self.read("dst", "a.txt"), "old")
        self.ui.conflict_answer = ("replace", "", False)
        self.ops.copy([self.u("src", "a.txt")], self.u("dst"))
        wait(self.ops)
        self.assertEqual(self.read("dst", "a.txt"), "new")

    def test_copy_conflict_rename_and_undo(self):
        self.ui.conflict_answer = ("rename", "a (2).txt", False)
        self.ops.copy([self.u("src", "a.txt")], self.u("dst"))
        wait(self.ops)
        self.assertEqual(self.read("dst", "a (2).txt"), "new")

    def test_move_and_undo(self):
        os.unlink(self.p("dst", "a.txt"))
        self.ops.move([self.u("src", "a.txt")], self.u("dst"))
        wait(self.ops)
        self.assertTrue(os.path.exists(self.p("dst", "a.txt")))
        self.assertFalse(os.path.exists(self.p("src", "a.txt")))
        self.ops.undo()
        wait(self.ops)
        self.assertTrue(os.path.exists(self.p("src", "a.txt")))

    def test_rename_undo_redo(self):
        self.ops.rename(self.u("src", "a.txt"), "b.txt")
        self.assertTrue(os.path.exists(self.p("src", "b.txt")))
        self.ops.undo()
        self.assertTrue(os.path.exists(self.p("src", "a.txt")))
        self.ops.redo()
        self.assertTrue(os.path.exists(self.p("src", "b.txt")))

    def test_duplicate_new_file_and_folder(self):
        created = []
        self.ops.duplicate([self.u("src", "a.txt")], created.extend)
        wait(self.ops)
        self.assertEqual(len(created), 1)
        self.assertTrue(os.path.exists(Gio.File.new_for_uri(created[0]).get_path()))
        self.ops.new_file(self.u("src"), "notes.md")
        self.assertEqual(self.read("src", "notes.md"), "")
        self.ops.create_folder(self.u("src"), "folder")
        self.assertTrue(os.path.isdir(self.p("src", "folder")))

    def test_trash_and_undo_restores(self):
        self.ops.trash([self.u("src", "a.txt")])
        self.assertFalse(os.path.exists(self.p("src", "a.txt")))
        self.ops.undo()
        self.assertEqual(self.read("src", "a.txt"), "new")

    def test_delete_permanently(self):
        os.makedirs(self.p("src", "deep", "er"))
        self.ops.delete([self.u("src", "deep")])
        wait(self.ops)
        self.assertFalse(os.path.exists(self.p("src", "deep")))
        self.assertEqual(self.ui.errors, [])


if __name__ == "__main__":
    unittest.main()
