"""A project: a directory with its own tabs, navigation lock and bookmarks."""

import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from metafiles import fileutil
from metafiles.i18n import _
from metafiles.pane import is_virtual
from metafiles.storage import new_tab_state
from metafiles.tab import Tab


class ProjectView(Gtk.Stack):
    __gsignals__ = {
        # Tabs, labels or the active tab changed; the sidebar row and window follow.
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "tab-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, state):
        super().__init__()
        self.app = app
        self.state = state
        self.loose = state.loose
        self.root = fileutil.HOME if self.loose else fileutil.real(state.path)
        self.started = False
        self.closed_tabs = []

        self.tab_view = Adw.TabView(vexpand=True, hexpand=True)
        self.tab_view.connect("notify::selected-page", lambda *_: self._on_selected())
        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("page-reordered", lambda *_: self.emit("changed"))
        self.tab_view.connect("page-detached", lambda *_: self._after_detach())
        self.add_named(self.tab_view, "tabs")

        self.placeholder = Adw.StatusPage(icon_name="folder-symbolic", vexpand=True)
        button = Gtk.Button(label=_("New Tab"), halign=Gtk.Align.CENTER, action_name="win.new-tab")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        self.placeholder.set_child(button)
        self.add_named(self.placeholder, "empty")
        self._update_visible()

    # --- identity ------------------------------------------------------------

    @property
    def name(self):
        return self.state.name

    @property
    def lock(self):
        return not self.loose and self.state.lock

    @property
    def hide_git_ignored(self):
        return self.state.hide_git_ignored

    @property
    def root_uri(self):
        return Gio.File.new_for_path(self.root).get_uri()

    @property
    def available(self):
        return self.loose or os.path.isdir(self.root)

    def contains(self, path):
        """Whether items at path may be listed here (always true when not locked)."""
        if not self.lock:
            return True
        return fileutil.inside_project(path, self.root)

    def allows(self, uri):
        """The navigation lock: may a pane show this location?"""
        if is_virtual(uri) or not self.lock:
            return True
        path = Gio.File.new_for_uri(uri).get_path()
        if path is None:
            return False
        return fileutil.inside_project(path, self.root)

    def refuse_navigation(self, uri):
        message = _("Navigation outside the project is locked")
        window = self.app.window_for(self)
        if window is None:
            return
        if uri:
            window.toast(message, _("Open in Unassigned Tabs"),
                         lambda: window.open_unassigned(uri))
        else:
            window.toast(message)

    def validate_location(self, uri):
        """Nearest existing folder at or above uri that this project may show."""
        if is_virtual(uri):
            return uri
        gf = Gio.File.new_for_uri(uri) if "://" in uri else Gio.File.new_for_path(uri)
        path = gf.get_path()
        if path is None:
            return gf.get_uri()
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        candidate = Gio.File.new_for_path(path).get_uri()
        if not self.allows(candidate):
            return self.root_uri
        return candidate

    # --- tabs ------------------------------------------------------------------

    @property
    def tabs(self):
        pages = self.tab_view.get_pages()
        return [pages.get_item(i).get_child() for i in range(pages.get_n_items())]

    @property
    def current(self):
        page = self.tab_view.get_selected_page()
        return page.get_child() if page is not None else None

    def ensure_started(self):
        if self.started or not self.available:
            self._update_visible()
            return
        self.started = True
        tabs = self.state.tabs or [new_tab_state(self.root_uri)]
        for state in tabs:
            self.new_tab(state=state, focus=False)
        pages = self.tab_view.get_n_pages()
        if pages:
            index = min(self.state.active_tab, pages - 1)
            self.tab_view.set_selected_page(self.tab_view.get_nth_page(max(0, index)))

    def new_tab(self, location=None, state=None, focus=True, background=False, after_current=False):
        if not self.available:
            self._update_visible()
            return None
        self.started = True
        if state is None:
            if location is None:
                current = self.current
                location = current.active.location if current is not None else self.root_uri
                view = current.active.view_mode if current is not None else ""
            else:
                view = ""
            state = new_tab_state(location, view)
        tab = Tab(self.app, self, state)
        tab.connect("changed", self._on_tab_changed)
        tab.connect("selection-changed", lambda t: t is self.current and self.emit("selection-changed"))
        current_page = self.tab_view.get_selected_page()
        if after_current and current_page is not None:
            page = self.tab_view.add_page(tab, current_page)
        else:
            page = self.tab_view.append(tab)
        tab.page = page
        self._refresh_page(tab)
        if not background:
            self.tab_view.set_selected_page(page)
        self._update_visible()
        if focus and not background:
            GLib.idle_add(lambda: tab.focus() and False)
        self.emit("changed")
        return tab

    def close_tab(self, tab):
        if tab is not None and tab.page is not None:
            self.tab_view.close_page(tab.page)

    def _on_close_page(self, view, page):
        tab = page.get_child()
        window = self.app.window_for(self)
        if tab.running_process() and window is not None:
            def answer(ok):
                if ok:
                    self._remember_closed(tab)
                    tab.close()
                view.close_page_finish(page, ok)
            window.confirm_async(_("Close tab?"), _("A process is still running in the tab's terminal."),
                                 _("_Close"), answer)
            return True
        self._remember_closed(tab)
        tab.close()
        view.close_page_finish(page, True)
        return True

    def _remember_closed(self, tab):
        self.closed_tabs.append(tab.state())
        del self.closed_tabs[:-20]

    def restore_closed_tab(self):
        if self.closed_tabs:
            self.new_tab(state=self.closed_tabs.pop())

    def _after_detach(self):
        self._update_visible()
        self.emit("changed")
        GLib.idle_add(lambda: self.current and self.current.focus() and False)

    def _on_selected(self):
        self.emit("tab-changed")
        self.emit("changed")

    def _on_tab_changed(self, tab):
        self._refresh_page(tab)
        if tab is self.current:
            self.emit("tab-changed")

    def _refresh_page(self, tab):
        page = tab.page
        if page is None:
            return
        page.set_title(tab.title())
        page.set_tooltip(GLib.markup_escape_text(tab.tooltip()))
        icon = tab.icon_name()
        page.set_icon(Gio.ThemedIcon.new(icon) if icon else None)
        page.set_indicator_icon(Gio.ThemedIcon.new("utilities-terminal-symbolic") if tab.terminal_visible() else None)

    def refresh_labels(self):
        for tab in self.tabs:
            self._refresh_page(tab)

    def select_index(self, index):
        if 0 <= index < self.tab_view.get_n_pages():
            self.tab_view.set_selected_page(self.tab_view.get_nth_page(index))

    def cycle(self, step):
        n = self.tab_view.get_n_pages()
        if n:
            page = self.tab_view.get_selected_page()
            index = self.tab_view.get_page_position(page)
            self.select_index((index + step) % n)

    def move_current(self, step):
        page = self.tab_view.get_selected_page()
        if page is None:
            return
        if step < 0:
            self.tab_view.reorder_backward(page)
        else:
            self.tab_view.reorder_forward(page)

    def _update_visible(self):
        if self.tab_view.get_n_pages():
            self.set_visible_child_name("tabs")
            return
        if self.available:
            self.placeholder.set_icon_name("folder-symbolic" if not self.loose else "user-home-symbolic")
            self.placeholder.set_title(_("No Open Tabs"))
            self.placeholder.set_description(_("Open a tab to browse {where}.").format(
                where=fileutil.short_path(self.root) if not self.loose else _("your files")))
            self.placeholder.get_child().set_visible(True)
        else:
            self.placeholder.set_icon_name("folder-visiting-symbolic")
            self.placeholder.set_title(_("Project Directory Not Found"))
            self.placeholder.set_description(self.root)
            self.placeholder.get_child().set_visible(False)
        self.set_visible_child_name("empty")

    # --- lock -------------------------------------------------------------------

    def set_lock(self, enabled):
        self.state.lock = enabled
        if enabled:
            for tab in self.tabs:
                for pane in tab.panes:
                    if not self.allows(pane.location):
                        pane.navigate(self.root_uri)
                    pane.back_stack = [e for e in pane.back_stack if self.allows(e[0])]
                    pane.forward_stack = [e for e in pane.forward_stack if self.allows(e[0])]
                if tab.terminal is not None:
                    tab.terminal.sync_lock()
        for tab in self.tabs:
            for pane in tab.panes:
                pane.refilter()
                pane.emit("location-changed")
            if tab.terminal is not None:
                tab.terminal.sync_lock()
        self.emit("changed")

    # --- session ------------------------------------------------------------------

    def snapshot(self):
        if self.started:
            self.state.tabs = [tab.state() for tab in self.tabs]
            page = self.tab_view.get_selected_page()
            self.state.active_tab = self.tab_view.get_page_position(page) if page is not None else 0
        return self.state

    def tab_count(self):
        return self.tab_view.get_n_pages() if self.started else len(self.state.tabs)

    def running_process_count(self):
        return sum(1 for tab in self.tabs if tab.running_process())

    def close_all(self):
        for tab in self.tabs:
            tab.close()
