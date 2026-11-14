"""Preferences (Ctrl+,) and the keyboard shortcuts dialog (Ctrl+?)."""

from gi.repository import Adw, Gio, GLib, Gtk

from metafiles import features
from metafiles.i18n import _
from metafiles.shortcuts import LABELS, SECTIONS
from metafiles.storage import config_path

NAUTILUS_ROWS = [
    ("preferences", "click-policy", _("Click policy")),
    ("preferences", "default-folder-viewer", _("Default view")),
    ("preferences", "default-sort-order", _("Sort order")),
    ("preferences", "show-hidden-files", _("Show hidden files")),
    ("preferences", "show-delete-permanently", _("“Delete Permanently” in menus")),
    ("preferences", "show-create-link", _("“Create Link” in menus")),
    ("preferences", "show-directory-item-counts", _("Count items in folders")),
    ("preferences", "show-image-thumbnails", _("Thumbnails")),
    ("preferences", "recursive-search", _("Search in subfolders")),
    ("list-view", "default-visible-columns", _("Visible columns")),
    ("list-view", "use-tree-view", _("Expandable folders in list view")),
    ("filechooser", "sort-directories-first", _("Folders before files")),
]

OPTIONAL = [
    ("terminal", _("Terminal panel (F4)")),
    ("sourceview", _("Syntax highlighting in previews")),
    ("poppler", _("Scrollable PDF previews")),
    ("git", _("Git integration")),
    ("meld", _("Comparing files")),
    ("autoar", _("Compress and extract")),
    ("thumbnails", _("Thumbnails")),
]


def _value_text(value):
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def show_preferences(window):
    app = window.app
    dialog = Adw.PreferencesDialog(title=_("Preferences"))
    page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")

    own = Adw.PreferencesGroup(title=_("MetaFiles"))
    hidden = Adw.ComboRow(title=_("Hidden files"), model=Gtk.StringList.new(
        [_("Follow Nautilus"), _("Always show"), _("Never show")]))
    hidden.set_selected({None: 0, True: 1, False: 2}[app.session.show_hidden])
    hidden.connect("notify::selected", lambda r, _p: app.set_show_hidden({0: None, 1: True, 2: False}[r.get_selected()]))
    own.add(hidden)
    status = Adw.SwitchRow(title=_("Status bar"), active=app.session.statusbar_visible)
    status.connect("notify::active", lambda r, _p: r.get_active() != app.session.statusbar_visible
                   and window.toggle_statusbar())
    own.add(status)
    git = Adw.SwitchRow(title=_("Git column in list view"), active=app.git_columns)

    def set_git(row, _p):
        app.git_columns = row.get_active()
        window.refilter_all()
    git.connect("notify::active", set_git)
    own.add(git)
    config = Adw.ActionRow(title=_("Shortcuts and custom actions"), subtitle=GLib.markup_escape_text(config_path()))
    open_config = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Open the configuration file"))
    open_config.add_css_class("flat")
    open_config.connect("clicked", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(config_path())).launch(
        window, None, None))
    config.add_suffix(open_config)
    own.add(config)
    page.add(own)

    nautilus = Adw.PreferencesGroup(title=_("Settings from Nautilus"),
                                    description=_("Read live from Nautilus' preferences; change them in Nautilus."))
    for group, key, title in NAUTILUS_ROWS:
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(_value_text(app.nautilus.get(group, key))))
        row.add_css_class("property")
        nautilus.add(row)
    button = Gtk.Button(label=_("Open Nautilus Preferences"), halign=Gtk.Align.START, margin_top=12)
    button.connect("clicked", lambda *_: _nautilus_preferences(window))
    nautilus.add(button)
    page.add(nautilus)

    components = Adw.PreferencesGroup(title=_("Optional components"))
    missing = []
    for name, title in OPTIONAL:
        ok = features.has(name)
        row = Adw.ActionRow(title=title, subtitle=_("Installed") if ok else
                            _("Missing: {pkg}").format(pkg=features.package_for(name)))
        row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "dialog-warning-symbolic"))
        if not ok and features.package_for(name):
            missing.append(features.package_for(name))
        components.add(row)
    nautilus_row = Adw.ActionRow(title=_("File operations through Nautilus"),
                                 subtitle=_("Active") if app.ops.delegated else _("Local (Nautilus not reachable)"))
    components.add(nautilus_row)
    if missing:
        command = "sudo apt install " + " ".join(missing)
        row = Adw.ActionRow(title=_("Install the missing components"), subtitle=command, subtitle_selectable=True)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Copy"))
        copy.add_css_class("flat")
        copy.connect("clicked", lambda *_: (app.clipboard.set_text(command), window.toast(_("Copied"))))
        row.add_suffix(copy)
        components.add(row)
    page.add(components)
    dialog.add(page)
    dialog.present(window)


def _nautilus_preferences(window):
    try:
        Gio.Subprocess.new(["gapplication", "action", "org.gnome.Nautilus", "preferences"], Gio.SubprocessFlags.NONE)
    except GLib.Error as e:
        window.show_error(_("Could not open Nautilus' preferences"), e.message)


def show_shortcuts(window):
    app = window.app
    dialog = Adw.ShortcutsDialog()
    for title, actions in SECTIONS:
        section = Adw.ShortcutsSection(title=title)
        for action in actions:
            accels = app.get_accels_for_action(f"win.{action}")
            if not accels:
                continue
            section.add(Adw.ShortcutsItem(title=LABELS.get(action, action), accelerator=" ".join(accels[:2])))
        dialog.add(section)
    extra = Adw.ShortcutsSection(title=_("In the view"))
    extra.add(Adw.ShortcutsItem(title=_("Quick preview"), accelerator="space"))
    extra.add(Adw.ShortcutsItem(title=_("Start search by typing"), accelerator="a"))
    extra.add(Adw.ShortcutsItem(title=_("Projects sidebar: add project / new group"), accelerator="p g"))
    dialog.add(extra)
    custom = [a for a in app.custom_actions if a.shortcut]
    if custom:
        section = Adw.ShortcutsSection(title=_("Custom actions"))
        for action in custom:
            section.add(Adw.ShortcutsItem(title=action.label, accelerator=action.shortcut))
        dialog.add(section)
    if not features.has("terminal"):
        section = Adw.ShortcutsSection(title=_("Not available"))
        section.add(Adw.ShortcutsItem(title=_("Terminal panel: install gir1.2-vte-3.91"), accelerator="F4"))
        dialog.add(section)
    dialog.present(window)
