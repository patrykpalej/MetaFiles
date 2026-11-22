"""Remote locations must never be touched synchronously, and a stuck main loop gets unstuck."""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import metafiles  # noqa: F401
from metafiles import fileutil, remote
from metafiles.fileview import HiddenNames
from metafiles.git import find_repo_root

UID = os.getuid()
MOUNTINFO = f"""\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
414 1395 0:130 / /run/user/{UID}/gvfs rw,nosuid,nodev,relatime shared:1051 - fuse.gvfsd-fuse gvfsd-fuse rw
415 22 0:131 / /mnt/my\\040server rw,relatime - fuse.sshfs me@host:/srv rw
416 22 0:132 / /mnt/nas rw,relatime - nfs4 nas:/export rw
417 416 0:133 / /mnt/nas/local rw,relatime - ext4 /dev/sdb1 rw
418 22 0:71 / /run/user/{UID}/doc rw - fuse.portal portal rw
"""


class FakeMounts:
    """Point remote.MOUNTS at a fake mountinfo file for the duration of a test."""

    def __init__(self, text=MOUNTINFO):
        self.text = text

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        path = os.path.join(self.dir.name, "mountinfo")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.text)
        self.patch = mock.patch.object(remote, "MOUNTS", remote._MountTable(path))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.dir.cleanup()


def _no_fs(*_args, **_kwargs):
    raise AssertionError("touched the filesystem of a remote path")


class DetectionTest(unittest.TestCase):
    def test_parse_mountinfo_keeps_remote_types_deepest_first(self):
        mounts = remote.parse_mountinfo(MOUNTINFO)
        self.assertEqual([m.root for m in mounts], [f"/run/user/{UID}/gvfs", "/mnt/my server", "/mnt/nas"])
        self.assertEqual({m.fstype for m in mounts}, {"fuse.gvfsd-fuse", "fuse.sshfs", "nfs4"})
        self.assertEqual(next(m for m in mounts if m.fstype == "nfs4").device, "0:132")

    def test_is_remote_path(self):
        with FakeMounts():
            self.assertTrue(remote.is_remote_path(f"/run/user/{UID}/gvfs/sftp:host=mikrus/root"))
            self.assertTrue(remote.is_remote_path("/mnt/my server/a/b"))
            self.assertTrue(remote.is_remote_path("/mnt/nas"))
            self.assertTrue(remote.is_remote_path("/mnt/nas/sub/../file"))
            self.assertFalse(remote.is_remote_path("/mnt/nasty"))
            self.assertFalse(remote.is_remote_path("/home/me/CloudDrive"))
            self.assertFalse(remote.is_remote_path(f"/run/user/{UID}/doc/abc"))  # document portal is local
            self.assertFalse(remote.is_remote_path("relative/path"))
            self.assertFalse(remote.is_remote_path(None))

    def test_gvfs_root_counts_without_a_mount_table(self):
        with FakeMounts(""):
            self.assertTrue(remote.is_remote_path(f"/run/user/{UID}/gvfs/smb-share:server=x,share=y/f"))

    def test_is_remote_location(self):
        with FakeMounts():
            self.assertTrue(remote.is_remote_location("sftp://mikrus/root/www"))
            self.assertTrue(remote.is_remote_location("smb://nas/share/"))
            self.assertFalse(remote.is_remote_location("trash:///"))
            self.assertFalse(remote.is_remote_location("file:///home"))
            self.assertTrue(remote.is_remote_location("file:///mnt/nas/x"))
            self.assertFalse(remote.is_remote_location(""))

    def test_local_path(self):
        from gi.repository import Gio

        with FakeMounts():
            self.assertEqual(remote.local_path(Gio.File.new_for_path("/home")), "/home")
            self.assertIsNone(remote.local_path(Gio.File.new_for_path("/mnt/nas/x")))
            self.assertIsNone(remote.local_path(None))


class NoBlockingTest(unittest.TestCase):
    """The GTK-thread helpers must answer for remote paths without any syscall on them."""

    def test_real_does_not_resolve_remote_paths(self):
        with FakeMounts(), mock.patch("os.path.realpath", _no_fs):
            self.assertEqual(fileutil.real("/mnt/nas/a/../b"), "/mnt/nas/b")

    def test_find_repo_root_skips_remote_paths(self):
        with FakeMounts(), mock.patch("os.path.realpath", _no_fs), mock.patch("os.path.lexists", _no_fs):
            self.assertIsNone(find_repo_root("/mnt/my server/repo/src"))

    def test_hidden_names_skips_remote_folders(self):
        with FakeMounts(), mock.patch("os.stat", _no_fs):
            self.assertEqual(HiddenNames().names("/mnt/nas/folder"), frozenset())

    def test_project_on_remote_root_is_available_without_stat(self):
        from metafiles.project import ProjectView

        project = ProjectView.__new__(ProjectView)
        project.loose, project.root = False, "/mnt/nas/proj"
        with FakeMounts(), mock.patch("os.path.isdir", _no_fs):
            self.assertTrue(project.available)


