"""Compress and extract with gnome-autoar, the library Nautilus uses (same formats and behaviour)."""

import os

from gi.repository import Adw, Gio, GLib, Gtk

from metafiles import fileutil
from metafiles.i18n import _
from metafiles.operations import Job

try:
    import gi

    gi.require_version("GnomeAutoar", "0.1")
    from gi.repository import GnomeAutoar
except (ImportError, ValueError):
    GnomeAutoar = None

# (key, label, extension, format, filter, encrypted) in Nautilus' order.
FORMATS = [
    ("zip", _(".zip — Compatible with all operating systems"), ".zip", "ZIP", "NONE", False),
    ("encrypted_zip", _(".zip — Password protected"), ".zip", "ZIP", "NONE", True),
    ("tar.xz", _(".tar.xz — Smaller archives but Linux and Mac only"), ".tar.xz", "GNUTAR", "XZ", False),
    ("7zip", _(".7z — Smaller archives but must be installed on Windows and Mac"), ".7z", "7ZIP", "NONE", False),
]


def default_format():
    from metafiles.nautilus_settings import schema_exists

    if schema_exists("org.gnome.nautilus.compression"):
        value = Gio.Settings.new("org.gnome.nautilus.compression").get_string("default-compression-format")
        if any(f[0] == value for f in FORMATS):
            return value
    return "zip"


def _start_job(window, label, cancellable):
    ops = window.app.ops
    job = Job(ops, label)
    job.cancellable = cancellable
    ops.jobs.append(job)
    ops.emit("progress-changed")
    return job


def _end_job(window, job):
    ops = window.app.ops
    if job in ops.jobs:
        ops.jobs.remove(job)
    ops.emit("progress-changed")


def compress_dialog(window, uris, folder, on_done=None):
    if GnomeAutoar is None:
        window.show_error(_("Compression is unavailable"), _("Install the package gir1.2-gnomeautoar-0.1."))
        return
    files = [Gio.File.new_for_uri(u) for u in uris]
    existing = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    if len(files) == 1:
        base = fileutil.split_extension(files[0].get_basename())[0] if not os.path.isdir(files[0].get_path() or "") \
            else files[0].get_basename()
    else:
        base = os.path.basename(folder) or _("Archive")
    dialog = Adw.AlertDialog(heading=_("Create Archive"))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    entry = Gtk.Entry(text=base, activates_default=True, width_chars=34)
    combo = Gtk.DropDown.new_from_strings([f[1] for f in FORMATS])
    keys = [f[0] for f in FORMATS]
    combo.set_selected(keys.index(default_format()))
    password = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text=_("Password"))
    message = Gtk.Label(xalign=0, wrap=True)
    message.add_css_class("error")
    message.add_css_class("caption")
    for widget in (entry, combo, password, message):
        box.append(widget)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("create", _("C_reate"))
    dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("create")
    dialog.set_close_response("cancel")

    def update(*_):
        fmt = FORMATS[combo.get_selected()]
        password.set_visible(fmt[5])
        name = entry.get_text().strip() + fmt[2]
        problem = fileutil.validate_filename(name, existing) if entry.get_text().strip() else None
        if fmt[5] and not password.get_text():
            problem = problem or ""
        message.set_text(problem or "")
        message.set_visible(bool(problem))
        dialog.set_response_enabled("create", bool(entry.get_text().strip()) and problem is None)

    for widget, signal in ((entry, "changed"), (combo, "notify::selected"), (password, "changed")):
        widget.connect(signal, update)
    update()

    def respond(_d, response):
        if response != "create":
            return
        fmt = FORMATS[combo.get_selected()]
        output = Gio.File.new_for_path(os.path.join(folder, entry.get_text().strip() + fmt[2]))
        compress(window, files, output, fmt, password.get_text() if fmt[5] else None, on_done)

    dialog.connect("response", respond)
    dialog.present(window)


def compress(window, files, output, fmt, passphrase=None, on_done=None):
    compressor = GnomeAutoar.Compressor.new(files, output, getattr(GnomeAutoar.Format, fmt[3]),
                                            getattr(GnomeAutoar.Filter, fmt[4]), False)
    compressor.set_output_is_dest(True)
    if passphrase and hasattr(compressor, "set_passphrase"):
        compressor.set_passphrase(passphrase)
    cancellable = Gio.Cancellable()
    job = _start_job(window, _("Compressing"), cancellable)

    def completed(*_):
        _end_job(window, job)
        uri = output.get_uri()
        window.app.ops.push_undo(_("Compress"), lambda cb: _trash(uri, cb))
        window.app.ops.emit("changed", [output.get_parent().get_uri()])
        window.toast(_("“{name}” created").format(name=output.get_basename()))
        if on_done:
            on_done(uri)

    def error(_c, err):
        _end_job(window, job)
        window.show_error(_("Error compressing files"), err.message)

    compressor.connect("completed", completed)
    compressor.connect("error", error)
    compressor.connect("cancelled", lambda *_: _end_job(window, job))
    compressor.connect("progress", lambda _c, size, files_done: _progress(window, job, compressor))
    compressor.start_async(cancellable)


def extract(window, uris, destination):
    if GnomeAutoar is None:
        window.show_error(_("Extraction is unavailable"), _("Install the package gir1.2-gnomeautoar-0.1."))
        return
    for uri in uris:
        _extract_one(window, Gio.File.new_for_uri(uri), Gio.File.new_for_path(destination))


def _extract_one(window, archive, destination):
    extractor = GnomeAutoar.Extractor.new(archive, destination)
    # Let autoar decide the output like Nautilus: one top-level item goes straight in,
    # several get a folder named after the archive.
    extractor.set_output_is_dest(False)
    cancellable = Gio.Cancellable()
    job = _start_job(window, _("Extracting “{name}”").format(name=archive.get_basename()), cancellable)
    created = {}

    def decide(_e, dest, files):
        created["dest"] = dest
        return None

    def completed(*_):
        _end_job(window, job)
        dest = created.get("dest")
        if dest is not None:
            uri = dest.get_uri()
            window.app.ops.push_undo(_("Extract"), lambda cb: _trash(uri, cb))
        window.app.ops.emit("changed", [destination.get_uri()])
        pane = window.active_pane
        if dest is not None and pane is not None:
            GLib.timeout_add(300, lambda: pane.select_uris([dest.get_uri()]) and False)

    def error(_e, err):
        _end_job(window, job)
        window.show_error(_("Error extracting “{name}”").format(name=archive.get_basename()), err.message)

    def conflict(_e, file, *_):
        # Keep both, like Nautilus' default when not asked: autoar picks a new name.
        return GnomeAutoar.ConflictAction.KEEP if hasattr(GnomeAutoar, "ConflictAction") else 0

    extractor.connect("decide-destination", decide)
    extractor.connect("completed", completed)
    extractor.connect("error", error)
    extractor.connect("cancelled", lambda *_: _end_job(window, job))
    extractor.connect("progress", lambda *_: _progress(window, job, extractor))
    try:
        extractor.connect("conflict", conflict)
    except TypeError:
        pass
    extractor.start_async(cancellable)


def _progress(window, job, worker):
    try:
        done = worker.get_completed_size()
        total = worker.get_total_size() if hasattr(worker, "get_total_size") else worker.get_size()
    except Exception:  # noqa: BLE001 - progress is best effort
        return
    job.current, job.total = done, total
    window.app.ops.emit("progress-changed")


def _trash(uri, callback):
    try:
        Gio.File.new_for_uri(uri).trash(None)
    except GLib.Error:
        pass
    callback()
