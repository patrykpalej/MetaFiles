"""Nautilus' settings are read (not written) and changes are reported live."""

import os
import unittest

os.environ["GSETTINGS_BACKEND"] = "memory"

from gi.repository import Gio, GLib  # noqa: E402

from metafiles.nautilus_settings import NautilusSettings, schema_exists  # noqa: E402


@unittest.skipUnless(schema_exists("org.gnome.nautilus.preferences"), "Nautilus schemas not installed")
class NautilusSettingsTest(unittest.TestCase):
    def test_reads_and_follows_changes(self):
        settings = NautilusSettings()
        seen = []
        settings.connect("changed", lambda _s, group, key: seen.append((group, key)))
        prefs = Gio.Settings.new("org.gnome.nautilus.preferences")
        prefs.set_string("click-policy", "single")
        prefs.set_string("default-folder-viewer", "list-view")
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        self.assertTrue(settings.single_click)
        self.assertIn(("preferences", "click-policy"), seen)
        self.assertIn(settings.default_view, ("list", "tree"))

    def test_zoom_and_sort_values(self):
        settings = NautilusSettings()
        self.assertIn(settings.zoom("grid"), ("small", "small-plus", "medium", "large", "extra-large"))
        key, reverse = settings.sort
        self.assertIsInstance(reverse, bool)
        self.assertTrue(key)


if __name__ == "__main__":
    unittest.main()
