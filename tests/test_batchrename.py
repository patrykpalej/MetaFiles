"""Tests for metafiles.batchrename."""

import os
import tempfile
import unittest
from unittest import mock

from metafiles.batchrename import (
    TEMP_PREFIX, RenameOptions, apply_plan, compute_new_names, plan_renames,
    split_name, undo_pairs, validate,
)


class SplitNameTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "a.txt": ("a", ".txt"),
            "photo.JPG": ("photo", ".JPG"),
            ".bashrc": (".bashrc", ""),
            ".config.json": (".config", ".json"),
            "README": ("README", ""),
            "trailing.": ("trailing.", ""),
            "backup.tar.gz": ("backup", ".tar.gz"),
            "x.TAR.XZ": ("x", ".TAR.XZ"),
            "a.tar.bz2": ("a", ".tar.bz2"),
            "a.tar.zst": ("a", ".tar.zst"),
            "a.b.c": ("a.b", ".c"),
            "Notes v1.2 final": ("Notes v1.2 final", ""),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(split_name(name), expected)


class ComputeTest(unittest.TestCase):
    def names(self, names, **kw):
        return compute_new_names(names, RenameOptions(**kw))

    def test_default_is_identity(self):
        self.assertEqual(self.names(["a.txt", ".bashrc"]), ["a.txt", ".bashrc"])

    def test_plain_find_replace_stem_only(self):
        self.assertEqual(self.names(["txt.txt"], find="txt", replace="doc"), ["doc.txt"])

    def test_find_case_insensitive(self):
        self.assertEqual(self.names(["Foo foo.txt"], find="FOO", replace="x", case_sensitive=False),
                         ["x x.txt"])
        self.assertEqual(self.names(["Foo.txt"], find="FOO", replace="x"), ["Foo.txt"])

    def test_plain_replace_is_literal(self):
        self.assertEqual(self.names(["a.b"], find="a", replace=r"\1$1"), [r"\1$1.b"])

    def test_include_extension(self):
        self.assertEqual(self.names(["a.txt"], find="txt", replace="md", include_extension=True),
                         ["a.md"])
        self.assertEqual(self.names(["a.txt"], case="upper", include_extension=True), ["A.TXT"])

    def test_regex_dollar_and_backslash_refs(self):
        for repl in ("x-$1", "x-${1}", r"x-\1", r"x-\g<1>"):
            with self.subTest(repl=repl):
                self.assertEqual(self.names(["ab12.txt"], find=r"ab(\d+)", replace=repl, regex=True),
                                 ["x-12.txt"])

    def test_regex_named_and_escapes(self):
        self.assertEqual(
            self.names(["ab"], find=r"(?P<x>a)b", replace=r"${x}$$\\", regex=True), ["a$\\"])

    def test_regex_errors(self):
        with self.assertRaises(ValueError):
            self.names(["a"], find="(", regex=True)
        with self.assertRaises(ValueError):
            self.names(["a"], find="(a)", replace="$2", regex=True)

    def test_prefix_suffix(self):
        self.assertEqual(self.names(["a.txt", "b"], prefix="p-", suffix="-s"), ["p-a-s.txt", "p-b-s"])

    def test_numbering(self):
        self.assertEqual(
            self.names(["a.txt", "b.txt"], numbering=True, start=9, padding=3),
            ["a 009.txt", "b 010.txt"])
        self.assertEqual(
            self.names(["a.txt"], numbering=True, number_format="{n}_{i}_{name}", start=5, padding=2),
            ["05_5_a.txt"])

    def test_case(self):
        self.assertEqual(self.names(["mY fiLe.TXT"], case="lower"), ["my file.TXT"])
        self.assertEqual(self.names(["my file.txt"], case="upper"), ["MY FILE.txt"])
        self.assertEqual(self.names(["my fILE-two_x.txt"], case="title"), ["My File-Two_X.txt"])

    def test_extension_modes(self):
        self.assertEqual(self.names(["a.JPG"], extension="lower"), ["a.jpg"])
        self.assertEqual(self.names(["a.jpg"], extension="upper"), ["a.JPG"])
        self.assertEqual(self.names(["a.jpg", "b"], extension="remove"), ["a", "b"])
        self.assertEqual(self.names(["a.jpeg", "b"], extension="jpg"), ["a.jpg", "b.jpg"])
        self.assertEqual(self.names(["a.jpeg"], extension=".png"), ["a.png"])
        self.assertEqual(self.names(["a.tar.gz"], extension="upper"), ["a.TAR.GZ"])

    def test_spec_scenario(self):
        names = [f"IMG_{i:04d}.JPG" for i in range(1, 41)]
        new = self.names(names, find=r"IMG_(\d+)", replace="wakacje-$1", regex=True, extension="lower")
        self.assertEqual(new[0], "wakacje-0001.jpg")
        self.assertEqual(new[-1], "wakacje-0040.jpg")
        self.assertEqual(validate(names, new, set(names)), [None] * 40)


