"""Git integration: repository discovery, status parsing and async git commands.

The pure functions (``find_repo_root``, ``parse_status_v2``, ``build_folder_map``,
``parse_ignored``, ``is_ignored_rel``) have no GTK dependencies and are unit-tested
directly. ``GitRepo`` runs git through ``Gio.Subprocess`` so the main loop is never
blocked; ``GitManager`` caches one ``GitRepo`` per working tree root.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

from gi.repository import Gio, GLib, GObject

STATUS_ORDER = ["conflicted", "modified", "deleted", "renamed", "added", "untracked", "ignored"]
_STRENGTH = {status: index for index, status in enumerate(STATUS_ORDER)}

STATUS_ARGS = ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all"]
IGNORED_ARGS = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"]

# XY combinations that denote an unmerged path (see git-status(1)).
_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

REFRESH_DELAY_MS = 500
# Outputs larger than this are parsed in a worker thread.
_THREAD_PARSE_THRESHOLD = 512 * 1024
# Upper bound on directory monitors created under refs/.
_MAX_REF_MONITORS = 64

ActionCallback = Callable[[bool, str], None]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def git_available() -> bool:
    """Return True when the git executable is on PATH."""
    return shutil.which("git") is not None


def find_repo_root(path: str) -> str | None:
    """Walk up from ``path`` (file or directory) to the nearest Git working tree root.

    A directory counts as a root when it contains ``.git`` (a directory, or a file
    as used by worktrees and submodules). Returns the realpath of the root or None.
    """
    if not path:
        return None
    current = os.path.realpath(path)
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    while True:
        if os.path.basename(current) == ".git":
            # Paths inside the git directory itself are not part of a work tree.
            return None
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def resolve_git_dirs(root: str) -> tuple[str, str] | None:
    """Return ``(gitdir, commondir)`` for a working tree root, or None.

    Handles a ``.git`` file (``gitdir: <path>``) used by worktrees and submodules,
    and the ``commondir`` file of linked worktrees (refs live in the common dir).
    """
    dotgit = os.path.join(root, ".git")
    if os.path.isdir(dotgit):
        gitdir = dotgit
    elif os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding="utf-8", errors="replace") as handle:
                line = handle.readline().strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        gitdir = line[len("gitdir:"):].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(root, gitdir)
        gitdir = os.path.realpath(gitdir)
    else:
        return None
    commondir = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8", errors="replace") as handle:
            value = handle.readline().strip()
        if value:
            commondir = os.path.realpath(value if os.path.isabs(value) else os.path.join(gitdir, value))
    except OSError:
        pass
    return gitdir, commondir


def stronger(a: str | None, b: str | None) -> str | None:
    """Return the stronger of two statuses (None = clean)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _STRENGTH.get(a, 99) <= _STRENGTH.get(b, 99) else b


def status_from_xy(xy: str) -> str:
    """Map a porcelain v2 ``XY`` code to a MetaFiles status string."""
    if len(xy) != 2:
        return "modified"
    x, y = xy[0], xy[1]
    if "U" in xy or xy in _CONFLICT_CODES:
        return "conflicted"
    if y in "MT":
        return "modified"
    if x == "D" or y == "D":
        return "deleted"
    if x in "RC" or y in "RC":
        return "renamed"
    if x == "A":
        return "added"
    if x in "MT":
        return "modified"
    return "modified"


