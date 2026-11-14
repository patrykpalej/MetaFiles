"""Keyboard shortcuts: Nautilus 50's defaults plus the project and extra-feature shortcuts.

The Nautilus table comes from org/gnome/nautilus/shortcuts-dialog.ui in Nautilus' resources and
the accelerators Nautilus sets in code. Window accelerators are handled in GTK's bubble phase, so a
focused text field (location entry, rename, dialogs) gets its own editing keys first.
"""

from metafiles.i18n import _
from metafiles.storage import DEFAULT_SHORTCUTS

# action -> accelerators, with Nautilus' meaning (mapped to projects where Nautilus means windows/home).
NAUTILUS_SHORTCUTS = {
    # Files
    "open": ["<Control>o", "<Alt>Down"],
    "open-new-tab": ["<Control>Return"],
    "open-as-project": ["<Shift>Return"],
    "open-item-location": ["<Control><Alt>o"],
    "open-terminal": ["<Control>period"],
    "context-menu": ["<Shift>F10", "Menu"],
    "new-folder": ["<Control><Shift>n"],
    "cut": ["<Control>x"],
    "copy": ["<Control>c"],
    "paste": ["<Control>v"],
    "rename": ["F2"],
    "trash": ["Delete", "KP_Delete"],
    "delete-permanently": ["<Shift>Delete", "<Shift>KP_Delete"],
    "link-to-copied": ["<Control>m"],
    "link-to-selected": ["<Control><Shift>m"],
    "properties": ["<Control>i", "<Alt>Return"],
    "undo": ["<Control>z"],
    "redo": ["<Control><Shift>z"],
    "bookmark": ["<Control>d"],
    # View
    "zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
    "zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
    "zoom-reset": ["<Control>0", "<Control>KP_0"],
    "view-list": ["<Control>1"],
    "view-grid": ["<Control>2"],
    "reload": ["F5", "<Control>r"],
    "show-hidden": ["<Control>h"],
    "toggle-places": ["F9"],
    # Selection
    "select-all": ["<Control>a"],
    "invert-selection": ["<Control><Shift>i"],
    "select-matching": ["<Control>s"],
    # Navigation
    "back": ["<Alt>Left", "Back"],
    "forward": ["<Alt>Right", "Forward"],
    "up": ["<Alt>Up"],
    "home": ["<Alt>Home"],
    "location": ["<Control>l"],
    "location-root": ["slash", "KP_Divide"],
    "location-home": ["asciitilde"],
    # Search
    "search": ["<Control>f"],
    "search-project": ["<Control><Shift>f"],
    # Tabs
    "new-tab": ["<Control>t"],
    "close-tab": ["<Control>w"],
    "restore-tab": ["<Control><Shift>t"],
    "prev-tab": ["<Control>Page_Up"],
    "next-tab": ["<Control>Page_Down"],
    "tab-move-left": ["<Control><Shift>Page_Up"],
    "tab-move-right": ["<Control><Shift>Page_Down"],
    **{f"tab-{i}": [f"<Alt>{i}"] for i in range(1, 10)},
    # Application
    "add-project": ["<Control>n"],
    "main-menu": ["F10"],
    "preferences": ["<Control>comma"],
    "show-shortcuts": ["<Control>question", "<Control>F1"],
    "quit": ["<Control>q"],
}

# Accelerators that must not fire while a text field has focus: they are handled on the view.
VIEW_ONLY = {"location-root", "location-home", "open", "context-menu"}

SECTIONS = [
    (_("Files"), ["open", "open-new-tab", "open-as-project", "open-item-location", "open-terminal", "new-folder",
                  "new-file", "cut", "copy", "paste", "rename", "duplicate", "trash", "delete-permanently",
                  "link-to-copied", "link-to-selected", "properties", "undo", "redo", "bookmark"]),
    (_("View"), ["zoom-in", "zoom-out", "zoom-reset", "view-list", "view-grid", "reload", "show-hidden",
                 "toggle-places"]),
    (_("Selection"), ["select-all", "invert-selection", "select-matching"]),
    (_("Navigation"), ["back", "forward", "up", "home", "location", "location-root", "location-home"]),
    (_("Search"), ["search", "search-project"]),
    (_("Tabs"), ["new-tab", "close-tab", "restore-tab", "prev-tab", "next-tab", "tab-move-left",
                 "tab-move-right"] + [f"tab-{i}" for i in range(1, 10)]),
    (_("Projects"), ["add-project", "prev-project", "next-project"] + [f"switch-to-project-{i}" for i in range(1, 10)]
     + ["switch-to-loose", "toggle-projects", "toggle-focus"]),
    (_("Beyond Nautilus"), ["quick-open", "toggle-preview", "toggle-split", "switch-pane", "copy-to-other-pane",
                            "move-to-other-pane", "toggle-terminal", "copy-full-path", "copy-relative-path",
                            "toggle-git-ignored"]),
    (_("Application"), ["main-menu", "preferences", "show-shortcuts", "quit"]),
]

