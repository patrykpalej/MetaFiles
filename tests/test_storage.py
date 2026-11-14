import json
import os
import tempfile
import unittest

from metafiles.storage import (
    DEFAULT_SHORTCUTS, LOOSE_NAME, ProjectState, SessionState, load_config, load_session, new_tab_state,
    normalize_tab, save_session,
)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_session_gives_general_entry(self):
        state = load_session(self.dir)
        self.assertEqual(len(state.projects), 1)
        self.assertTrue(state.projects[0].loose)
        self.assertEqual(state.projects[0].name, LOOSE_NAME)
        self.assertFalse(state.projects[0].lock)

    def test_round_trip_with_tabs_and_bookmarks(self):
        tab = new_tab_state("file:///srv/api/src", "tree")
        tab["panes"].append({"location": "file:///srv/api", "view": "grid"})
        tab.update({"preview": True, "terminal": True, "name": "Serwer"})
        state = SessionState(projects=[ProjectState(path="/srv/api", name="api", lock=False, tabs=[tab],
                                                    bookmarks=["/srv/api/src"], group="Praca")],
                             groups=["Praca", "Pusta"], show_hidden=True).ensure_loose()
        save_session(state, self.dir)
        loaded = load_session(self.dir)
        self.assertEqual(loaded, state)
        self.assertEqual(loaded.groups, ["Praca", "Pusta"])  # empty groups are kept
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".tmp-")], [])

    def test_order_general_ungrouped_groups(self):
        state = SessionState(projects=[
            ProjectState(path="/a", name="a", group="G"), ProjectState(path="/b", name="b"),
            ProjectState(path="/c", name="c", group="G"),
        ]).ensure_loose()
        self.assertEqual([p.name for p in state.projects], [LOOSE_NAME, "b", "a", "c"])

    def test_broken_entries_are_ignored(self):
        with open(os.path.join(self.dir, "session.json"), "w") as f:
            json.dump({"projects": [{"path": "/x/y", "name": "", "tabs": ["file:///x/y", 3, {"panes": []}]},
                                    {"oops": 1}, 3], "places_mode": "weird", "future": True}, f)
        state = load_session(self.dir)
        self.assertEqual(state.projects[1].name, "y")
        self.assertEqual(len(state.projects[1].tabs), 1)
        self.assertEqual(state.places_mode, "places")

    def test_normalize_tab_limits_panes(self):
        tab = normalize_tab({"panes": [{"location": "a"}, {"location": "b"}, {"location": "c"}], "active_pane": 5,
                             "name": 3})
        self.assertEqual(len(tab["panes"]), 2)
        self.assertEqual(tab["active_pane"], 0)
        self.assertEqual(tab["name"], "")

    def test_config_created_with_defaults_and_overrides(self):
        config = load_config(self.dir)
        self.assertEqual(config["shortcuts"]["next-project"], DEFAULT_SHORTCUTS["next-project"])
        self.assertTrue(config["actions"])
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump({"shortcuts": {"quick-open": "<Control><Shift>p", "toggle-split": ["disabled"]}}, f)
        config = load_config(self.dir)
        self.assertEqual(config["shortcuts"]["quick-open"], ["<Control><Shift>p"])
        self.assertEqual(config["shortcuts"]["toggle-split"], [])


if __name__ == "__main__":
    unittest.main()
