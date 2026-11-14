"""Dialogs in the GNOME/Nautilus style (libadwaita), all asynchronous."""

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from metafiles import fileutil
from metafiles.fileutil import format_date, format_size, split_extension, validate_filename
from metafiles.i18n import _


def confirm(parent, title, body, accept_label, callback, destructive=True):
    dialog = Adw.AlertDialog(heading=title, body=body)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("accept", accept_label)
    dialog.set_response_appearance("accept", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel" if destructive else "accept")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: callback(response == "accept"))
    dialog.present(parent)
    return dialog


def error(parent, title, detail=""):
    dialog = Adw.AlertDialog(heading=title, body=detail or "")
    dialog.add_response("close", _("_Close"))
    dialog.present(parent)


def ask_text(parent, title, initial, accept_label, callback, validate=None, select_stem=False, body=None):
    """Entry dialog; validate(text) returns an error message or None. callback(text or None)."""
    dialog = Adw.AlertDialog(heading=title, body=body or "")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    entry = Gtk.Entry(text=initial, activates_default=True, width_chars=32)
    message = Gtk.Label(xalign=0, wrap=True)
    message.add_css_class("caption")
    message.add_css_class("error")
    message.set_visible(False)
    box.append(entry)
    box.append(message)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("accept", accept_label)
    dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("accept")
    dialog.set_close_response("cancel")

    def update(*_):
        text = entry.get_text()
        problem = validate(text) if validate else None
        message.set_text(problem or "")
        message.set_visible(bool(problem))
        dialog.set_response_enabled("accept", bool(text.strip()) and not problem)

    entry.connect("changed", update)
    update()
    dialog.connect("response", lambda _d, r: callback(entry.get_text() if r == "accept" else None))
    dialog.present(parent)

    def focus():
        entry.grab_focus()
        if select_stem:
            stem, _ext = split_extension(initial)
            entry.select_region(0, len(stem))
        else:
            entry.select_region(0, -1)
        return False

    GLib.idle_add(focus)
    return dialog


def new_item(parent, folder_path, is_folder, callback, initial=None):
    existing = set(os.listdir(folder_path)) if folder_path and os.path.isdir(folder_path) else set()
    if initial is None:
        initial = _("Untitled Folder") if is_folder else _("New File.txt")
    initial = fileutil.unique_name(initial, existing)
    ask_text(parent, _("New Folder") if is_folder else _("New File"), initial, _("_Create"),
             lambda text: callback(text.strip() if text else None),
             validate=lambda text: validate_filename(text, existing, is_folder) if text != "" else None,
             select_stem=not is_folder)


def rename_popover(anchor, x, y, name, is_folder, existing, callback):
    """Nautilus renames in a popover pointing at the item; the stem is preselected."""
    popover = Gtk.Popover()
    popover.set_parent(anchor)
    if x is not None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=6, margin_end=6,
                  margin_top=6, margin_bottom=6)
    title = Gtk.Label(label=_("Rename Folder") if is_folder else _("Rename File"), xalign=0)
    title.add_css_class("heading")
    row = Gtk.Box(spacing=6)
    entry = Gtk.Entry(text=name, width_chars=30, hexpand=True)
    button = Gtk.Button(label=_("_Rename"), use_underline=True)
    button.add_css_class("suggested-action")
    message = Gtk.Label(xalign=0, wrap=True, max_width_chars=40)
    message.add_css_class("caption")
    message.add_css_class("error")
    message.set_visible(False)
    row.append(entry)
    row.append(button)
    box.append(title)
    box.append(row)
    box.append(message)
    popover.set_child(box)
    others = set(existing) - {name}

    def problem():
        text = entry.get_text()
        if text == name:
            return None
        return validate_filename(text, others, is_folder)

    def update(*_):
        p = problem()
        message.set_text(p or "")
        message.set_visible(bool(p))
        button.set_sensitive(bool(entry.get_text().strip()) and not p)

    def apply(*_):
        if not button.get_sensitive():
            return
        text = entry.get_text()
        popover.popdown()
        if text != name:
            callback(text)

    entry.connect("changed", update)
    entry.connect("activate", apply)
    button.connect("clicked", apply)
    popover.connect("closed", lambda p: GLib.idle_add(lambda: p.unparent() and False))
    popover.popup()

    def focus():
        entry.grab_focus()
        stem, _ext = split_extension(name)
        entry.select_region(0, len(stem) if not is_folder else -1)
        return False

    GLib.idle_add(focus)
    update()
    return popover