class DisconnectTest(unittest.TestCase):
    def test_gvfs_mounts_are_unmounted_once_per_server(self):
        calls = []
        with FakeMounts(), mock.patch.object(remote, "_run", lambda argv, timeout=15: calls.append(argv) or True), \
                mock.patch("shutil.which", lambda tool: f"/usr/bin/{tool}"):
            done = remote.disconnect_sync(["sftp://mikrus/root/a", "sftp://mikrus/root/b", "smb://nas/share/"])
        self.assertEqual(done, ["sftp://mikrus", "smb://nas"])
        self.assertEqual(calls, [["gio", "mount", "--unmount", "--force", "sftp://mikrus/root/a"],
                                 ["gio", "mount", "--unmount", "--force", "smb://nas/share/"]])

    def test_fuse_mounts_are_lazily_unmounted(self):
        calls = []
        with FakeMounts(), mock.patch.object(remote, "_run", lambda argv, timeout=15: calls.append(argv) or True), \
                mock.patch("shutil.which", lambda tool: f"/usr/bin/{tool}"):
            done = remote.disconnect_sync(["/mnt/my server/x", "file:///mnt/my%20server/y", "/mnt/nas/z"])
        self.assertEqual(done, ["/mnt/my server"])  # NFS cannot be unmounted by a user
        self.assertEqual(calls, [["fusermount3", "-uz", "/mnt/my server"]])

    def test_abort_only_remote_fuse_connections_with_waiting_requests(self):
        with tempfile.TemporaryDirectory() as conns, FakeMounts(), \
                mock.patch.object(remote, "FUSE_CONNECTIONS", conns):
            for minor, waiting in (("130", "2"), ("131", "0"), ("71", "5")):
                os.makedirs(os.path.join(conns, minor))
                with open(os.path.join(conns, minor, "waiting"), "w") as f:
                    f.write(waiting + "\n")
                open(os.path.join(conns, minor, "abort"), "w").close()
            aborted = remote.abort_stuck_fuse()
            self.assertEqual([m.root for m in aborted], [f"/run/user/{UID}/gvfs"])
            with open(os.path.join(conns, "130", "abort")) as f:
                self.assertEqual(f.read(), "1")
            for untouched in ("131", "71"):  # idle sshfs, and the local document portal
                with open(os.path.join(conns, untouched, "abort")) as f:
                    self.assertEqual(f.read(), "")


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.cache.cleanup()

    def _run(self, state, wchan, until, wait=3):
        dog = remote.Watchdog()
        dog.POLL_S, dog.RECOVERED_S, dog.STALL_S, dog.ABORT_S, dog.LONG_STALL_S = 0.01, 0.5, 1, 2, 4
        dog._beat = time.monotonic() - 10
        calls = []
        stuck = remote.RemoteMount(f"/run/user/{UID}/gvfs", "fuse.gvfsd-fuse", "0:130")
        with mock.patch.object(remote, "_thread_state", lambda tid: (state, wchan)), \
                mock.patch.object(remote, "tracked", lambda: ["sftp://mikrus/root"]), \
                mock.patch.object(remote, "disconnect_sync", lambda locs: calls.append(("unmount", locs)) or locs), \
                mock.patch.object(remote, "abort_stuck_fuse", lambda: calls.append(("abort",)) or [stuck]), \
                mock.patch.object(remote, "restart_gvfs_fuse", lambda mount: calls.append(("restart", mount.root))):
            thread = threading.Thread(target=dog._watch, daemon=True)
            thread.start()
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not until(calls):
                time.sleep(0.01)
            time.sleep(0.05)
            dog._beat = time.monotonic()  # the main loop runs again
            time.sleep(0.05)
            dog.stop()
            thread.join(1)
        return dog, calls

    def test_stuck_on_fuse_unmounts_then_aborts_and_restarts_gvfs(self):
        dog, calls = self._run("D", "request_wait_answer", lambda c: any(x[0] == "restart" for x in c))
        self.assertEqual(calls, [("unmount", ["sftp://mikrus/root"]), ("abort",),
                                 ("restart", f"/run/user/{UID}/gvfs")])
        with open(remote.log_path()) as f:
            log = f.read()
        self.assertIn("main loop stuck", log)
        self.assertIn("aborted the FUSE connection", log)
        self.assertIn("main loop running again", log)
        self.assertEqual(dog._actions, [])

    def test_busy_main_thread_is_left_alone(self):
        _dog, calls = self._run("R", "", lambda c: False, wait=0.3)
        self.assertEqual(calls, [])

    def test_other_waits_never_abort_fuse(self):
        _dog, calls = self._run("S", "futex_wait_queue", lambda c: bool(c))
        self.assertEqual(calls, [("unmount", ["sftp://mikrus/root"])])


if __name__ == "__main__":
    unittest.main()
