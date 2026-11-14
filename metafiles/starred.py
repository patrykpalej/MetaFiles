"""Starred files: MetaFiles' own store merged with Nautilus' stars (read-only).

Own stars live in ``data_dir()/starred.json`` as
``{"starred": [uri, ...], "hidden": [uri, ...]}``. "hidden" lists URIs starred
in Nautilus that the user unstarred in MetaFiles. Nautilus' Tracker database
is only ever opened read-only (``mode=ro``) and never written to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import urllib.parse

from gi.repository import Gio, GLib, GObject

__all__ = ["data_dir", "NAUTILUS_DB", "read_nautilus_starred", "StarredStore"]

NAUTILUS_DB = "~/.local/share/nautilus/tags/meta.db"
STARRED_QUERY = (
    'SELECT r.Uri FROM "nautilus:File" f JOIN Resource r ON r.ID = f.ID '
    'WHERE f."nautilus:starred" = 1'
)
RELOAD_DELAY_MS = 300


def data_dir() -> str:
    """``$METAFILES_DATA_DIR``, else ``$XDG_DATA_HOME/metafiles`` (``~/.local/share/metafiles``)."""
    override = os.environ.get("METAFILES_DATA_DIR")
    if override:
        return override
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "metafiles")


def _query_nautilus(db_path: str) -> set[str] | None:
    """Return Nautilus' starred URIs, or None (with a warning) on any error."""
    path = os.path.expanduser(db_path)
    if not os.path.exists(path):
        return set()
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = conn.execute(STARRED_QUERY).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"metafiles: cannot read Nautilus stars from {path}: {exc}", file=sys.stderr)
        return None
    return {row[0] for row in rows if isinstance(row[0], str)}


def read_nautilus_starred(db_path: str = NAUTILUS_DB) -> set[str]:
    """Return the ``file://`` URIs starred in Nautilus (empty set on any error).

    A missing database means Nautilus never starred anything: empty set,
    no warning.
    """
    result = _query_nautilus(db_path)
    return result if result is not None else set()


def _remap(uri: str, old: str, new: str) -> str | None:
    """Map ``uri`` from ``old`` to ``new`` if it is ``old`` or inside it."""
    if uri == old:
        return new
    prefix = old.rstrip("/") + "/"
    if uri.startswith(prefix):
        return new.rstrip("/") + "/" + uri[len(prefix):]
    return None


def _is_under(uri: str, base: str) -> bool:
    return uri == base or uri.startswith(base.rstrip("/") + "/")


class StarredStore(GObject.Object):
    """Effective set of starred URIs: (own ∪ nautilus) − hidden.

    Emits ``changed`` whenever the effective set changes.
    """

    __gtype_name__ = "MetaFilesStarredStore"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, directory: str | None = None, nautilus_db: str = NAUTILUS_DB,
                 watch: bool = True):
        super().__init__()
        self._directory = directory or data_dir()
        self._path = os.path.join(self._directory, "starred.json")
        self._nautilus_db = os.path.expanduser(nautilus_db) if nautilus_db else ""
        self._own: set[str] = set()
        self._hidden: set[str] = set()
        self._nautilus: set[str] = set()
        self._monitors: list[Gio.FileMonitor] = []
        self._reload_source = 0
        self._load()
        self._read_nautilus(prune=False)
        if watch and self._nautilus_db:
            self._start_watching()

    # -- persistence -------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            print(f"metafiles: cannot read {self._path}: {exc}", file=sys.stderr)
            return
        if isinstance(data, dict):
            self._own = {u for u in data.get("starred", []) if isinstance(u, str)}
            self._hidden = {u for u in data.get("hidden", []) if isinstance(u, str)}

    def _save(self) -> None:
        os.makedirs(self._directory, exist_ok=True)
        data = {"starred": sorted(self._own), "hidden": sorted(self._hidden)}
        fd, tmp = tempfile.mkstemp(prefix=".starred-", suffix=".json", dir=self._directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- Nautilus ------------------------------------------------------------

    def _read_nautilus(self, prune: bool) -> None:
        if not self._nautilus_db:
            self._nautilus = set()
            return
        result = _query_nautilus(self._nautilus_db)
        if result is None:
            return  # keep the last known set on transient errors
        self._nautilus = result
        if prune:
            stale = {u for u in self._hidden if u not in result}
            if stale:
                self._hidden -= stale
                self._save()

    def reload_nautilus(self) -> None:
        """Re-read Nautilus' stars; emits ``changed`` if the effective set changed."""
        before = self._effective()
        self._read_nautilus(prune=True)
        if self._effective() != before:
            self.emit("changed")

    def _start_watching(self) -> None:
        for path in (self._nautilus_db, self._nautilus_db + "-wal"):
            try:
                monitor = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error as exc:
                print(f"metafiles: cannot monitor {path}: {exc.message}", file=sys.stderr)
                continue
            monitor.connect("changed", self._on_db_changed)
            self._monitors.append(monitor)

    def _on_db_changed(self, *_args) -> None:
        if self._reload_source:
            GLib.source_remove(self._reload_source)
        self._reload_source = GLib.timeout_add(RELOAD_DELAY_MS, self._on_reload_timeout)

    def _on_reload_timeout(self) -> bool:
        self._reload_source = 0
        self.reload_nautilus()
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        """Stop watching Nautilus' database."""
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()
        if self._reload_source:
            GLib.source_remove(self._reload_source)
            self._reload_source = 0

    # -- queries ---------------------------------------------------------------

    def _effective(self) -> set[str]:
        return (self._own | self._nautilus) - self._hidden

    def is_starred(self, uri: str) -> bool:
        return uri not in self._hidden and (uri in self._own or uri in self._nautilus)

    def is_from_nautilus(self, uri: str) -> bool:
        """True if the star comes from Nautilus (unstarring will only hide it)."""
        return uri in self._nautilus and uri not in self._hidden

    def uris(self) -> list[str]:
        return sorted(self._effective())

    # -- modifications ---------------------------------------------------------

    def _commit(self, before: set[str], own_before: set[str], hidden_before: set[str]) -> None:
        if self._own != own_before or self._hidden != hidden_before:
            self._save()
        if self._effective() != before:
            self.emit("changed")

    def _snapshot(self):
        return self._effective(), set(self._own), set(self._hidden)

    def star(self, uri: str) -> None:
        snap = self._snapshot()
        if uri in self._nautilus:
            self._hidden.discard(uri)
        else:
            self._own.add(uri)
        self._commit(*snap)

    def unstar(self, uri: str) -> None:
        snap = self._snapshot()
        self._own.discard(uri)
        if uri in self._nautilus:
            self._hidden.add(uri)
        self._commit(*snap)

    def moved(self, old_uri: str, new_uri: str) -> None:
        """Make stars follow a rename/move of ``old_uri`` (and its children)."""
        if old_uri == new_uri:
            return
        snap = self._snapshot()
        effective = snap[0]
        for uri in list(effective):
            target = _remap(uri, old_uri, new_uri)
            if target is None:
                continue
            self._own.discard(uri)
            if uri in self._nautilus:
                self._hidden.add(uri)
            self._own.add(target)
            self._hidden.discard(target)
        self._commit(*snap)

    def removed(self, uri: str) -> None:
        """Forget stars of a permanently deleted ``uri`` (and its children)."""
        snap = self._snapshot()
        self._own = {u for u in self._own if not _is_under(u, uri)}
        self._hidden |= {u for u in self._nautilus if _is_under(u, uri)}
        self._commit(*snap)
