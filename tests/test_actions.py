"""Tests for metafiles.actions."""

import json
import os
import stat
import subprocess
import tempfile
import unittest

from metafiles.actions import (
    DEFAULT_ACTIONS, CustomAction, expand_command, is_available, load_actions_file,
    matches, parse_actions,
)

NASTY = [
    "/tmp/with space/a b.txt",
    "/tmp/$HOME;rm -rf ~.txt",
    "/tmp/quo'te\"s`id`.txt",
    "/tmp/new\nline.txt",
    "/tmp/-dash.txt",
]


def action(command, contexts=("files",), **kw):
    return CustomAction(id="t", label="T", command=list(command), contexts=set(contexts), **kw)


class ParseTest(unittest.TestCase):
    def test_valid_full(self):
        actions, errors = parse_actions({"actions": [{
            "label": "Zmniejsz do 50%", "command": ["convert", "%f"], "context": "files",
            "icon": "img", "mime": "image/*", "extensions": [".PNG", "jpg"], "min": 1, "max": 3,
            "shortcut": "<Control>r", "terminal": True, "confirm": True, "enabled": False,
            "id": "resize",
        }]})
        self.assertEqual(errors, [])
        a = actions[0]
        self.assertEqual((a.id, a.label, a.command, a.contexts), ("resize", "Zmniejsz do 50%",
                                                                   ["convert", "%f"], {"files"}))
        self.assertEqual(a.mime_types, ["image/*"])
        self.assertEqual(a.extensions, ["png", "jpg"])
        self.assertEqual((a.min_count, a.max_count), (1, 3))
        self.assertTrue(a.terminal and a.confirm and not a.enabled and not a.shell)

    def test_string_command_split(self):
        actions, errors = parse_actions([{"label": "X", "command": "code --new-window '%F'",
                                          "context": ["files", "folders"]}])
        self.assertEqual(errors, [])
        self.assertEqual(actions[0].command, ["code", "--new-window", "%F"])
        self.assertEqual(actions[0].contexts, {"files", "folders"})

    def test_shell_string_kept(self):
        actions, _ = parse_actions([{"label": "X", "command": "ls %F | wc -l", "context": "files",
                                     "shell": True}])
        self.assertEqual(actions[0].command, ["ls %F | wc -l"])

    def test_default_id_slug_and_dedupe(self):
        actions, errors = parse_actions([
            {"label": "Otwórz w Łodzi", "command": "x", "context": "folder"},
            {"label": "Otwórz w Łodzi", "command": "y", "context": "folder"},
        ])
        self.assertEqual(errors, [])
        self.assertEqual([a.id for a in actions], ["otworz-w-lodzi", "otworz-w-lodzi-2"])

    def test_errors_skip_only_invalid(self):
        actions, errors = parse_actions([
            {"label": "Good", "command": "true", "context": "project"},
            {"label": "X", "command": "true", "context": "foo"},
            {"command": "true", "context": "files"},
            {"label": "NoCmd", "context": "files"},
            {"label": "NoCtx", "command": "true"},
            {"label": "BadQuote", "command": "echo 'x", "context": "files"},
            {"label": "BadMax", "command": "a", "context": "files", "min": 2, "max": 1},
            {"label": "BadBool", "command": "a", "context": "files", "shell": "yes"},
            {"label": "Mix", "command": "echo %n %F", "context": "files", "shell": True},
            "nonsense",
        ])
        self.assertEqual([a.label for a in actions], ["Good"])
        self.assertEqual(len(errors), 9)
        self.assertIn("Action “X”: unknown context 'foo'", errors)
        self.assertTrue(errors[1].startswith("Action “#3”: missing 'label'"))
        self.assertIn("Action “NoCmd”: missing 'command'", errors)
        self.assertIn("Action “NoCtx”: missing 'context'", errors)

    def test_defaults_parse(self):
        actions, errors = parse_actions(DEFAULT_ACTIONS)
        self.assertEqual(errors, [])
        labels = {a.label for a in actions}
        self.assertIn("Otwórz w VS Code", labels)
        self.assertIn("Otwórz w MetaTerminalu", labels)

    def test_load_actions_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            self.assertEqual(load_actions_file(path), ([], []))
            with open(path, "w") as fh:
                json.dump({"actions": DEFAULT_ACTIONS, "other": 1}, fh)
            actions, errors = load_actions_file(path)
            self.assertEqual((len(actions), errors), (len(DEFAULT_ACTIONS), []))
            with open(path, "w") as fh:
                fh.write("{broken")
            actions, errors = load_actions_file(path)
            self.assertEqual(actions, [])
            self.assertEqual(len(errors), 1)


