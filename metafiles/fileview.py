"""The file view of one pane: models, list/tree/grid widgets, navigation and history.

One model chain serves all three view modes:
source (DirectoryList or ListStore) -> TreeListModel (children only in tree mode)
-> SortListModel(TreeListRowSorter) -> FilterListModel -> MultiSelection.
Items are Gio.FileInfo objects carrying their Gio.File in "standard::file".
"""

import os
import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from metafiles import fileutil
from metafiles.fileutil import format_date, format_size
from metafiles.i18n import _, ngettext
from metafiles.nautilus_settings import GRID_ICON_SIZES, GRID_ZOOM_LEVELS, LIST_ICON_SIZES, LIST_ZOOM_LEVELS

ATTRIBUTES = ",".join([
    "standard::name", "standard::display-name", "standard::edit-name", "standard::type", "standard::size",
    "standard::icon", "standard::symbolic-icon", "standard::content-type", "standard::fast-content-type",
    "standard::is-hidden", "standard::is-backup", "standard::is-symlink", "standard::symlink-target",
    "standard::target-uri", "standard::allocated-size",
    "time::modified", "time::access", "time::created", "unix::mode", "unix::inode", "unix::nlink",
    "owner::user", "owner::group", "access::can-read", "access::can-write", "access::can-execute",
    "access::can-trash", "access::can-delete", "access::can-rename",
    "thumbnail::path", "thumbnail::is-valid", "thumbnail::failed",
    "trash::orig-path", "trash::deletion-date", "trash::item-count", "recent::modified",
    "filesystem::remote", "id::filesystem",
])

VIRTUAL = ("recent:///", "starred:///", "trash:///", "git-changes:///", "search:///")

# Column key -> (title, xalign, expands)
COLUMNS = {
    "name": (_("Name"), 0.0, True),
    "location": (_("Location"), 0.0, True),
    "size": (_("Size"), 1.0, False),
    "type": (_("Type"), 0.0, False),
    "detailed_type": (_("Detailed Type"), 0.0, False),
    "owner": (_("Owner"), 0.0, False),
    "group": (_("Group"), 0.0, False),
    "permissions": (_("Permissions"), 0.0, False),
    "date_modified": (_("Modified"), 0.0, False),
    "date_accessed": (_("Accessed"), 0.0, False),
    "date_created": (_("Created"), 0.0, False),
    "recency": (_("Last Used"), 0.0, False),
    "trashed_on": (_("Trashed On"), 0.0, False),
    "git": (_("Git"), 0.0, False),
    "starred": ("", 0.5, False),
}
CHOOSABLE_COLUMNS = ["size", "type", "detailed_type", "owner", "group", "permissions", "date_modified",
                     "date_accessed", "date_created", "git", "starred"]

GIT_LABELS = {
    "conflicted": _("Conflict"), "modified": _("Changed"), "deleted": _("Deleted"),
    "renamed": _("Renamed"), "added": _("Added"), "untracked": _("New"), "ignored": _("Ignored"),
}


def item_file(info):
    return info.get_attribute_object("standard::file")


def is_dir(info):
    return info.get_file_type() == Gio.FileType.DIRECTORY


def info_time(info, attr):
    return info.get_attribute_uint64(attr) if info.has_attribute(attr) else 0


def query_info_with_file(gf, cancellable=None):
    info = gf.query_info(ATTRIBUTES, Gio.FileQueryInfoFlags.NONE, cancellable)
    info.set_attribute_object("standard::file", gf)
    return info


def item_mime(info):
    return info.get_content_type() or info.get_attribute_string("standard::fast-content-type") or "application/octet-stream"


def type_description(info):
    if is_dir(info):
        return _("Folder")
    return Gio.content_type_get_description(item_mime(info)) or ""


class HiddenNames:
    """Cache of `.hidden` files (Nautilus hides the names listed there).

    The file is re-checked at most once a second per folder, so filtering a large folder
    does not stat `.hidden` once per item.
    """

    def __init__(self):
        self._cache = {}

    def names(self, folder_path):
        if not folder_path:
            return frozenset()
        now = time.monotonic()
        cached = self._cache.get(folder_path)
        if cached and now - cached[2] < 1.0:
            return cached[1]
        hidden = os.path.join(folder_path, ".hidden")
        try:
            mtime = os.stat(hidden).st_mtime
        except OSError:
            self._cache[folder_path] = (None, frozenset(), now)
            return frozenset()
        if cached and cached[0] == mtime:
            self._cache[folder_path] = (mtime, cached[1], now)
            return cached[1]
        try:
            with open(hidden, encoding="utf-8", errors="replace") as f:
                names = frozenset(line.strip() for line in f if line.strip())
        except OSError:
            names = frozenset()
        self._cache[folder_path] = (mtime, names, now)
        return names


