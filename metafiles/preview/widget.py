"""PreviewWidget: shows one item (or a selection summary) with an information list below."""

from gi.repository import GLib, Gtk, Pango

from metafiles import fileutil
from metafiles.fileutil import format_date, format_size
from metafiles.fileview import GIT_LABELS, is_dir, item_file, item_mime, type_description
from metafiles.i18n import _, ngettext
from metafiles.preview import providers

DEBOUNCE_MS = 120


class PreviewWidget(Gtk.Box):
    def __init__(self, app, compact=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True)
        self.app = app
        self.compact = compact
        self.token = providers.Token()
        self._timeout = 0
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, margin_start=12,
                               margin_end=12, margin_top=12)
        self.title.add_css_class("heading")
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.info = Gtk.Grid(column_spacing=12, row_spacing=4, margin_start=12, margin_end=12, margin_bottom=12)
        self.append(self.title)
        self.append(self.content)
        self.append(self.info)
        self.show_nothing()

    # --- public --------------------------------------------------------------

    def show_infos(self, infos, pane=None, immediate=False):
        """Debounced: fast keyboard navigation only renders the item it stops on."""
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        self.token.cancelled = True
        self.token = providers.Token()
        if immediate:
            self._render(list(infos), pane, self.token)
        else:
            token = self.token
            self._timeout = GLib.timeout_add(DEBOUNCE_MS, lambda: self._render(list(infos), pane, token))

    def show_nothing(self):
        self._set_content(providers._placeholder(_("Select a file to preview it")))
        self.title.set_text("")
        self._set_info({})

    # --- rendering -------------------------------------------------------------

    def _render(self, infos, pane, token):
        self._timeout = 0
        if token.cancelled:
            return False
        if not infos:
            self.show_nothing()
            return False
        if len(infos) > 1:
            size = sum(i.get_size() for i in infos if not is_dir(i))
            self.title.set_text(ngettext("{n} item selected", "{n} items selected", len(infos)).format(n=len(infos)))
            self._set_content(providers._placeholder(format_size(size) if size else ""))
            self._set_info({_("Total size"): format_size(size)})
            return False
        info = infos[0]
        gf = item_file(info)
        self.title.set_text(info.get_display_name())
        mime = item_mime(info)
        kind = providers.kind_for(mime, info.get_name(), is_dir(info))
        base = self._base_info(info, pane)
        self._set_info(base)
        self._set_content(providers._placeholder(_("Loading…")))

        def done(widget, extra):
            if token.cancelled:
                return
            if widget is None:
                providers.other(gf, info, token, done, self.compact, self._thumbnail(gf, info))
                return
            self._set_content(widget)
            merged = dict(base)
            merged.update(extra)
            self._set_info(merged)

        if gf.get_path() is None and kind not in ("other", "media"):
            kind = "other"
        if kind != "folder" and info.get_size() > 512 * 1024 * 1024 and kind not in ("media",):
            kind = "other"
        provider = getattr(providers, kind)
        if kind in ("other", "pdf"):
            provider(gf, info, token, done, self.compact, self._thumbnail(gf, info))
        else:
            provider(gf, info, token, done, self.compact)
        return False

    def _thumbnail(self, gf, info):
        def request(callback):
            self.app.thumbnailer.request(gf.get_uri(), item_mime(info), info.get_attribute_uint64("time::modified"),
                                         256, callback)
        return request

    def _base_info(self, info, pane):
        gf = item_file(info)
        result = {_("Type"): type_description(info)}
        if not is_dir(info):
            result[_("Size")] = format_size(info.get_size())
        result[_("Modified")] = format_date(info.get_attribute_uint64("time::modified"), "detailed")
        if info.has_attribute("unix::mode"):
            result[_("Permissions")] = fileutil.format_permissions(info.get_attribute_uint32("unix::mode"), is_dir(info))
        parent = gf.get_parent()
        if parent is not None and parent.get_path():
            result[_("Location")] = fileutil.short_path(parent.get_path())
        if pane is not None:
            status = pane.git_status(info)
            if status:
                result[_("Git")] = GIT_LABELS.get(status, status)
        return result

    def _set_content(self, widget):
        child = self.content.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.content.remove(child)
            child = nxt
        self.content.append(widget)

    def _set_info(self, rows):
        child = self.info.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.info.remove(child)
            child = nxt
        for row, (key, value) in enumerate(rows.items()):
            name = Gtk.Label(label=key, xalign=1, valign=Gtk.Align.START)
            name.add_css_class("dim-label")
            self.info.attach(name, 0, row, 1, 1)
            if isinstance(value, Gtk.Widget):
                widget = value
                widget.set_halign(Gtk.Align.START)
            else:
                widget = Gtk.Label(label=str(value), xalign=0, selectable=True, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.MIDDLE, width_chars=8, max_width_chars=40,
                                   tooltip_text=str(value))
            self.info.attach(widget, 1, row, 1, 1)

    def cancel(self):
        self.token.cancelled = True
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0