@dataclass
class RepoStatus:
    """Parsed result of ``git status --porcelain=v2 --branch``."""

    branch: str | None = None
    oid: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    entries: dict[str, str] = field(default_factory=dict)
    dirty: bool = False
    # new path -> original path for rename/copy entries.
    renames: dict[str, str] = field(default_factory=dict)

    def branch_label(self) -> str:
        """Human readable branch, e.g. ``"main ↑1 ↓3"`` or a short oid when detached."""
        if self.branch:
            label = self.branch
        elif self.oid:
            label = self.oid[:7]
        else:
            label = "(detached)"
        if self.ahead:
            label += f" ↑{self.ahead}"
        if self.behind:
            label += f" ↓{self.behind}"
        return label


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def parse_status_v2(data: bytes) -> RepoStatus:
    """Parse ``git status --porcelain=v2 -z --branch --untracked-files=all`` output."""
    status = RepoStatus()
    records = data.split(b"\0")
    count = len(records)
    i = 0
    while i < count:
        raw = records[i]
        i += 1
        if not raw:
            continue
        kind = raw[:1]
        if kind == b"#":
            text = _decode(raw)
            parts = text.split(" ", 2)
            if len(parts) < 3:
                continue
            key, value = parts[1], parts[2]
            if key == "branch.oid":
                status.oid = None if value == "(initial)" else value
            elif key == "branch.head":
                status.branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                status.upstream = value
            elif key == "branch.ab":
                for token in value.split():
                    try:
                        if token.startswith("+"):
                            status.ahead = int(token[1:])
                        elif token.startswith("-"):
                            status.behind = int(token[1:])
                    except ValueError:
                        pass
        elif kind == b"1":
            # 1 XY sub mH mI mW hH hI path
            parts = raw.split(b" ", 8)
            if len(parts) < 9:
                continue
            status.entries[_decode(parts[8])] = status_from_xy(_decode(parts[1]))
        elif kind == b"2":
            # 2 XY sub mH mI mW hH hI Xscore path NUL origPath
            parts = raw.split(b" ", 9)
            original = records[i] if i < count else b""
            i += 1
            if len(parts) < 10:
                continue
            path = _decode(parts[9])
            status.entries[path] = status_from_xy(_decode(parts[1]))
            if original:
                status.renames[path] = _decode(original)
        elif kind == b"u":
            # u XY sub m1 m2 m3 mW h1 h2 h3 path
            parts = raw.split(b" ", 10)
            if len(parts) < 11:
                continue
            status.entries[_decode(parts[10])] = "conflicted"
        elif kind == b"?":
            status.entries[_decode(raw[2:])] = "untracked"
        elif kind == b"!":
            path = _decode(raw[2:])
            status.entries[path.rstrip("/")] = "ignored"
    status.dirty = any(value != "ignored" for value in status.entries.values())
    return status


def build_folder_map(entries: dict[str, str]) -> dict[str, str]:
    """Strongest status of every ancestor folder ("" = root) of non-ignored entries."""
    folders: dict[str, str] = {}
    strength = _STRENGTH
    for path, status in entries.items():
        if status == "ignored":
            continue
        rank = strength.get(status, 99)
        folder = path.rstrip("/")
        while True:
            cut = folder.rfind("/")
            folder = folder[:cut] if cut >= 0 else ""
            current = folders.get(folder)
            if current is not None and strength.get(current, 99) <= rank:
                # Ancestors already carry an equal or stronger status.
                break
            folders[folder] = status
            if not folder:
                break
    return folders


def parse_ignored_entries(data: bytes) -> tuple[set[str], set[str]]:
    """Parse ``git ls-files --others --ignored --directory`` output.

    Returns ``(ignored, ignored_dirs)``: all ignored paths without trailing slash,
    and the subset that git reported as directories.
    """
    ignored: set[str] = set()
    directories: set[str] = set()
    for raw in data.split(b"\0"):
        if not raw:
            continue
        path = _decode(raw)
        if path.endswith("/"):
            path = path.rstrip("/")
            directories.add(path)
        ignored.add(path)
    return ignored, directories


def parse_ignored(data: bytes) -> set[str]:
    """Set of ignored paths (directories without their trailing slash)."""
    return parse_ignored_entries(data)[0]


def is_ignored_rel(relpath: str, ignored: set[str]) -> bool:
    """True if ``relpath`` or any of its ancestors is in ``ignored``."""
    if not ignored or not relpath:
        return False
    path = relpath.rstrip("/")
    while path:
        if path in ignored:
            return True
        cut = path.rfind("/")
        if cut < 0:
            return False
        path = path[:cut]
    return False


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def run_status_sync(root: str) -> tuple[RepoStatus, set[str]]:
    """Run both status commands synchronously (blocking). Raises on git failure."""
    env = _git_env()
    status_out = subprocess.run(
        ["git", "-C", root, *STATUS_ARGS], env=env, capture_output=True, check=True
    ).stdout
    ignored_out = subprocess.run(
        ["git", "-C", root, *IGNORED_ARGS], env=env, capture_output=True, check=True
    ).stdout
    return parse_status_v2(status_out), parse_ignored(ignored_out)


# ---------------------------------------------------------------------------
# Async repository object
# ---------------------------------------------------------------------------

def _bytes_to_str(gbytes: GLib.Bytes | None) -> str:
    if gbytes is None:
        return ""
    data = gbytes.get_data()
    return _decode(bytes(data)) if data else ""


def _gbytes_to_bytes(gbytes: GLib.Bytes | None) -> bytes:
    if gbytes is None:
        return b""
    data = gbytes.get_data()
    return bytes(data) if data else b""


