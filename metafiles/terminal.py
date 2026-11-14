"""Terminal panel (F4): the user's shell in the tab's folder, synchronized with the file view."""

import hashlib
import os
import pwd

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from metafiles import features
from metafiles.i18n import _

SHELL_RC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shell", "metafiles.bashrc")
SEQ_SYNC_CD = b"\x1b[24;6~"
SEQ_RUN = b"\x1b[24;7~"

# Window shortcuts the panel lets through (they do not collide with shell input).
PASS_THROUGH = {"toggle-terminal", "toggle-focus", "prev-tab", "next-tab", "tab-move-left", "tab-move-right",
                "prev-project", "next-project", "switch-to-loose", "toggle-projects", "toggle-places",
                "toggle-split", "switch-pane", *[f"switch-to-project-{i}" for i in range(1, 10)]}


def terminal_available():
    return features.has("terminal")


def user_shell():
    shell = os.environ.get("SHELL")
    if not shell:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            shell = None
    return shell or "/bin/sh"


def shell_argv(shell):
    if os.path.basename(shell) == "bash":
        return [shell, "--rcfile", SHELL_RC, "-i"]
    return [shell]


def _rgba(spec):
    if not spec:
        return None
    color = Gdk.RGBA()
    return color if color.parse(spec) else None


