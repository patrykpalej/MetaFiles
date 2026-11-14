"""System clipboard in Nautilus' format (x-special/gnome-copied-files)."""

from gi.repository import Gdk, Gio, GLib, GObject

GNOME_MIME = "x-special/gnome-copied-files"
URI_LIST = "text/uri-list"


def _bytes(text):
    return GLib.Bytes.new(text.encode("utf-8"))


def files_content(uris, cut=False):
    """A content provider Nautilus and other apps understand."""
    gnome = ("cut" if cut else "copy") + "\n" + "\n".join(uris)
    uri_list = "\r\n".join(uris) + "\r\n"
    paths = [Gio.File.new_for_uri(u).get_path() or u for u in uris]
    text = GObject.Value(str, "\n".join(paths))
    return Gdk.ContentProvider.new_union([
        Gdk.ContentProvider.new_for_bytes(GNOME_MIME, _bytes(gnome)),
        Gdk.ContentProvider.new_for_bytes(URI_LIST, _bytes(uri_list)),
        Gdk.ContentProvider.new_for_value(text),
    ])


def parse_gnome_copied(data):
    """Return (cut, [uris]) from x-special/gnome-copied-files bytes."""
    lines = [line for line in data.decode("utf-8", "replace").replace("\r", "").split("\n") if line]
    if not lines or lines[0] not in ("copy", "cut"):
        return False, []
    return lines[0] == "cut", lines[1:]


def parse_uri_list(data):
    lines = data.decode("utf-8", "replace").replace("\r", "").split("\n")
    return [line for line in lines if line and not line.startswith("#")]


class Clipboard(GObject.Object):
    """Wraps the display clipboard and remembers which files we cut (they are shown dimmed)."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, display=None):
        super().__init__()
        self.clipboard = (display or Gdk.Display.get_default()).get_clipboard()
        self.cut_uris = set()
        self.clipboard.connect("changed", self._on_changed)

    def _on_changed(self, clipboard):
        if not clipboard.is_local():
            self.cut_uris = set()
        self.emit("changed")

    def set_files(self, uris, cut=False):
        self.cut_uris = set(uris) if cut else set()
        self.clipboard.set_content(files_content(uris, cut))
        self.emit("changed")

    def set_text(self, text):
        self.cut_uris = set()
        self.clipboard.set(text)

    def has_files(self):
        formats = self.clipboard.get_formats()
        return formats.contain_mime_type(GNOME_MIME) or formats.contain_mime_type(URI_LIST)

    def read_files(self, callback):
        """callback(cut: bool, uris: list); ([], False) when there are no files."""
        formats = self.clipboard.get_formats()
        mimes = [m for m in (GNOME_MIME, URI_LIST) if formats.contain_mime_type(m)]
        if not mimes:
            callback(False, [])
            return

        def on_read(clipboard, result):
            try:
                stream, mime = clipboard.read_finish(result)
            except GLib.Error:
                callback(False, [])
                return
            out = Gio.MemoryOutputStream.new_resizable()
            out.splice_async(stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
                             GLib.PRIORITY_DEFAULT, None, lambda o, r: on_data(o, r, mime))

        def on_data(out, result, mime):
            try:
                out.splice_finish(result)
            except GLib.Error:
                callback(False, [])
                return
            data = out.steal_as_bytes().get_data()
            if mime == GNOME_MIME:
                cut, uris = parse_gnome_copied(data)
            else:
                cut, uris = False, parse_uri_list(data)
            callback(cut, uris)

        self.clipboard.read_async(mimes, GLib.PRIORITY_DEFAULT, None, on_read)

    def clear_cut(self):
        self.cut_uris = set()
        self.emit("changed")
