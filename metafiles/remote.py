"""Remote (network) locations: detection, forced disconnects and a main-loop watchdog.

A stalled network mount (sftp/smb through the gvfs FUSE bridge, sshfs, NFS, ...) blocks
every syscall on its paths with no timeout, and a process stuck that way cannot even be
killed. So the GTK thread must never stat, list or realpath such paths synchronously;
``is_remote_path`` / ``local_path`` let callers skip that work, and the ``Watchdog``
cuts the connection if the main loop gets stuck anyway.
"""

import faulthandler
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from gi.repository import Gio, GLib

# Seconds a remote folder may take to answer before the pane gives up and disconnects.
REMOTE_TIMEOUT_S = 20

REMOTE_FSTYPES = frozenset({
    "fuse.gvfsd-fuse", "fuse.sshfs", "sshfs", "fuse.rclone", "fuse.s3fs", "fuse.curlftpfs",
    "fuse.davfs", "davfs", "fuse.smbnetfs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "9p", "afs",
    "ceph", "fuse.ceph", "glusterfs", "fuse.glusterfs", "fuse.goofys", "fuse.gcsfuse",
})
# Schemes that are local even though they are not file:// (virtual or local-backed gvfs).
LOCAL_SCHEMES = frozenset({"file", "trash", "recent", "starred", "git-changes", "search", "burn",
                           "computer", "admin", "archive"})


# --- mount table ----------------------------------------------------------------------

class RemoteMount:
    __slots__ = ("root", "fstype", "device")

    def __init__(self, root, fstype, device):
        self.root, self.fstype, self.device = root, fstype, device

    def contains(self, path):
        return path == self.root or path.startswith(self.root.rstrip("/") + "/")

    def __repr__(self):
        return f"RemoteMount({self.root!r}, {self.fstype!r}, {self.device!r})"


def _unescape(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text):
    """Remote mounts from /proc/self/mountinfo text, deepest mount point first."""
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields, rest = left.split(), right.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        if rest[0] in REMOTE_FSTYPES:
            mounts.append(RemoteMount(_unescape(fields[4]), rest[0], fields[2]))
    mounts.sort(key=lambda m: -len(m.root))
    return mounts


class _MountTable:
    """Cached remote mounts; /proc is never a network filesystem, so reading it is safe."""

    TTL = 2.0

    def __init__(self, source="/proc/self/mountinfo"):
        self.source = source
        self._lock = threading.Lock()
        self._mounts = []
        self._stamp = -1e9

    def mounts(self):
        with self._lock:
            now = time.monotonic()
            if now - self._stamp > self.TTL:
                try:
                    with open(self.source, encoding="utf-8", errors="replace") as f:
                        self._mounts = parse_mountinfo(f.read())
                except OSError:
                    self._mounts = []
                self._stamp = now
            return self._mounts


MOUNTS = _MountTable()
FUSE_CONNECTIONS = "/sys/fs/fuse/connections"
GVFS_FUSE_ROOTS = (f"/run/user/{os.getuid()}/gvfs", os.path.join(os.path.expanduser("~"), ".gvfs"))


def remote_mount_for(path):
    if not path:
        return None
    path = os.path.normpath(path)
    for mount in MOUNTS.mounts():
        if mount.contains(path):
            return mount
    return None


def is_remote_path(path):
    """True for paths on a network filesystem (pure string checks, never touches the path)."""
    if not path or not os.path.isabs(path):
        return False
    path = os.path.normpath(path)
    if any(path == root or path.startswith(root + "/") for root in GVFS_FUSE_ROOTS):
        return True
    return remote_mount_for(path) is not None


def _as_file(location):
    if isinstance(location, Gio.File):
        return location
    if "://" in location:
        return Gio.File.new_for_uri(location)
    return Gio.File.new_for_path(location)


def is_remote_location(location):
    """True for a remote URI (sftp://, smb://, ...) or a path on a network mount."""
    if not location:
        return False
    if isinstance(location, str) and "://" in location:
        scheme = location.split("://", 1)[0].lower()
        if scheme not in LOCAL_SCHEMES:
            return True
        if scheme != "file":
            return False
    gf = _as_file(location)
    scheme = (gf.get_uri_scheme() or "").lower()
    if scheme not in LOCAL_SCHEMES:
        return True
    return scheme == "file" and is_remote_path(gf.get_path())


