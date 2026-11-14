"""Nautilus' preferences, read live (never written) from GSettings."""

from gi.repository import Gio, GObject

# Nautilus' stock defaults, used when its schemas are not installed.
DEFAULTS = {
    "preferences": {
        "click-policy": "double",
        "default-folder-viewer": "icon-view",
        "default-sort-order": "name",
        "default-sort-in-reverse-order": False,
        "show-hidden-files": False,
        "show-delete-permanently": False,
        "show-create-link": False,
        "show-directory-item-counts": "local-only",
        "show-image-thumbnails": "local-only",
        "thumbnail-limit": 50,
        "recursive-search": "local-only",
        "mouse-use-extra-buttons": True,
        "mouse-back-button": 8,
        "mouse-forward-button": 9,
        "date-time-format": "simple",
        "open-folder-on-dnd-hover": False,
    },
    "list-view": {
        "default-zoom-level": "small",
        "default-visible-columns": ["name", "size", "date_modified"],
        "default-column-order": ["name", "size", "type", "owner", "group", "permissions", "date_modified",
                                 "date_accessed", "date_created", "recency", "detailed_type"],
        "use-tree-view": False,
    },
    "icon-view": {
        "default-zoom-level": "medium",
        "captions": ["none", "none", "none"],
    },
    "filechooser": {
        "sort-directories-first": True,
    },
}

SCHEMAS = {
    "preferences": "org.gnome.nautilus.preferences",
    "list-view": "org.gnome.nautilus.list-view",
    "icon-view": "org.gnome.nautilus.icon-view",
    "filechooser": "org.gtk.gtk4.Settings.FileChooser",
}

LIST_ZOOM_LEVELS = ["small", "medium", "large"]
LIST_ICON_SIZES = {"small": 16, "medium": 32, "large": 64}
GRID_ZOOM_LEVELS = ["small", "small-plus", "medium", "large", "extra-large"]
GRID_ICON_SIZES = {"small": 64, "small-plus": 96, "medium": 128, "large": 192, "extra-large": 256}

SORT_KEYS = {"name": "name", "size": "size", "type": "type", "mtime": "date_modified",
             "atime": "date_accessed", "btime": "date_created", "starred": "starred"}


def schema_exists(schema_id):
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


class NautilusSettings(GObject.Object):
    """Emits "changed" (group, key) whenever one of the watched settings changes."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str))}

    def __init__(self):
        super().__init__()
        self._settings = {}
        self._cache = {}
        for group, schema in SCHEMAS.items():
            if schema_exists(schema):
                s = Gio.Settings.new(schema)
                s.connect("changed", lambda _s, key, group=group: self._on_changed(group, key))
                self._settings[group] = s

    def _on_changed(self, group, key):
        self._cache.pop((group, key), None)
        self.emit("changed", group, key)

    def get(self, group, key):
        cached = self._cache.get((group, key), self)
        if cached is not self:
            return cached
        value = DEFAULTS[group][key]
        s = self._settings.get(group)
        if s is not None and key in s.props.settings_schema.list_keys():
            value = s.get_value(key).unpack()
        self._cache[(group, key)] = value
        return value

    @property
    def available(self):
        return "preferences" in self._settings

    # Convenience accessors used across the UI.
    @property
    def show_hidden(self):
        return bool(self.get("preferences", "show-hidden-files"))

    @property
    def single_click(self):
        return self.get("preferences", "click-policy") == "single"

    @property
    def default_view(self):
        if self.get("preferences", "default-folder-viewer") == "icon-view":
            return "grid"
        return "tree" if self.get("list-view", "use-tree-view") else "list"

    @property
    def sort(self):
        """(column key, reversed)."""
        key = SORT_KEYS.get(self.get("preferences", "default-sort-order"), "name")
        return key, bool(self.get("preferences", "default-sort-in-reverse-order"))

    @property
    def directories_first(self):
        return bool(self.get("filechooser", "sort-directories-first"))

    def zoom(self, view):
        if view == "grid":
            level = self.get("icon-view", "default-zoom-level")
            return level if level in GRID_ZOOM_LEVELS else "medium"
        level = self.get("list-view", "default-zoom-level")
        return level if level in LIST_ZOOM_LEVELS else "small"

    @property
    def visible_columns(self):
        return list(self.get("list-view", "default-visible-columns"))

    @property
    def column_order(self):
        return list(self.get("list-view", "default-column-order"))


def local_only_allows(value, is_local):
    """For the always/local-only/never settings."""
    return value == "always" or (value == "local-only" and is_local)
