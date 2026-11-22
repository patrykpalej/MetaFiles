"""Quick open (Ctrl+P): fuzzy search over the project's files and folders."""

import os
import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from metafiles import fileutil
from metafiles.remote import is_remote_path
from metafiles.fuzzy import FuzzyIndex, build_index_async
from metafiles.i18n import _

# One index per (root, hidden) kept for the session; refreshed in the background on each open.
_INDEXES = {}


def _markup(path, positions):
    marks = set(positions)
    out = []
    for i, ch in enumerate(path):
        text = GLib.markup_escape_text(ch)
        out.append(f"<b><u>{text}</u></b>" if i in marks else text)
    return "".join(out)


class QuickOpen(Adw.Dialog):
    def __init__(self, window, project):
        super().__init__(content_width=680, content_height=480, title=_("Quick Open"))
        self.window = window
        self.project = project
        self.root = project.root
        self.index = None
        self.generation = 0
        self.results = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text=_("Go to file or folder in {name}…").format(
            name=project.name if not project.loose else _("Home")))
        header.set_title_widget(self.entry)
        toolbar.add_top_bar(header)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-activated", lambda _l, row: self._accept(row, "go"))
        self.status = Gtk.Label(xalign=0, margin_start=12, margin_bottom=6, margin_top=6)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Gtk.ScrolledWindow(child=self.listbox, vexpand=True))
        box.append(self.status)
        toolbar.set_content(box)
        hint = Gtk.Label(label=_("Enter: go to · Ctrl+Enter: new tab · Shift+Enter: open · Alt+Enter: preview"),
                         margin_bottom=8)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        toolbar.add_bottom_bar(hint)
        self.set_child(toolbar)

        self.entry.connect("search-changed", lambda *_: self._search())
        self.entry.connect("activate", lambda *_: self._accept(self.listbox.get_selected_row(), "go"))
        self.entry.connect("stop-search", lambda *_: self.close())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)

    def popup(self):
        self.present(self.window)
        GLib.idle_add(lambda: self.entry.grab_focus() and False)
        key = (self.root, self.window.app.show_hidden)
        cached = _INDEXES.get(key)
        if cached is not None:
            self.index = cached
            self._search()
        else:
            self.status.set_text(_("Indexing…"))
            self._show_recent()
        use_git = self.window.app.git.get(self.root) is not None
        build_index_async(self.root, use_git, self.window.app.show_hidden, self._on_index)

    def _on_index(self, paths):
        if self.project.lock:
            paths = [p for p in paths if self.project.contains(os.path.join(self.root, p.rstrip("/")))]
        self.index = FuzzyIndex(paths)
        _INDEXES[(self.root, self.window.app.show_hidden)] = self.index
        self._search()

    # --- searching -------------------------------------------------------------

    def _search(self):
        query = self.entry.get_text().strip()
        if not query or self.index is None:
            self._show_recent()
            return
        self.generation += 1
        generation = self.generation
        index = self.index

        def work():
            results = index.search(query, 200)
            GLib.idle_add(lambda: generation == self.generation and self._show(results, query) and False)

        threading.Thread(target=work, daemon=True).start()

    def _show_recent(self):
        manager = Gtk.RecentManager.get_default()
        items = []
        for item in manager.get_items():
            uri = item.get_uri()
            if not uri.startswith("file://"):
                continue
            path = Gio.File.new_for_uri(uri).get_path()
            if path and fileutil.inside(path, self.root) and (is_remote_path(path) or os.path.exists(path)):
                modified = item.get_modified()
                items.append((modified.to_unix() if modified else 0, os.path.relpath(path, self.root)))
        items.sort(reverse=True)
        self._show([(0, rel, []) for _t, rel in items[:30]], "", recent=True)

    def _show(self, results, query, recent=False):
        self.results = results
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        for _score, rel, positions in results:
            row = Gtk.ListBoxRow()
            row.rel = rel
            box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=6)
            is_folder = rel.endswith("/")
            box.append(Gtk.Image.new_from_icon_name("folder-symbolic" if is_folder else "text-x-generic-symbolic"))
            label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START, hexpand=True)
            label.set_markup(_markup(rel, positions))
            box.append(label)
            row.set_child(box)
            self.listbox.append(row)
        first = self.listbox.get_row_at_index(0)
        if first is not None:
            self.listbox.select_row(first)
        if recent:
            self.status.set_text(_("Recently opened in this project") if results else
                                 (_("Indexing…") if self.index is None else _("Type to search")))
        else:
            self.status.set_text(_("{n} matches").format(n=len(results)) if results else _("No matches"))
        return False

    # --- keys and actions --------------------------------------------------------

    def _move(self, step):
        row = self.listbox.get_selected_row()
        index = row.get_index() + step if row is not None else 0
        target = self.listbox.get_row_at_index(max(0, index))
        if target is not None:
            self.listbox.select_row(target)
            scroller = self.listbox.get_ancestor(Gtk.ScrolledWindow)
            adj = scroller.get_vadjustment() if scroller is not None else None
            if adj is not None:
                _x, y = fileutil.translate(target, self.listbox, 0, 0)
                if (y < adj.get_value() or y + target.get_height() > adj.get_value() + adj.get_page_size()):
                    adj.set_value(max(0, y - adj.get_page_size() / 2))

    def _on_key(self, _ctl, keyval, _code, state):
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Tab):
            self._move(+1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            mode = "go"
            if state & Gdk.ModifierType.CONTROL_MASK:
                mode = "tab"
            elif state & Gdk.ModifierType.SHIFT_MASK:
                mode = "open"
            elif state & Gdk.ModifierType.ALT_MASK:
                mode = "preview"
            self._accept(self.listbox.get_selected_row(), mode)
            return True
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _accept(self, row, mode):
        if row is None:
            return
        path = os.path.join(self.root, row.rel.rstrip("/"))
        gf = Gio.File.new_for_path(path)
        is_folder = os.path.isdir(path)
        window = self.window
        self.close()
        if mode == "open" and not is_folder:
            window._launch(_info(gf))
            return
        if mode == "tab":
            window.open_in_new_tab(gf.get_uri() if is_folder else gf.get_parent().get_uri())
            if not is_folder:
                GLib.timeout_add(300, lambda: window.active_pane.select_uris([gf.get_uri()]) and False)
            return
        pane = window.active_pane
        if pane is None:
            return
        if is_folder:
            pane.navigate(gf.get_uri())
        else:
            pane.navigate(gf.get_parent().get_uri(), select=[gf.get_uri()])
        pane.focus_view()
        if mode == "preview" and not is_folder:
            GLib.timeout_add(250, lambda: window.quick_preview() and False)


def _info(gf):
    from metafiles.fileview import query_info_with_file

    return query_info_with_file(gf)
