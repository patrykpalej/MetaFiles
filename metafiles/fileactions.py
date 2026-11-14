"""File actions of the main window: activation, context menus, clipboard, operations, search."""

import os
import shlex

from gi.repository import Gdk, Gio, GLib, Gtk

from metafiles import dialogs, features, fileutil, menus
from metafiles.actions import expand_command, is_available, matches
from metafiles.fileview import CHOOSABLE_COLUMNS, COLUMNS, is_dir, item_file, item_mime
from metafiles.i18n import _, ngettext
from metafiles.operations import show_in_nautilus, show_properties

ARCHIVE_MIMES = (
    "application/zip", "application/x-tar", "application/x-compressed-tar", "application/x-xz-compressed-tar",
    "application/x-bzip-compressed-tar", "application/x-7z-compressed", "application/x-rar",
    "application/vnd.rar", "application/gzip", "application/x-xz", "application/x-bzip2",
    "application/x-zstd-compressed-tar", "application/x-cpio", "application/vnd.debian.binary-package",
    "application/x-rpm", "application/x-iso9660-image", "application/java-archive",
)
SCRIPT_MIMES = ("application/x-shellscript", "text/x-python", "text/x-python3", "text/x-perl", "text/x-script",
                "application/x-perl", "text/x-sh")


def drop_mode(state):
    """Nautilus' DnD modifiers: Shift moves, Ctrl copies, otherwise move within a filesystem."""
    if state & Gdk.ModifierType.SHIFT_MASK:
        return "move"
    if state & Gdk.ModifierType.CONTROL_MASK:
        return "copy"
    return "auto"


def same_filesystem(uri, dest_uri):
    try:
        a = Gio.File.new_for_uri(uri).query_info("id::filesystem", Gio.FileQueryInfoFlags.NONE, None)
        b = Gio.File.new_for_uri(dest_uri).query_info("id::filesystem", Gio.FileQueryInfoFlags.NONE, None)
        return a.get_attribute_string("id::filesystem") == b.get_attribute_string("id::filesystem")
    except GLib.Error:
        return False