def conflict(parent, src, dst, callback):
    """GIO-fallback conflict dialog: callback((action, new_name, apply_all))."""
    try:
        sinfo = src.query_info("standard::size,time::modified,standard::type", Gio.FileQueryInfoFlags.NONE, None)
        dinfo = dst.query_info("standard::size,time::modified,standard::type", Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        sinfo = dinfo = None
    both_dirs = sinfo is not None and sinfo.get_file_type() == dinfo.get_file_type() == Gio.FileType.DIRECTORY
    name = dst.get_basename()
    heading = (_("Merge folder “{name}”?") if both_dirs else _("Replace file “{name}”?")).format(name=name)
    body = ""
    if sinfo is not None:
        body = _("Original: {size}, last modified {date}\nReplace with: {nsize}, last modified {ndate}").format(
            size=format_size(dinfo.get_size()), date=format_date(dinfo.get_attribute_uint64("time::modified")),
            nsize=format_size(sinfo.get_size()), ndate=format_date(sinfo.get_attribute_uint64("time::modified")))
    dialog = Adw.AlertDialog(heading=heading, body=body)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    existing = set(os.listdir(dst.get_parent().get_path())) if dst.get_parent() and dst.get_parent().get_path() else set()
    entry = Gtk.Entry(text=fileutil.copy_name(name, existing))
    expander = Gtk.Expander(label=_("Select a new name for the destination"), child=entry)
    check = Gtk.CheckButton(label=_("Apply this action to all files and folders"))
    box.append(expander)
    box.append(check)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("skip", _("_Skip"))
    dialog.add_response("rename", _("Re_name"))
    dialog.add_response("replace", _("_Merge") if both_dirs else _("_Replace"))
    dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_close_response("cancel")
    dialog.set_response_enabled("rename", False)
    expander.connect("notify::expanded", lambda e, _p: dialog.set_response_enabled("rename", e.get_expanded()))

    def respond(_d, response):
        action = {"replace": "merge" if both_dirs else "replace"}.get(response, response)
        callback((action, entry.get_text(), check.get_active()))

    dialog.connect("response", respond)
    dialog.present(parent)


def select_matching(parent, callback):
    ask_text(parent, _("Select Items Matching"), "", _("_Select"), lambda text: text and callback(text),
             body=_("Examples: *.png, file??.txt, pict*"))


def basic_properties(parent, infos_and_files):
    """Fallback properties dialog (used when Nautilus' properties window is unavailable)."""
    dialog = Adw.Dialog(title=_("Properties"), content_width=420)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()
    for info, gf in infos_and_files[:5]:
        group = Adw.PreferencesGroup(title=GLib.markup_escape_text(info.get_display_name()))
        rows = [
            (_("Type"), Gio.content_type_get_description(info.get_content_type() or "") or ""),
            (_("Size"), format_size(info.get_size())),
            (_("Location"), fileutil.short_path(gf.get_parent().get_path() or "") if gf.get_parent() else ""),
            (_("Modified"), format_date(info.get_attribute_uint64("time::modified"), "detailed")),
            (_("Accessed"), format_date(info.get_attribute_uint64("time::access"), "detailed")),
            (_("Created"), format_date(info.get_attribute_uint64("time::created"), "detailed")),
            (_("Permissions"), fileutil.format_permissions(info.get_attribute_uint32("unix::mode"),
                                                           info.get_file_type() == Gio.FileType.DIRECTORY)),
            (_("Owner"), info.get_attribute_string("owner::user") or ""),
        ]
        for title, value in rows:
            if value:
                row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value))
                row.add_css_class("property")
                row.set_subtitle_selectable(True)
                group.add(row)
        page.add(group)
    toolbar.set_content(page)
    dialog.set_child(toolbar)
    dialog.present(parent)


def info_dialog(parent, title, text, monospace=False):
    dialog = Adw.Dialog(title=title, content_width=640, content_height=480)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    view = Gtk.TextView(editable=False, monospace=monospace, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                        left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
    view.get_buffer().set_text(text)
    toolbar.set_content(Gtk.ScrolledWindow(child=view, vexpand=True))
    dialog.set_child(toolbar)
    dialog.present(parent)


def ellipsize_label(text):
    label = Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.MIDDLE)
    return label
