"""The GTK bookmarks file shared with Nautilus and file choosers (~/.config/gtk-3.0/bookmarks)."""

import os
import tempfile
from urllib.parse import unquote

from gi.repository import Gio, GLib, GObject


def bookmarks_path():
    if os.environ.get("METAFILES_BOOKMARKS_FILE"):
        return os.environ["METAFILES_BOOKMARKS_FILE"]
    return os.path.join(GLib.get_user_config_dir(), "gtk-3.0", "bookmarks")


def parse(text):
    """Return [(uri, label)] from the bookmarks file format: 'uri[ label]' per line."""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        uri, _sep, label = line.partition(" ")
        result.append((uri, label.strip()))
    return result


def serialize(entries):
    return "".join(f"{uri} {label}\n" if label else f"{uri}\n" for uri, label in entries)


def display_name(uri, label):
    if label:
        return label
    gf = Gio.File.new_for_uri(uri)
    return gf.get_basename() if gf.get_path() else unquote(uri)


class Bookmarks(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, path=None, watch=True):
        super().__init__()
        self.path = path or bookmarks_path()
        self.entries = []
        self._monitor = None
        self.load()
        if watch:
            try:
                self._monitor = Gio.File.new_for_path(self.path).monitor_file(Gio.FileMonitorFlags.NONE, None)
                self._monitor.connect("changed", lambda *_: self._reload())
            except GLib.Error:
                self._monitor = None

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.entries = parse(f.read())
        except OSError:
            self.entries = []

    def _reload(self):
        old = list(self.entries)
        self.load()
        if self.entries != old:
            self.emit("changed")

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bookmarks-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(serialize(self.entries))
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise
        self.emit("changed")

    def contains(self, uri):
        return any(u == uri for u, _l in self.entries)

    def add(self, uri, label=""):
        if not self.contains(uri):
            self.entries.append((uri, label))
            self.save()

    def remove(self, uri):
        self.entries = [(u, l) for u, l in self.entries if u != uri]
        self.save()

    def rename(self, uri, label):
        self.entries = [(u, label if u == uri else l) for u, l in self.entries]
        self.save()

    def move(self, uri, index):
        entry = next((e for e in self.entries if e[0] == uri), None)
        if entry is None:
            return
        self.entries.remove(entry)
        self.entries.insert(max(0, min(index, len(self.entries))), entry)
        self.save()
