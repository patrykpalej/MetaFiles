"""Left places sidebar (like Nautilus' sidebar), scoped to the active project, plus a folder tree mode."""

import os

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from metafiles import fileutil
from metafiles.bookmarks import display_name
from metafiles.fileutil import format_size
from metafiles.i18n import _

XDG_DIRS = [
    (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "folder-documents-symbolic"),
    (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "folder-download-symbolic"),
    (GLib.UserDirectory.DIRECTORY_MUSIC, "folder-music-symbolic"),
    (GLib.UserDirectory.DIRECTORY_PICTURES, "folder-pictures-symbolic"),
    (GLib.UserDirectory.DIRECTORY_VIDEOS, "folder-videos-symbolic"),
]


class PlaceRow(Gtk.ListBoxRow):
    """kind: place | project-bookmark | bookmark | device | trash."""

    def __init__(self, kind, label, icon, uri=None, mount=None, volume=None, tooltip=None):
        super().__init__()
        self.kind = kind
        self.uri = uri
        self.mount = mount
        self.volume = volume
        self.label_text = label
        box = Gtk.Box(spacing=10, margin_start=6, margin_end=6)
        self.icon = Gtk.Image.new_from_icon_name(icon) if isinstance(icon, str) else Gtk.Image.new_from_gicon(icon)
        label_widget = Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(self.icon)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        column.append(label_widget)
        self.level = None
        box.append(column)
        self.column = column
        self.set_tooltip_text(tooltip or (Gio.File.new_for_uri(uri).get_parse_name() if uri and "://" in uri else None))
        self.box = box
        self.set_child(box)

    def add_usage(self, used_fraction, text):
        self.level = Gtk.LevelBar(min_value=0, max_value=1, value=used_fraction)
        self.level.add_css_class("mf-usage")
        self.level.set_size_request(-1, 3)
        self.column.append(self.level)
        self.set_tooltip_text(text)

    def add_eject(self, callback):
        button = Gtk.Button(icon_name="media-eject-symbolic", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.add_css_class("sidebar-button")
        button.set_tooltip_text(_("Eject"))
        button.connect("clicked", lambda *_: callback())
        self.box.append(button)


class PlacesSidebar(Gtk.Box):
    __gsignals__ = {
        "open": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),  # uri, new tab
        "menu": (GObject.SignalFlags.RUN_FIRST, None, (object, float, float)),
        "drop-files": (GObject.SignalFlags.RUN_FIRST, None, (str, object, str)),
    }

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.project = None
        self.current_uri = None
        self.add_css_class("mf-places")

        switch = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        switch.add_css_class("linked")
        self.places_button = Gtk.ToggleButton(label=_("Places"), active=True)
        self.tree_button = Gtk.ToggleButton(label=_("Tree"), group=self.places_button)
        self.places_button.connect("toggled", lambda b: b.get_active() and self.set_mode("places"))
        self.tree_button.connect("toggled", lambda b: b.get_active() and self.set_mode("tree"))
        switch.append(self.places_button)
        switch.append(self.tree_button)
        self.append(switch)

        self.stack = Gtk.Stack(vexpand=True)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_header_func(self._header)
        self.listbox.connect("row-activated", lambda _l, row: self._activate(row, False))
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_click)
        self.listbox.add_controller(click)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.listbox)
        self.stack.add_named(scroller, "places")

        self.tree = FolderTree(app, self)
        self.stack.add_named(self.tree, "tree")
        self.append(self.stack)

        self.volumes = Gio.VolumeMonitor.get()
        for signal in ("mount-added", "mount-removed", "mount-changed", "volume-added", "volume-removed"):
            self.volumes.connect(signal, lambda *_: self.rebuild())
        app.bookmarks.connect("changed", lambda *_: self.rebuild())
        self._trash_monitor = Gio.File.new_for_uri("trash:///").monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._trash_monitor.connect("changed", lambda *_: self._update_trash_icon())

    # --- modes -----------------------------------------------------------------

    def set_mode(self, mode):
        self.stack.set_visible_child_name(mode)
        (self.tree_button if mode == "tree" else self.places_button).set_active(True)
        self.app.session.places_mode = mode
        if mode == "tree":
            self.tree.set_root(self.project)
            if self.current_uri:
                self.tree.reveal(self.current_uri)

    @property
    def mode(self):
        return self.stack.get_visible_child_name()

    # --- content -----------------------------------------------------------------

    def set_project(self, project):
        self.project = project
        self.rebuild()
        if self.mode == "tree":
            self.tree.set_root(project)

    def rebuild(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        project = self.project
        if project is None:
            return
        rows = []
        if project.loose:
            rows.append(PlaceRow("place", _("Home"), "user-home-symbolic", Gio.File.new_for_path(fileutil.HOME).get_uri()))
        else:
            rows.append(PlaceRow("place", _("Project Folder"), "folder-symbolic", project.root_uri,
                                 tooltip=project.root))
        rows.append(PlaceRow("place", _("Recent"), "document-open-recent-symbolic", "recent:///"))
        rows.append(PlaceRow("place", _("Starred"), "starred-symbolic", "starred:///"))
        if project.loose:
            for directory, icon in XDG_DIRS:
                path = GLib.get_user_special_dir(directory)
                if path and path != fileutil.HOME and os.path.isdir(path):
                    rows.append(PlaceRow("place", os.path.basename(path), icon, Gio.File.new_for_path(path).get_uri()))
        else:
            if self.app.git.get(project.root) is not None:
                rows.append(PlaceRow("place", _("Changed Files"), "document-edit-symbolic", "git-changes:///"))
            for path in project.state.bookmarks:
                uri = Gio.File.new_for_path(path).get_uri()
                row = PlaceRow("project-bookmark", os.path.basename(path) or path, "folder-symbolic", uri,
                               tooltip=fileutil.relative_label(path, project.root))
                row.section = "project-bookmarks"
                rows.append(row)
        for uri, label in self.app.bookmarks.entries:
            gf = Gio.File.new_for_uri(uri)
            path = gf.get_path()
            if project.lock and (path is None or not project.contains(path)):
                continue  # unreachable from a locked project, so not shown at all
            row = PlaceRow("bookmark", display_name(uri, label), "folder-symbolic" if path else "folder-remote-symbolic",
                           uri)
            row.section = "bookmarks"
            self._make_bookmark_draggable(row)
            rows.append(row)
        if project.loose:
            rows.extend(self._device_rows())
        trash = PlaceRow("trash", _("Trash"), "user-trash-symbolic", "trash:///")
        trash.section = "trash"
        self.trash_row = trash
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.MOVE)
        target.connect("drop", lambda _t, value, _x, _y: self.app.ops.trash([f.get_uri() for f in value.get_files()]) or True)
        trash.add_controller(target)
        rows.append(trash)
        for row in rows:
            if not hasattr(row, "section"):
                row.section = "devices" if row.kind == "device" else "places"
            if row.kind in ("place", "project-bookmark", "bookmark") and row.uri and "://" in row.uri \
                    and not row.uri.endswith(":///"):
                target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
                target.connect("drop", lambda t, value, _x, _y, uri=row.uri: self._on_drop(t, uri, value))
                row.add_controller(target)
            self.listbox.append(row)
        self._update_trash_icon()
        self.highlight(self.current_uri)

    def _device_rows(self):
        rows = []
        seen = set()
        for mount in self.volumes.get_mounts():
            if mount.is_shadowed():
                continue
            root = mount.get_root()
            path = root.get_path()
            if path in ("/", None) or path.startswith(("/boot", "/snap", "/var")):
                continue
            seen.add(mount.get_volume())
            row = PlaceRow("device", mount.get_name(), mount.get_symbolic_icon(), root.get_uri(), mount=mount)
            try:
                info = root.query_filesystem_info("filesystem::size,filesystem::free", None)
                size = info.get_attribute_uint64("filesystem::size")
                free = info.get_attribute_uint64("filesystem::free")
                if size:
                    row.add_usage((size - free) / size, _("{free} free of {size}").format(
                        free=format_size(free), size=format_size(size)))
            except GLib.Error:
                pass
            if mount.can_eject() or mount.can_unmount():
                row.add_eject(lambda m=mount: self.eject(m))
            rows.append(row)
        for volume in self.volumes.get_volumes():
            if volume in seen or volume.get_mount() is not None or not volume.can_mount():
                continue
            rows.append(PlaceRow("device", volume.get_name(), volume.get_symbolic_icon(), None, volume=volume))
        return rows

    def _header(self, row, before):
        if before is not None and getattr(before, "section", None) != getattr(row, "section", None):
            if row.get_header() is None:
                row.set_header(Gtk.Separator(margin_top=6, margin_bottom=6, margin_start=12, margin_end=12))
        else:
            row.set_header(None)

    def _update_trash_icon(self):
        row = getattr(self, "trash_row", None)
        if row is None:
            return
        try:
            info = Gio.File.new_for_uri("trash:///").query_info("trash::item-count", Gio.FileQueryInfoFlags.NONE, None)
            full = info.get_attribute_uint32("trash::item-count") > 0
        except GLib.Error:
            full = False
        row.icon.set_from_icon_name("user-trash-full-symbolic" if full else "user-trash-symbolic")

    def highlight(self, uri):
        self.current_uri = uri
        i = 0
        target = None
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if row.uri and uri and row.uri == uri:
                target = row
                break
            i += 1
        if target is not None:
            self.listbox.select_row(target)
        else:
            self.listbox.unselect_all()
        if self.mode == "tree" and uri:
            self.tree.reveal(uri)

    # --- interaction ----------------------------------------------------------------

    def _activate(self, row, new_tab):
        if row.kind == "device" and row.uri is None and row.volume is not None:
            self.mount_volume(row.volume, new_tab)
            return
        if row.uri:
            self.emit("open", row.uri, new_tab)

    def _on_click(self, gesture, _n, x, y):
        row = self.listbox.get_row_at_y(int(y))
        if row is None:
            return
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_SECONDARY:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            rx, ry = fileutil.translate(self.listbox, row, x, y)
            self.emit("menu", row, rx, ry)
        elif button == Gdk.BUTTON_MIDDLE:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._activate(row, True)

    def _on_drop(self, target, uri, value):
        from metafiles.fileactions import drop_mode

        self.emit("drop-files", uri, [f.get_uri() for f in value.get_files()],
                  drop_mode(target.get_current_event_state()))
        return True

    def _make_bookmark_draggable(self, row):
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(f"metafiles-bookmark:{row.uri}"))
        row.add_controller(source)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def drop(_t, value, _x, _y):
            if not isinstance(value, str) or not value.startswith("metafiles-bookmark:"):
                return False
            uri = value[len("metafiles-bookmark:"):]
            index = [u for u, _l in self.app.bookmarks.entries].index(row.uri)
            self.app.bookmarks.move(uri, index)
            return True

        target.connect("drop", drop)
        row.add_controller(target)

    def mount_volume(self, volume, new_tab=False):
        operation = Gtk.MountOperation.new(self.get_root())

        def done(vol, result):
            try:
                vol.mount_finish(result)
            except GLib.Error as e:
                self.app.toast(_("Unable to mount {name}: {error}").format(name=vol.get_name(), error=e.message))
                return
            mount = vol.get_mount()
            if mount is not None:
                self.emit("open", mount.get_root().get_uri(), new_tab)

        volume.mount(Gio.MountMountFlags.NONE, operation, None, done)

    def eject(self, mount):
        operation = Gtk.MountOperation.new(self.get_root())

        def done(m, result):
            try:
                if m.can_eject():
                    m.eject_with_operation_finish(result)
                else:
                    m.unmount_with_operation_finish(result)
            except GLib.Error as e:
                self.app.toast(_("Unable to eject {name}: {error}").format(name=m.get_name(), error=e.message))

        if mount.can_eject():
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, operation, None, done)
        else:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, operation, None, done)


