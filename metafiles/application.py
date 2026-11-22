"""Adw.Application: single instance, shared services, shortcuts and session handling."""

import os
import sys

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from metafiles import APP_ID, APP_NAME
from metafiles.actions import parse_actions
from metafiles.bookmarks import Bookmarks
from metafiles.clipboard import Clipboard
from metafiles.git import GitManager
from metafiles.i18n import _
from metafiles.nautilus_settings import NautilusSettings
from metafiles.operations import FileOperations
from metafiles.remote import Watchdog
from metafiles.shortcuts import accel_map
from metafiles.starred import StarredStore
from metafiles.storage import config_path, load_config, load_session, save_session
from metafiles.thumbnails import Thumbnailer


def normalize_accel(accel):
    """Return a canonical accelerator string, or None if GTK cannot parse it."""
    ok, key, mods = Gtk.accelerator_parse(accel)
    if not ok or key == 0:
        return None
    return Gtk.accelerator_name(key, mods)


class OperationsUI:
    """Bridges file operations to the active window's dialogs and toasts."""

    def __init__(self, app):
        self.app = app

    @property
    def window(self):
        return self.app.window

    def error(self, title, detail=""):
        if self.window is not None:
            self.window.show_error(title, detail)

    def confirm_async(self, title, body, accept, callback):
        if self.window is not None:
            self.window.confirm_async(title, body, accept, callback)
        else:
            callback(False)

    def ask_conflict(self, src, dst, done):
        from metafiles.dialogs import conflict

        if self.window is None:
            done(("skip", "", False))
            return
        conflict(self.window, src, dst, done)

    def file_moved(self, old_uri, new_uri):
        self.app.starred.moved(old_uri, new_uri)


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)
        self.window = None
        self.session = None
        self._show_hidden_override = None

    # --- services ---------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(root, "data", "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.session = load_session()
        self.config = load_config()
        self.nautilus = NautilusSettings()
        self.nautilus.connect("changed", self._on_nautilus_changed)
        self.thumbnailer = Thumbnailer()
        self.clipboard = Clipboard(display)
        self.starred = StarredStore()
        self.bookmarks = Bookmarks()
        self.git = GitManager()
        self.git_columns = True
        self.ops = FileOperations(OperationsUI(self),
                                  use_nautilus=os.environ.get("METAFILES_NO_NAUTILUS") != "1")
        self._load_actions()
        self._watch_config()
        self.apply_accels()
        self.watchdog = Watchdog(self._on_watchdog_recovered)
        if os.environ.get("METAFILES_NO_WATCHDOG") != "1":
            self.watchdog.start()

    def _on_watchdog_recovered(self, actions):
        if actions:
            self.toast(_("A server stopped responding; its connection was closed"))

    def _load_actions(self):
        self.custom_actions, errors = parse_actions(self.config.get("actions", []))
        self.action_errors = errors
        for problem in errors:
            print(f"metafiles: {problem}", file=sys.stderr)

    def _watch_config(self):
        try:
            self._config_monitor = Gio.File.new_for_path(config_path()).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._config_monitor.connect("changed", self._on_config_changed)
        except GLib.Error:
            self._config_monitor = None

    def _on_config_changed(self, _monitor, _file, _other, event):
        if event not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            return
        self.config = load_config()
        self._load_actions()
        self.apply_accels()
        if self.window is not None:
            if self.action_errors:
                self.window.toast(self.action_errors[0])
            self.window.refresh_actions()

    def do_activate(self):
        if self.window is None:
            from metafiles.window import MainWindow

            self.window = MainWindow(self, self.session)
            self.ops.set_window(self.window)
            self.window.connect("destroy", self._on_window_destroy)
        self.window.present()

    def do_open(self, files, _n_files, _hint):
        self.do_activate()
        for f in files:
            path = f.get_path()
            if path and os.path.isdir(path):
                self.window.add_project(path)
            elif path and os.path.exists(path):
                self.window.add_project(os.path.dirname(path), select=[f.get_uri()])
            elif path:
                print(f"metafiles: not a directory: {path}", file=sys.stderr)

    def _on_window_destroy(self, *_):
        self.window = None
        self.watchdog.stop()
        self.ops.shutdown()
        self.git.shutdown()
        self.quit()

    # --- shared state -------------------------------------------------------

    @property
    def show_hidden(self):
        if self.session.show_hidden is not None:
            return self.session.show_hidden
        return self.nautilus.show_hidden

    def set_show_hidden(self, value):
        self.session.show_hidden = value
        if self.window is not None:
            self.window.refilter_all()

    def _on_nautilus_changed(self, _settings, group, key):
        if key == "show-hidden-files" and self.session.show_hidden is None and self.window is not None:
            self.window.refilter_all()

    def window_for(self, widget):
        root = widget.get_root() if widget is not None else None
        return root if root is not None and hasattr(root, "activate_items") else self.window

    def toast(self, text, button=None, callback=None):
        if self.window is not None:
            self.window.toast(text, button, callback)

    # --- shortcuts ------------------------------------------------------------

    def apply_accels(self):
        mapping = accel_map(self.config["shortcuts"])
        for action, accels in mapping.items():
            valid = []
            for accel in accels:
                normalized = normalize_accel(accel)
                if normalized and normalized not in valid:
                    valid.append(normalized)
            self.set_accels_for_action(f"win.{action}", valid)
        for action in self.custom_actions:
            if action.shortcut and normalize_accel(action.shortcut):
                self.set_accels_for_action(f"win.custom-{action.id}", [normalize_accel(action.shortcut)])

    def accel_label(self, action):
        accels = self.get_accels_for_action(f"win.{action}")
        if not accels:
            return None
        ok, key, mods = Gtk.accelerator_parse(accels[0])
        return Gtk.accelerator_get_label(key, mods) if ok else None

    # --- session ----------------------------------------------------------------

    def save_session(self):
        if self.window is not None:
            self.window.snapshot()
        try:
            save_session(self.session)
        except OSError as e:
            print(f"metafiles: cannot save session: {e}", file=sys.stderr)


def main(argv):
    return Application().run(argv)
