"""Right sidebar with projects and groups (mechanics ported from MetaTerminal to GTK 4)."""

from gi.repository import Gdk, GLib, GObject, Gtk, Pango

from metafiles import fileutil
from metafiles.i18n import _

# Single-key shortcuts that work while the project list has keyboard focus.
SIDEBAR_KEYS = {"add-project": "P", "new-group": "G"}


class ProjectRow(Gtk.ListBoxRow):
    def __init__(self, view):
        super().__init__()
        self.view = view
        grid = Gtk.Grid(column_spacing=8, row_spacing=2, margin_start=8, margin_end=8, margin_top=7,
                        margin_bottom=7)
        self.name = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.path = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START)
        self.path.add_css_class("dim-label")
        self.path.add_css_class("caption")
        self.branch = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.branch.add_css_class("dim-label")
        self.branch.add_css_class("caption")
        self.branch.add_css_class("mf-branch")
        self.count = Gtk.Label()
        self.count.add_css_class("mf-count")
        self.lock_icon = Gtk.Image.new_from_icon_name("changes-allow-symbolic")
        self.lock_icon.add_css_class("dim-label")
        icon = "user-home-symbolic" if view.loose else "folder-symbolic"
        self.icon = Gtk.Image.new_from_icon_name(icon)
        grid.attach(self.icon, 0, 0, 1, 1)
        grid.attach(self.name, 1, 0, 1, 1)
        grid.attach(self.lock_icon, 2, 0, 1, 1)
        grid.attach(self.count, 3, 0, 1, 1)
        grid.attach(self.path, 0, 1, 4, 1)
        grid.attach(self.branch, 0, 2, 4, 1)
        if view.loose:
            self.add_css_class("mf-loose-row")
            self.name.add_css_class("mf-loose-name")
        self.set_child(grid)
        self.refresh()

    def refresh(self, branch=None):
        view = self.view
        self.name.set_markup(f"<b>{GLib.markup_escape_text(view.state.name)}</b>")
        if view.loose:
            self.path.set_visible(False)
            self.set_tooltip_text(_("Browsing not tied to any project; tabs start in ~"))
            self.lock_icon.set_visible(False)
        else:
            self.path.set_text(fileutil.short_path(view.root))
            self.set_tooltip_text(view.root)
            self.lock_icon.set_visible(not view.state.lock)
            self.lock_icon.set_tooltip_text(_("Navigation lock is off"))
        n = view.tab_count()
        self.count.set_text(str(n))
        self.count.set_visible(n > 0)
        self.branch.set_text(branch or "")
        self.branch.set_visible(bool(branch))
        self.set_opacity(1.0 if view.available else 0.5)


class EmptyGroupRow(Gtk.ListBoxRow):
    """Stands in for a group without projects so its header can be shown."""

    def __init__(self, group, on_add):
        super().__init__(selectable=False, activatable=False, focusable=False)
        self.group = group
        self.add_css_class("mf-empty-group")
        box = Gtk.Box(spacing=8, margin_start=12, margin_end=8, margin_top=6, margin_bottom=6)
        label = Gtk.Label(label=_("Empty group"), xalign=0, hexpand=True)
        label.add_css_class("dim-label")
        button = Gtk.Button(label=_("Add project…"), focus_on_click=False)
        button.add_css_class("flat")
        button.set_tooltip_text(_("Add a directory as a project in group “{group}”").format(group=group))
        button.connect("clicked", lambda *_: on_add(self.group))
        box.append(label)
        box.append(button)
        self.set_child(box)


