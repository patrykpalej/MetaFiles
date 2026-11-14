"""Batch rename dialog with a live preview (find/replace, regex, numbering, case, extension)."""

import os
from collections import defaultdict

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from metafiles import batchrename
from metafiles.batchrename import RenameOptions
from metafiles.i18n import _

ERRORS = {
    "empty": _("Empty name"),
    "invalid": _("Invalid name"),
    "duplicate": _("Two items get this name"),
    "exists": _("A file with this name already exists"),
}
CASES = [("none", _("Keep case")), ("lower", _("lowercase")), ("upper", _("UPPERCASE")), ("title", _("Title Case"))]
EXTENSIONS = [("keep", _("Keep extension")), ("lower", _("Lowercase extension")),
              ("upper", _("Uppercase extension")), ("remove", _("Remove extension")), ("custom", _("New extension…"))]


class BatchRenameDialog(Adw.Dialog):
    def __init__(self, window, pane, files):
        super().__init__(title=_("Rename {n} Items").format(n=len(files)), content_width=760, content_height=620)
        self.window = window
        self.pane = pane
        self.files = files
        self.names = [f.get_basename() for f in files]

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        self.apply_button = Gtk.Button(label=_("_Rename"), use_underline=True)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply())
        header.pack_start(cancel)
        header.pack_end(self.apply_button)
        toolbar.add_top_bar(header)

        form = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_top=6)
        self.find = Adw.EntryRow(title=_("Find"))
        self.replace = Adw.EntryRow(title=_("Replace with ($1, \\1: regex groups)"))
        self.regex = Adw.SwitchRow(title=_("Regular expression"))
        self.case_sensitive = Adw.SwitchRow(title=_("Match case"), active=True)
        self.prefix = Adw.EntryRow(title=_("Add before name"))
        self.suffix = Adw.EntryRow(title=_("Add after name"))
        self.numbering = Adw.ExpanderRow(title=_("Numbering"), show_enable_switch=True, enable_expansion=False)
        self.number_format = Adw.EntryRow(title=_("Pattern ({name}, {n})"), text="{name} {n}")
        self.start = Adw.SpinRow.new_with_range(0, 100000, 1)
        self.start.set_title(_("Start at"))
        self.start.set_value(1)
        self.padding = Adw.SpinRow.new_with_range(1, 8, 1)
        self.padding.set_title(_("Digits"))
        self.padding.set_value(2)
        for row in (self.number_format, self.start, self.padding):
            self.numbering.add_row(row)
        self.case = Adw.ComboRow(title=_("Case"), model=Gtk.StringList.new([c[1] for c in CASES]))
        self.extension = Adw.ComboRow(title=_("Extension"), model=Gtk.StringList.new([e[1] for e in EXTENSIONS]))
        self.new_extension = Adw.EntryRow(title=_("New extension"), visible=False)
        self.include_extension = Adw.SwitchRow(title=_("Apply find and case to the extension too"))
        for row in (self.find, self.replace, self.regex, self.case_sensitive, self.prefix, self.suffix,
                    self.numbering, self.case, self.extension, self.new_extension, self.include_extension):
            form.add(row)

        self.store = Gtk.StringList()
        self.preview = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.preview.add_css_class("boxed-list")
        self.message = Gtk.Label(xalign=0, margin_start=14)
        self.message.add_css_class("error")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_bottom=12)
        content.append(form)
        content.append(self.message)
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12)
        preview_box.append(self.preview)
        content.append(preview_box)
        toolbar.set_content(Gtk.ScrolledWindow(child=content, vexpand=True))
        self.set_child(toolbar)

        for widget in (self.find, self.replace, self.prefix, self.suffix, self.number_format, self.new_extension):
            widget.connect("changed", lambda *_: self._update())
        for widget in (self.regex, self.case_sensitive, self.include_extension):
            widget.connect("notify::active", lambda *_: self._update())
        for widget in (self.start, self.padding):
            widget.connect("notify::value", lambda *_: self._update())
        self.numbering.connect("notify::enable-expansion", lambda *_: self._update())
        self.case.connect("notify::selected", lambda *_: self._update())
        self.extension.connect("notify::selected", lambda *_: self._update())
        self.new_names = list(self.names)
        self._update()
        GLib.idle_add(lambda: self.find.grab_focus() and False)

    def _options(self):
        ext_key = EXTENSIONS[self.extension.get_selected()][0]
        self.new_extension.set_visible(ext_key == "custom")
        extension = self.new_extension.get_text().strip().lstrip(".") if ext_key == "custom" else ext_key
        return RenameOptions(
            find=self.find.get_text(), replace=self.replace.get_text(), regex=self.regex.get_active(),
            case_sensitive=self.case_sensitive.get_active(), prefix=self.prefix.get_text(),
            suffix=self.suffix.get_text(), numbering=self.numbering.get_enable_expansion(),
            number_format=self.number_format.get_text() or "{name} {n}", start=int(self.start.get_value()),
            padding=int(self.padding.get_value()), case=CASES[self.case.get_selected()][0],
            extension=extension or "keep", include_extension=self.include_extension.get_active())

    def _update(self):
        try:
            self.new_names = batchrename.compute_new_names(self.names, self._options())
            error = None
        except ValueError as e:
            self.new_names = list(self.names)
            error = str(e)
        problems = [None] * len(self.names)
        by_dir = defaultdict(list)
        for i, f in enumerate(self.files):
            by_dir[f.get_parent().get_path()].append(i)
        for directory, indexes in by_dir.items():
            existing = set(os.listdir(directory)) if directory and os.path.isdir(directory) else set()
            result = batchrename.validate([self.names[i] for i in indexes], [self.new_names[i] for i in indexes],
                                          existing)
            for i, problem in zip(indexes, result):
                problems[i] = problem
        self._fill_preview(problems)
        changed = any(a != b for a, b in zip(self.names, self.new_names))
        blocking = [p for p in problems if p]
        self.message.set_text(error or (ERRORS[blocking[0]] if blocking else ""))
        self.message.set_visible(bool(error or blocking))
        self.apply_button.set_sensitive(changed and not blocking and error is None)

    def _fill_preview(self, problems):
        while (row := self.preview.get_row_at_index(0)) is not None:
            self.preview.remove(row)
        for old, new, problem in list(zip(self.names, self.new_names, problems))[:300]:
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
            left = Gtk.Label(label=old, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
            right = Gtk.Label(label=new, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
            if new == old:
                right.add_css_class("dim-label")
            row.append(left)
            row.append(arrow)
            row.append(right)
            list_row = Gtk.ListBoxRow(child=row, activatable=False)
            if problem:
                list_row.add_css_class("conflict-row")
                list_row.set_tooltip_text(ERRORS[problem])
            self.preview.append(list_row)

    def _apply(self):
        groups = defaultdict(list)
        for f, old, new in zip(self.files, self.names, self.new_names):
            if old != new:
                groups[f.get_parent().get_path()].append((old, new))
        done = {}
        try:
            for directory, pairs in groups.items():
                steps = batchrename.plan_renames(pairs)
                done[directory] = batchrename.apply_plan(directory, steps)
        except (OSError, ValueError) as e:
            self.window.show_error(_("Renaming failed"), str(e))
            self._undo(done, lambda: None)
            return

        def undo(callback):
            self._undo(done, callback)

        def redo(callback):
            for directory, pairs in done.items():
                batchrename.apply_plan(directory, batchrename.plan_renames(pairs))
            self.window.app.ops.emit("changed", [Gio.File.new_for_path(d).get_uri() for d in done])
            callback()

        self.window.app.ops.push_undo(_("Batch rename"), undo, redo)
        for directory, pairs in done.items():
            for old, new in pairs:
                self.window.app.starred.moved(Gio.File.new_for_path(os.path.join(directory, old)).get_uri(),
                                              Gio.File.new_for_path(os.path.join(directory, new)).get_uri())
        self.window.app.ops.emit("changed", [Gio.File.new_for_path(d).get_uri() for d in done])
        new_uris = [Gio.File.new_for_path(os.path.join(d, n)).get_uri() for d, pairs in done.items() for _o, n in pairs]
        pane = self.pane
        GLib.timeout_add(300, lambda: pane.select_uris(new_uris) and False)
        self.close()

    def _undo(self, done, callback):
        for directory, pairs in done.items():
            try:
                batchrename.apply_plan(directory, batchrename.plan_renames(batchrename.undo_pairs(pairs)))
            except (OSError, ValueError) as e:
                self.window.show_error(_("Cannot undo the rename"), str(e))
        self.window.app.ops.emit("changed", [Gio.File.new_for_path(d).get_uri() for d in done])
        callback()
