"""The navigation lock: one gate decides what a pane of a locked project may show."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from gi.repository import Gio

import metafiles  # noqa: F401
from metafiles import fileutil
from metafiles.project import ProjectView


def project(root, lock=True, loose=False):
    stub = SimpleNamespace(root=root, loose=loose, state=SimpleNamespace(lock=lock))
    stub.lock = not loose and lock
    stub.contains = lambda path: ProjectView.contains(stub, path)
    stub.allows = lambda uri: ProjectView.allows(stub, uri)
    stub.root_uri = Gio.File.new_for_path(root).get_uri()
    return stub


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(os.path.join(self.tmp.name, "proj"))
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.tmp.name, "outside"))
        os.symlink(os.path.join(self.tmp.name, "outside"), os.path.join(self.root, "escape"))
        os.makedirs(os.path.join(self.tmp.name, "proj-other"))

    def tearDown(self):
        self.tmp.cleanup()

    def uri(self, path):
        return Gio.File.new_for_path(path).get_uri()

    def test_inside_and_outside(self):
        p = project(self.root)
        self.assertTrue(p.allows(self.uri(self.root)))
        self.assertTrue(p.allows(self.uri(os.path.join(self.root, "src"))))
        self.assertFalse(p.allows(self.uri(self.tmp.name)))
        self.assertFalse(p.allows(self.uri("/etc")))

    def test_symlink_pointing_outside_is_refused(self):
        self.assertFalse(project(self.root).allows(self.uri(os.path.join(self.root, "escape"))))

    def test_prefix_sibling_is_not_inside(self):
        self.assertFalse(project(self.root).allows(self.uri(os.path.join(self.tmp.name, "proj-other"))))

    def test_virtual_places_allowed(self):
        p = project(self.root)
        for uri in ("recent:///", "starred:///", "trash:///", "git-changes:///"):
            self.assertTrue(p.allows(uri))

    def test_unlocked_and_loose(self):
        self.assertTrue(project(self.root, lock=False).allows(self.uri("/etc")))
        self.assertTrue(project(fileutil.HOME, loose=True).allows(self.uri("/usr")))

    def test_validate_location_falls_back(self):
        p = project(self.root)
        gone = self.uri(os.path.join(self.root, "src", "deleted", "deeper"))
        self.assertEqual(ProjectView.validate_location(p, gone), self.uri(os.path.join(self.root, "src")))
        self.assertEqual(ProjectView.validate_location(p, self.uri("/etc")), p.root_uri)


if __name__ == "__main__":
    unittest.main()
