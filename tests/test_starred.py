"""Tests for metafiles.starred."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from gi.repository import GLib

from metafiles import starred
from metafiles.starred import StarredStore, data_dir, read_nautilus_starred

N1 = "file:///home/u/Dev/api/docs/spec.md"
N2 = "file:///home/u/Dev/api/docs/sub/old.md"
N_OFF = "file:///home/u/not-starred.txt"


def make_db(path, starred_uris, unstarred_uris=(), wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS Resource (ID INTEGER NOT NULL PRIMARY KEY, Uri TEXT, "
                 "BlankNode INTEGER DEFAULT 0, UNIQUE (Uri))")
    conn.execute('CREATE TABLE IF NOT EXISTS "nautilus:File" (ID INTEGER NOT NULL PRIMARY KEY, '
                 '"nautilus:starred" INTEGER)')
    conn.execute('DELETE FROM "nautilus:File"')
    conn.execute("DELETE FROM Resource")
    for i, uri in enumerate(list(starred_uris) + list(unstarred_uris), start=1):
        conn.execute("INSERT INTO Resource (ID, Uri) VALUES (?, ?)", (i, uri))
        conn.execute('INSERT INTO "nautilus:File" VALUES (?, ?)', (i, 1 if uri in starred_uris else 0))
    conn.commit()
    return conn


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "meta.db")
        make_db(self.db, {N1, N2}, {N_OFF}).close()
        self.store_dir = os.path.join(self.tmp, "data")

    def tearDown(self):
        self._tmp.cleanup()

    def store(self, watch=False):
        s = StarredStore(directory=self.store_dir, nautilus_db=self.db, watch=watch)
        self.changes = 0

        def on_changed(_s):
            self.changes += 1

        s.connect("changed", on_changed)
        return s


class ReadNautilusTest(Base):
    def test_reads_starred_only(self):
        self.assertEqual(read_nautilus_starred(self.db), {N1, N2})

    def test_missing_and_broken(self):
        self.assertEqual(read_nautilus_starred(os.path.join(self.tmp, "none.db")), set())
        bad = os.path.join(self.tmp, "bad.db")
        with open(bad, "w") as fh:
            fh.write("not a database at all" * 100)
        with mock.patch("sys.stderr") as err:
            self.assertEqual(read_nautilus_starred(bad), set())
        self.assertTrue(err.write.called)

    def test_read_only_does_not_modify(self):
        db = os.path.join(self.tmp, "wal.db")
        writer = make_db(db, {N1}, wal=True)  # writer stays open, like Nautilus
        try:
            files = [db, db + "-wal"]
            before = {f: (os.stat(f).st_mtime_ns, os.stat(f).st_size) for f in files}
            self.assertEqual(read_nautilus_starred(db), {N1})
            after = {f: (os.stat(f).st_mtime_ns, os.stat(f).st_size) for f in files}
            self.assertEqual(before, after)
        finally:
            writer.close()

    def test_real_database_smoke(self):
        self.assertIsInstance(read_nautilus_starred(), set)


class DataDirTest(unittest.TestCase):
    def test_env(self):
        with mock.patch.dict(os.environ, {"METAFILES_DATA_DIR": "/x/y"}):
            self.assertEqual(data_dir(), "/x/y")
        env = {k: v for k, v in os.environ.items() if k != "METAFILES_DATA_DIR"}
        with mock.patch.dict(os.environ, {**env, "XDG_DATA_HOME": "/xdg"}, clear=True):
            self.assertEqual(data_dir(), "/xdg/metafiles")
        env.pop("XDG_DATA_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(data_dir(), os.path.expanduser("~/.local/share/metafiles"))


class StoreTest(Base):
    def test_effective_set(self):
        s = self.store()
        self.assertEqual(s.uris(), sorted({N1, N2}))
        self.assertTrue(s.is_starred(N1) and s.is_from_nautilus(N1))
        self.assertFalse(s.is_starred(N_OFF))

    def test_star_unstar_own(self):
        s = self.store()
        own = "file:///home/u/a.txt"
        s.star(own)
        self.assertTrue(s.is_starred(own))
        self.assertFalse(s.is_from_nautilus(own))
        s.unstar(own)
        self.assertFalse(s.is_starred(own))
        self.assertEqual(self.changes, 2)
        s.unstar(own)  # no-op
        self.assertEqual(self.changes, 2)

    def test_unstar_nautilus_hides(self):
        s = self.store()
        s.unstar(N1)
        self.assertFalse(s.is_starred(N1))
        self.assertFalse(s.is_from_nautilus(N1))
        with open(s.path) as fh:
            self.assertEqual(json.load(fh), {"starred": [], "hidden": [N1]})
        s.star(N1)
        self.assertTrue(s.is_starred(N1) and s.is_from_nautilus(N1))
        with open(s.path) as fh:
            self.assertEqual(json.load(fh), {"starred": [], "hidden": []})

    def test_persistence_round_trip(self):
        s = self.store()
        s.star("file:///home/u/ą ę.txt")
        s.unstar(N2)
        s2 = StarredStore(directory=self.store_dir, nautilus_db=self.db, watch=False)
        self.assertEqual(s2.uris(), sorted({N1, "file:///home/u/ą ę.txt"}))
        self.assertEqual(s2.uris(), s.uris())

    def test_moved_file(self):
        s = self.store()
        s.star("file:///home/u/todo.md")
        s.moved("file:///home/u/todo.md", "file:///home/u/plan.md")
        self.assertTrue(s.is_starred("file:///home/u/plan.md"))
        self.assertFalse(s.is_starred("file:///home/u/todo.md"))

    def test_moved_folder_with_nautilus_children(self):
        s = self.store()
        s.star("file:///home/u/Dev/api/docs")
        s.star("file:///home/u/Dev/api/docsX")  # same prefix, but not a child
        s.moved("file:///home/u/Dev/api/docs", "file:///home/u/Dev/api/doc")
        expected = sorted({
            "file:///home/u/Dev/api/doc", "file:///home/u/Dev/api/doc/spec.md",
            "file:///home/u/Dev/api/doc/sub/old.md", "file:///home/u/Dev/api/docsX",
        })
        self.assertEqual(s.uris(), expected)
        self.assertFalse(s.is_from_nautilus("file:///home/u/Dev/api/doc/spec.md"))
        self.assertFalse(s.is_starred(N1))
        self.assertEqual(self.changes, 3)

    def test_removed(self):
        s = self.store()
        s.star("file:///home/u/Dev/api/docs/own.txt")
        s.removed("file:///home/u/Dev/api/docs")
        self.assertEqual(s.uris(), [])

    def test_reload_nautilus_and_prune(self):
        s = self.store()
        s.unstar(N2)
        make_db(self.db, {N1, N_OFF}).close()
        s.reload_nautilus()
        self.assertEqual(s.uris(), sorted({N1, N_OFF}))
        self.assertEqual(self.changes, 2)
        s.reload_nautilus()
        self.assertEqual(self.changes, 2)
        # N2 is no longer starred in Nautilus, so its hidden entry is pruned.
        with open(s.path) as fh:
            self.assertEqual(json.load(fh)["hidden"], [])

    def test_watch_reloads(self):
        s = self.store(watch=True)
        try:
            loop = GLib.MainLoop()
            s.connect("changed", lambda _s: loop.quit())
            GLib.timeout_add(100, lambda: make_db(self.db, {N_OFF}).close())
            timeout = GLib.timeout_add(5000, loop.quit)
            loop.run()
            GLib.source_remove(timeout)
            self.assertEqual(s.uris(), [N_OFF])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