class ValidateTest(unittest.TestCase):
    def test_codes(self):
        old = ["a", "b", "c", "d", "e", "f", "g", "h"]
        new = ["", "x/y", "..", "dup", "dup", "taken", "b", "h"]
        existing = set(old) | {"taken"}
        self.assertEqual(validate(old, new, existing),
                         ["empty", "invalid", "invalid", "duplicate", "duplicate", "exists", None, None])

    def test_nul_and_too_long(self):
        self.assertEqual(validate(["a", "b"], ["x\0", "y" * 256], set()), ["invalid", "invalid"])

    def test_swap_is_valid(self):
        self.assertEqual(validate(["a", "b"], ["b", "a"], {"a", "b"}), [None, None])

    def test_new_equal_to_unchanged_item_is_duplicate(self):
        self.assertEqual(validate(["a", "b"], ["b", "b"], {"a", "b"}), ["duplicate", "duplicate"])


class PlanApplyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, *names):
        for name in names:
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write(name)

    def contents(self):
        result = {}
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name)) as fh:
                result[name] = fh.read()
        return result

    def test_plan_drops_unchanged_and_orders_chain(self):
        steps = plan_renames([("a", "b"), ("b", "c"), ("x", "x")])
        self.assertEqual(steps, [("b", "c"), ("a", "b")])

    def test_plan_rejects_duplicate_targets(self):
        with self.assertRaises(ValueError):
            plan_renames([("a", "c"), ("b", "c")])

    def test_swap(self):
        self.make("a", "b")
        steps = plan_renames([("a", "b"), ("b", "a")])
        self.assertTrue(any(new.startswith(TEMP_PREFIX) for _, new in steps))
        pairs = apply_plan(self.dir, steps)
        self.assertEqual(self.contents(), {"a": "b", "b": "a"})
        self.assertEqual(sorted(pairs), [("a", "b"), ("b", "a")])

    def test_three_cycle_and_undo(self):
        self.make("a", "b", "c", "other")
        pairs = apply_plan(self.dir, plan_renames([("a", "b"), ("b", "c"), ("c", "a")]))
        self.assertEqual(self.contents(), {"b": "a", "c": "b", "a": "c", "other": "other"})
        apply_plan(self.dir, plan_renames(undo_pairs(pairs)))
        self.assertEqual(self.contents(), {"a": "a", "b": "b", "c": "c", "other": "other"})

    def test_undo_round_trip_spec(self):
        names = [f"IMG_{i:04d}.JPG" for i in range(1, 6)]
        self.make(*names)
        new = compute_new_names(names, RenameOptions(find=r"IMG_(\d+)", replace=r"wakacje-\1",
                                                     regex=True, extension="lower"))
        pairs = apply_plan(self.dir, plan_renames(list(zip(names, new))))
        self.assertEqual(sorted(self.contents()), sorted(new))
        apply_plan(self.dir, plan_renames(undo_pairs(pairs)))
        self.assertEqual(sorted(self.contents()), sorted(names))

    def test_refuses_overwrite_and_rolls_back(self):
        self.make("a", "b", "c")
        # c -> x is done first, then a -> b hits the existing "b".
        with self.assertRaises(FileExistsError):
            apply_plan(self.dir, [("c", "x"), ("a", "b")])
        self.assertEqual(self.contents(), {"a": "a", "b": "b", "c": "c"})

    def test_rollback_on_os_error(self):
        self.make("a", "b")
        real_rename = os.rename
        calls = []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:
                raise PermissionError("denied")
            return real_rename(src, dst)

        with mock.patch("metafiles.batchrename.os.rename", side_effect=flaky):
            with self.assertRaises(PermissionError):
                apply_plan(self.dir, plan_renames([("a", "b"), ("b", "a")]))
        self.assertEqual(self.contents(), {"a": "a", "b": "b"})


if __name__ == "__main__":
    unittest.main()
