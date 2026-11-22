"""Preview providers chosen by content type; heavy work runs in threads."""

import os
import tarfile
import threading
import zipfile

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from metafiles import features
from metafiles.i18n import _, ngettext

TEXT_LIMIT = 1024 * 1024
ARCHIVE_SUFFIXES = (".zip", ".jar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")
TEXT_MIMES = ("application/json", "application/xml", "application/javascript", "application/x-shellscript",
              "application/x-yaml", "application/toml", "application/x-desktop", "application/sql",
              "application/x-php", "application/x-perl", "application/x-ruby", "application/x-python",
              "application/x-ipynb+json", "application/x-trash", "application/x-subrip")


def is_binary(data):
    return b"\0" in data[:8192]


def kind_for(mime, name, is_dir):
    """Which provider handles this item."""
    if is_dir:
        return "folder"
    lower = name.lower()
    if mime == "image/gif":
        return "gif"
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith(("video/", "audio/")):
        return "media"
    if lower.endswith(ARCHIVE_SUFFIXES) or mime in ("application/zip", "application/x-tar",
                                                     "application/x-compressed-tar", "application/x-xz-compressed-tar",
                                                     "application/x-bzip-compressed-tar", "application/java-archive"):
        return "archive"
    if mime.startswith("text/") or mime in TEXT_MIMES or Gio.content_type_is_a(mime, "text/plain"):
        return "text"
    return "other"


def _run(work, deliver, token):
    def thread():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 - shown as a preview error
            result = e
        GLib.idle_add(lambda: (not token.cancelled) and deliver(result) and False)

    threading.Thread(target=thread, daemon=True).start()


class Token:
    def __init__(self):
        self.cancelled = False


def _placeholder(text):
    label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, vexpand=True)
    label.add_css_class("dim-label")
    return label


def image(gf, info, token, done, compact):
    path = gf.get_path()

    def work():
        return Gdk.Texture.new_from_filename(path) if path else Gdk.Texture.new_from_file(gf)

    def deliver(result):
        if isinstance(result, Exception):
            done(_placeholder(_("Cannot display this image")), {})
            return
        picture = Gtk.Picture.new_for_paintable(result)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        picture.set_vexpand(True)
        done(picture, {_("Dimensions"): f"{result.get_width()} × {result.get_height()} px"})

    _run(work, deliver, token)


def gif(gf, info, token, done, compact):
    path = gf.get_path()

    def work():
        return GdkPixbuf.PixbufAnimation.new_from_file(path)

    def deliver(result):
        if isinstance(result, Exception):
            image(gf, info, token, done, compact)
            return
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, vexpand=True)
        iterator = result.get_iter(None)

        def tick():
            if token.cancelled or picture.get_root() is None and getattr(picture, "_shown", False):
                return False
            picture._shown = True
            picture.set_paintable(Gdk.Texture.new_for_pixbuf(iterator.get_pixbuf()))
            iterator.advance(None)
            delay = iterator.get_delay_time()
            if delay > 0 and not result.is_static_image():
                GLib.timeout_add(max(20, delay), tick)
            return False

        tick()
        done(picture, {_("Dimensions"): f"{result.get_width()} × {result.get_height()} px"})

    _run(work, deliver, token)


def text(gf, info, token, done, compact):
    path = gf.get_path()

    def work():
        with open(path, "rb") as f:
            data = f.read(TEXT_LIMIT)
        return data

    def deliver(data):
        if isinstance(data, Exception):
            done(_placeholder(str(data)), {})
            return
        if is_binary(data):
            done(None, {})
            return
        content = data.decode("utf-8", "replace")
        truncated = info.get_size() > TEXT_LIMIT
        lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        view = _source_view(content, gf.get_basename(), info.get_content_type() or "")
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True, hexpand=True)
        extra = {_("Lines"): str(lines) + (" " + _("(first 1 MB)") if truncated else "")}
        done(scroller, extra)

    _run(work, deliver, token)


def _source_view(content, name, mime):
    if features.has("sourceview"):
        import gi

        gi.require_version("GtkSource", "5")
        from gi.repository import Adw, GtkSource

        buffer = GtkSource.Buffer()
        language = GtkSource.LanguageManager.get_default().guess_language(name, mime)
        if language is not None:
            buffer.set_language(language)
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if dark else "Adwaita")
        if scheme is not None:
            buffer.set_style_scheme(scheme)
        buffer.set_text(content)
        view = GtkSource.View(buffer=buffer, editable=False, monospace=True, show_line_numbers=True,
                              wrap_mode=Gtk.WrapMode.NONE, cursor_visible=False)
    else:
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE, cursor_visible=False)
        view.get_buffer().set_text(content)
    view.set_top_margin(8)
    view.set_left_margin(8)
    view.set_bottom_margin(8)
    view.add_css_class("mf-preview-text")
    return view