HIDDEN_NAMES = HiddenNames()


class DirCounter:
    """Counts folder items in the background for the size column (show-directory-item-counts)."""

    def __init__(self):
        self.cache = {}
        self.pending = {}

    def get(self, path, callback):
        if path in self.cache:
            return self.cache[path]
        waiting = self.pending.setdefault(path, [])
        waiting.append(callback)
        if len(waiting) == 1:
            threading.Thread(target=self._count, args=(path,), daemon=True).start()
        return None

    def _count(self, path):
        try:
            n = sum(1 for _ in os.scandir(path))
        except OSError:
            n = -1
        GLib.idle_add(self._deliver, path, n)

    def _deliver(self, path, n):
        self.cache[path] = n
        for cb in self.pending.pop(path, []):
            cb(n)
        return False

    def invalidate(self, path):
        self.cache.pop(path, None)


DIR_COUNTER = DirCounter()


class SizeCalculator:
    """Recursive folder sizes ("Oblicz rozmiar folderów"), cached until the folder changes."""

    def __init__(self):
        self.cache = {}
        self.running = {}

    def get(self, path):
        return self.cache.get(path)

    def compute(self, path, callback):
        if path in self.cache:
            callback(self.cache[path])
            return
        if path in self.running:
            self.running[path][1].append(callback)
            return
        cancel = threading.Event()
        self.running[path] = (cancel, [callback])
        threading.Thread(target=self._walk, args=(path, cancel), daemon=True).start()

    def _walk(self, root, cancel):
        total, seen = 0, set()
        stack = [root]
        while stack and not cancel.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in seen:
                                continue
                            seen.add(key)
                        total += st.st_size
            except OSError:
                continue
        GLib.idle_add(self._done, root, None if cancel.is_set() else total)

    def _done(self, root, total):
        _cancel, callbacks = self.running.pop(root, (None, []))
        if total is not None:
            self.cache[root] = total
        for cb in callbacks:
            cb(total)
        return False

    def cancel_all(self):
        for cancel, _cbs in self.running.values():
            cancel.set()

    def invalidate(self, path):
        for cached in list(self.cache):
            if cached == path or path.startswith(cached + os.sep):
                del self.cache[cached]


SIZE_CALCULATOR = SizeCalculator()