class ProjectSidebar(Gtk.Box):
    """The project list. The window supplies callbacks for menus and actions."""

    __gsignals__ = {
        "project-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "project-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "row-menu": (GObject.SignalFlags.RUN_FIRST, None, (object, float, float)),
        "group-menu": (GObject.SignalFlags.RUN_FIRST, None, (str, object, float, float)),
        "escape": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "drop-files": (GObject.SignalFlags.RUN_FIRST, None, (object, object, str)),
        # A project row was dragged to (group, before): before is the ProjectState it now precedes, or None.
        "project-moved": (GObject.SignalFlags.RUN_FIRST, None, (object, str, object)),
    }

    def __init__(self, session, on_add_to_group):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self.on_add_to_group = on_add_to_group
        self.set_size_request(200, -1)

        # Single click only selects (and focuses the sidebar); double click or Enter jumps to the files.
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, activate_on_single_click=False)
        self.listbox.add_css_class("mf-projects")
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)
        self.listbox.connect("row-activated", lambda _l, row: self.emit("project-activated", row.view)
                             if isinstance(row, ProjectRow) else None)
        self.listbox.set_sort_func(self._row_sort)
        self.listbox.set_header_func(self._row_header)
        context = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        context.connect("pressed", self._on_context)
        self.listbox.add_controller(context)
        # A click anywhere in the project list moves keyboard focus there, like Ctrl+Tab.
        press = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", self._on_pressed)
        self.listbox.add_controller(press)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.listbox.add_controller(keys)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.listbox)
        self.append(scroller)

    # --- rows -------------------------------------------------------------------

    def rows(self):
        """Project rows in display order (empty-group placeholders excluded)."""
        rows, i = [], 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, ProjectRow):
                rows.append(row)
            i += 1
        return rows

    @property
    def views(self):
        return [row.view for row in self.rows()]

    def add_view(self, view):
        row = ProjectRow(view)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", lambda t, value, _x, _y, view=view: self._on_drop(t, view, value))
        row.add_controller(target)
        if not view.loose:
            self._make_draggable(row)
        self._add_project_drop(row, motion=self._row_drop_motion, drop=self._drop_on_row)
        self.listbox.append(row)
        return row

    # --- dragging projects between groups --------------------------------------

    def _make_draggable(self, row):
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        # The row itself is the drag content: its GType never matches file or text drops.
        source.connect("prepare", self._drag_prepare, row)
        source.connect("drag-begin", lambda s, _d: s.set_icon(Gtk.WidgetPaintable.new(row), *row.drag_hotspot))
        source.connect("drag-end", lambda *_: self._clear_drop_marks())
        row.add_controller(source)

    @staticmethod
    def _drag_prepare(_source, x, y, row):
        row.drag_hotspot = (int(x), int(y))
        return Gdk.ContentProvider.new_for_value(row)

    def _add_project_drop(self, widget, drop, motion=None):
        target = Gtk.DropTarget.new(ProjectRow.__gtype__, Gdk.DragAction.MOVE)
        if motion is not None:
            target.connect("motion", motion)
            target.connect("leave", lambda *_: self._clear_drop_marks())
        target.connect("drop", lambda _t, value, _x, y: drop(widget, value, y))
        widget.add_controller(target)

    def _row_drop_motion(self, target, _x, y):
        row = target.get_widget()
        above = y < row.get_height() / 2
        self._clear_drop_marks()
        if not row.view.loose:
            row.add_css_class("mf-drop-above" if above else "mf-drop-below")
            self._marked = row
        return Gdk.DragAction.MOVE

    def _clear_drop_marks(self):
        row = getattr(self, "_marked", None)
        if row is not None:
            row.remove_css_class("mf-drop-above")
            row.remove_css_class("mf-drop-below")
            self._marked = None

    def _states(self):
        return [p for p in self.session.projects if not p.loose]

    def _first_in(self, group):
        return next((p for p in self._states() if p.group == group), None)

    def _drop_on_row(self, row, dragged, y):
        self._clear_drop_marks()
        if not isinstance(dragged, ProjectRow) or dragged is row:
            return False
        target = row.view.state
        if target.loose:
            # Onto the general entry: becomes the first project without a group.
            self.emit("project-moved", dragged.view, "", self._first_in(""))
            return True
        projects = self.session.projects
        if y < row.get_height() / 2:
            before = target
        else:
            index = projects.index(target) + 1
            before = projects[index] if index < len(projects) else None
        self.emit("project-moved", dragged.view, target.group, before)
        return True

    def _drop_into_group(self, group):
        def drop(_widget, dragged, _y):
            if not isinstance(dragged, ProjectRow):
                return False
            self.emit("project-moved", dragged.view, group, self._first_in(group))
            return True
        return drop

    def _on_drop(self, target, view, value):
        from metafiles.fileactions import drop_mode

        uris = [f.get_uri() for f in value.get_files()]
        # Dropping on a project copies unless Shift asks to move.
        mode = drop_mode(target.get_current_event_state())
        self.emit("drop-files", view, uris, "copy" if mode == "auto" else mode)
        return True

    def remove_view(self, view):
        row = self.row_for(view)
        if row is not None:
            self.listbox.remove(row)

    def row_for(self, view):
        for row in self.rows():
            if row.view is view:
                return row
        return None

    def selected_view(self):
        row = self.listbox.get_selected_row()
        return row.view if isinstance(row, ProjectRow) else None

    def select_view(self, view):
        row = self.row_for(view)
        if row is not None:
            self.listbox.select_row(row)

    def select_index(self, index):
        rows = self.rows()
        if 0 <= index < len(rows):
            self.listbox.select_row(rows[index])

    def cycle(self, step):
        rows = self.rows()
        if rows:
            current = self.listbox.get_selected_row()
            index = rows.index(current) if current in rows else 0
            self.select_index((index + step) % len(rows))

    def sync_empty_groups(self):
        used = {p.group for p in self.session.projects if p.group}
        empty = [g for g in self.session.groups if g not in used]
        existing = {}
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            i += 1
            if isinstance(row, EmptyGroupRow):
                if row.group in empty and row.group not in existing:
                    existing[row.group] = row
                else:
                    self.listbox.remove(row)
                    i -= 1
        for group in empty:
            if group not in existing:
                row = EmptyGroupRow(group, self.on_add_to_group)
                self._add_project_drop(row, self._drop_into_group(group))
                self.listbox.append(row)

    def resort(self):
        self.session.ensure_loose()
        self.sync_empty_groups()
        self.listbox.invalidate_sort()
        self.listbox.invalidate_headers()

    def _sort_key(self, row):
        groups = self.session.groups
        if isinstance(row, EmptyGroupRow):
            return (2, groups.index(row.group) if row.group in groups else len(groups), 0)
        state = row.view.state
        try:
            index = self.session.projects.index(state)
        except ValueError:
            index = len(self.session.projects)
        if state.loose:
            return (0, 0, 0)
        if not state.group:
            return (1, 0, index)
        return (2, groups.index(state.group) if state.group in groups else len(groups), index)

    def _row_sort(self, a, b):
        ka, kb = self._sort_key(a), self._sort_key(b)
        return (ka > kb) - (ka < kb)

    @staticmethod
    def _section(row):
        if isinstance(row, EmptyGroupRow):
            return row.group
        return "\0general" if row.view.loose else row.view.state.group

    def _row_header(self, row, before):
        section = self._section(row)
        if section == "\0general":
            want = None
        else:
            previous = None if before is None else self._section(before)
            want = None if previous == section else (("group", section) if section else ("ungrouped",))
        if getattr(row, "header_key", None) == want:
            return
        row.header_key = want
        row.set_header(None if want is None else self._make_header(want))

    def _make_header(self, key):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        separator = Gtk.Separator(margin_top=6)
        separator.add_css_class("mf-section-separator")
        box.append(separator)
        if key[0] == "ungrouped":
            label = Gtk.Label(label=_("Unassigned projects"), xalign=0, ellipsize=Pango.EllipsizeMode.END)
            label.add_css_class("mf-ungrouped-header")
            self._add_project_drop(label, self._drop_into_group(""))
            box.append(label)
            return box
        name = key[1]
        line = Gtk.Box(spacing=4)
        line.add_css_class("mf-group-band")
        title = Gtk.Box(spacing=7, hexpand=True)
        title.append(Gtk.Image.new_from_icon_name("view-list-symbolic"))
        title.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        title.set_tooltip_text(_("Project group; click or right-click for group options"))
        gesture = Gtk.GestureClick(button=0)
        gesture.connect("pressed", lambda g, _n, x, y: self.emit("group-menu", name, title, x, y))
        title.add_controller(gesture)
        line.append(title)
        add = Gtk.Button(icon_name="list-add-symbolic", focus_on_click=False, valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.add_css_class("mf-group-add")
        add.set_tooltip_text(_("Add a directory as a project in group “{group}”").format(group=name))
        add.connect("clicked", lambda *_: self.on_add_to_group(name))
        line.append(add)
        self._add_project_drop(line, self._drop_into_group(name))
        box.append(line)
        return box

    # --- events -------------------------------------------------------------------

    def _on_row_selected(self, _listbox, row):
        if isinstance(row, ProjectRow):
            self.emit("project-selected", row.view)

    def _on_pressed(self, _gesture, _n, _x, y):
        # Focus before the row gets selected, so selection does not send focus to the files.
        row = self.listbox.get_row_at_y(int(y))
        if row is None or not row.get_focusable():
            row = self.listbox.get_selected_row() or self.listbox.get_row_at_index(0)
        if row is not None and not row.has_focus():
            row.grab_focus()

    def _on_context(self, gesture, _n, x, y):
        row = self.listbox.get_row_at_y(int(y))
        if row is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if isinstance(row, EmptyGroupRow):
            rx, ry = fileutil.translate(self.listbox, row, x, y)
            self.emit("group-menu", row.group, row, rx, ry)
            return
        row.grab_focus()
        self.listbox.select_row(row)
        rx, ry = fileutil.translate(self.listbox, row, x, y)
        GLib.idle_add(lambda: self.emit("row-menu", row, rx, ry) and False)

    def _on_key(self, _ctl, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.emit("escape")
            return True
        if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
            row = self.listbox.get_selected_row()
            if row is not None:
                self.emit("row-menu", row, -1.0, -1.0)
            return True
        modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        if not state & modifiers:
            key = chr(Gdk.keyval_to_unicode(Gdk.keyval_to_upper(keyval)) or 0)
            for action, letter in SIDEBAR_KEYS.items():
                if key == letter:
                    self.activate_action(f"win.{action}", None)
                    return True
        return False

    def has_focus_inside(self):
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        return focus is not None and (focus is self.listbox or focus.is_ancestor(self.listbox))

    def focus_selected(self):
        row = self.listbox.get_selected_row() or self.listbox.get_row_at_index(0)
        if row is not None:
            row.grab_focus()
