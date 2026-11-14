"""Preview panel at the right edge of a tab, following the active pane's selection."""

from gi.repository import Gtk

from metafiles.preview.widget import PreviewWidget


class PreviewPanel(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("mf-preview")
        self.set_size_request(220, -1)
        self.widget = PreviewWidget(app)
        self.append(self.widget)
        self.pane = None
        self._handler = None

    def follow(self, pane):
        self.detach()
        self.pane = pane
        self._handler = pane.connect("selection-changed", lambda p: self.widget.show_infos(p.selected_infos(), p))
        self.widget.show_infos(pane.selected_infos(), pane, immediate=True)

    def detach(self):
        if self.pane is not None and self._handler is not None:
            self.pane.disconnect(self._handler)
        self.pane = None
        self._handler = None
        self.widget.cancel()
