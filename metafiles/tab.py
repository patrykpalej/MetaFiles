"""A tab: one or two panes (split view) plus optional preview and terminal panels."""

from gi.repository import GLib, GObject, Gtk

from metafiles import fileutil
from metafiles.i18n import _
from metafiles.pane import Pane, is_virtual
from metafiles.storage import new_tab_state


class Tab(Gtk.Box):
    __gsignals__ = {
        # Location, title or active pane changed: the window refreshes header and tab label.
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, project, state):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.app = app
        self.project = project
        self.page = None  # Adw.TabPage, set by the project view
        self.custom_name = state.get("name", "")
        self.panes = []
        self.active = None
        self.preview = None
        self.terminal = None
        self.preview_width = state.get("preview_width", 320)
        self.terminal_height = state.get("terminal_height", 220)

        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True,
                               shrink_start_child=False, shrink_end_child=False)
        self.side = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, shrink_end_child=False,
                              resize_end_child=False)
        self.side.set_start_child(self.split)
        self.vertical = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, shrink_end_child=False,
                                  resize_end_child=False)
        self.vertical.set_start_child(self.side)
        self.append(self.vertical)

        panes = state.get("panes") or [{"location": project.root_uri, "view": ""}]
        for pane_state in panes[:2]:
            self._add_pane(pane_state["location"], pane_state.get("view", ""))
        self.set_active(self.panes[min(state.get("active_pane", 0), len(self.panes) - 1)])
        if state.get("preview"):
            GLib.idle_add(lambda: self.toggle_preview(True) and False)
        if state.get("terminal"):
            GLib.idle_add(lambda: self.toggle_terminal(True, focus=False) and False)

    # --- panes ------------------------------------------------------------------

    def _add_pane(self, location, view=""):
        location = self.project.validate_location(location)
        pane = Pane(self.app, self.project, location, view)
        pane.tab = self
        pane.connect("focused", lambda p: self.set_active(p))
        pane.connect("location-changed", self._on_pane_changed)
        pane.connect("search-changed", self._on_pane_changed)
        pane.connect("view-changed", self._on_pane_changed)
        pane.connect("selection-changed", self._on_selection)
        self.panes.append(pane)
        if len(self.panes) == 1:
            self.split.set_start_child(pane)
        else:
            self.split.set_end_child(pane)
            GLib.idle_add(self._center_split)
        self._update_split_style()
        return pane

    def _center_split(self):
        width = self.split.get_allocated_width()
        if width > 1:
            self.split.set_position(width // 2)
        return False

    @property
    def is_split(self):
        return len(self.panes) == 2

    def toggle_split(self):
        if self.is_split:
            inactive = self.other_pane()
            self._remove_pane(inactive)
        else:
            pane = self._add_pane(self.active.location, self.active.view_mode)
            self.set_active(pane)
            GLib.idle_add(lambda: pane.focus_view() and False)
        self.emit("changed")

    def _remove_pane(self, pane):
        pane.destroy_pane()
        self.panes.remove(pane)
        if self.split.get_end_child() is pane:
            self.split.set_end_child(None)
        else:
            self.split.set_start_child(None)
            other = self.split.get_end_child()
            self.split.set_end_child(None)
            self.split.set_start_child(other)
        self.set_active(self.panes[0])
        self._update_split_style()

    def other_pane(self):
        if not self.is_split:
            return None
        return self.panes[1] if self.active is self.panes[0] else self.panes[0]

    def switch_pane(self):
        other = self.other_pane()
        if other is not None:
            self.set_active(other)
            other.focus_view()

    def set_active(self, pane):
        if pane is self.active:
            return
        self.active = pane
        self._update_split_style()
        if self.preview is not None:
            self.preview.follow(pane)
        self.emit("changed")
        self.emit("selection-changed")

    def _update_split_style(self):
        for pane in self.panes:
            if self.is_split and pane is self.active:
                pane.add_css_class("active-pane")
            else:
                pane.remove_css_class("active-pane")
            if self.is_split:
                pane.add_css_class("split-pane")
            else:
                pane.remove_css_class("split-pane")

    def _on_pane_changed(self, pane):
        if pane is self.active:
            self.emit("changed")
            if self.terminal is not None:
                self.terminal.follow_view(pane)

    def _on_selection(self, pane):
        if pane is self.active:
            self.emit("selection-changed")

    # --- panels -------------------------------------------------------------------

    def toggle_preview(self, show=None):
        from metafiles.preview.panel import PreviewPanel

        show = self.preview is None if show is None else show
        if show and self.preview is None:
            self.preview = PreviewPanel(self.app)
            self.side.set_end_child(self.preview)
            GLib.idle_add(self._size_preview)
            self.preview.follow(self.active)
        elif not show and self.preview is not None:
            self.preview_width = self.side.get_allocated_width() - self.side.get_position()
            self.preview.detach()
            self.side.set_end_child(None)
            self.preview = None
        self.emit("changed")

    def _size_preview(self):
        width = self.side.get_allocated_width()
        if width > 1:
            self.side.set_position(max(200, width - self.preview_width))
        return False

    def toggle_terminal(self, show=None, focus=True):
        from metafiles.terminal import TerminalPanel, terminal_available

        if not terminal_available():
            self.app.toast(_("The terminal panel needs the package {pkg}.").format(pkg="gir1.2-vte-3.91"))
            return
        if show is None:
            show = self.terminal is None or not self.terminal.get_visible()
        if show:
            if self.terminal is None:
                self.terminal = TerminalPanel(self.app, self.project, self.active)
                self.terminal.connect("directory-changed", self._on_terminal_cwd)
                self.terminal.connect("exited", self._on_terminal_exited)
                self.vertical.set_end_child(self.terminal)
                GLib.idle_add(self._size_terminal)
            self.terminal.set_visible(True)
            if focus:
                self.terminal.focus()
        elif self.terminal is not None:
            self.terminal_height = self.vertical.get_allocated_height() - self.vertical.get_position()
            self.terminal.set_visible(False)
            self.active.focus_view()
        self.emit("changed")

    def _size_terminal(self):
        height = self.vertical.get_allocated_height()
        if height > 1:
            self.vertical.set_position(max(120, height - self.terminal_height))
        return False

    def terminal_visible(self):
        return self.terminal is not None and self.terminal.get_visible()

    def _on_terminal_cwd(self, _terminal, uri):
        if self.active.location != uri and not self.active.search_text:
            if self.project.allows(uri):
                self.active.navigate(uri)

    def _on_terminal_exited(self, terminal):
        self.vertical.set_end_child(None)
        self.terminal = None
        self.active.focus_view()
        self.emit("changed")

    def running_process(self):
        return self.terminal is not None and self.terminal.has_running_process()

    # --- labels ---------------------------------------------------------------------

    def title(self):
        return self.custom_name or self.active.title()

    def tooltip(self):
        path = self.active.path()
        if path is None:
            label = self.active.title()
        elif self.project.loose:
            label = fileutil.short_path(path)
        else:
            label = fileutil.relative_label(path, self.project.root)
        if self.custom_name:
            label = f"{self.custom_name}\n{label}"
        return label + _("\n(double-click: rename)")

    def icon_name(self):
        if is_virtual(self.active.location):
            from metafiles.pane import PLACE_TITLES

            return PLACE_TITLES[self.active.location][1]
        return None

    # --- state ------------------------------------------------------------------------

    def state(self):
        state = new_tab_state(self.panes[0].location)
        state["panes"] = [p.state() for p in self.panes]
        state["active_pane"] = self.panes.index(self.active)
        state["name"] = self.custom_name
        state["preview"] = self.preview is not None
        state["terminal"] = self.terminal_visible()
        if self.preview is not None:
            self.preview_width = self.side.get_allocated_width() - self.side.get_position()
        if self.terminal_visible():
            self.terminal_height = self.vertical.get_allocated_height() - self.vertical.get_position()
        state["preview_width"] = max(200, self.preview_width)
        state["terminal_height"] = max(120, self.terminal_height)
        return state

    def focus(self):
        self.active.focus_view()

    def close(self):
        for pane in self.panes:
            pane.destroy_pane()
        if self.terminal is not None:
            self.terminal.terminate()
        if self.preview is not None:
            self.preview.detach()