def local_path(gf):
    """gf.get_path(), or None when the file lives on a network mount (or has no path)."""
    if gf is None:
        return None
    path = gf.get_path()
    return None if path is None or is_remote_path(path) else path


# --- disconnecting ----------------------------------------------------------------------

_tracked_lock = threading.Lock()
_tracked = {}  # uri -> last use (monotonic); remote locations the app has opened


def track(location):
    """Remember a remote location so the watchdog knows which connections to cut."""
    if not is_remote_location(location):
        return
    uri = _as_file(location).get_uri()  # a gvfs FUSE path maps back to its sftp:// URI
    with _tracked_lock:
        _tracked[uri] = time.monotonic()
        while len(_tracked) > 32:
            del _tracked[min(_tracked, key=_tracked.get)]


def tracked():
    with _tracked_lock:
        return list(_tracked)


def _mount_key(uri):
    """Scheme and authority: one gvfs mount per server (sftp://host/a and sftp://host/b)."""
    if "://" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    return scheme + "://" + rest.split("/", 1)[0]


def _run(argv, timeout=15):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def disconnect_sync(locations):
    """Force-unmount the mounts holding locations; safe to call from any thread.

    gvfs locations (sftp://...) are unmounted through gvfsd, which stops the backend and
    makes the FUSE bridge fail pending requests; other FUSE mounts (sshfs, rclone) are
    lazily unmounted. Returns the URIs/paths that were disconnected.
    """
    done, seen = [], set()
    for location in locations:
        if "://" in location and not location.startswith("file://"):
            key = _mount_key(location)
            if key in seen:
                continue
            seen.add(key)
            if shutil.which("gio") and _run(["gio", "mount", "--unmount", "--force", location]):
                done.append(key)
            continue
        path = Gio.File.new_for_uri(location).get_path() if location.startswith("file://") else location
        mount = remote_mount_for(path)
        if mount is None or mount.root in seen or not mount.fstype.startswith("fuse"):
            continue
        seen.add(mount.root)
        for tool in ("fusermount3", "fusermount"):
            if shutil.which(tool) and _run([tool, "-uz", mount.root]):
                done.append(mount.root)
                break
    return done


def disconnect_async(locations, callback=None):
    """disconnect_sync in a worker thread; callback(disconnected) runs on the main loop."""
    locations = [_as_file(loc).get_uri() if isinstance(loc, Gio.File) else loc for loc in locations]

    def work():
        result = disconnect_sync(locations)
        with _tracked_lock:
            for uri in list(_tracked):
                if _mount_key(uri) in result or uri in locations:
                    del _tracked[uri]
        if callback is not None:
            GLib.idle_add(lambda: callback(result) and False)

    threading.Thread(target=work, name="metafiles-disconnect", daemon=True).start()


def abort_stuck_fuse():
    """Abort remote FUSE connections that have requests waiting; returns the aborted mounts.

    Writing to /sys/fs/fuse/connections/<n>/abort (owned by the mounting user) fails every
    pending request with ENOTCONN, which is the only thing that frees a thread stuck in
    request_wait_answer once the FUSE daemon itself hangs.
    """
    aborted = []
    for mount in MOUNTS.mounts():
        if not mount.fstype.startswith("fuse"):
            continue
        minor = mount.device.split(":")[-1]
        conn = os.path.join(FUSE_CONNECTIONS, minor)
        try:
            with open(f"{conn}/waiting", encoding="ascii") as f:
                if int(f.read().strip() or 0) <= 0:
                    continue
            with open(f"{conn}/abort", "w", encoding="ascii") as f:
                f.write("1")
        except (OSError, ValueError):
            continue
        aborted.append(mount)
    return aborted


def restart_gvfs_fuse(mount):
    """Bring the gvfs FUSE bridge back after its connection was aborted."""
    for tool in ("fusermount3", "fusermount"):
        if shutil.which(tool) and _run([tool, "-uz", mount.root]):
            break
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "restart", "gvfs-daemon.service"], timeout=30)


