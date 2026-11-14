"""A pane: one file view with its location, history, sources, sorting, filters and decorations."""

import os
import threading
import time

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from metafiles import fileutil
from metafiles.fileutil import format_date, format_size, strip_accents
from metafiles.fileview import (
    ATTRIBUTES, DIR_COUNTER, GIT_LABELS, HIDDEN_NAMES, SIZE_CALCULATOR, FileView, info_time, is_dir,
    item_file, item_mime, query_info_with_file, type_description,
)
from metafiles.i18n import _, ngettext
from metafiles.nautilus_settings import GRID_ZOOM_LEVELS, LIST_ZOOM_LEVELS, local_only_allows

THUMBNAIL_MIME_PREFIXES = ("image/", "video/", "application/pdf", "application/vnd.oasis", "application/epub",
                           "application/x-cbr", "application/x-cbz", "audio/x-mpegurl", "font/",
                           "application/vnd.openxmlformats", "application/msword", "application/vnd.ms-")

PLACE_TITLES = {
    "recent:///": (_("Recent"), "document-open-recent-symbolic"),
    "starred:///": (_("Starred"), "starred-symbolic"),
    "trash:///": (_("Trash"), "user-trash-symbolic"),
    "git-changes:///": (_("Changed Files"), "document-edit-symbolic"),
}


def is_virtual(uri):
    return uri in PLACE_TITLES


def _name_key(info):
    return info.get_display_name()


def _type_rank(info):
    return 0 if is_dir(info) else 1