class GitRepo(GObject.Object):
    """One Git working tree: cached status, ignored set, monitors and actions."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, root: str):
        super().__init__()
        self.root = os.path.realpath(root)
        self.status: RepoStatus | None = None
        self.folder_map: dict[str, str] = {}
        self.ignored: set[str] = set()
        self.ignored_dirs: set[str] = set()
        self._timeout_id = 0
        self._in_flight = False
        self._pending = False
        self._results: dict[str, tuple[bool, bytes]] = {}
        self._monitors: list[Gio.FileMonitor] = []
        self._stopped = False
        self._cancellable = Gio.Cancellable()

    # -- paths -------------------------------------------------------------

    def relpath(self, abs_path: str) -> str | None:
        """Path relative to the root using "/" ("" for the root), or None if outside."""
        if not abs_path:
            return None
        path = abs_path.rstrip("/") or "/"
        root = self.root
        if path == root:
            return ""
        prefix = root.rstrip("/") + "/"
        if not path.startswith(prefix):
            path = os.path.realpath(path)
            if path == root:
                return ""
            if not path.startswith(prefix):
                return None
        rel = path[len(prefix):]
        if os.sep != "/":
            rel = rel.replace(os.sep, "/")
        return rel

    def status_for(self, abs_path: str, is_dir: bool | None = None) -> str | None:
        """Status of a file, or the aggregated status of a directory (None = clean)."""
        rel = self.relpath(abs_path)
        if rel is None or self.status is None:
            return None
        if is_dir is None:
            is_dir = os.path.isdir(abs_path)
        if is_dir:
            result = self.folder_map.get(rel)
            if result is None:
                result = self.status.entries.get(rel)
        else:
            result = self.status.entries.get(rel)
        if result is None and is_ignored_rel(rel, self.ignored):
            return "ignored"
        return result

    def is_ignored(self, abs_path: str) -> bool:
        rel = self.relpath(abs_path)
        if not rel:
            return False
        if self.status is not None and self.status.entries.get(rel) == "ignored":
            return True
        return is_ignored_rel(rel, self.ignored)

    def changed_files(self) -> list[str]:
        """Absolute paths of all entries that are not ignored (may include deleted files)."""
        if self.status is None:
            return []
        return [
            os.path.join(self.root, rel)
            for rel, status in self.status.entries.items()
            if status != "ignored"
        ]

    # -- refresh -----------------------------------------------------------

    def refresh(self, delay_ms: int = REFRESH_DELAY_MS) -> None:
        """Schedule an asynchronous status refresh (coalesced, debounced)."""
        if self._stopped:
            return
        if self._in_flight:
            self._pending = True
            return
        if self._timeout_id:
            return
        self._timeout_id = GLib.timeout_add(max(0, delay_ms), self._on_refresh_timeout)

    def _on_refresh_timeout(self) -> bool:
        self._timeout_id = 0
        if self._stopped:
            return GLib.SOURCE_REMOVE
        if self._in_flight:
            self._pending = True
        else:
            self._start_refresh()
        return GLib.SOURCE_REMOVE

    def _start_refresh(self) -> None:
        self._in_flight = True
        self._pending = False
        self._results = {}
        for key, args in (("status", STATUS_ARGS), ("ignored", IGNORED_ARGS)):
            self._spawn(args, self._make_refresh_cb(key))

    def _make_refresh_cb(self, key: str):
        def callback(ok: bool, stdout: bytes, _stderr: bytes) -> None:
            self._results[key] = (ok, stdout)
            if len(self._results) == 2:
                self._on_refresh_output()
        return callback

    def _on_refresh_output(self) -> None:
        status_ok, status_out = self._results["status"]
        ignored_ok, ignored_out = self._results["ignored"]
        self._results = {}
        if self._stopped:
            self._in_flight = False
            return
        if len(status_out) + len(ignored_out) > _THREAD_PARSE_THRESHOLD:
            def work() -> None:
                parsed = self._parse(status_ok, status_out, ignored_ok, ignored_out)
                GLib.idle_add(self._apply_parsed, parsed)
            threading.Thread(target=work, name="metafiles-git-parse", daemon=True).start()
        else:
            self._apply_parsed(self._parse(status_ok, status_out, ignored_ok, ignored_out))

    @staticmethod
    def _parse(status_ok, status_out, ignored_ok, ignored_out):
        if not status_ok:
            return None
        status = parse_status_v2(status_out)
        folder_map = build_folder_map(status.entries)
        if ignored_ok:
            ignored, ignored_dirs = parse_ignored_entries(ignored_out)
        else:
            ignored, ignored_dirs = set(), set()
        return status, folder_map, ignored, ignored_dirs

    def _apply_parsed(self, parsed) -> bool:
        self._in_flight = False
        if self._stopped:
            return GLib.SOURCE_REMOVE
        if parsed is None:
            self.status = None
            self.folder_map = {}
            self.ignored = set()
            self.ignored_dirs = set()
        else:
            self.status, self.folder_map, self.ignored, self.ignored_dirs = parsed
        self.emit("changed")
        if self._pending:
            self._pending = False
            self.refresh()
        return GLib.SOURCE_REMOVE

    # -- subprocess plumbing -----------------------------------------------

    def _spawn(self, args: list[str], callback, stdin: bytes | None = None) -> None:
        """Run ``git -C root <args>``; ``callback(ok, stdout, stderr)`` on the main loop.

        ``ok`` is True when the process exited with status 0.
        """
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        )
        launcher.setenv("GIT_OPTIONAL_LOCKS", "0", True)
        launcher.setenv("LC_ALL", "C", True)
        argv = ["git", "--literal-pathspecs", "-C", self.root, *args]
        try:
            proc = launcher.spawnv(argv)
        except GLib.Error as error:
            GLib.idle_add(lambda: callback(False, b"", str(error).encode()) and False)
            return

        def on_done(source, result) -> None:
            try:
                _ok, out, err = source.communicate_finish(result)
            except GLib.Error as error:
                callback(False, b"", str(error).encode())
                return
            success = source.get_if_exited() and source.get_exit_status() == 0
            callback(success, _gbytes_to_bytes(out), _gbytes_to_bytes(err))

        proc.communicate_async(None, self._cancellable, on_done)

    def _run_chain(self, commands: list[list[str]], callback: ActionCallback | None,
                   notes: list[str] | None = None, ok_codes_any: bool = False,
                   refresh: bool = True) -> None:
        """Run git commands sequentially, concatenating output; stop on first failure.

        With ``ok_codes_any`` a non-zero exit status is not treated as failure
        (used for ``diff --no-index`` which exits 1 when files differ).
        """
        outputs: list[str] = list(notes or [])
        queue = list(commands)

        def finish(ok: bool) -> None:
            if refresh:
                self.refresh(0)
            if callback is not None:
                callback(ok, "\n".join(part for part in outputs if part))

        def step() -> None:
            if not queue:
                finish(True)
                return
            args = queue.pop(0)

            def done(ok: bool, out: bytes, err: bytes) -> None:
                text = _decode(out)
                if text:
                    outputs.append(text.rstrip("\n"))
                if not ok and not ok_codes_any:
                    error = _decode(err).strip()
                    if error:
                        outputs.append(error)
                    finish(False)
                    return
                step()

            self._spawn(args, done)

        step()

    # -- actions -----------------------------------------------------------

    def _rel_args(self, paths: Iterable[str]) -> list[str]:
        result = []
        for path in paths:
            rel = self.relpath(path)
            if rel is None:
                continue
            result.append(rel if rel else ".")
        return result

    def _entry(self, rel: str) -> str | None:
        if self.status is None:
            return None
        return self.status.entries.get(rel)

    def _is_untracked(self, rel: str) -> bool:
        if rel in ("", "."):
            return False
        status = self._entry(rel)
        if status == "untracked":
            return True
        if status is None and self.folder_map.get(rel) == "untracked":
            # Directory that only contains untracked files.
            return True
        return False

    def diff(self, paths: Iterable[str], callback: ActionCallback) -> None:
        """``git diff HEAD -- paths``; untracked files are shown as new-file diffs."""
        rels = self._rel_args(paths)
        if not rels:
            callback(False, "No paths inside the repository")
            return
        tracked = [rel for rel in rels if not self._is_untracked(rel)]
        untracked = [rel for rel in rels if self._is_untracked(rel)
                     and os.path.isfile(os.path.join(self.root, rel))]
        has_head = self.status is None or self.status.oid is not None
        commands: list[list[str]] = []
        if tracked:
            base = ["diff", "HEAD"] if has_head else ["diff", "--cached"]
            commands.append([*base, "--", *tracked])
        commands.extend(["diff", "--no-index", "--", os.devnull, rel] for rel in untracked)
        if not commands:
            callback(True, "")
            return
        # diff --no-index exits with 1 when there are differences; plain diff exits 0.
        self._run_chain(commands, callback, ok_codes_any=bool(untracked), refresh=False)

    def restore(self, paths: Iterable[str], callback: ActionCallback) -> None:
        """Restore paths to HEAD (index and work tree).

        Untracked paths are skipped; newly added (staged) files are only removed
        from the index so their content is not lost. Both are reported in the output.
        """
        rels = self._rel_args(paths)
        notes: list[str] = []
        restore: list[str] = []
        unstage: list[str] = []
        skipped: list[str] = []
        for rel in rels:
            status = self._entry(rel)
            if self._is_untracked(rel):
                skipped.append(rel)
            elif status == "added":
                unstage.append(rel)
            else:
                restore.append(rel)
                original = self.status.renames.get(rel) if self.status else None
                if original and original not in restore:
                    restore.append(original)
        if skipped:
            notes.append("Skipped untracked: " + ", ".join(skipped))
        if unstage:
            notes.append("Removed from index (kept as untracked): " + ", ".join(unstage))
        commands: list[list[str]] = []
        if restore:
            commands.append(["restore", "--staged", "--worktree", "--source=HEAD", "--", *restore])
        if unstage:
            commands.append(self._unstage_args(unstage))
        self._run_chain(commands, callback, notes)

    def add(self, paths: Iterable[str], callback: ActionCallback) -> None:
        """``git add -- paths``."""
        rels = self._rel_args(paths)
        self._run_chain([["add", "--", *rels]] if rels else [], callback)

    def unstage(self, paths: Iterable[str], callback: ActionCallback) -> None:
        """``git restore --staged -- paths`` (``git rm --cached`` before the first commit)."""
        rels = self._rel_args(paths)
        self._run_chain([self._unstage_args(rels)] if rels else [], callback)

    def _unstage_args(self, rels: list[str]) -> list[str]:
        if self.status is not None and self.status.oid is None:
            return ["rm", "--cached", "-r", "-q", "--", *rels]
        return ["restore", "--staged", "--", *rels]

    # -- monitoring --------------------------------------------------------

    def start_monitoring(self) -> None:
        """Watch the git directory (index, HEAD, refs) and refresh on changes."""
        if self._monitors or self._stopped:
            return
        dirs = resolve_git_dirs(self.root)
        if dirs is None:
            return
        gitdir, commondir = dirs
        self._monitor_dir(gitdir, self._on_gitdir_event)
        if commondir != gitdir:
            self._monitor_dir(commondir, self._on_gitdir_event)
        refs = os.path.join(commondir, "refs")
        count = 0
        for current, subdirs, _files in os.walk(refs):
            if count >= _MAX_REF_MONITORS:
                break
            self._monitor_dir(current, self._on_any_event)
            count += 1
            subdirs[:] = [name for name in subdirs if name != "tags"]

    def _monitor_dir(self, path: str, handler) -> None:
        try:
            monitor = Gio.File.new_for_path(path).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
        except GLib.Error:
            return
        monitor.set_rate_limit(REFRESH_DELAY_MS)
        monitor.connect("changed", handler)
        self._monitors.append(monitor)

    _GITDIR_FILES = {"index", "HEAD", "packed-refs", "MERGE_HEAD", "FETCH_HEAD", "ORIG_HEAD"}

    def _on_gitdir_event(self, _monitor, file, other, _event) -> None:
        for item in (file, other):
            if item is not None and item.get_basename() in self._GITDIR_FILES:
                self.refresh()
                return

    def _on_any_event(self, _monitor, file, _other, _event) -> None:
        name = file.get_basename() if file is not None else ""
        if not name.endswith(".lock"):
            self.refresh()

    def stop(self) -> None:
        """Cancel monitors, timers and running commands; no further signals."""
        self._stopped = True
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors = []
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        self._cancellable.cancel()


class GitManager:
    """Cache of ``GitRepo`` objects, one per working tree root."""

    def __init__(self):
        self._repos: dict[str, GitRepo] = {}
        self._git_available: bool | None = None

    def available(self) -> bool:
        if self._git_available is None:
            self._git_available = git_available()
        return self._git_available

    def get(self, path: str) -> GitRepo | None:
        """Repository containing ``path``; None when git is missing or path is not in a repo."""
        if not self.available():
            return None
        root = find_repo_root(path)
        if root is None:
            return None
        repo = self._repos.get(root)
        if repo is None:
            repo = GitRepo(root)
            self._repos[root] = repo
            repo.start_monitoring()
            repo.refresh(0)
        return repo

    def repos(self) -> list[GitRepo]:
        return list(self._repos.values())

    def shutdown(self) -> None:
        for repo in self._repos.values():
            repo.stop()
        self._repos.clear()