# --- watchdog ---------------------------------------------------------------------------

def log_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "metafiles", "hang.log")


def _thread_state(tid):
    """(state letter, wchan) of a thread of this process, read from /proc."""
    try:
        with open(f"/proc/self/task/{tid}/stat", encoding="ascii", errors="replace") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        state = "?"
    try:
        with open(f"/proc/self/task/{tid}/wchan", encoding="ascii", errors="replace") as f:
            wchan = f.read().strip()
    except OSError:
        wchan = ""
    return state, wchan


class Watchdog:
    """Notices a stuck GTK main loop and cuts the network connection it is waiting on.

    The main loop ticks a heartbeat every second. If it stops while the main thread is
    asleep in the kernel (not busy computing), a thread of ours:
      1. dumps all Python stacks to ~/.cache/metafiles/hang.log (once per stall),
      2. force-unmounts the remote locations the app has opened (after STALL_S when the
         thread waits on FUSE, after LONG_STALL_S otherwise),
      3. if it is still stuck on FUSE after ABORT_S, aborts the stuck FUSE connection and
         restarts the gvfs FUSE bridge.
    on_recovered(message) is called on the main loop once it runs again.
    """

    POLL_S = 1.0
    RECOVERED_S = 3
    STALL_S = 20
    ABORT_S = 35
    LONG_STALL_S = 45
    FUSE_WAIT = "request_wait_answer"

    def __init__(self, on_recovered=None):
        self.on_recovered = on_recovered
        self._beat = time.monotonic()
        self._tid = threading.main_thread().native_id
        self._stop = threading.Event()
        self._source = 0
        self._actions = []

    def start(self):
        self._source = GLib.timeout_add(1000, self._tick)
        threading.Thread(target=self._watch, name="metafiles-watchdog", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0

    def _tick(self):
        self._beat = time.monotonic()
        return True

    def _watch(self):
        dumped = unmounted = aborted = False
        while not self._stop.wait(self.POLL_S):
            stalled_for = time.monotonic() - self._beat
            if stalled_for < self.RECOVERED_S:
                if self._actions:
                    self._report_recovery()
                dumped = unmounted = aborted = False
                continue
            state, wchan = _thread_state(self._tid)
            if state == "R":  # busy computing, not blocked: leave it alone
                continue
            on_fuse = wchan == self.FUSE_WAIT
            if stalled_for >= self.STALL_S and not dumped:
                dumped = True
                self._dump(stalled_for, state, wchan)
            if not unmounted and (stalled_for >= self.LONG_STALL_S or (on_fuse and stalled_for >= self.STALL_S)):
                unmounted = True
                done = disconnect_sync(tracked())
                if done:
                    self._note(f"force-unmounted {', '.join(done)}")
            if on_fuse and not aborted and stalled_for >= self.ABORT_S:
                aborted = True
                for mount in abort_stuck_fuse():
                    self._note(f"aborted the FUSE connection of {mount.root} ({mount.fstype})")
                    if mount.fstype == "fuse.gvfsd-fuse":
                        restart_gvfs_fuse(mount)
                        self._note("restarted gvfs-daemon.service")

    def _note(self, text):
        self._actions.append(text)
        self._log(text)

    def _log(self, text):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} metafiles watchdog: {text}\n"
        sys.stderr.write(line)
        try:
            os.makedirs(os.path.dirname(log_path()), exist_ok=True)
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _dump(self, stalled_for, state, wchan):
        self._log(f"main loop stuck for {stalled_for:.0f}s (state {state}, waiting in {wchan or '?'}); "
                  f"tracked remote locations: {', '.join(tracked()) or 'none'}")
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
        except OSError:
            pass

    def _report_recovery(self):
        actions, self._actions = self._actions, []
        self._log("main loop running again")
        if self.on_recovered is not None:
            GLib.idle_add(lambda: self.on_recovered(actions) and False)


__all__ = ["REMOTE_TIMEOUT_S", "Watchdog", "abort_stuck_fuse", "disconnect_async", "disconnect_sync",
           "is_remote_location", "is_remote_path", "local_path", "log_path", "parse_mountinfo",
           "remote_mount_for", "track", "tracked"]