class Pane(Gtk.Box):
    """Signals let the tab/window follow the active pane (header bar, status bar, sidebar)."""

    __gsignals__ = {
        "location-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "focused": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "view-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "search-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, project, location, view_mode=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("metafiles-pane")
        self.app = app
        self.project = project
        self.settings = app.nautilus
        self.location = ""
        self.back_stack = []
        self.forward_stack = []
        self.view_mode = view_mode if view_mode in ("list", "tree", "grid") else self.settings.default_view
        self.sort_key, self.sort_reverse = self.settings.sort
        self._zoom = {"list": self.settings.zoom("list"), "grid": self.settings.zoom("grid")}
        self._columns = None  # None: Nautilus' default visible columns
        self.search_text = ""
        self.search_root = None
        self.only_changed = False
        self._search_cancel = None
        self._fill_cancel = None
        self._bound = set()
        self._thumb_requests = {}
        self._git_repo = None
        self._git_handler = None
        self._monitor = None
        self._pending_select = None
        self._loading = False
        self._status_timeout = 0
        self._flat_folder = None
        self.free_space = None

        self._build_model()
        self.view = FileView(self)
        self.view.set_model(self.selection)
        self.view.connect("activate", self._on_activate)
        self.append(self.view)
        self.status = self._build_statusbar()
        self.append(self.status)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.emit("focused"))
        self.add_controller(focus)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_pressed)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.view.add_controller(click)

        self._setup_input()
        self.tab = None
        self.settings.connect("changed", self._on_settings_changed)
        app.starred.connect("changed", lambda *_: self._on_starred_changed())
        app.clipboard.connect("changed", lambda *_: self.rebind_all())
        self.navigate(location, record=False)
        self.view.show_mode(self.view_mode, self.visible_columns())

    # --- model ----------------------------------------------------------------

    def _update_flat_folder(self):
        """For a plain folder listing every item has the same parent; skip per-item lookups."""
        if is_virtual(self.location) or self.search_text or self.view_mode == "tree":
            self._flat_folder = None
        else:
            self._flat_folder = Gio.File.new_for_uri(self.location).get_path()

    def _build_model(self):
        self.source = Gio.ListStore.new(Gio.FileInfo)
        self.tree = Gtk.TreeListModel.new(self.source, False, False, self._create_children)
        self.dirs_sorter = Gtk.NumericSorter.new(Gtk.ClosureExpression.new(GObject.TYPE_INT, _type_rank, None))
        self.key_sorter = Gtk.MultiSorter()
        self.sorter = Gtk.MultiSorter()
        self.sorter.append(self.dirs_sorter)
        self.sorter.append(self.key_sorter)
        self.sort_model = Gtk.SortListModel.new(self.tree, Gtk.TreeListRowSorter.new(self.sorter))
        self.filter = Gtk.CustomFilter.new(self._filter_row)
        self.filter_model = Gtk.FilterListModel.new(self.sort_model, self.filter)
        self.selection = Gtk.MultiSelection.new(self.filter_model)
        self.selection.connect("selection-changed", lambda *_: self._on_selection_changed())
        self.filter_model.connect("items-changed", lambda *_: self._schedule_status())
        self._update_sorter()

    def _create_children(self, info):
        if self.view_mode != "tree" or not isinstance(info, Gio.FileInfo) or not is_dir(info):
            return None
        gf = item_file(info)
        if gf is None:
            return None
        children = Gtk.DirectoryList.new(ATTRIBUTES, gf)
        children.set_monitored(True)
        return children

    def sorter_for_column(self, key):
        expr = self._sort_expression(key)
        if expr is None:
            return None
        if key in ("name", "type", "detailed_type", "owner", "group", "permissions", "location", "git"):
            sorter = Gtk.StringSorter.new(expr)
            sorter.set_collation(Gtk.Collation.FILENAME if key == "name" else Gtk.Collation.UNICODE)
            return sorter
        return Gtk.NumericSorter.new(expr)

    def _sort_expression(self, key):
        funcs = {
            "name": (GObject.TYPE_STRING, _name_key),
            "size": (GObject.TYPE_INT64, self._sort_size),
            "type": (GObject.TYPE_STRING, type_description),
            "detailed_type": (GObject.TYPE_STRING, item_mime),
            "owner": (GObject.TYPE_STRING, lambda i: i.get_attribute_string("owner::user") or ""),
            "group": (GObject.TYPE_STRING, lambda i: i.get_attribute_string("owner::group") or ""),
            "permissions": (GObject.TYPE_STRING, lambda i: self.column_text("permissions", i)),
            "date_modified": (GObject.TYPE_INT64, lambda i: info_time(i, "time::modified")),
            "date_accessed": (GObject.TYPE_INT64, lambda i: info_time(i, "time::access")),
            "date_created": (GObject.TYPE_INT64, lambda i: info_time(i, "time::created")),
            "recency": (GObject.TYPE_INT64, lambda i: info_time(i, "metafiles::recency")),
            "trashed_on": (GObject.TYPE_INT64, self._trash_time),
            "location": (GObject.TYPE_STRING, lambda i: self.location_caption(i) or ""),
            "git": (GObject.TYPE_STRING, lambda i: self.git_status(i) or ""),
            "starred": (GObject.TYPE_INT, lambda i: 0 if self.is_starred(i) else 1),
        }
        if key not in funcs:
            return None
        value_type, func = funcs[key]
        return Gtk.ClosureExpression.new(value_type, lambda item: func(item) if isinstance(item, Gio.FileInfo) else 0
                                         if value_type != GObject.TYPE_STRING else "", None)

    def _sort_size(self, info):
        if is_dir(info):
            path = item_file(info).get_path()
            total = SIZE_CALCULATOR.get(path) if path else None
            return total if total is not None else -1
        return info.get_size()

    @staticmethod
    def _trash_time(info):
        date = info.get_deletion_date() if info.has_attribute("trash::deletion-date") else None
        return date.to_unix() if date else 0

    def _update_sorter(self):
        while self.key_sorter.get_n_items():
            self.key_sorter.remove(0)
        sorter = self.sorter_for_column(self.sort_key) or self.sorter_for_column("name")
        if self.sort_reverse:
            if isinstance(sorter, Gtk.NumericSorter):
                sorter.set_sort_order(Gtk.SortType.DESCENDING)
            else:
                inner = sorter
                sorter = Gtk.CustomSorter.new(lambda a, b, _d: -int(inner.compare(a, b)), None)
                sorter.inner = inner
        self.key_sorter.append(sorter)
        if self.sort_key != "name":
            self.key_sorter.append(self.sorter_for_column("name"))
        # Nautilus keeps folders first only when sorting by name/type and the GTK setting is on.
        dirs_first = self.settings.directories_first and self.sort_key not in ("recency", "trashed_on")
        self.dirs_sorter.set_property("expression", Gtk.ClosureExpression.new(
            GObject.TYPE_INT, (lambda i: _type_rank(i) if isinstance(i, Gio.FileInfo) else 0) if dirs_first
            else (lambda i: 0), None))

    def set_sort(self, key, reverse, from_header=False):
        if (key, reverse) == (self.sort_key, self.sort_reverse):
            return
        self.sort_key, self.sort_reverse = key, reverse
        self._update_sorter()
        if not from_header and key in self.view.columns:
            self.view.sync_header_sort(key, reverse)
        self.emit("view-changed")

    # --- filter ---------------------------------------------------------------

    def _filter_row(self, row):
        info = row.get_item() if isinstance(row, Gtk.TreeListRow) else row
        if not isinstance(info, Gio.FileInfo):
            return True
        if is_virtual(self.location) or self.search_text:
            gf = item_file(info)
            path = gf.get_path() if gf else None
            if self.location == "trash:///":
                path = info.get_attribute_byte_string("trash::orig-path")
            if path and not self.project.contains(path):
                return False
        if not self.app.show_hidden:
            name = info.get_name()
            if info.get_is_hidden() or info.get_is_backup() or name.startswith("."):
                return False
            if self._flat_folder is not None:
                folder = self._flat_folder
            else:
                gf = item_file(info)
                parent = gf.get_parent() if gf else None
                folder = parent.get_path() if parent is not None else None
            if folder and name in HIDDEN_NAMES.names(folder):
                return False
        if self.project.hide_git_ignored and self._git_repo is not None:
            gf = item_file(info)
            if gf and gf.get_path() and self._git_repo.is_ignored(gf.get_path()):
                return False
        if self.search_text and not self._matches_search(info):
            return False
        return True

    def _matches_search(self, info):
        name = strip_accents(info.get_display_name()).casefold()
        return all(word in name for word in self._search_words)

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._schedule_status()

    # --- navigation -----------------------------------------------------------

    def navigate(self, uri, select=None, record=True, from_history=False):
        """Change location; refuses (with a toast) locations outside a locked project."""
        if isinstance(uri, Gio.File):
            uri = uri.get_uri()
        if not is_virtual(uri):
            gf = Gio.File.new_for_uri(uri)
            path = gf.get_path()
            if path and not os.path.isdir(path):
                if os.path.exists(path):
                    # A file: show its folder with the file selected.
                    return self.navigate(gf.get_parent().get_uri(), select=[uri], record=record)
                self.app.toast(_("“{path}” does not exist").format(path=fileutil.short_path(path)))
                return False
            if not self.project.allows(uri):
                self.project.refuse_navigation(uri)
                return False
        if uri == self.location and not self.search_text:
            if select:
                self.select_uris(select)
            return True
        if self.search_text:
            self._stop_search(emit=False)
        if record and self.location:
            self.back_stack.append((self.location, [f.get_uri() for f in self.selected_files()]))
            del self.back_stack[:-100]
            if not from_history:
                self.forward_stack.clear()
        self.location = uri
        self.only_changed = uri == "git-changes:///"
        self._update_flat_folder()
        self._load(select)
        self.emit("location-changed")
        return True

    def _load(self, select=None):
        self._pending_select = select
        self._cancel_fill()
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        self._attach_git()
        uri = self.location
        self.view.show_mode(self.view_mode, self.visible_columns())
        if uri in ("recent:///", "starred:///", "git-changes:///"):
            self._fill_virtual(uri)
        else:
            self._fill_directory(Gio.File.new_for_uri(uri))
        self._query_free_space()

    def _cancel_fill(self):
        if self._fill_cancel is not None:
            self._fill_cancel.cancel()
            self._fill_cancel = None

    def _fill_directory(self, gf):
        """Enumerate the folder asynchronously and keep it updated with a monitor."""
        cancellable = Gio.Cancellable()
        self._fill_cancel = cancellable
        self._loading = True
        self.source.remove_all()
        self.view.show_mode(self.view_mode, self.visible_columns())
        started = time.monotonic()

        def on_enum(source, result):
            try:
                enumerator = source.enumerate_children_finish(result)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    self._loading = False
                    self.view.show_empty("dialog-error-symbolic", _("Unable to access location"), e.message)
                return
            enumerator.next_files_async(500, GLib.PRIORITY_DEFAULT, cancellable, on_batch, [])

        def on_batch(enumerator, result, collected):
            try:
                infos = enumerator.next_files_finish(result)
            except GLib.Error:
                infos = []
            if cancellable.is_cancelled():
                return
            for info in infos:
                info.set_attribute_object("standard::file", enumerator.get_child(info))
            collected.extend(infos)
            # Show the first batch right away, then splice the rest in larger chunks.
            if infos and (not self.source.get_n_items() or time.monotonic() - started > 0.5):
                self.source.splice(self.source.get_n_items(), 0, collected)
                collected.clear()
            if infos:
                enumerator.next_files_async(1000, GLib.PRIORITY_DEFAULT, cancellable, on_batch, collected)
                return
            self.source.splice(self.source.get_n_items(), 0, collected)
            enumerator.close_async(GLib.PRIORITY_DEFAULT, None, None, None)
            self._loading = False
            self._finish_loading()

        gf.enumerate_children_async(ATTRIBUTES, Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
                                    cancellable, on_enum)
        try:
            self._monitor = gf.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.set_rate_limit(300)
            self._monitor.connect("changed", self._on_monitor)
        except GLib.Error:
            self._monitor = None

    def _fill_virtual(self, uri):
        cancellable = Gio.Cancellable()
        self._fill_cancel = cancellable
        self._loading = True
        self.source.remove_all()
        if uri == "recent:///":
            manager = Gtk.RecentManager.get_default()
            entries = []
            for item in manager.get_items():
                if not item.get_uri().startswith("file://") or not item.exists():
                    continue
                modified = item.get_modified()
                entries.append((item.get_uri(), modified.to_unix() if modified else 0))
            entries.sort(key=lambda e: -e[1])
            entries = entries[:500]
        elif uri == "starred:///":
            entries = [(u, 0) for u in self.app.starred.uris()]
        else:
            repo = self.app.git.get(self.project.root) if not self.project.loose else None
            entries = [(Gio.File.new_for_path(p).get_uri(), 0) for p in (repo.changed_files() if repo else [])]
            self._git_repo = repo
            self._connect_git(repo)

        def work():
            infos = []
            for entry_uri, stamp in entries:
                if cancellable.is_cancelled():
                    return
                gf = Gio.File.new_for_uri(entry_uri)
                path = gf.get_path()
                if path and not self.project.contains(path):
                    continue
                try:
                    info = query_info_with_file(gf, cancellable)
                except GLib.Error:
                    continue
                if stamp:
                    info.set_attribute_uint64("metafiles::recency", stamp)
                infos.append(info)
            GLib.idle_add(done, infos)

        def done(infos):
            if cancellable.is_cancelled():
                return False
            self.source.splice(0, self.source.get_n_items(), infos)
            self._loading = False
            self._finish_loading()
            return False

        threading.Thread(target=work, daemon=True).start()
        if uri == "recent:///":
            self.sort_key, self.sort_reverse = "recency", True
            self._update_sorter()

    def _finish_loading(self):
        self._update_empty_state()
        select = self._pending_select
        self._pending_select = None
        if select:
            GLib.idle_add(lambda: self.select_uris(select) and False)
        elif self.filter_model.get_n_items():
            # Like Nautilus: keyboard focus on the first item without selecting it.
            GLib.idle_add(lambda: self._focus_first() and False)
        self._schedule_status()

    def _focus_first(self):
        if self.filter_model.get_n_items() and not self.selected_positions():
            self.view.scroll_to(0, select=False)

    def _on_monitor(self, _monitor, gf, other, event):
        E = Gio.FileMonitorEvent
        if event in (E.CREATED, E.MOVED_IN):
            self._add_file(gf)
        elif event in (E.DELETED, E.MOVED_OUT):
            self._remove_file(gf)
        elif event == E.RENAMED:
            self._remove_file(gf)
            if other is not None:
                self._add_file(other)
        elif event in (E.CHANGES_DONE_HINT, E.ATTRIBUTE_CHANGED, E.CHANGED):
            self._update_file(gf)
        if gf.get_path():
            DIR_COUNTER.invalidate(os.path.dirname(gf.get_path()))
            SIZE_CALCULATOR.invalidate(os.path.dirname(gf.get_path()))
        if self._git_repo is not None:
            self._git_repo.refresh()

    def _find_index(self, gf):
        for i in range(self.source.get_n_items()):
            if item_file(self.source.get_item(i)).equal(gf):
                return i
        return -1

    def _add_file(self, gf):
        if self._find_index(gf) >= 0:
            self._update_file(gf)
            return
        try:
            info = query_info_with_file(gf)
        except GLib.Error:
            return
        self.source.append(info)
        self._update_empty_state()
        if self._pending_select and gf.get_uri() in self._pending_select:
            select = self._pending_select
            self._pending_select = None
            GLib.idle_add(lambda: self.select_uris(select) and False)

    def _remove_file(self, gf):
        index = self._find_index(gf)
        if index >= 0:
            self.source.remove(index)
            self._update_empty_state()

    def _update_file(self, gf):
        index = self._find_index(gf)
        if index < 0:
            return
        try:
            info = query_info_with_file(gf)
        except GLib.Error:
            return
        self.source.splice(index, 1, [info])

    def reload(self):
        DIR_COUNTER.cache.clear()
        if self.search_text:
            self._start_search_worker()
        else:
            self._load([f.get_uri() for f in self.selected_files()])

    def _update_empty_state(self):
        if self._loading:
            return
        if self.filter_model.get_n_items():
            self.view.show_mode(self.view_mode, self.visible_columns())
            return
        if self.search_text:
            self.view.show_empty("edit-find-symbolic", _("No Results Found"), _("Try a different search."))
        elif self.location == "trash:///":
            self.view.show_empty("user-trash-symbolic", _("Trash is Empty"))
        elif self.location == "starred:///":
            self.view.show_empty("starred-symbolic", _("No Starred Files"),
                                 _("Use the context menu or the star in the list to star files."))
        elif self.location == "recent:///":
            self.view.show_empty("document-open-recent-symbolic", _("No Recent Files"))
        elif self.location == "git-changes:///":
            self.view.show_empty("emblem-ok-symbolic", _("No Changes"), _("The working tree is clean."))
        else:
            self.view.show_empty("folder-symbolic", _("Folder is Empty"))

    # --- history ----------------------------------------------------------------

    def can_go_back(self):
        return bool(self.back_stack)

    def can_go_forward(self):
        return bool(self.forward_stack)

    def go_back(self):
        while self.back_stack:
            uri, selection = self.back_stack.pop()
            current = (self.location, [f.get_uri() for f in self.selected_files()])
            if self.navigate(uri, select=selection or self._child_hint(uri), record=False):
                self.forward_stack.append(current)
                return
            # A refused or vanished entry is dropped and the next one is tried.

    def go_forward(self):
        while self.forward_stack:
            uri, selection = self.forward_stack.pop()
            current = (self.location, [f.get_uri() for f in self.selected_files()])
            if self.navigate(uri, select=selection, record=False):
                self.back_stack.append(current)
                return

    def _child_hint(self, parent_uri):
        """When going back up, select the folder we came from (Nautilus does this)."""
        gf = Gio.File.new_for_uri(self.location)
        parent = gf.get_parent()
        if parent is not None and parent.get_uri() == parent_uri:
            return [gf.get_uri()]
        return None

    def parent_uri(self):
        if is_virtual(self.location):
            return None
        gf = Gio.File.new_for_uri(self.location)
        parent = gf.get_parent()
        if parent is None:
            return None
        if not self.project.allows(parent.get_uri()):
            return None
        return parent.get_uri()

    def can_go_up(self):
        return self.parent_uri() is not None

    def go_up(self):
        parent = self.parent_uri()
        if parent is None:
            if not is_virtual(self.location):
                self.project.refuse_navigation(None)
            return
        self.navigate(parent, select=[self.location])

    def go_home(self):
        self.navigate(Gio.File.new_for_path(self.project.root).get_uri())

    # --- selection ----------------------------------------------------------------

    def selected_positions(self):
        bitset = self.selection.get_selection()
        return [bitset.get_nth(i) for i in range(bitset.get_size())]

    def info_at(self, position):
        row = self.filter_model.get_item(position)
        return row.get_item() if isinstance(row, Gtk.TreeListRow) else row

    def selected_infos(self):
        return [self.info_at(p) for p in self.selected_positions()]

    def selected_files(self):
        return [item_file(i) for i in self.selected_infos() if i is not None]

    def selected_uris(self):
        return [f.get_uri() for f in self.selected_files()]

    def all_infos(self):
        return [self.info_at(i) for i in range(self.filter_model.get_n_items())]

    def position_of(self, uri):
        for i in range(self.filter_model.get_n_items()):
            info = self.info_at(i)
            if info is not None and item_file(info).get_uri() == uri:
                return i
        return -1

    def select_uris(self, uris, scroll=True):
        positions = [p for p in (self.position_of(u) for u in uris) if p >= 0]
        if not positions:
            if uris and self._loading:
                self._pending_select = uris
            return False
        self.selection.unselect_all()
        for p in positions:
            self.selection.select_item(p, False)
        if scroll:
            self.view.scroll_to(positions[0], select=False)
        return True

    def select_all(self):
        self.selection.select_all()

    def invert_selection(self):
        selected = set(self.selected_positions())
        self.selection.unselect_all()
        for i in range(self.filter_model.get_n_items()):
            if i not in selected:
                self.selection.select_item(i, False)

    def select_matching(self, pattern):
        import fnmatch

        pattern = pattern.strip()
        if not pattern:
            return 0
        if not any(c in pattern for c in "*?["):
            pattern = f"*{pattern}*"
        self.selection.unselect_all()
        count = 0
        for i in range(self.filter_model.get_n_items()):
            info = self.info_at(i)
            if fnmatch.fnmatch(info.get_display_name().casefold(), pattern.casefold()):
                self.selection.select_item(i, False)
                count += 1
        return count

    def _on_selection_changed(self):
        self.emit("selection-changed")
        self._schedule_status()

    # --- activation ---------------------------------------------------------------

    def _on_activate(self, _view, position):
        infos = self.selected_infos() or [self.info_at(position)]
        self.app.window_for(self).activate_items(self, infos)

    def _on_pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        self.emit("focused")
        if not self._has_focus_inside():
            # Clicks on empty space don't move focus by themselves; take it from e.g. the sidebar.
            self.focus_view()
        settings = self.settings
        if settings.get("preferences", "mouse-use-extra-buttons"):
            if button == settings.get("preferences", "mouse-back-button"):
                self.go_back()
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                return
            if button == settings.get("preferences", "mouse-forward-button"):
                self.go_forward()
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                return
        position = self.view.item_at(x, y)
        if button == Gdk.BUTTON_SECONDARY:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if position is None:
                self.selection.unselect_all()
            elif not self.selection.is_selected(position):
                self.selection.select_item(position, True)
            self.app.window_for(self).show_context_menu(self, x, y, background=position is None)
        elif button == Gdk.BUTTON_MIDDLE and position is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            info = self.info_at(position)
            if is_dir(info):
                self.app.window_for(self).open_in_new_tab(item_file(info).get_uri(), background=True)
        elif button == Gdk.BUTTON_PRIMARY and position is None and n_press == 1:
            state = gesture.get_current_event_state()
            if not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                self.selection.unselect_all()

    # --- keyboard and drag-and-drop ------------------------------------------------

    def tab_is_split(self):
        return self.tab is not None and self.tab.is_split

    def _setup_input(self):
        # Space previews (capture phase: the list views would use it for selection).
        capture = Gtk.EventControllerKey()
        capture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        capture.connect("key-pressed", self._on_key_capture)
        self.view.add_controller(capture)
        # Printable characters start a search, like Nautilus' type-ahead.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)

        for widget in (self.view.column_view, self.view.grid_view):
            source = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
            source.connect("prepare", self._on_drag_prepare)
            source.connect("drag-begin", self._on_drag_begin)
            widget.add_controller(source)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", self._on_drop)
        target.connect("motion", self._on_drop_motion)
        self.view.add_controller(target)

    def _on_key_capture(self, _ctl, keyval, _code, state):
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_space and not mods and self.selected_positions():
            self.activate_action("win.quick-preview", None)
            return True
        return False

    def _on_key(self, _ctl, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        char = Gdk.keyval_to_unicode(keyval)
        if char and chr(char).isprintable() and not chr(char).isspace() and chr(char) not in "/~":
            window = self.app.window_for(self)
            window.start_search(text=chr(char))
            return True
        return False

    def _drag_uris(self, x, y):
        position = self.view.item_at(x, y)
        if position is None:
            return []
        if not self.selection.is_selected(position):
            self.selection.select_item(position, True)
        return self.selected_files()

    def _on_drag_prepare(self, source, x, y):
        widget = source.get_widget()
        vx, vy = fileutil.translate(widget, self.view, x, y)
        files = self._drag_uris(vx, vy)
        if not files:
            return None
        self._drag_files = files
        return Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list(files))

    def _on_drag_begin(self, source, drag):
        files = getattr(self, "_drag_files", [])
        if files:
            try:
                info = files[0].query_info("standard::icon", Gio.FileQueryInfoFlags.NONE, None)
                paintable = _paintable(info, 48, self.get_scale_factor())
                source.set_icon(paintable, 0, 0)
            except GLib.Error:
                pass

    def _drop_destination(self, x, y):
        position = self.view.item_at(x, y)
        if position is not None:
            info = self.info_at(position)
            if info is not None and is_dir(info):
                return item_file(info).get_uri()
        return self.location if self.path() is not None and not self.search_text else None

    def _on_drop_motion(self, target, x, y):
        dest = self._drop_destination(x, y)
        if dest is None:
            return 0
        state = target.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            return Gdk.DragAction.COPY
        return Gdk.DragAction.MOVE

    def _on_drop(self, target, value, x, y):
        from metafiles.fileactions import drop_mode

        dest = self._drop_destination(x, y)
        if dest is None:
            return False
        uris = [f.get_uri() for f in value.get_files()]
        # Dropping a selection onto its own folder does nothing.
        if all(Gio.File.new_for_uri(u).get_parent() is not None and
               Gio.File.new_for_uri(u).get_parent().get_uri() == dest for u in uris):
            return False
        self.app.window_for(self).drop_files(uris, dest, drop_mode(target.get_current_event_state()))
        return True

    # --- view mode / zoom / columns -----------------------------------------------

    def set_view_mode(self, mode):
        if mode == self.view_mode:
            return
        selected = self.selected_uris()
        tree_changed = (mode == "tree") != (self.view_mode == "tree")
        self.view_mode = mode
        self._update_flat_folder()
        if tree_changed:
            # Rebuild the tree so rows (don't) get children.
            items = [self.source.get_item(i) for i in range(self.source.get_n_items())]
            self.source.remove_all()
            self.source.splice(0, 0, items)
        self.view.show_mode(mode, self.visible_columns())
        self._update_empty_state()
        self.rebind_all()
        if selected:
            GLib.idle_add(lambda: self.select_uris(selected) and False)
        self.emit("view-changed")

    def zoom_level(self, mode):
        return self._zoom["grid" if mode == "grid" else "list"]

    def _zoom_levels(self):
        return GRID_ZOOM_LEVELS if self.view_mode == "grid" else LIST_ZOOM_LEVELS

    def zoom(self, step):
        key = "grid" if self.view_mode == "grid" else "list"
        levels = self._zoom_levels()
        if step == 0:
            self._zoom[key] = self.settings.zoom(key)
        else:
            index = levels.index(self._zoom[key]) + step
            self._zoom[key] = levels[max(0, min(len(levels) - 1, index))]
        compact = key == "list" and self._zoom[key] == "small"
        if compact:
            self.view.list_scroller.add_css_class("compact")
        else:
            self.view.list_scroller.remove_css_class("compact")
        self.rebind_all()
        self.emit("view-changed")

    def can_zoom(self, step):
        levels = self._zoom_levels()
        index = levels.index(self.zoom_level(self.view_mode))
        return 0 <= index + step < len(levels)

    def visible_columns(self):
        columns = list(self._columns) if self._columns is not None else self.settings.visible_columns
        order = self.settings.column_order
        columns = sorted((c for c in columns if c != "name"),
                         key=lambda c: order.index(c) if c in order else len(order))
        result = ["name"]
        if self.search_text or is_virtual(self.location):
            result.append("location")
        result += columns
        if self.location == "recent:///" and "recency" not in result:
            result.append("recency")
        if self.location == "trash:///":
            result = [c for c in result if c not in ("date_modified",)] + ["trashed_on"]
        if "git" not in result and self._git_repo is not None and self.app.git_columns:
            result.append("git")
        if "starred" not in result:
            result.append("starred")
        return result

    def set_columns(self, columns):
        self._columns = list(columns)
        self.view.show_mode(self.view_mode, self.visible_columns())
        self.emit("view-changed")

    # --- decorations ----------------------------------------------------------------

    def rebind_all(self):
        """Refresh every bound cell (stars, git, cut state, zoom)."""
        for cell in list(self._bound):
            if cell.list_item is not None and cell.info is not None:
                row = cell.list_item.get_item()
                cell.bind(cell.list_item, row, cell.info)

    def track(self, cell, bound):
        if bound:
            self._bound.add(cell)
        else:
            self._bound.discard(cell)

    def apply_item_classes(self, cell, info):
        self.track(cell, True)
        gf = item_file(info)
        cut = gf is not None and gf.get_uri() in self.app.clipboard.cut_uris
        hidden = info.get_is_hidden() or info.get_name().startswith(".")
        for cls, on in (("cut", cut), ("hidden-file", hidden)):
            if on:
                cell.add_css_class(cls)
            else:
                cell.remove_css_class(cls)
        git = self.git_status(info)
        for status in GIT_LABELS:
            cell.remove_css_class(f"git-{status}")
        if git:
            cell.add_css_class(f"git-{git}")

    def decorate_icon(self, cell, info, size):
        scale = self.get_scale_factor()
        cell.icon.set_from_paintable(_paintable(info, size, scale))
        cell.icon.set_pixel_size(size)
        cell.icon.remove_css_class("thumbnail")
        self.thumbnailer_cancel(cell)
        gf = item_file(info)
        mime = item_mime(info)
        if gf is None or is_dir(info) or not mime.startswith(THUMBNAIL_MIME_PREFIXES):
            return
        is_local = gf.is_native()
        if not local_only_allows(self.settings.get("preferences", "show-image-thumbnails"), is_local):
            return
        limit = self.settings.get("preferences", "thumbnail-limit")
        if limit and info.get_size() > limit * 1024 * 1024:
            return
        uri = gf.get_uri()

        def done(texture, cell=cell, uri=uri):
            self._thumb_requests.pop(cell, None)
            if texture is None or cell.info is None or item_file(cell.info).get_uri() != uri:
                return
            cell.icon.set_from_paintable(texture)
            cell.icon.add_css_class("thumbnail")

        self._thumb_requests[cell] = done
        self.app.thumbnailer.request(uri, mime, info_time(info, "time::modified"), size * scale, done)

    def thumbnailer_cancel(self, cell):
        done = self._thumb_requests.pop(cell, None)
        if done is not None:
            self.app.thumbnailer.cancel(done)
        self.track(cell, False)

    def set_emblems(self, box, info):
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
        names = []
        if info.get_is_symlink():
            names.append("emblem-symbolic-link")
        if info.has_attribute("access::can-read") and not info.get_attribute_boolean("access::can-read"):
            names.append("emblem-unreadable")
        elif info.has_attribute("access::can-write") and not info.get_attribute_boolean("access::can-write") \
                and is_dir(info):
            names.append("emblem-readonly")
        git = self.git_status(info)
        if git and git != "ignored":
            names.append({"modified": "document-edit-symbolic", "added": "list-add-symbolic",
                          "untracked": "list-add-symbolic", "deleted": "list-remove-symbolic",
                          "renamed": "document-edit-symbolic",
                          "conflicted": "dialog-warning-symbolic"}.get(git, "document-edit-symbolic"))
        for name in names:
            image = Gtk.Image.new_from_icon_name(name)
            if name.startswith(("document-edit", "list-", "dialog-warning")):
                image.add_css_class(f"git-emblem-{git}")
                image.set_tooltip_text(GIT_LABELS.get(git, ""))
            box.append(image)

    def location_caption(self, info):
        """Location shown for search results and places (relative to the project when inside)."""
        if not (self.search_text or is_virtual(self.location)):
            return None
        if self.location == "trash:///" and info.has_attribute("trash::orig-path"):
            path = os.path.dirname(info.get_attribute_byte_string("trash::orig-path") or "")
        else:
            gf = item_file(info)
            parent = gf.get_parent() if gf else None
            path = parent.get_path() if parent else None
        if not path:
            return ""
        base = self.search_root if self.search_text and self.search_root else None
        if base and fileutil.inside(path, base):
            rel = fileutil.relative_label(path, base)
            return "" if rel == "." else rel
        if not self.project.loose and fileutil.inside(path, self.project.root):
            rel = fileutil.relative_label(path, self.project.root)
            return self.project.name if rel == "." else f"{self.project.name}/{rel}"
        return fileutil.short_path(path)

    def column_text(self, column, info, cell=None):
        if column == "size":
            if is_dir(info):
                path = item_file(info).get_path()
                total = SIZE_CALCULATOR.get(path) if path else None
                if total is not None:
                    return format_size(total)
                if path and path in SIZE_CALCULATOR.running:
                    return _("calculating…")
                if path and local_only_allows(self.settings.get("preferences", "show-directory-item-counts"), True):
                    def update(n, cell=cell, info=info):
                        if cell is not None and cell.info is info:
                            cell.label.set_text(self.column_text("size", info))
                    n = DIR_COUNTER.get(path, update)
                    if n is None:
                        return ""
                    return "—" if n < 0 else ngettext("{n} item", "{n} items", n).format(n=n)
                return ""
            return format_size(info.get_size())
        if column == "type":
            return type_description(info)
        if column == "detailed_type":
            return item_mime(info)
        if column == "owner":
            return info.get_attribute_string("owner::user") or ""
        if column == "group":
            return info.get_attribute_string("owner::group") or ""
        if column == "permissions":
            if not info.has_attribute("unix::mode"):
                return ""
            return fileutil.format_permissions(info.get_attribute_uint32("unix::mode"), is_dir(info))
        fmt = self.settings.get("preferences", "date-time-format")
        if column == "date_modified":
            return format_date(info_time(info, "time::modified"), fmt)
        if column == "date_accessed":
            return format_date(info_time(info, "time::access"), fmt)
        if column == "date_created":
            return format_date(info_time(info, "time::created"), fmt)
        if column == "recency":
            return format_date(info_time(info, "metafiles::recency"), fmt)
        if column == "trashed_on":
            return format_date(self._trash_time(info), fmt)
        if column == "location":
            return self.location_caption(info) or ""
        if column == "git":
            return GIT_LABELS.get(self.git_status(info) or "", "")
        return ""

    # --- stars ------------------------------------------------------------------

    def is_starred(self, info):
        gf = item_file(info)
        return gf is not None and self.app.starred.is_starred(gf.get_uri())

    def toggle_star(self, uris):
        store = self.app.starred
        if all(store.is_starred(u) for u in uris):
            from_nautilus = [u for u in uris if store.is_from_nautilus(u)]
            for uri in uris:
                store.unstar(uri)
            if from_nautilus:
                self.app.toast(_("Hidden in MetaFiles only; the star stays in Nautilus."))
        else:
            for uri in uris:
                store.star(uri)

    def _on_starred_changed(self):
        if self.location == "starred:///":
            self._load()
        else:
            self.rebind_all()

    # --- git ----------------------------------------------------------------------

    def _attach_git(self):
        if is_virtual(self.location):
            folder = self.project.root if not self.project.loose else None
        else:
            folder = Gio.File.new_for_uri(self.location).get_path()
        repo = self.app.git.get(folder) if folder else None
        if repo is not self._git_repo:
            self._connect_git(repo)
        self._git_repo = repo

    def _connect_git(self, repo):
        if self._git_handler is not None and self._git_repo is not None:
            self._git_repo.disconnect(self._git_handler)
            self._git_handler = None
        if repo is not None:
            self._git_handler = repo.connect("changed", lambda *_: self._on_git_changed())
            repo.refresh()

    def _on_git_changed(self):
        if self.project.hide_git_ignored:
            self.refilter()
        if self.location == "git-changes:///":
            self._load()
            return
        self.rebind_all()
        self.view.show_mode(self.view_mode, self.visible_columns())
        self._schedule_status()

    def git_status(self, info):
        repo = self._git_repo
        if repo is None or repo.status is None:
            return None
        gf = item_file(info)
        path = gf.get_path() if gf else None
        if not path:
            return None
        return repo.status_for(path, is_dir=is_dir(info))

    @property
    def git_repo(self):
        return self._git_repo

    # --- search -------------------------------------------------------------------

    def start_search(self, text, whole_project=False):
        text = text.strip()
        if not text:
            self._stop_search()
            return
        root = self.project.root if whole_project and not self.project.loose else None
        if root is None:
            gf = Gio.File.new_for_uri(self.location) if not is_virtual(self.location) else None
            root = gf.get_path() if gf else self.project.root
        changed_root = root != self.search_root
        self.search_root = root
        self.search_text = text
        self._update_flat_folder()
        self._search_words = [strip_accents(w).casefold() for w in text.split()]
        if changed_root or not self._search_cancel:
            self._start_search_worker()
        else:
            self.refilter()
            self._update_empty_state()
        self.view.show_mode(self.view_mode, self.visible_columns())
        self.emit("search-changed")
        self._schedule_status()

    def _start_search_worker(self):
        if self._search_cancel is not None:
            self._search_cancel.set()
        cancel = threading.Event()
        self._search_cancel = cancel
        self._cancel_fill()
        self.source.remove_all()
        self._loading = True
        root = self.search_root
        recursive = local_only_allows(self.settings.get("preferences", "recursive-search"), True)
        hidden = self.app.show_hidden
        project = self.project

        def work():
            batch = []
            last = time.monotonic()
            stack = [root]
            while stack and not cancel.is_set():
                current = stack.pop()
                try:
                    entries = list(os.scandir(current))
                except OSError:
                    continue
                for entry in entries:
                    if cancel.is_set():
                        return
                    name = entry.name
                    if not hidden and name.startswith("."):
                        continue
                    try:
                        directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        directory = False
                    if directory and recursive and name != ".git" and project.contains(entry.path):
                        stack.append(entry.path)
                    folded = strip_accents(name).casefold()
                    if all(w in folded for w in self._search_words_snapshot):
                        try:
                            batch.append(query_info_with_file(Gio.File.new_for_path(entry.path)))
                        except GLib.Error:
                            continue
                if batch and time.monotonic() - last > 0.2:
                    GLib.idle_add(deliver, batch, False)
                    batch = []
                    last = time.monotonic()
            if not cancel.is_set():
                GLib.idle_add(deliver, batch, True)

        def deliver(batch, finished):
            if cancel.is_set():
                return False
            self.source.splice(self.source.get_n_items(), 0, batch)
            if finished:
                self._loading = False
                self._update_empty_state()
                self._schedule_status()
            return False

        self._search_words_snapshot = list(self._search_words)
        threading.Thread(target=work, daemon=True).start()

    def _stop_search(self, emit=True):
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None
        was = bool(self.search_text)
        self.search_text = ""
        self.search_root = None
        self._update_flat_folder()
        if was and emit:
            self._load()
            self.emit("search-changed")

    def stop_search(self):
        self._stop_search()

    # --- status bar -------------------------------------------------------------------

    def _build_statusbar(self):
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("metafiles-statusbar")
        self.status_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.status_branch = Gtk.Label(xalign=1)
        self.status_branch.add_css_class("dim-label")
        self.status_free = Gtk.Label(xalign=1)
        self.status_free.add_css_class("dim-label")
        bar.append(self.status_label)
        bar.append(self.status_branch)
        bar.append(self.status_free)
        return bar

    def _schedule_status(self):
        if not self._status_timeout:
            self._status_timeout = GLib.timeout_add(80, self._update_status)

    def _update_status(self):
        self._status_timeout = 0
        selected = self.selected_infos()
        total = self.filter_model.get_n_items()
        if self._loading and not total:
            text = _("Loading…")
        elif selected:
            size = sum(i.get_size() for i in selected if not is_dir(i))
            folders = sum(1 for i in selected if is_dir(i))
            if len(selected) == 1:
                text = _("“{name}” selected").format(name=selected[0].get_display_name())
            else:
                text = ngettext("{n} item selected", "{n} items selected", len(selected)).format(n=len(selected))
            if size and folders < len(selected):
                text += f" ({format_size(size)})"
        else:
            text = ngettext("{n} item", "{n} items", total).format(n=total)
        self.status_label.set_text(text)
        repo = self._git_repo
        if repo is not None and repo.status is not None:
            label = repo.status.branch_label()
            self.status_branch.set_text(("● " if repo.status.dirty else "") + label)
            self.status_branch.set_tooltip_text(_("Git branch; ● means uncommitted changes"))
            self.status_branch.set_visible(True)
        else:
            self.status_branch.set_visible(False)
        self.status_free.set_text(self.free_space or "")
        self.emit("status-changed")
        return False

    def _query_free_space(self):
        if is_virtual(self.location):
            self.free_space = None
            return
        gf = Gio.File.new_for_uri(self.location)

        def done(source, result):
            try:
                info = source.query_filesystem_info_finish(result)
            except GLib.Error:
                return
            free = info.get_attribute_uint64("filesystem::free")
            self.free_space = _("{size} free").format(size=format_size(free))
            self._schedule_status()

        gf.query_filesystem_info_async("filesystem::free", GLib.PRIORITY_LOW, None, done)

    # --- settings -----------------------------------------------------------------

    def _on_settings_changed(self, _settings, group, key):
        if key in ("show-hidden-files",):
            self.refilter()
        elif key in ("default-visible-columns", "default-column-order"):
            self.view.show_mode(self.view_mode, self.visible_columns())
        elif key == "sort-directories-first":
            self._update_sorter()
        else:
            self.rebind_all()

    # --- state --------------------------------------------------------------------

    def state(self):
        return {"location": self.location, "view": self.view_mode}

    def title(self):
        if self.search_text:
            return _("Search: {text}").format(text=self.search_text)
        if is_virtual(self.location):
            return PLACE_TITLES[self.location][0]
        gf = Gio.File.new_for_uri(self.location)
        path = gf.get_path()
        if path and not self.project.loose and path == self.project.root:
            return self.project.name
        if path == fileutil.HOME:
            return _("Home")
        return gf.get_basename() if path != "/" else _("Computer")

    def path(self):
        if is_virtual(self.location):
            return None
        return Gio.File.new_for_uri(self.location).get_path()

    def _has_focus_inside(self):
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        return focus is not None and (focus is self or focus.is_ancestor(self))

    def focus_view(self):
        widget = self.view.active_widget
        if self.view.stack.get_visible_child_name() == "empty":
            self.view.stack.grab_focus()
            return
        widget.grab_focus()

    def destroy_pane(self):
        self._cancel_fill()
        if self._search_cancel is not None:
            self._search_cancel.set()
        if self._monitor is not None:
            self._monitor.cancel()
        self._connect_git(None)


def _paintable(info, size, scale):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon = info.get_icon() or Gio.ThemedIcon.new("text-x-generic")
    return theme.lookup_by_gicon(icon, size, scale, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE)
