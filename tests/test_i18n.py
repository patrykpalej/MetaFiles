"""Every UI string has a Polish translation, and Polish plural forms are chosen correctly."""

import ast
import pathlib
import unittest

from metafiles.i18n import detect_language
from metafiles.i18n_pl import PL

ROOT = pathlib.Path(__file__).resolve().parent.parent / "metafiles"


def _strings():
    singles, plurals = set(), set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            args = node.args
            if node.func.id == "_" and args and isinstance(args[0], ast.Constant):
                singles.add(args[0].value)
            if node.func.id == "ngettext" and len(args) >= 2 and isinstance(args[1], ast.Constant):
                plurals.add(args[1].value)
    return singles, plurals


class TranslationTest(unittest.TestCase):
    def test_all_strings_translated(self):
        singles, plurals = _strings()
        self.assertEqual(sorted(s for s in singles if s not in PL), [])
        self.assertEqual(sorted(p for p in plurals if not isinstance(PL.get(p), tuple)), [])

    def test_placeholders_kept(self):
        import re

        for source, target in PL.items():
            targets = target if isinstance(target, tuple) else (target,)
            for t in targets:
                self.assertEqual(set(re.findall(r"{\w+}", source)), set(re.findall(r"{\w+}", t)), source)

    def test_language_detection(self):
        self.assertEqual(detect_language({"LANG": "pl_PL.UTF-8"}), "pl")
        self.assertEqual(detect_language({"LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(detect_language({"LANG": "en_US.UTF-8", "LANGUAGE": "pl:en"}), "pl")

    def test_polish_plural_forms(self):
        import importlib
        import os

        os.environ["METAFILES_LANGUAGE"] = "pl"
        import metafiles.i18n as i18n

        importlib.reload(i18n)
        try:
            pick = lambda n: i18n.ngettext("{n} item", "{n} items", n).format(n=n)  # noqa: E731
            self.assertEqual([pick(n) for n in (1, 2, 5, 12, 22, 25)],
                             ["1 element", "2 elementy", "5 elementów", "12 elementów", "22 elementy", "25 elementów"])
        finally:
            del os.environ["METAFILES_LANGUAGE"]
            importlib.reload(i18n)


if __name__ == "__main__":
    unittest.main()