class FileActionsMixin:
    # --- activation ----------------------------------------------------------------

    def activate_items(self, pane, infos):
        infos = [i for i in infos if i is not None]
        if not infos:
            return
        folders = [i for i in infos if is_dir(i)]
        files = [i for i in infos if not is_dir(i)]
        if pane.location == "trash:///":
            self.toast(_("Restore items from the trash to open them."))
            return
        if folders:
            first = item_file(folders[0]).get_uri()
            if len(folders) == 1 and not files:
                pane.navigate(first)
            else:
                for info in folders:
                    self.open_in_new_tab(item_file(info).get_uri(), background=True)
        for info in files:
            self._launch(info)

    def _launch(self, info, ask=False):
        gf = item_file(info)
        mime = item_mime(info)
        executable = info.get_attribute_boolean("access::can-execute") and not ask
        if executable and (mime in SCRIPT_MIMES or mime == "application/x-executable"):
            self._ask_run(info)
            return
        launcher = Gtk.FileLauncher.new(gf)
        launcher.set_always_ask(ask)

        def done(l, result):
            try:
                l.launch_finish(result)
            except GLib.Error as e:
                if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.show_error(_("Could not open “{name}”").format(name=gf.get_basename()), e.message)
                return
            if gf.get_uri().startswith("file://"):
                Gtk.RecentManager.get_default().add_item(gf.get_uri())

        launcher.launch(self, None, done)

    def _ask_run(self, info):
        gf = item_file(info)
        dialog = dialogs.Adw.AlertDialog(
            heading=_("Run “{name}”?").format(name=info.get_display_name()),
            body=_("This file is executable. Do you want to run it, or display its contents?"))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("display", _("_Display"))
        dialog.add_response("terminal", _("Run in _Terminal"))
        dialog.add_response("run", _("_Run"))
        dialog.set_close_response("cancel")

        def respond(_d, response):
            path = gf.get_path()
            if response == "display":
                launcher = Gtk.FileLauncher.new(gf)
                launcher.launch(self, None, None)
            elif response == "run" and path:
                Gio.Subprocess.new([path], Gio.SubprocessFlags.NONE)
            elif response == "terminal" and path:
                self.run_in_terminal([path], os.path.dirname(path))

        dialog.connect("response", respond)
        dialog.present(self)

    def open_selection(self, new_tab=False):
        pane = self.active_pane
        if pane is None:
            return
        infos = pane.selected_infos()
        if new_tab:
            for info in infos:
                if is_dir(info):
                    self.open_in_new_tab(item_file(info).get_uri(), background=len(infos) > 1)
            return
        self.activate_items(pane, infos)

    def open_with(self):
        pane = self.active_pane
        for info in (pane.selected_infos() if pane else []):
            if not is_dir(info):
                self._launch(info, ask=True)

    def open_selection_as_project(self):
        pane = self.active_pane
        if pane is None:
            return
        folders = [item_file(i).get_path() for i in pane.selected_infos() if is_dir(i)]
        if not folders and pane.path():
            folders = [pane.path()]
        for path in folders:
            self.add_project(path)

    def open_item_location(self):
        pane = self.active_pane
        if pane is None:
            return
        infos = pane.selected_infos()
        if not infos:
            return
        gf = item_file(infos[0])
        if pane.location == "trash:///":
            orig = infos[0].get_attribute_byte_string("trash::orig-path")
            gf = Gio.File.new_for_path(orig) if orig else gf
        parent = gf.get_parent()
        if parent is not None:
            pane.navigate(parent.get_uri(), select=[gf.get_uri()])

    # --- context menus -----------------------------------------------------------------

    def context_menu_from_keyboard(self):
        pane = self.active_pane
        if pane is None:
            return
        positions = pane.selected_positions()
        self.show_context_menu(pane, None, None, background=not positions)

    def show_context_menu(self, pane, x, y, background=False, anchor=None):
        if pane is None:
            return
        self.current_tab and self.current_tab.set_active(pane)
        entries = self._background_entries(pane) if background else self._selection_entries(pane)
        widget = anchor or pane.view
        if anchor is not None:
            x = y = None
        elif x is None:
            # Keyboard: point at the first selected item when visible.
            x, y = 40, 40
        menus.popup(widget, entries, x, y, accel_for=self.accel_for)

    def _selection_entries(self, pane):
        infos = pane.selected_infos()
        if not infos:
            return self._background_entries(pane)
        files = [item_file(i) for i in infos]
        uris = [f.get_uri() for f in files]
        single = len(infos) == 1
        folders = [i for i in infos if is_dir(i)]
        only_folders = len(folders) == len(infos)
        in_trash = pane.location == "trash:///"
        virtual = pane.location in ("recent:///", "starred:///", "git-changes:///") or bool(pane.search_text)
        settings = self.app.nautilus

        if in_trash:
            return [
                ("section", [("item", _("_Restore From Trash"), lambda: self.app.ops.restore_from_trash(uris)),
                             ("item", _("Open Item Location"), self.open_item_location, single)]),
                ("section", [("item", _("_Delete From Trash…"), self.delete_selection, True, "delete-permanently")]),
                ("section", [("item", _("P_roperties"), self.show_properties, True, "properties")]),
            ]

        open_section = []
        if only_folders:
            if single:
                open_section.append(("item", _("_Open"), self.open_selection, True, "open"))
            open_section.append(("item", _("Open in New _Tab"), lambda: self.open_selection(new_tab=True), True,
                                 "open-new-tab"))
            open_section.append(("item", _("Open as _Project"), self.open_selection_as_project, True,
                                 "open-as-project"))
            if pane.tab_is_split():
                open_section.append(("item", _("Show in Other Pane"), self.show_in_other_pane, single))
        else:
            app_name = self._default_app_name(infos[0]) if single else None
            label = _("Open With {app}").format(app=app_name) if app_name else _("_Open")
            open_section.append(("item", label, self.open_selection, True, "open"))
            open_section.append(("item", _("Open _With…"), self.open_with))
            if single and infos[0].get_attribute_boolean("access::can-execute") and \
                    (item_mime(infos[0]) in SCRIPT_MIMES or item_mime(infos[0]) == "application/x-executable"):
                open_section.append(("item", _("_Run as a Program"), lambda: self._ask_run(infos[0])))
        archives = [i for i in infos if item_mime(i) in ARCHIVE_MIMES]
        if archives and len(archives) == len(infos) and features.has("autoar"):
            open_section.append(("item", _("_Extract"), lambda: self.extract(uris, here=True)))
            open_section.append(("item", _("E_xtract to…"), lambda: self.extract(uris, here=False)))
        if virtual:
            open_section.append(("item", _("_Open Item Location"), self.open_item_location, single,
                                 "open-item-location"))

        move_copy = [
            ("item", _("Cu_t"), lambda: self.copy_selection(cut=True), True, "cut"),
            ("item", _("_Copy"), self.copy_selection, True, "copy"),
            ("item", _("Move to…"), lambda: self.transfer_with_chooser(move=True)),
            ("item", _("Copy to…"), lambda: self.transfer_with_chooser(move=False)),
        ]
        file_actions = [("item", _("Rena_me…"), self.rename_selection, True, "rename")]
        if single and only_folders and self.app.clipboard.has_files():
            file_actions.append(("item", _("_Paste Into Folder"), lambda: self.paste(into=uris[0])))
        if settings.get("preferences", "show-create-link"):
            file_actions.append(("item", _("Create _Link"), self.link_to_selected, True, "link-to-selected"))
        if features.has("autoar"):
            file_actions.append(("item", _("C_ompress…"), lambda: self.compress(uris)))
        file_actions.append(("item", _("_Duplicate"), self.duplicate_selection, True, "duplicate"))
        starred = all(self.app.starred.is_starred(u) for u in uris)
        file_actions.append(("item", _("Unstar") if starred else _("Star"), lambda: pane.toggle_star(uris)))
        file_actions.append(("item", _("Mo_ve to Trash"), self.trash_selection, True, "trash"))
        if settings.get("preferences", "show-delete-permanently"):
            file_actions.append(("item", _("_Delete Permanently…"), self.delete_selection, True,
                                 "delete-permanently"))
        if pane.location == "recent:///":
            file_actions.append(("item", _("_Remove From Recent"), lambda: self.remove_from_recent(uris)))

        extras = [("submenu", _("Copy Path"), [
            ("item", _("Full Path"), lambda: self.copy_paths("full"), True, "copy-full-path"),
            ("item", _("Path Relative to Project"), lambda: self.copy_paths("relative"),
             not pane.project.loose, "copy-relative-path"),
            ("item", _("Name"), lambda: self.copy_paths("name")),
            ("item", _("URI"), lambda: self.copy_paths("uri")),
        ])]
        extras.append(("item", _("Preview"), self.quick_preview, single, "quick-preview"))
        if pane.tab_is_split():
            extras.append(("item", _("Copy to Other Pane"), lambda: self.transfer_to_other_pane(False), True,
                           "copy-to-other-pane"))
            extras.append(("item", _("Move to Other Pane"), lambda: self.transfer_to_other_pane(True), True,
                           "move-to-other-pane"))
        if folders:
            extras.append(("item", _("Calculate Size"), lambda: self.calculate_sizes(pane, folders)))
        if len(infos) == 2 or (pane.tab_is_split() and single and self._other_selection_single()):
            extras.append(("item", _("Compare"), self.compare_selection, features.has("meld")))
        git = self._git_entries(pane, files)
        if git:
            extras.append(("submenu", _("Git"), git))
        custom = self._custom_entries("folders" if only_folders else "files", infos, pane.path(), pane.project)
        if custom:
            extras.append(("submenu", _("Actions"), custom))

        tail = []
        if single and only_folders:
            tail.append(("item", _("Open in Terminal"), lambda: self.open_terminal(files[0].get_path())))
        tail.append(("item", _("Open in Nautilus"), lambda: self.open_in_nautilus(uris)))
        return [
            ("section", open_section),
            ("section", move_copy),
            ("section", file_actions),
            ("section", extras),
            ("section", tail),
            ("section", [("item", _("P_roperties"), self.show_properties, True, "properties")]),
        ]

    def _background_entries(self, pane):
        location_entries = []
        if pane.location == "trash:///":
            return [("section", [("item", _("Empty _Trash…"), self.empty_trash)]),
                    ("section", [("item", _("Select _All"), self.select_all, True, "select-all")])]
        real_folder = pane.path() is not None and not pane.search_text
        if real_folder:
            location_entries.append(("item", _("New _Folder…"), self.new_folder, True, "new-folder"))
            location_entries.append(("item", _("New Fil_e…"), self.new_file, True, "new-file"))
            templates = self._template_entries()
            if templates:
                location_entries.append(("submenu", _("New _Document"), templates))
            location_entries.append(("item", _("Open in Terminal"), lambda: self.open_terminal(pane.path()), True,
                                     "open-terminal"))
            if not pane.project.loose and self._has("metaterminal"):
                location_entries.append(("item", _("Open Plain Terminal Here"),
                                         lambda: self.open_terminal(pane.path(), plain=True)))
            location_entries.append(("item", _("Open in Nautilus"),
                                     lambda: self.open_in_nautilus([pane.location], folders=True)))
        edit = []
        if real_folder:
            edit.append(("item", _("_Paste"), self.paste, self.app.clipboard.has_files(), "paste"))
            edit.append(("item", _("Paste as _Link"), self.link_to_copied, self.app.clipboard.has_files(),
                         "link-to-copied"))
        edit.append(("item", _("Select _All"), self.select_all, True, "select-all"))
        if pane.view_mode != "grid":
            edit.append(("item", _("_Visible Columns…"), lambda: self.choose_columns(pane)))
        extras = []
        if real_folder:
            extras.append(("item", _("Bookmark This Folder"), self.bookmark_current, True, "bookmark"))
            extras.append(("item", _("Calculate Folder Sizes"),
                           lambda: self.calculate_sizes(pane, [i for i in pane.all_infos() if is_dir(i)])))
            custom = self._custom_entries("folder", [], pane.path(), pane.project)
            if not pane.project.loose:
                custom += self._custom_entries("project", [], pane.path(), pane.project)
            if custom:
                extras.append(("submenu", _("Actions"), custom))
        props = [("item", _("P_roperties"), lambda: self.show_properties_for([pane.location]), real_folder)]
        return [("section", location_entries), ("section", edit), ("section", extras), ("section", props)]

    def _default_app_name(self, info):
        app = Gio.AppInfo.get_default_for_type(item_mime(info), False)
        return app.get_display_name() if app else None

    def _template_entries(self, directory=None):
        directory = directory or GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
        if not directory or os.path.realpath(directory) == fileutil.HOME or not os.path.isdir(directory):
            return []
        entries = []
        try:
            names = sorted(os.listdir(directory), key=fileutil.natural_key)
        except OSError:
            return []
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(directory, name)
            if os.path.isdir(path):
                sub = self._template_entries(path)
                if sub:
                    entries.append(("submenu", name, sub))
            else:
                label = fileutil.split_extension(name)[0]
                entries.append(("item", label, lambda p=path: self.new_file(template=p)))
        return entries

    # --- clipboard ------------------------------------------------------------------------

    def copy_selection(self, cut=False):
        if self._text_focus_action("cut-clipboard" if cut else "copy-clipboard"):
            return
        pane = self.active_pane
        if pane is None:
            return
        uris = pane.selected_uris()
        if uris:
            self.app.clipboard.set_files(uris, cut)
            n = len(uris)
            text = (ngettext("{n} item will be moved when you paste", "{n} items will be moved when you paste", n)
                    if cut else ngettext("{n} item copied", "{n} items copied", n))
            self.toast(text.format(n=n))

    def paste(self, into=None):
        if self._text_focus_action("paste-clipboard"):
            return
        pane = self.active_pane
        if pane is None:
            return
        dest = into or (pane.location if pane.path() else None)
        if dest is None:
            return

        def done(cut, uris):
            if not uris:
                return
            if cut:
                self.app.ops.move(uris, dest)
                self.app.clipboard.clear_cut()
            else:
                self.app.ops.copy(uris, dest)
            if into is None:
                pane._pending_select = [Gio.File.new_for_uri(dest).get_child(Gio.File.new_for_uri(u).get_basename()).get_uri()
                                        for u in uris]

        self.app.clipboard.read_files(done)

    def _text_focus_action(self, signal):
        """Ctrl+C/X/V/A in a text field act on its text."""
        focus = self.get_focus()
        if isinstance(focus, Gtk.Text) or isinstance(focus, Gtk.Editable):
            editable = focus
            if signal in ("copy-clipboard", "cut-clipboard", "paste-clipboard"):
                try:
                    editable.emit(signal)
                except TypeError:
                    return False
                return True
        return False

    def copy_paths(self, kind):
        pane = self.active_pane
        if pane is None:
            return
        files = pane.selected_files()
        if not files and pane.path():
            files = [Gio.File.new_for_uri(pane.location)]
        lines = []
        for gf in files:
            path = gf.get_path() or gf.get_uri()
            if kind == "relative" and not pane.project.loose and gf.get_path():
                lines.append(fileutil.relative_label(gf.get_path(), pane.project.root))
            elif kind == "name":
                lines.append(gf.get_basename())
            elif kind == "uri":
                lines.append(gf.get_uri())
            else:
                lines.append(path)
        if lines:
            self.app.clipboard.set_text("\n".join(lines))
            self.toast(_("Copied: {text}").format(text=lines[0] if len(lines) == 1 else
                                                    ngettext("{n} path", "{n} paths", len(lines)).format(n=len(lines))))

    def select_all(self):
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.Editable)):
            focus.select_region(0, -1)
            return
        if self.active_pane is not None:
            self.active_pane.select_all()

    # --- operations ---------------------------------------------------------------------------

    def _target_folder(self):
        pane = self.active_pane
        return pane.path() if pane is not None and not pane.search_text else None

    def new_folder(self):
        pane = self.active_pane
        folder = self._target_folder()
        if folder is None:
            return

        def create(name):
            if name:
                uri = Gio.File.new_for_path(folder).get_uri()
                self.app.ops.create_folder(uri, name, lambda new_uri: pane.select_uris([new_uri]))

        dialogs.new_item(self, folder, True, create)

    def new_file(self, template=None):
        pane = self.active_pane
        folder = self._target_folder()
        if folder is None:
            return
        initial = os.path.basename(template) if template else None

        def create(name):
            if name:
                uri = Gio.File.new_for_path(folder).get_uri()
                template_uri = Gio.File.new_for_path(template).get_uri() if template else None
                self.app.ops.new_file(uri, name, template_uri, lambda new_uri: pane.select_uris([new_uri]))

        dialogs.new_item(self, folder, False, create, initial=initial)

    def rename_selection(self):
        pane = self.active_pane
        if pane is None:
            return
        infos = pane.selected_infos()
        if not infos or pane.location == "trash:///":
            return
        if len(infos) > 1:
            from metafiles.renamedialog import BatchRenameDialog

            BatchRenameDialog(self, pane, [item_file(i) for i in infos]).present(self)
            return
        info = infos[0]
        gf = item_file(info)
        parent = gf.get_parent()
        existing = os.listdir(parent.get_path()) if parent and parent.get_path() else []
        position = pane.position_of(gf.get_uri())
        x, y = self._item_point(pane, position)
        dialogs.rename_popover(pane.view, x, y, info.get_display_name(), is_dir(info), existing,
                               lambda name: self.app.ops.rename(gf.get_uri(), name,
                                                                lambda new: pane.select_uris([new])))

    def _item_point(self, pane, position):
        """Approximate point of a row for anchoring popovers."""
        cell = None
        for c in pane._bound:
            if c.list_item is not None and c.list_item.get_position() == position:
                cell = c
                break
        if cell is None:
            return 40, 40
        x, y = fileutil.translate(cell, pane.view, cell.get_width() / 2, cell.get_height())
        return x, y

    def duplicate_selection(self):
        pane = self.active_pane
        if pane is None or not pane.selected_uris():
            return
        self.app.ops.duplicate(pane.selected_uris(), lambda new: pane.select_uris(new))

    def trash_selection(self):
        if self._text_focus_delete():
            return
        pane = self.active_pane
        if pane is None:
            return
        uris = pane.selected_uris()
        if not uris:
            return
        if pane.location == "trash:///":
            self.delete_selection()
            return
        n = len(uris)
        name = pane.selected_infos()[0].get_display_name()

        def done(ok):
            if ok:
                text = (_("“{name}” moved to trash").format(name=name) if n == 1 else
                        ngettext("{n} item moved to trash", "{n} items moved to trash", n).format(n=n))
                self.toast(text, _("Undo"), self.app.ops.undo)

        self.app.ops.trash(uris, done)

    def _text_focus_delete(self):
        focus = self.get_focus()
        return isinstance(focus, (Gtk.Text, Gtk.Editable))

    def delete_selection(self):
        pane = self.active_pane
        if pane is None:
            return
        uris = pane.selected_uris()
        if not uris:
            return
        n = len(uris)
        name = pane.selected_infos()[0].get_display_name()
        title = (_("Permanently delete “{name}”?").format(name=name) if n == 1 else
                 ngettext("Permanently delete {n} selected item?", "Permanently delete {n} selected items?", n)
                 .format(n=n))
        self.confirm_async(title, _("If you delete an item, it will be permanently lost."), _("_Delete"),
                           lambda ok: ok and self.app.ops.delete(uris))

    def empty_trash(self):
        view = self.current_view
        if view is None or view.loose or not view.lock:
            self.app.ops.empty_trash()
            return
        # In a locked project only the project's items are deleted.
        items = []
        trash = Gio.File.new_for_uri("trash:///")
        try:
            for info in trash.enumerate_children("standard::name,trash::orig-path", Gio.FileQueryInfoFlags.NONE, None):
                orig = info.get_attribute_byte_string("trash::orig-path")
                if orig and view.contains(orig):
                    items.append(trash.get_child(info.get_name()).get_uri())
        except GLib.Error:
            pass
        if not items:
            self.toast(_("The trash has no items from this project."))
            return
        self.confirm_async(_("Empty the project's items from Trash?"),
                           ngettext("{n} item from this project will be permanently deleted.",
                                    "{n} items from this project will be permanently deleted.", len(items))
                           .format(n=len(items)),
                           _("_Empty"), lambda ok: ok and self.app.ops.delete(items))

    def remove_from_recent(self, uris):
        manager = Gtk.RecentManager.get_default()
        for uri in uris:
            try:
                manager.remove_item(uri)
            except GLib.Error:
                pass
        if self.active_pane is not None:
            self.active_pane.reload()

    def link_to_selected(self):
        pane = self.active_pane
        if pane is None or not pane.path():
            return
        self.app.ops.make_links(pane.selected_uris(), pane.location, lambda new: pane.select_uris(new))

    def link_to_copied(self):
        pane = self.active_pane
        if pane is None or not pane.path():
            return
        self.app.clipboard.read_files(lambda _cut, uris: uris and self.app.ops.make_links(uris, pane.location))

    def transfer_with_chooser(self, move):
        pane = self.active_pane
        if pane is None:
            return
        uris = pane.selected_uris()
        project = pane.project
        dialog = Gtk.FileDialog(title=_("Select Move Destination") if move else _("Select Copy Destination"),
                                accept_label=_("_Select"))
        start = pane.path() or project.root
        dialog.set_initial_folder(Gio.File.new_for_path(start))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            if project.lock and (folder.get_path() is None or not project.contains(folder.get_path())):
                self.show_error(_("Destination outside the project"),
                                _("While navigation is locked, the destination must be inside the project."))
                return
            (self.app.ops.move if move else self.app.ops.copy)(uris, folder.get_uri())

        dialog.select_folder(self, None, done)

    def drop_files(self, uris, dest_uri, mode="auto"):
        uris = [u for u in uris if u != dest_uri]
        if not uris or dest_uri is None:
            return
        if any(dest_uri == u or dest_uri.startswith(u.rstrip("/") + "/") for u in uris):
            self.toast(_("You cannot move a folder into itself."))
            return
        if mode == "auto":
            mode = "move" if same_filesystem(uris[0], dest_uri) else "copy"
        if mode == "move":
            self.app.ops.move(uris, dest_uri)
        else:
            self.app.ops.copy(uris, dest_uri)

    def transfer_to_other_pane(self, move):
        tab = self.current_tab
        if tab is None or not tab.is_split:
            self.toast(_("Split the view first (F3)."))
            return
        other = tab.other_pane()
        if other.path() is None:
            return
        uris = tab.active.selected_uris()
        if uris:
            (self.app.ops.move if move else self.app.ops.copy)(uris, other.location)
            other._pending_select = [Gio.File.new_for_uri(other.location).get_child(
                Gio.File.new_for_uri(u).get_basename()).get_uri() for u in uris]

    def show_in_other_pane(self):
        tab = self.current_tab
        if tab is None or not tab.is_split:
            return
        pane = tab.active
        infos = [i for i in pane.selected_infos() if is_dir(i)]
        target = item_file(infos[0]).get_uri() if infos else pane.location
        tab.other_pane().navigate(target)

    def _other_selection_single(self):
        tab = self.current_tab
        other = tab.other_pane() if tab is not None else None
        return other is not None and len(other.selected_positions()) == 1

    def compare_selection(self):
        pane = self.active_pane
        files = pane.selected_files()
        if len(files) == 1 and self.current_tab.is_split:
            files += self.current_tab.other_pane().selected_files()
        paths = [f.get_path() for f in files if f.get_path()]
        if len(paths) == 2:
            Gio.Subprocess.new(["meld", *paths], Gio.SubprocessFlags.NONE)

    def calculate_sizes(self, pane, folders):
        from metafiles.fileview import SIZE_CALCULATOR

        for info in folders:
            path = item_file(info).get_path()
            if path:
                SIZE_CALCULATOR.compute(path, lambda _total: pane.rebind_all())
        pane.rebind_all()

    def show_properties(self):
        pane = self.active_pane
        if pane is None:
            return
        uris = pane.selected_uris() or [pane.location]
        self.show_properties_for(uris)

    def show_properties_for(self, uris):
        uris = [u for u in uris if not u.endswith(":///")]
        if not uris:
            return
        if self.app.ops.delegated:
            show_properties(uris)
            return
        pairs = []
        for uri in uris:
            gf = Gio.File.new_for_uri(uri)
            try:
                pairs.append((gf.query_info("standard::*,time::*,unix::mode,owner::user", Gio.FileQueryInfoFlags.NONE,
                                            None), gf))
            except GLib.Error:
                continue
        if pairs:
            dialogs.basic_properties(self, pairs)

    def bookmark_current(self):
        pane = self.active_pane
        if pane is None:
            return
        infos = [i for i in pane.selected_infos() if is_dir(i)]
        path = pane.path()
        if path is None:
            return
        view = pane.project
        if not view.loose:
            if path not in view.state.bookmarks:
                view.state.bookmarks.append(path)
                self.places.rebuild()
                self.app.save_session()
                self.toast(_("“{name}” added to project bookmarks").format(name=os.path.basename(path) or path))
            return
        uri = pane.location
        if self.app.bookmarks.contains(uri):
            self.toast(_("This folder is already bookmarked"))
            return
        self.app.bookmarks.add(uri)
        self.toast(_("“{name}” added to bookmarks").format(name=os.path.basename(path) or path))
        del infos

    # --- archives -----------------------------------------------------------------------------

    def compress(self, uris):
        from metafiles.archives import compress_dialog

        pane = self.active_pane
        compress_dialog(self, uris, pane.path() or os.path.dirname(Gio.File.new_for_uri(uris[0]).get_path()),
                        lambda new: pane.select_uris([new]))

    def extract(self, uris, here=True):
        from metafiles.archives import extract

        pane = self.active_pane
        if here:
            extract(self, uris, pane.path() or os.path.dirname(Gio.File.new_for_uri(uris[0]).get_path()))
            return
        dialog = Gtk.FileDialog(title=_("Select Extract Destination"), accept_label=_("_Select"))
        dialog.set_initial_folder(Gio.File.new_for_path(pane.path() or pane.project.root))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                extract(self, uris, folder.get_path())

        dialog.select_folder(self, None, done)

    # --- search -----------------------------------------------------------------------------------

    def start_search(self, whole_project=False, text=None):
        pane = self.active_pane
        if pane is None:
            return
        self._search_whole_project = whole_project and not pane.project.loose
        placeholder = _("Search whole project") if self._search_whole_project else _("Search current folder")
        self.pathbar.show_search(text if text is not None else pane.search_text, placeholder)
        self.search_button.set_active(True)

    def _on_search_text(self, text):
        pane = self.active_pane
        if pane is None:
            return
        if text.strip():
            pane.start_search(text, whole_project=getattr(self, "_search_whole_project", False))
        elif pane.search_text:
            pane.stop_search()

    def _on_search_closed(self):
        self.search_button.set_active(False)
        pane = self.active_pane
        if pane is not None:
            if pane.search_text:
                pane.stop_search()
            pane.focus_view()

    def _on_search_toggled(self, button):
        if button.get_active() and not self.pathbar.searching:
            self.start_search()
        elif not button.get_active() and self.pathbar.searching:
            self.pathbar.close_search()

    # --- view menu ------------------------------------------------------------------------------------

    def _fill_view_menu(self):
        pane = self.active_pane
        popover = self.view_button.get_popover()
        if pane is None:
            return
        group = Gio.SimpleActionGroup()

        def radio(label, key, reverse):
            return ("radio", label, (pane.sort_key, pane.sort_reverse) == (key, reverse),
                    lambda: pane.set_sort(key, reverse))

        sort = [radio(_("_A-Z"), "name", False), radio(_("_Z-A"), "name", True),
                radio(_("Last _Modified"), "date_modified", True), radio(_("_First Modified"), "date_modified", False),
                radio(_("_Size"), "size", True), radio(_("_Type"), "type", False)]
        if pane.location == "recent:///":
            sort.append(radio(_("Recency"), "recency", True))
        if pane.location == "trash:///":
            sort.append(radio(_("Last _Trashed"), "trashed_on", True))
        view_entries = [
            ("radio", _("_List"), pane.view_mode == "list", lambda: pane.set_view_mode("list")),
            ("radio", _("_Tree"), pane.view_mode == "tree", lambda: pane.set_view_mode("tree")),
            ("radio", _("_Grid"), pane.view_mode == "grid", lambda: pane.set_view_mode("grid")),
        ]
        zoom = [("item", _("Zoom _In"), lambda: pane.zoom(+1), pane.can_zoom(+1), "zoom-in"),
                ("item", _("Zoom _Out"), lambda: pane.zoom(-1), pane.can_zoom(-1), "zoom-out"),
                ("item", _("_Reset Zoom"), lambda: pane.zoom(0), True, "zoom-reset")]
        options = [("check", _("Show _Hidden Files"), self.app.show_hidden, self.toggle_hidden)]
        repo = pane.git_repo
        if repo is not None:
            options.append(("check", _("Hide Files Ignored by _Git"), pane.project.hide_git_ignored,
                            self.toggle_git_ignored))
            options.append(("item", _("Show Only _Changed Files"), lambda: pane.navigate("git-changes:///"),
                            not pane.project.loose))
        options.append(("item", _("Calculate Folder _Sizes"),
                        lambda: self.calculate_sizes(pane, [i for i in pane.all_infos() if is_dir(i)])))
        if pane.view_mode != "grid":
            options.append(("item", _("_Visible Columns…"), lambda: self.choose_columns(pane)))
        model = menus.build_menu([("section", sort), ("section", view_entries), ("section", zoom),
                                  ("section", options)], group, self.accel_for)
        popover.insert_action_group("m", group)
        popover.set_menu_model(model)

    def choose_columns(self, pane):
        dialog = dialogs.Adw.AlertDialog(heading=_("Visible Columns"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        current = [c for c in pane.visible_columns() if c in CHOOSABLE_COLUMNS]
        checks = []
        for key in CHOOSABLE_COLUMNS:
            title = COLUMNS[key][0] or _("Star")
            check = Gtk.CheckButton(label=title, active=key in current)
            checks.append((key, check))
            box.append(check)
        dialog.set_extra_child(box)
        dialog.add_response("default", _("_Reset to Default"))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("apply", _("_Apply"))
        dialog.set_response_appearance("apply", dialogs.Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def respond(_d, response):
            if response == "apply":
                pane.set_columns([k for k, c in checks if c.get_active()])
            elif response == "default":
                pane._columns = None
                pane.set_columns(pane.settings.visible_columns)
                pane._columns = None

        dialog.connect("response", respond)
        dialog.present(self)

    # --- terminal, Nautilus, MetaTerminal ----------------------------------------------------------

    def open_terminal(self, path=None, plain=False):
        pane = self.active_pane
        if path is None:
            infos = [i for i in pane.selected_infos() if is_dir(i)] if pane else []
            path = item_file(infos[0]).get_path() if len(infos) == 1 else (pane.path() if pane else None)
        if path is None:
            return
        view = self.current_view
        if not plain and view is not None and not view.loose and self._has("metaterminal"):
            self.open_in_metaterminal(view.root)
            return
        self.run_in_terminal(None, path)

    def open_in_metaterminal(self, root):
        try:
            Gio.Subprocess.new(["metaterminal", root], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.show_error(_("Could not start MetaTerminal"), e.message)

    def run_in_terminal(self, argv, cwd):
        """Open the user's default terminal in cwd, optionally running argv."""
        if self._has("xdg-terminal-exec"):
            command = ["xdg-terminal-exec", f"--dir={cwd}"] + (argv or [])
        elif self._has("gnome-terminal"):
            command = ["gnome-terminal", f"--working-directory={cwd}"] + (["--", *argv] if argv else [])
        else:
            self.show_error(_("No terminal found"), _("Install xdg-terminal-exec or gnome-terminal."))
            return
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        launcher.set_cwd(cwd)
        try:
            launcher.spawnv(command)
        except GLib.Error as e:
            self.show_error(_("Could not open a terminal"), e.message)

    def open_in_nautilus(self, uris, folders=False):
        show_in_nautilus(uris, folders=folders)

    def toggle_terminal(self):
        tab = self.current_tab
        if tab is None:
            return
        terminal = tab.terminal
        if terminal is None or not terminal.get_visible():
            tab.toggle_terminal(True)
        elif terminal.has_focus_inside():
            tab.toggle_terminal(False)
        else:
            terminal.focus()

    # --- custom actions -----------------------------------------------------------------------------

    def _custom_entries(self, context, infos, folder, project):
        items = [(item_file(i).get_path() or item_file(i).get_uri(), item_mime(i), is_dir(i)) for i in infos]
        entries = []
        for action in self.app.custom_actions:
            if context not in action.contexts or not is_available(action):
                continue
            if context in ("files", "folders") and not matches(action, context, items):
                continue
            entries.append(("item", action.label,
                            lambda a=action, c=context: self.run_custom_action(a, context=c, project=project),
                            True, f"custom-{action.id}"))
        return entries

    def run_custom_action(self, action, context=None, project=None, from_shortcut=False):
        pane = self.active_pane
        project = project or (pane.project if pane else self.current_view)
        if pane is None and project is None:
            return
        infos = pane.selected_infos() if pane else []
        paths = [item_file(i).get_path() for i in infos if item_file(i).get_path()]
        folder = pane.path() if pane and pane.path() else (project.root if project else fileutil.HOME)
        project_root = None if project is None or project.loose else project.root
        if from_shortcut:
            items = [(item_file(i).get_path() or "", item_mime(i), is_dir(i)) for i in infos]
            if items and not (matches(action, "files", items) or matches(action, "folders", items)) and \
                    not ({"folder", "project"} & set(action.contexts)):
                self.toast(_("“{name}” does not apply to the selection").format(name=action.label))
                return
            if not items and not ({"folder", "project"} & set(action.contexts)):
                return
        if context in ("folder", "project"):
            paths = []
        try:
            argv, env = expand_command(action, paths, folder, project_root)
        except ValueError as e:
            self.show_error(_("Action “{name}” is invalid").format(name=action.label), str(e))
            return

        def run():
            if action.terminal:
                tab = self.current_tab
                if tab is not None and features.has("terminal"):
                    tab.toggle_terminal(True)
                    tab.terminal.run_command(shlex.join(argv))
                else:
                    self.run_in_terminal(argv, folder)
                return
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
            launcher.set_cwd(folder)
            for key, value in env.items():
                launcher.setenv(key, value, True)
            try:
                proc = launcher.spawnv(argv)
            except GLib.Error as e:
                self.show_error(_("Action “{name}” failed").format(name=action.label), e.message)
                return

            def finished(p, result):
                try:
                    _ok, out, _err = p.communicate_utf8_finish(result)
                except GLib.Error as e:
                    out = e.message
                if p.get_if_exited() and p.get_exit_status() != 0:
                    self.toast(_("Action “{name}” failed").format(name=action.label), _("Show Output"),
                               lambda: dialogs.info_dialog(self, action.label, out or "", monospace=True))

            proc.communicate_utf8_async(None, None, finished)

        if action.confirm:
            self.confirm_async(_("Run “{name}”?").format(name=action.label), " ".join(argv), _("_Run"),
                               lambda ok: ok and run())
        else:
            run()

    # --- git ---------------------------------------------------------------------------------------

    def _git_entries(self, pane, files):
        repo = pane.git_repo
        if repo is None or repo.status is None:
            return []
        paths = [f.get_path() for f in files if f.get_path()]
        if not paths:
            return []

        def report(ok, output, what):
            if not ok:
                self.show_error(_("Git: {what} failed").format(what=what), output)
            elif output.strip():
                self.toast(output.strip().splitlines()[0])
            pane.reload()

        def diff():
            def show(ok, output):
                if not ok:
                    self.show_error(_("Git diff failed"), output)
                elif not output.strip():
                    self.toast(_("No changes"))
                else:
                    dialogs.info_dialog(self, _("Changes"), output, monospace=True)
            repo.diff(paths, show)

        def restore():
            self.confirm_async(_("Discard changes?"), _("The selected files will be restored to the last commit. "
                                                         "Uncommitted changes in them will be lost."),
                               _("_Discard"), lambda ok: ok and repo.restore(paths, lambda o, out: report(o, out, _("restore"))))

        entries = [("item", _("Show Changes"), diff)]
        if features.has("meld") and len(paths) == 1 and os.path.isfile(paths[0]):
            entries.append(("item", _("Show Changes in Meld"), lambda: self._meld_diff(repo, paths[0])))
        entries += [
            ("item", _("Discard Changes…"), restore),
            ("item", _("Stage"), lambda: repo.add(paths, lambda o, out: report(o, out, _("stage")))),
            ("item", _("Unstage"), lambda: repo.unstage(paths, lambda o, out: report(o, out, _("unstage")))),
        ]
        return entries

    def _meld_diff(self, repo, path):
        try:
            Gio.Subprocess.new(["git", "-C", repo.root, "difftool", "--no-prompt", "--tool=meld", "--", path],
                               Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.show_error(_("Could not start Meld"), e.message)

    # --- quick open / preview / dialogs ------------------------------------------------------------

    def quick_open(self):
        from metafiles.quickopen import QuickOpen

        view = self.current_view
        if view is None:
            return
        QuickOpen(self, view).popup()

    def quick_preview(self):
        from metafiles.preview.quicklook import QuickLook

        pane = self.active_pane
        if pane is None or len(pane.selected_positions()) != 1:
            return
        QuickLook(self, pane).present(self)

    def show_preferences(self):
        from metafiles.prefs import show_preferences

        show_preferences(self)

    def show_shortcuts(self):
        from metafiles.prefs import show_shortcuts

        show_shortcuts(self)