class ExpandTest(unittest.TestCase):
    def test_placeholders_whole_args(self):
        argv, env = expand_command(action(["prog", "%F", "%f", "%n", "%d", "%p"]),
                                   NASTY, "/fold er", "/proj")
        names = [os.path.basename(p) for p in NASTY]
        self.assertEqual(argv, ["prog", *NASTY, NASTY[0], *names, "/fold er", "/proj"])
        self.assertEqual(env, {"METAFILES_FOLDER": "/fold er", "METAFILES_PROJECT": "/proj"})

    def test_placeholders_inside_args(self):
        argv, _ = expand_command(action(["prog", "--in=%f", "all:%F", "n:%n", "%d/x", "%%F", "100%", "%q"]),
                                 ["/a b", "/c"], "/d", None)
        self.assertEqual(argv, ["prog", "--in=/a b", "all:/a b /c", "n:a b c", "/d/x", "%F", "100%", "%q"])

    def test_project_falls_back_to_folder(self):
        argv, env = expand_command(action(["code", "%p"]), [], "/cur", None)
        self.assertEqual(argv, ["code", "/cur"])
        self.assertEqual(env["METAFILES_PROJECT"], "/cur")

    def test_empty_selection(self):
        argv, _ = expand_command(action(["prog", "%F", "%f", "%n"]), [], "/d", None)
        self.assertEqual(argv, ["prog"])

    def test_paths_verbatim_when_executed(self):
        argv, _ = expand_command(action(["printf", "%s\\0", "%F"]), NASTY, "/d", None)
        out = subprocess.run(argv, capture_output=True, check=True).stdout
        self.assertEqual(out.decode().split("\0")[:-1], NASTY)

    def test_shell_mode_never_interpolates_paths(self):
        a = action(["for f in %F; do printf '%%s|%%s\\0' \"$f\" %d; done; echo %p"], shell=True)
        argv, env = expand_command(a, NASTY, "/fold er", "/pro;ject")
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        script = argv[2]
        for p in NASTY + ["/fold er", "/pro;ject"]:
            self.assertNotIn(p, script)
        self.assertEqual(argv[3], "metafiles-action")
        self.assertEqual(argv[4:], NASTY)
        self.assertIn('"$@"', script)
        self.assertIn('"$METAFILES_FOLDER"', script)
        self.assertIn('"$METAFILES_PROJECT"', script)
        out = subprocess.run(argv, capture_output=True, check=True,
                             env={**os.environ, **env}).stdout.decode()
        records, last = out.rsplit("\0", 1)
        self.assertEqual(records.split("\0"), [f"{p}|/fold er" for p in NASTY])
        self.assertEqual(last, "/pro;ject\n")

    def test_shell_first_and_names(self):
        argv, _ = expand_command(action(["cat %f"], shell=True), NASTY, "/d", None)
        self.assertEqual(argv[2], 'cat "$1"')
        argv, _ = expand_command(action(["echo %n"], shell=True), NASTY, "/d", None)
        self.assertEqual(argv[2], 'echo "$@"')
        self.assertEqual(argv[4:], [os.path.basename(p) for p in NASTY])


class MatchTest(unittest.TestCase):
    IMG = ("/a/x.png", "image/png", False)
    TXT = ("/a/y.TXT", "text/plain", False)
    DIR = ("/a/dir", "inode/directory", True)

    def test_kinds(self):
        files = action(["x"], ("files",))
        self.assertTrue(matches(files, "files", [self.IMG, self.TXT]))
        self.assertFalse(matches(files, "files", [self.IMG, self.DIR]))
        self.assertFalse(matches(files, "folders", [self.DIR]))
        both = action(["x"], ("files", "folders"))
        self.assertTrue(matches(both, "folders", [self.DIR]))
        self.assertTrue(matches(both, "files", [self.IMG, self.DIR]))

    def test_count(self):
        a = action(["x"], ("files",), min_count=2, max_count=2)
        self.assertFalse(matches(a, "files", [self.IMG]))
        self.assertTrue(matches(a, "files", [self.IMG, self.TXT]))
        self.assertFalse(matches(a, "files", [self.IMG, self.TXT, self.IMG]))
        self.assertFalse(matches(action(["x"]), "files", []))

    def test_mime_filter(self):
        a = action(["x"], ("files",), mime_types=["image/*"])
        self.assertTrue(matches(a, "files", [self.IMG]))
        self.assertFalse(matches(a, "files", [self.IMG, self.TXT]))

    def test_extension_filter(self):
        a = action(["x"], ("files",), extensions=["txt", "tar.gz"])
        self.assertTrue(matches(a, "files", [self.TXT, ("/b/c.tar.gz", "application/gzip", False)]))
        self.assertFalse(matches(a, "files", [self.IMG]))

    def test_folder_and_project_contexts(self):
        a = action(["code", "%p"], ("project",))
        self.assertTrue(matches(a, "project", []))
        self.assertFalse(matches(a, "folder", []))
        self.assertFalse(matches(a, "files", [self.TXT]))

    def test_disabled(self):
        self.assertFalse(matches(action(["x"], ("project",), enabled=False), "project", []))


class AvailabilityTest(unittest.TestCase):
    def test_path_lookup(self):
        self.assertTrue(is_available(action(["sh"])))
        self.assertFalse(is_available(action(["definitely-not-a-program-xyz"])))
        self.assertTrue(is_available(action(["definitely-not-a-program-xyz"], shell=True)))

    def test_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            prog = os.path.join(d, "tool")
            self.assertFalse(is_available(action([prog])))
            with open(prog, "w") as fh:
                fh.write("#!/bin/sh\n")
            self.assertFalse(is_available(action([prog])))
            os.chmod(prog, stat.S_IRWXU)
            self.assertTrue(is_available(action([prog])))


if __name__ == "__main__":
    unittest.main()
