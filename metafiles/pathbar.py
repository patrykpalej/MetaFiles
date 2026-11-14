"""Nautilus-style path bar that turns into a location entry (Ctrl+L, '/', '~') or a search entry."""

import os

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from metafiles import fileutil
from metafiles.i18n import _


class PathBar(Gtk.Stack):
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "current-menu": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "drop-files": (GObject.SignalFlags.RUN_FIRST, None, (str, object, str)),
        "search": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "search-closed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "location-closed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self.set_size_request(120, -1)
        self.segments = []
        self.pane = None

        self.bar = Gtk.Box()
        self.bar.add_css_class("nautilus-pathbar")
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                           hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.buttons = Gtk.Box()
        self.scroller.set_child(self.buttons)
        self.bar.append(self.scroller)
        # Clicking empty space in the bar edits the location, like Nautilus.
        click = Gtk.GestureClick()
        click.connect("released", self._on_bar_click)
        self.bar.add_controller(click)
        self.add_named(self.bar, "path")

        box = Gtk.Box()
        box.add_css_class("linked")
        self.entry = Gtk.Entry(hexpand=True, input_purpose=Gtk.InputPurpose.URL)
        self.entry.connect("activate", self._on_entry_activate)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(key)
        cancel = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.show_path())
        box.append(self.entry)
        box.append(cancel)
        self.add_named(box, "location")

        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text=_("Search current folder"))
        self.search_entry.connect("search-changed", lambda e: self.emit("search", e.get_text()))
        self.search_entry.connect("stop-search", lambda *_: self.close_search())
        self.add_named(self.search_entry, "search")
        self.set_visible_child_name("path")

    # --- path ------------------------------------------------------------------

    def update(self, pane):
        """Rebuild the segments for the pane's location and lock state."""
        self.pane = pane
        child = self.buttons.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.buttons.remove(child)
            child = nxt
        self.segments = self._segments(pane)
        for index, (label, icon, uri) in enumerate(self.segments):
            current = index == len(self.segments) - 1
            self.buttons.append(self._make_button(label, icon, uri, current))
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        adj = self.scroller.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    @staticmethod
    def _segments(pane):
        from metafiles.pane import PLACE_TITLES, is_virtual

        if is_virtual(pane.location):
            title, icon = PLACE_TITLES[pane.location]
            return [(title, icon, pane.location)]
        path = Gio.File.new_for_uri(pane.location).get_path()
        if not path:
            return [(Gio.File.new_for_uri(pane.location).get_parse_name(), "folder-remote-symbolic", pane.location)]
        project = pane.project
        if not project.loose and project.lock and fileutil.inside(path, project.root):
            base, base_label, base_icon = project.root, project.name, "folder-symbolic"
        elif fileutil.inside(path, fileutil.HOME):
            base, base_label, base_icon = fileutil.HOME, _("Home"), "user-home-symbolic"
        else:
            base, base_label, base_icon = "/", "", "drive-harddisk-symbolic"
        segments = [(base_label, base_icon, Gio.File.new_for_path(base).get_uri())]
        rel = os.path.relpath(path, base) if path != base else ""
        current = base
        for part in [p for p in rel.split(os.sep) if p and p != "."]:
            current = os.path.join(current, part)
            segments.append((part, None, Gio.File.new_for_path(current).get_uri()))
        return segments

    def _make_button(self, label, icon, uri, current):
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("nautilus-path-button")
        content = Gtk.Box(spacing=6)
        if icon:
            content.append(Gtk.Image.new_from_icon_name(icon))
        if label:
            text = Gtk.Label(label=label, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=24)
            if current:
                text.add_css_class("heading")
            else:
                text.add_css_class("dim-label")
            content.append(text)
        if current:
            button.add_css_class("current-dir")
            content.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
            button.set_tooltip_text(_("Current folder menu"))
            button.connect("clicked", lambda b: self.emit("current-menu", b))
        else:
            button.set_tooltip_text(Gio.File.new_for_uri(uri).get_parse_name())
            button.connect("clicked", lambda *_: self.emit("navigate", uri))
        button.set_child(content)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", lambda t, value, _x, _y: self._on_drop(t, uri, value))
        button.add_controller(target)
        wrapper = Gtk.Box()
        wrapper.append(button)
        if not current:
            sep = Gtk.Label(label="/")
            sep.add_css_class("dim-label")
            wrapper.append(sep)
        return wrapper

    def _on_drop(self, target, uri, value):
        from metafiles.fileactions import drop_mode

        uris = [f.get_uri() for f in value.get_files()]
        self.emit("drop-files", uri, uris, drop_mode(target.get_current_event_state()))
        return True

    def _on_bar_click(self, gesture, _n, x, y):
        widget = self.bar.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.bar:
            if isinstance(widget, Gtk.Button):
                return
            widget = widget.get_parent()
        self.show_location()

    # --- location entry --------------------------------------------------------------

    def show_location(self, text=None):
        if text is None:
            text = self._current_text()
        self.set_visible_child_name("location")
        self.entry.set_text(text)
        self.entry.grab_focus()
        self.entry.set_position(-1)

    def _current_text(self):
        pane = self.pane
        if pane is None:
            return ""
        path = pane.path()
        if path is None:
            return pane.location
        project = pane.project
        if not project.loose and project.lock and fileutil.inside(path, project.root):
            rel = fileutil.relative_label(path, project.root)
            return "~/" if rel == "." else f"~/{rel}/"
        return fileutil.short_path(path) + ("/" if path != "/" else "")

    def show_path(self):
        self.set_visible_child_name("path")
        self.emit("location-closed")

    def _on_entry_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.show_path()
            return True
        return False

    def resolve(self, text):
        """Location entry text -> URI; '~' means the project root in a locked project."""
        text = text.strip()
        pane = self.pane
        if not text:
            return None
        if "://" in text:
            return text
        project = pane.project if pane else None
        locked = project is not None and not project.loose and project.lock
        if text == "~" or text.startswith("~/"):
            base = project.root if locked else fileutil.HOME
            path = os.path.join(base, text[2:]) if text != "~" else base
        elif os.path.isabs(text):
            path = text
        else:
            current = pane.path() if pane else None
            path = os.path.join(current or fileutil.HOME, text)
        return Gio.File.new_for_path(os.path.normpath(path)).get_uri()

    def _on_entry_activate(self, entry):
        uri = self.resolve(entry.get_text())
        if uri:
            self.emit("navigate", uri)
        self.show_path()

    # --- search ----------------------------------------------------------------------

    def show_search(self, text=None, placeholder=None):
        self.set_visible_child_name("search")
        if placeholder:
            self.search_entry.set_placeholder_text(placeholder)
        if text is not None:
            self.search_entry.set_text(text)
        self.search_entry.grab_focus()
        self.search_entry.set_position(-1)

    def close_search(self):
        if self.get_visible_child_name() == "search":
            self.search_entry.set_text("")
            self.set_visible_child_name("path")
            self.emit("search-closed")

    @property
    def searching(self):
        return self.get_visible_child_name() == "search"