class Cell(Gtk.Box):
    """Base for cells: remembers its list item so clicks can find the file under the pointer."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.list_item = None
        self.info = None


def _icon_paintable(info, size, scale):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon = info.get_icon()
    if icon is None:
        icon = Gio.ThemedIcon.new("text-x-generic")
    return theme.lookup_by_gicon(icon, size, scale, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE)


class NameCell(Cell):
    """Icon, name, emblems and (for search/recent) the location, like NautilusNameCell."""

    def __init__(self, pane):
        super().__init__(spacing=0)
        self.pane = pane
        self.add_css_class("nautilus-view-cell")
        self.expander = Gtk.TreeExpander()
        self.content = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.icon = Gtk.Image(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.icon.add_css_class("nautilus-image")
        self.icon_box = Gtk.CenterBox(valign=Gtk.Align.CENTER)
        self.icon_box.set_center_widget(self.icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        line = Gtk.Box(spacing=6)
        self.label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.emblems = Gtk.Box(spacing=6)
        self.emblems.add_css_class("dim-label")
        line.append(self.label)
        line.append(self.emblems)
        self.path = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START)
        self.path.add_css_class("caption")
        self.path.add_css_class("dim-label")
        labels.append(line)
        labels.append(self.path)
        self.content.append(self.icon_box)
        self.content.append(labels)
        self.expander.set_child(self.content)
        self.append(self.expander)
        self._thumb_cb = None

    def bind(self, list_item, row, info):
        self.list_item = list_item
        self.info = info
        pane = self.pane
        self.expander.set_list_row(row if pane.view_mode == "tree" else None)
        self.expander.set_indent_for_icon(pane.view_mode == "tree")
        self.expander.set_indent_for_depth(pane.view_mode == "tree")
        size = LIST_ICON_SIZES[pane.zoom_level("list")]
        self.icon_box.set_size_request(size, size)
        self.label.set_text(info.get_display_name())
        pane.decorate_icon(self, info, size)
        pane.set_emblems(self.emblems, info)
        location = pane.location_caption(info)
        self.path.set_text(location or "")
        self.path.set_visible(bool(location) and "location" not in pane.visible_columns())
        pane.apply_item_classes(self, info)

    def unbind(self):
        self.pane.thumbnailer_cancel(self)
        self.list_item = None
        self.info = None


class TextCell(Cell):
    def __init__(self, pane, column):
        super().__init__()
        self.pane = pane
        self.column = column
        self.add_css_class("nautilus-view-cell")
        xalign = COLUMNS[column][1]
        self.label = Gtk.Label(xalign=xalign, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.label.add_css_class("dim-label")
        if column in ("size", "permissions"):
            self.label.add_css_class("numeric")
        self.append(self.label)

    def bind(self, list_item, row, info):
        self.list_item = list_item
        self.info = info
        self.label.set_text(self.pane.column_text(self.column, info, self))
        self.pane.apply_item_classes(self, info)

    def unbind(self):
        self.pane.track(self, False)
        self.list_item = None
        self.info = None


class StarCell(Cell):
    def __init__(self, pane):
        super().__init__(halign=Gtk.Align.CENTER)
        self.pane = pane
        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.add_css_class("flat")
        self.button.add_css_class("circular")
        self.button.add_css_class("star")
        self.button.connect("clicked", self._toggle)
        self.append(self.button)

    def bind(self, list_item, row, info):
        self.list_item = list_item
        self.info = info
        starred = self.pane.is_starred(info)
        self.button.set_icon_name("starred-symbolic" if starred else "non-starred-symbolic")
        self.button.set_tooltip_text(_("Unstar") if starred else _("Star"))
        if starred:
            self.button.add_css_class("starred")
        else:
            self.button.remove_css_class("starred")
        self.button.set_opacity(1.0 if starred else 0.4)
        self.pane.track(self, True)

    def _toggle(self, *_):
        if self.info is not None:
            self.button.add_css_class("interacted")
            self.pane.toggle_star([item_file(self.info).get_uri()])

    def unbind(self):
        self.pane.track(self, False)
        self.list_item = None
        self.info = None


class GridCell(Cell):
    """Big icon over a wrapped name, like NautilusGridCell."""

    def __init__(self, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START, spacing=6)
        self.pane = pane
        self.add_css_class("nautilus-view-cell")
        overlay = Gtk.Overlay()
        self.icon = Gtk.Image(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.icon.add_css_class("nautilus-image")
        self.icon_box = Gtk.CenterBox()
        self.icon_box.set_center_widget(self.icon)
        overlay.set_child(self.icon_box)
        self.emblems = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END,
                               valign=Gtk.Align.END)
        self.emblems.add_css_class("dim-label")
        overlay.add_overlay(self.emblems)
        self.label = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                               lines=3, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.caption = Gtk.Label(justify=Gtk.Justification.CENTER, ellipsize=Pango.EllipsizeMode.START, lines=1)
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")
        self.append(overlay)
        self.append(self.label)
        self.append(self.caption)

    def bind(self, list_item, row, info):
        self.list_item = list_item
        self.info = info
        size = GRID_ICON_SIZES[self.pane.zoom_level("grid")]
        self.icon_box.set_size_request(size, size)
        self.label.set_size_request(max(size, 96), -1)
        self.label.set_max_width_chars(max(8, size // 9))
        self.label.set_text(info.get_display_name())
        self.pane.decorate_icon(self, info, size)
        self.pane.set_emblems(self.emblems, info)
        caption = self.pane.location_caption(info)
        self.caption.set_text(caption or "")
        self.caption.set_visible(bool(caption))
        self.pane.apply_item_classes(self, info)

    def unbind(self):
        self.pane.thumbnailer_cancel(self)
        self.list_item = None
        self.info = None


def _factory(make, pane):
    factory = Gtk.SignalListItemFactory()

    def setup(_f, list_item):
        list_item.set_child(make())

    def bind(_f, list_item):
        row = list_item.get_item()
        info = row.get_item() if isinstance(row, Gtk.TreeListRow) else row
        list_item.get_child().bind(list_item, row, info)

    def unbind(_f, list_item):
        list_item.get_child().unbind()

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


class FileView(Gtk.Box):
    """The list/tree/grid widgets of one pane, showing its model."""

    __gsignals__ = {
        "activate": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.pane = pane
        self.columns = {}
        self.column_view = Gtk.ColumnView(enable_rubberband=True, reorderable=True, show_row_separators=False,
                                          show_column_separators=False, tab_behavior=Gtk.ListTabBehavior.ITEM)
        self.column_view.add_css_class("data-table")
        self.column_view.connect("activate", lambda _v, pos: self.emit("activate", pos))
        for key in COLUMNS:
            self._add_column(key)
        self.column_view.get_sorter().connect("changed", self._on_header_sort)
        self.grid_view = Gtk.GridView(enable_rubberband=True, max_columns=40, min_columns=1,
                                      tab_behavior=Gtk.ListTabBehavior.ITEM)
        self.grid_view.set_factory(_factory(lambda: GridCell(pane), pane))
        self.grid_view.connect("activate", lambda _v, pos: self.emit("activate", pos))

        self.list_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.column_view)
        self.list_scroller.add_css_class("nautilus-list-view")
        self.list_scroller.add_css_class("view")
        self.grid_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.grid_view)
        self.grid_scroller.add_css_class("nautilus-grid-view")
        self.grid_scroller.add_css_class("view")
        self.empty = Adw.StatusPage(vexpand=True)
        self.empty.add_css_class("compact")
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.add_named(self.list_scroller, "list")
        self.stack.add_named(self.grid_scroller, "grid")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)
        self._syncing_sort = False

    def _add_column(self, key):
        title, _xalign, expands = COLUMNS[key]
        pane = self.pane
        if key == "name":
            make = lambda: NameCell(pane)  # noqa: E731
        elif key == "starred":
            make = lambda: StarCell(pane)  # noqa: E731
        else:
            make = lambda key=key: TextCell(pane, key)  # noqa: E731
        column = Gtk.ColumnViewColumn(title=title, factory=_factory(make, pane), expand=expands,
                                      resizable=True)
        column.key = key
        sorter = pane.sorter_for_column(key)
        if sorter is not None:
            column.set_sorter(sorter)
        if key == "name":
            column.set_fixed_width(-1)
        self.columns[key] = column
        self.column_view.append_column(column)

    def set_model(self, selection):
        self.column_view.set_model(selection)
        self.grid_view.set_model(selection)

    def show_mode(self, mode, columns):
        if mode == "grid":
            self.stack.set_visible_child_name("grid")
        else:
            self.stack.set_visible_child_name("list")
            order = [k for k in columns if k in self.columns]
            for key, column in self.columns.items():
                column.set_visible(key in order)
            # Keep the requested order.
            for index, key in enumerate(order):
                self.column_view.insert_column(index, self.columns[key])

    def show_empty(self, icon, title, description=""):
        self.empty.set_icon_name(icon)
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.stack.set_visible_child_name("empty")

    @property
    def active_widget(self):
        return self.grid_view if self.stack.get_visible_child_name() == "grid" else self.column_view

    def sync_header_sort(self, key, reverse):
        column = self.columns.get(key)
        self._syncing_sort = True
        self.column_view.sort_by_column(column, Gtk.SortType.DESCENDING if reverse else Gtk.SortType.ASCENDING)
        self._syncing_sort = False

    def _on_header_sort(self, sorter, _change):
        if self._syncing_sort:
            return
        column = sorter.get_primary_sort_column()
        if column is None:
            return
        self.pane.set_sort(column.key, sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING, from_header=True)

    def item_at(self, x, y):
        """(list_item position or None) for the point in this widget's coordinates."""
        widget = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self:
            if isinstance(widget, Cell) and widget.list_item is not None:
                return widget.list_item.get_position()
            widget = widget.get_parent()
        return None

    def scroll_to(self, position, select=True):
        flags = Gtk.ListScrollFlags.FOCUS | (Gtk.ListScrollFlags.SELECT if select else Gtk.ListScrollFlags.NONE)
        if self.stack.get_visible_child_name() == "grid":
            self.grid_view.scroll_to(position, flags, None)
        else:
            self.column_view.scroll_to(position, None, flags, None)

    def scroll_position(self):
        scroller = self.grid_scroller if self.stack.get_visible_child_name() == "grid" else self.list_scroller
        return scroller.get_vadjustment().get_value()

    def set_scroll_position(self, value):
        scroller = self.grid_scroller if self.stack.get_visible_child_name() == "grid" else self.list_scroller
        GLib.idle_add(lambda: scroller.get_vadjustment().set_value(value) and False)


def ngettext_items(n):
    return ngettext("{n} item", "{n} items", n).format(n=n)


__all__ = ["ATTRIBUTES", "COLUMNS", "CHOOSABLE_COLUMNS", "FileView", "item_file", "is_dir", "query_info_with_file",
           "type_description", "item_mime", "format_size", "format_date", "fileutil", "GRID_ZOOM_LEVELS",
           "LIST_ZOOM_LEVELS", "HIDDEN_NAMES", "DIR_COUNTER", "SIZE_CALCULATOR", "VIRTUAL", "GIT_LABELS"]