LABELS = {
    "open": _("Open"), "open-new-tab": _("Open in New Tab"), "open-as-project": _("Open as Project"),
    "open-item-location": _("Open Item Location"), "open-terminal": _("Open in Terminal"),
    "context-menu": _("Open Context Menu"), "new-folder": _("Create Folder"), "new-file": _("Create Empty File"),
    "cut": _("Cut"), "copy": _("Copy"), "paste": _("Paste"), "rename": _("Rename"), "duplicate": _("Duplicate"),
    "trash": _("Move to Trash"), "delete-permanently": _("Delete Permanently"),
    "link-to-copied": _("Create Link to Copied Item"), "link-to-selected": _("Create Link to Selected Item"),
    "properties": _("Show Item Properties"), "undo": _("Undo"), "redo": _("Redo"),
    "bookmark": _("Bookmark Current Location"), "zoom-in": _("Zoom In"), "zoom-out": _("Zoom Out"),
    "zoom-reset": _("Reset Zoom"), "view-list": _("List View"), "view-grid": _("Grid View"),
    "reload": _("Refresh View"), "show-hidden": _("Show/Hide Hidden Files"),
    "toggle-places": _("Show/Hide Places Sidebar"), "select-all": _("Select All"),
    "invert-selection": _("Invert Selection"), "select-matching": _("Select Items Matching"),
    "back": _("Go Back"), "forward": _("Go Forward"), "up": _("Go Up"), "home": _("Go to Project Folder"),
    "location": _("Enter Location"), "location-root": _("Location Bar with Root Location"),
    "location-home": _("Location Bar with Project Location"), "search": _("Search Current Folder"),
    "search-project": _("Search Whole Project"), "new-tab": _("New Tab"), "close-tab": _("Close Tab"),
    "restore-tab": _("Restore Closed Tab"), "prev-tab": _("Go to Previous Tab"), "next-tab": _("Go to Next Tab"),
    "tab-move-left": _("Move Tab Left"), "tab-move-right": _("Move Tab Right"),
    **{f"tab-{i}": _("Tab {n}").format(n=i) for i in range(1, 10)},
    "add-project": _("Add Project"), "prev-project": _("Previous Project"), "next-project": _("Next Project"),
    **{f"switch-to-project-{i}": _("Project {n}").format(n=i) for i in range(1, 10)},
    "switch-to-loose": _("Unassigned Tabs"), "toggle-projects": _("Show/Hide Projects Sidebar"),
    "toggle-focus": _("Focus: Files ⇄ Projects"), "quick-open": _("Quick Open"),
    "toggle-preview": _("Preview Panel"), "toggle-split": _("Split View"), "switch-pane": _("Switch Pane"),
    "copy-to-other-pane": _("Copy to Other Pane"), "move-to-other-pane": _("Move to Other Pane"),
    "toggle-terminal": _("Terminal Panel"), "copy-full-path": _("Copy Full Path"),
    "copy-relative-path": _("Copy Path Relative to Project"),
    "toggle-git-ignored": _("Show/Hide Files Ignored by Git"), "main-menu": _("Main Menu"),
    "preferences": _("Preferences"), "show-shortcuts": _("Keyboard Shortcuts"), "quit": _("Quit"),
}

EXTRA_LABELS = {"preview": _("Quick Preview"), "space": "Space"}


def accel_map(config_shortcuts):
    """All window actions -> accelerators, the user's config overriding the extra shortcuts."""
    mapping = {k: list(v) for k, v in NAUTILUS_SHORTCUTS.items()}
    for action, accels in DEFAULT_SHORTCUTS.items():
        mapping[action] = list(config_shortcuts.get(action, accels))
    return mapping