class FolderTree(Gtk.ScrolledWindow):
    """Lazily expanded folder tree of the project (or home), synchronized with the active tab."""

    def __init__(self, app, places):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vexpand=True)
        self.app = app
        self.places = places
        self.root_path = None
        self.model = None
        self.selection = Gtk.SingleSelection(autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        self.view = Gtk.ListView(model=self.selection, factory=factory, single_click_activate=True)
        self.view.add_css_class("navigation-sidebar")
        self.view.connect("activate", self._on_activate)
        self.set_child(self.view)
        self._pending_reveal = None

    def _filter(self, info):
        if info.get_file_type() != Gio.FileType.DIRECTORY:
            return False
        if not self.app.show_hidden and (info.get_is_hidden() or info.get_name().startswith(".")):
            return False
        return True

    def _children(self, gf):
        listing = Gtk.DirectoryList.new("standard::name,standard::display-name,standard::type,standard::is-hidden,"
                                        "standard::symbolic-icon", gf)
        listing.set_monitored(True)
        filtered = Gtk.FilterListModel.new(listing, Gtk.CustomFilter.new(self._filter))
        sorter = Gtk.StringSorter.new(Gtk.ClosureExpression.new(GObject.TYPE_STRING,
                                                                lambda i: i.get_display_name(), None))
        sorter.set_collation(Gtk.Collation.FILENAME)
        return Gtk.SortListModel.new(filtered, sorter)

    def set_root(self, project):
        if project is None:
            return
        root = project.root if not project.loose else fileutil.HOME
        if root == self.root_path and self.model is not None:
            return
        self.root_path = root
        self.model = Gtk.TreeListModel.new(self._children(Gio.File.new_for_path(root)), False, False,
                                           lambda info: self._children(info.get_attribute_object("standard::file")))
        self.selection.set_model(self.model)

    def refresh(self):
        self.root_path = None
        self.set_root(self.places.project)

    def _setup(self, _f, item):
        expander = Gtk.TreeExpander()
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        box.append(label)
        expander.set_child(box)
        expander.label = label
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", lambda t, value, _x, _y, e=expander: self._on_drop(t, e, value))
        expander.add_controller(target)
        item.set_child(expander)

    def _bind(self, _f, item):
        row = item.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        info = row.get_item()
        expander.label.set_text(info.get_display_name())
        expander.uri = info.get_attribute_object("standard::file").get_uri()

    def _on_drop(self, target, expander, value):
        from metafiles.fileactions import drop_mode

        self.places.emit("drop-files", expander.uri, [f.get_uri() for f in value.get_files()],
                         drop_mode(target.get_current_event_state()))
        return True

    def _on_activate(self, _view, position):
        row = self.model.get_item(position)
        if row is not None:
            self.places.emit("open", row.get_item().get_attribute_object("standard::file").get_uri(), False)

    def reveal(self, uri, attempts=20):
        """Expand the ancestors of uri and select it (children load asynchronously)."""
        if self.model is None or self.root_path is None:
            return
        path = Gio.File.new_for_uri(uri).get_path() if "://" in uri and not uri.endswith(":///") else None
        if not path or not fileutil.inside(path, self.root_path) or path == self.root_path:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            return
        parts = os.path.relpath(path, self.root_path).split(os.sep)
        self._reveal_step(parts, 0, None, attempts)

    def _reveal_step(self, parts, depth, parent_row, attempts):
        target_name = parts[depth]
        n = self.model.get_n_items()
        for i in range(n):
            row = self.model.get_item(i)
            if row.get_depth() != depth:
                continue
            if parent_row is not None and row.get_parent() != parent_row:
                continue
            if row.get_item().get_name() == target_name:
                if depth == len(parts) - 1:
                    self.selection.set_selected(i)
                    self.view.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                    return
                row.set_expanded(True)
                GLib.timeout_add(60, lambda: self._reveal_step(parts, depth + 1, row, 20) and False)
                return
        if attempts > 0:
            GLib.timeout_add(80, lambda: self._reveal_step(parts, depth, parent_row, attempts - 1) and False)
