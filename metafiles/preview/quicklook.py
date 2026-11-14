"""Quick preview (Space), like GNOME Sushi: arrows move through the folder, Enter opens."""

from gi.repository import Adw, Gdk, Gio, Gtk

from metafiles.fileview import item_file, item_mime
from metafiles.i18n import _
from metafiles.preview.widget import PreviewWidget


class QuickLook(Adw.Dialog):
    def __init__(self, window, pane):
        super().__init__(content_width=900, content_height=680)
        self.window = window
        self.pane = pane
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.open_button = Gtk.Button()
        self.open_button.add_css_class("suggested-action")
        self.open_button.connect("clicked", lambda *_: self._open())
        header.pack_start(self.open_button)
        toolbar.add_top_bar(header)
        self.preview = PreviewWidget(window.app, compact=False)
        toolbar.set_content(self.preview)
        self.set_child(toolbar)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._handler = pane.connect("selection-changed", lambda *_: self._update())
        self.connect("closed", lambda *_: self._closed())
        self._update(immediate=True)

    def _update(self, immediate=False):
        infos = self.pane.selected_infos()
        if not infos:
            return
        info = infos[0]
        self.set_title(info.get_display_name())
        app = Gio.AppInfo.get_default_for_type(item_mime(info), False)
        self.open_button.set_label(_("Open With {app}").format(app=app.get_display_name()) if app else _("Open"))
        self.preview.show_infos([info], self.pane, immediate=immediate)

    def _move(self, step):
        positions = self.pane.selected_positions()
        if not positions:
            return
        n = self.pane.filter_model.get_n_items()
        target = max(0, min(n - 1, positions[0] + step))
        self.pane.selection.select_item(target, True)
        self.pane.view.scroll_to(target, select=False)

    def _on_key(self, _ctl, keyval, _code, state):
        if keyval in (Gdk.KEY_space, Gdk.KEY_Escape):
            self.close()
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down):
            self._move(+1)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._open()
            return True
        return False

    def _open(self):
        infos = self.pane.selected_infos()
        self.close()
        if infos:
            self.window.activate_items(self.pane, infos)

    def _closed(self):
        self.pane.disconnect(self._handler)
        self.preview.cancel()
        self.pane.focus_view()

    def current_file(self):
        infos = self.pane.selected_infos()
        return item_file(infos[0]) if infos else None
