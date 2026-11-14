"""MetaFiles' shortcut table matches the installed Nautilus and does not collide with GNOME."""

import re
import shutil
import subprocess
import unittest

import metafiles  # noqa: F401
from gi.repository import Gtk

from metafiles.shortcuts import NAUTILUS_SHORTCUTS, accel_map
from metafiles.storage import DEFAULT_SHORTCUTS

# Nautilus shortcuts-dialog titles -> MetaFiles actions (projects replace windows/home).
TITLE_TO_ACTION = {
    "Open": "open", "Open in New Tab": "open-new-tab", "Open in New Window": "open-as-project",
    "Open Item Location (Search and Recent Only)": "open-item-location",
    "Open Current Directory in Console": "open-terminal", "Open Context Menu": "context-menu",
    "Create Folder": "new-folder", "Cut": "cut", "Copy": "copy", "Paste": "paste", "Rename": "rename",
    "Move to Trash": "trash", "Delete Permanently": "delete-permanently",
    "Create Link to Copied Item": "link-to-copied", "Create Link to Selected Item": "link-to-selected",
    "Show Item Properties": "properties", "Bookmark Current Location": "bookmark", "Zoom In": "zoom-in",
    "Zoom Out": "zoom-out", "Reset Zoom": "zoom-reset", "List View": "view-list", "Grid View": "view-grid",
    "Refresh View": "reload", "Show/Hide Hidden Files": "show-hidden", "Select All": "select-all",
    "Invert Selection": "invert-selection", "Select Items Matching": "select-matching", "Go Up": "up",
    "Go to Home Folder": "home", "Enter Location": "location", "Location Bar With Root Location": "location-root",
    "Location Bar With Home Location": "location-home", "Search Everywhere": "search-project",
    "Search Current Folder": "search", "Go to Previous Tab": "prev-tab", "Go to Next Tab": "next-tab",
}


def norm(accel):
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_name(key, mods) if ok else None


def nautilus_table():
    if not shutil.which("gresource"):
        return None
    try:
        xml = subprocess.run(["gresource", "extract", "/usr/bin/nautilus", "/org/gnome/nautilus/shortcuts-dialog.ui"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    import html

    table = {}
    for block in re.findall(r'<object class="AdwShortcutsItem">(.*?)</object>', xml, re.S):
        title = re.search(r'name="title"[^>]*>([^<]*)', block)
        accel = re.search(r'name="accelerator"[^>]*>([^<]*)', block)
        if title and accel:
            table.setdefault(html.unescape(title.group(1)), html.unescape(accel.group(1)).split())
    return table


class ShortcutsTest(unittest.TestCase):
    def test_matches_installed_nautilus(self):
        table = nautilus_table()
        if table is None:
            self.skipTest("Nautilus resources not available")
        mismatches = []
        for title, accels in table.items():
            action = TITLE_TO_ACTION.get(title)
            if action is None:
                continue
            ours = {norm(a) for a in NAUTILUS_SHORTCUTS[action]}
            for accel in accels:
                # Enter is the list/grid view's own activation (it must stay free for text fields).
                if "..." in accel or (action, accel) == ("open", "Return"):
                    continue
                if norm(accel) not in ours:
                    mismatches.append((title, accel, sorted(a for a in ours if a)))
        self.assertEqual(mismatches, [], "Nautilus changed its shortcuts; update metafiles/shortcuts.py")

    def test_no_duplicate_accelerators(self):
        seen = {}
        for action, accels in accel_map(DEFAULT_SHORTCUTS).items():
            for accel in accels:
                key = norm(accel)
                self.assertIsNotNone(key, accel)
                self.assertNotIn(key, seen, f"{accel} used by {seen.get(key)} and {action}")
                seen[key] = action

    def test_no_collision_with_gnome_window_manager(self):
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        taken = set()
        for schema in ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings"):
            if source.lookup(schema, True) is None:
                continue
            settings = Gio.Settings.new(schema)
            for key in settings.props.settings_schema.list_keys():
                value = settings.get_value(key).unpack()
                if isinstance(value, list):
                    taken.update(norm(v) for v in value if isinstance(v, str))
        ours = {norm(a): action for action, accels in accel_map(DEFAULT_SHORTCUTS).items() for a in accels}
        self.assertEqual({k: v for k, v in ours.items() if k in taken}, {})


if __name__ == "__main__":
    unittest.main()