def pdf(gf, info, token, done, compact, thumbnail=None):
    if not features.has("poppler"):
        other(gf, info, token, done, compact, thumbnail)
        return
    import gi

    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler

    def work():
        document = Poppler.Document.new_from_gfile(gf, None, None)
        return document

    def deliver(document):
        if isinstance(document, Exception):
            other(gf, info, token, done, compact, thumbnail)
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        pages = document.get_n_pages()
        width = 900

        def render(index):
            if token.cancelled or index >= min(pages, 30):
                return False
            import cairo

            page = document.get_page(index)
            pw, ph = page.get_size()
            scale = width / pw
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(pw * scale), int(ph * scale))
            ctx = cairo.Context(surface)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            ctx.scale(scale, scale)
            page.render(ctx)
            surface.flush()
            data = GLib.Bytes.new(bytes(surface.get_data()))
            texture = Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(),
                                            Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, surface.get_stride())
            picture = Gtk.Picture.new_for_paintable(texture)
            picture.set_can_shrink(True)
            picture.add_css_class("mf-pdf-page")
            box.append(picture)
            GLib.idle_add(lambda: render(index + 1) and False)
            return False

        render(0)
        done(Gtk.ScrolledWindow(child=box, vexpand=True), {_("Pages"): str(pages)})

    _run(work, deliver, token)


def media(gf, info, token, done, compact):
    video = Gtk.Video.new_for_file(gf)
    video.set_autoplay(False)
    video.set_vexpand(True)
    extra = {}
    stream = video.get_media_stream()

    def prepared(*_):
        duration = stream.get_duration() if stream is not None else 0
        if duration > 0:
            seconds = duration // 1_000_000
            label.set_text(f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600
                           else f"{seconds // 60}:{seconds % 60:02d}")

    label = Gtk.Label()
    if stream is not None:
        stream.connect("notify::prepared", prepared)
    extra[_("Duration")] = label
    done(video, extra)


def archive(gf, info, token, done, compact):
    path = gf.get_path()

    def work():
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return [(i.filename, i.file_size) for i in z.infolist()[:2000]], len(z.infolist())
        with tarfile.open(path) as t:
            members = []
            total = 0
            for member in t:
                total += 1
                if len(members) < 2000:
                    members.append((member.name + ("/" if member.isdir() else ""), member.size))
            return members, total

    def deliver(result):
        if isinstance(result, Exception):
            done(_placeholder(_("Cannot read the archive")), {})
            return
        members, total = result
        lines = "\n".join(name for name, _size in members)
        view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, left_margin=8, top_margin=8)
        view.get_buffer().set_text(lines)
        done(Gtk.ScrolledWindow(child=view, vexpand=True),
             {_("Entries"): ngettext("{n} entry", "{n} entries", total).format(n=total)})

    _run(work, deliver, token)


def folder(gf, info, token, done, compact, size_callback=None):
    path = gf.get_path()

    def work():
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
        return [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]

    def deliver(entries):
        if isinstance(entries, Exception):
            done(_placeholder(str(entries)), {})
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12, margin_end=12,
                      margin_top=12)
        for name, directory in entries[:40]:
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Image.new_from_icon_name("folder-symbolic" if directory else "text-x-generic-symbolic"))
            row.append(Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
            box.append(row)
        if len(entries) > 40:
            more = Gtk.Label(label=_("…and {n} more").format(n=len(entries) - 40), xalign=0)
            more.add_css_class("dim-label")
            box.append(more)
        size_label = Gtk.Label(xalign=0)
        button = Gtk.Button(label=_("Calculate"), halign=Gtk.Align.START)
        button.add_css_class("flat")
        stack = Gtk.Stack()
        stack.add_named(button, "button")
        stack.add_named(size_label, "label")

        def calculate(*_):
            from metafiles.fileutil import format_size
            from metafiles.fileview import SIZE_CALCULATOR

            stack.set_visible_child_name("label")
            size_label.set_text(_("calculating…"))
            SIZE_CALCULATOR.compute(path, lambda total: size_label.set_text(format_size(total) if total is not None else ""))

        button.connect("clicked", calculate)
        done(Gtk.ScrolledWindow(child=box, vexpand=True),
             {_("Contents"): ngettext("{n} item", "{n} items", len(entries)).format(n=len(entries)),
              _("Total size"): stack})

    _run(work, deliver, token)


def other(gf, info, token, done, compact, thumbnail=None):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon = info.get_icon() or Gio.ThemedIcon.new("text-x-generic")
    paintable = theme.lookup_by_gicon(icon, 128, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags(0))
    picture = Gtk.Picture.new_for_paintable(paintable)
    picture.set_can_shrink(False)
    picture.set_vexpand(True)
    picture.set_valign(Gtk.Align.CENTER)
    done(picture, {})
    if thumbnail is not None:
        thumbnail(lambda texture: texture is not None and picture.set_paintable(texture))