class TerminalPanel(Gtk.Box):
    __gsignals__ = {
        "directory-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, project, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        import gi

        gi.require_version("Vte", "3.91")
        from gi.repository import Vte

        self.Vte = Vte
        self.app = app
        self.project = project
        self.pid = None
        self.sync_enabled = True
        self.cwd = pane.path() or project.root
        self.is_bash = os.path.basename(user_shell()) == "bash"
        self.add_css_class("mf-terminal-panel")
        self.set_size_request(-1, 100)

        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=4, margin_top=2, margin_bottom=2)
        title = Gtk.Label(label=_("Terminal"), xalign=0, hexpand=True)
        title.add_css_class("caption-heading")
        self.sync_button = Gtk.ToggleButton(icon_name="emblem-synchronizing-symbolic", active=True,
                                            tooltip_text=_("Follow the folder shown in the view"))
        self.sync_button.add_css_class("flat")
        self.sync_button.connect("toggled", lambda b: setattr(self, "sync_enabled", b.get_active()))
        self.sync_button.set_sensitive(self.is_bash)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Hide terminal (F4)"))
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.activate_action("win.toggle-terminal", None))
        bar.append(title)
        bar.append(self.sync_button)
        bar.append(close)
        self.append(bar)

        self.term = Vte.Terminal(hexpand=True, vexpand=True)
        self.term.set_mouse_autohide(True)
        self.term.set_scroll_unit_is_pixels(True)
        self.term.set_enable_fallback_scrolling(True)
        self.term.connect("child-exited", self._on_child_exited)
        self.term.connect("termprop-changed", self._on_termprop_changed)
        scroller = Gtk.ScrolledWindow(child=self.term, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.append(scroller)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.term.add_controller(keys)

        from metafiles.gnome_terminal import GnomeProfile, terminal_keys

        self._profile_source = GnomeProfile(self._apply_profile)
        self._keys = terminal_keys()
        self._apply_profile()

        runtime = os.path.join(GLib.get_user_runtime_dir(), "metafiles")
        os.makedirs(runtime, mode=0o700, exist_ok=True)
        ident = hashlib.sha1(f"{project.root}-{id(self)}".encode()).hexdigest()[:16]
        self.sync_file = os.path.join(runtime, f"sync-{ident}")
        self.confine_file = os.path.join(runtime, "confine-" + hashlib.sha1(project.root.encode()).hexdigest()[:16])
        self.sync_lock()
        self._spawn()

    # --- process -------------------------------------------------------------

    def _spawn(self):
        Vte = self.Vte
        env = [
            f"METAFILES_ROOT={'' if self.project.loose else self.project.root}",
            f"METAFILES_CONFINE_FILE={'' if self.project.loose else self.confine_file}",
            f"METAFILES_CONFINE={1 if self.project.lock else 0}",
            f"METAFILES_SYNC_FILE={self.sync_file}",
            "COLORTERM=truecolor",
            "TERM_PROGRAM=MetaFiles",
        ]
        self.term.spawn_async(Vte.PtyFlags.DEFAULT, self.cwd, shell_argv(user_shell()), env,
                              GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, self._on_spawned, None)

    def _on_spawned(self, _term, pid, error, *_):
        if error is not None:
            self.term.feed(f"\r\nmetafiles: failed to start shell: {error.message}\r\n".encode())
            return
        self.pid = pid

    def _on_child_exited(self, *_):
        self.pid = None
        self._cleanup()
        self.emit("exited")

    def shell_idle(self):
        """True when the shell itself (not a program it started) owns the terminal."""
        pty = self.term.get_pty()
        if self.pid is None or pty is None:
            return False
        try:
            return os.tcgetpgrp(pty.get_fd()) == self.pid
        except OSError:
            return False

    def has_running_process(self):
        pty = self.term.get_pty()
        if self.pid is None or pty is None:
            return False
        try:
            return os.tcgetpgrp(pty.get_fd()) != self.pid
        except OSError:
            return False

    def terminate(self):
        if self.pid is not None:
            try:
                os.kill(self.pid, 1)
            except ProcessLookupError:
                pass
        self._cleanup()

    def _cleanup(self):
        try:
            os.unlink(self.sync_file)
        except OSError:
            pass

    def sync_lock(self):
        """Mirror the project's lock into the flag file the running shell checks on every cd."""
        if self.project.loose:
            return
        try:
            if self.project.lock:
                open(self.confine_file, "a").close()
            elif os.path.exists(self.confine_file):
                os.unlink(self.confine_file)
        except OSError as e:
            print(f"metafiles: cannot update cd confinement flag: {e}")

    # --- synchronization ---------------------------------------------------------

    def follow_view(self, pane):
        """The view moved: cd the shell there if it is idle at the prompt."""
        path = pane.path()
        if not self.sync_enabled or not self.is_bash or path is None or path == self.cwd:
            return
        if not self.shell_idle():
            return
        self._write_sync(path)
        self.term.feed_child(SEQ_SYNC_CD)

    def run_command(self, command):
        """Type and run a command at the prompt (custom actions marked for the terminal)."""
        if self.is_bash and self.shell_idle():
            self._write_sync(command)
            GLib.timeout_add(150, lambda: self.term.feed_child(SEQ_RUN) and False)
        else:
            self.app.toast(_("The terminal is busy; the action was not started."))

    def _write_sync(self, text):
        tmp = self.sync_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.sync_file)

    def _on_termprop_changed(self, _term, name):
        if name != self.Vte.TERMPROP_CURRENT_DIRECTORY_URI:
            return
        uri = self.term.ref_termprop_uri(self.Vte.TERMPROP_CURRENT_DIRECTORY_URI)
        path = uri.get_path() if uri is not None else None
        if path and path != self.cwd:
            self.cwd = path
            if self.sync_enabled:
                self.emit("directory-changed", Gio.File.new_for_path(path).get_uri())

    # --- appearance and keys ---------------------------------------------------------

    def _apply_profile(self):
        profile = self._profile_source.read()
        t = self.term
        t.set_font(Pango.FontDescription.from_string(profile.font))
        t.set_cell_height_scale(profile.cell_height_scale)
        t.set_cell_width_scale(profile.cell_width_scale)
        if not profile.use_theme_colors:
            fg = _rgba(profile.foreground) or _rgba("#d0d0d0")
            bg = _rgba(profile.background) or _rgba("#000000")
            palette = [c for c in (_rgba(p) for p in profile.palette) if c is not None]
            if len(palette) not in (0, 8, 16, 232, 256):
                palette = []
            t.set_colors(fg, bg, palette or None)
        t.set_bold_is_bright(profile.bold_is_bright)
        t.set_audible_bell(profile.audible_bell)
        t.set_scrollback_lines(profile.scrollback_lines)

    def _on_key(self, _ctl, keyval, _code, state):
        state &= Gtk.accelerator_get_default_mod_mask()
        name = Gtk.accelerator_name(Gdk.keyval_to_lower(keyval), state)
        for key, accel in self._keys.items():
            ok, k, m = Gtk.accelerator_parse(accel)
            if ok and Gtk.accelerator_name(k, m) == name:
                if key == "copy":
                    self.term.copy_clipboard_format(self.Vte.Format.TEXT)
                else:
                    self.term.paste_clipboard()
                return True
        app = self.app
        for action in PASS_THROUGH:
            if name in app.get_accels_for_action(f"win.{action}"):
                self.activate_action(f"win.{action}", None)
                return True
        return False

    def focus(self):
        self.term.grab_focus()

    def has_focus_inside(self):
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        return focus is not None and (focus is self.term or focus.is_ancestor(self))
