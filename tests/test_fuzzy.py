"""Tests for metafiles.fuzzy."""

import os
import random
import shutil
import subprocess
import tempfile
import time
import unittest

from metafiles import fuzzy
from metafiles.fuzzy import FuzzyIndex, build_index, fuzzy_filter, normalize, score

HAS_GIT = shutil.which("git") is not None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ScoreTest(unittest.TestCase):
    def test_srvut_ranking(self):
        paths = [
            "scripts/revert_utils.sh",
            "docs/srv/output.txt",
            "src/services/router/util_test.go",
            "src/server/utils.py",
        ]
        results = fuzzy_filter("srvut", paths)
        self.assertEqual(results[0][1], "src/server/utils.py")
        self.assertEqual(len(results), 4)

    def test_positions_point_to_matched_chars(self):
        candidate = "src/server/utils.py"
        result = score("srvut", candidate)
        self.assertIsNotNone(result)
        _, positions = result
        self.assertEqual("".join(candidate[i] for i in positions), "srvut")
        self.assertEqual(positions, sorted(positions))
        # Best placement: "srv" inside "server", "ut" at the start of "utils".
        self.assertEqual(positions, [4, 6, 7, 11, 12])

    def test_no_match(self):
        self.assertIsNone(score("xyz", "src/server/utils.py"))
        self.assertIsNone(score("tus", "uts"))
        self.assertIsNone(score("abcdef", "abc"))

    def test_case_insensitive(self):
        self.assertIsNotNone(score("README", "docs/readme.md"))
        self.assertIsNotNone(score("readme", "README.md"))

    def test_accent_insensitive(self):
        result = score("zolw", "Żółw.txt")
        self.assertIsNotNone(result)
        self.assertEqual(result[1], [0, 1, 2, 3])
        self.assertIsNotNone(score("łódź", "Lodz/plan.txt"))
        self.assertIsNotNone(score("lodz", "Łódź/plan.txt"))
        self.assertEqual(len(normalize("Żółw Łąka")), len("Żółw Łąka"))
        self.assertEqual(normalize("Żółw Łąka"), "zolw laka")

    def test_basename_preferred(self):
        results = fuzzy_filter("utils", ["utils/foo.py", "lib/utils.py", "lib/futils.py"])
        self.assertEqual(results[0][1], "lib/utils.py")

    def test_word_start_preferred(self):
        results = fuzzy_filter("main", ["src/domain/remains.txt", "src/main.py"])
        self.assertEqual(results[0][1], "src/main.py")

    def test_consecutive_preferred(self):
        results = fuzzy_filter("abc", ["a/xb/xc.txt", "x/abc.txt"])
        self.assertEqual(results[0][1], "x/abc.txt")

    def test_shorter_path_preferred(self):
        results = fuzzy_filter("app", ["deep/nested/folder/app.py", "app.py"])
        self.assertEqual(results[0][1], "app.py")

    def test_camel_case(self):
        results = fuzzy_filter("fb", ["fab.txt", "FooBar.java"])
        self.assertEqual(results[0][1], "FooBar.java")

    def test_directory_entries(self):
        result = score("serv", "src/server/")
        self.assertIsNotNone(result)
        self.assertEqual(result[1], [4, 5, 6, 7])

    def test_spaces_in_query_are_ignored(self):
        self.assertIsNotNone(score("server utils", "src/server/utils.py"))

    def test_regex_special_characters(self):
        self.assertIsNotNone(score("a[b]^-\\", "x/a[b]^-\\.txt"))
        self.assertIsNotNone(score(".*", "a.b*c"))
        self.assertIsNone(score(".*", "abc"))

    def test_empty_query(self):
        self.assertEqual(score("", "abc"), (0, []))
        self.assertEqual(fuzzy_filter("", ["a", "b"]), [(0, "a", []), (0, "b", [])])

    def test_limit(self):
        paths = [f"dir/file{i}.txt" for i in range(50)]
        self.assertEqual(len(fuzzy_filter("file", paths, limit=10)), 10)

    def test_fuzzy_filter_candidates(self):
        paths = ["src/server/utils.py", "docs/readme.md", "src/srv.c"]
        first = [p for _, p, _ in fuzzy_filter("sr", paths)]
        narrowed = fuzzy_filter("srvut", paths, candidates=first)
        self.assertEqual([p for _, p, _ in narrowed], ["src/server/utils.py"])


def _synthetic_paths(count, seed=42):
    rng = random.Random(seed)
    dirs = ["src", "lib", "tests", "docs", "app", "components", "server", "client", "utils",
            "models", "views", "controllers", "services", "api", "core", "common", "helpers",
            "config", "scripts", "assets", "styles", "images", "vendor", "internal", "pkg",
            "cmd", "handlers", "middleware", "router", "store", "hooks", "types", "i18n"]
    words = ["user", "account", "login", "session", "token", "cache", "index", "main",
             "config", "settings", "handler", "service", "manager", "factory", "util",
             "helper", "parser", "render", "button", "dialog", "window", "list", "item",
             "view", "model", "schema", "query", "request", "response", "error", "logger",
             "stream", "buffer", "file", "path", "image", "color", "theme", "layout", "grid"]
    exts = [".py", ".js", ".ts", ".tsx", ".go", ".rs", ".md", ".json", ".css", ".html",
            ".c", ".h", ".txt", ".yml"]
    seen = set()
    result = []
    while len(result) < count:
        depth = rng.randint(1, 6)
        folder = "/".join(
            rng.choice(dirs) + (str(rng.randint(1, 9)) if rng.random() < 0.2 else "")
            for _ in range(depth))
        name = rng.choice(["_", "-", ""]).join(rng.choice(words) for _ in range(rng.randint(1, 3)))
        if rng.random() < 0.3:
            name = name[:1].upper() + name[1:]
        path = f"{folder}/{name}{rng.choice(exts)}"
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = _synthetic_paths(100_000)
        cls.paths.append("src/server/utils.py")

    def test_incremental_equals_full(self):
        index = FuzzyIndex(self.paths)
        for query in ["s", "sr", "srv", "srvu", "srvut"]:
            incremental = index.search(query)
            full = FuzzyIndex(self.paths).search(query)
            self.assertEqual(incremental, full, query)
        # Prefilter narrowing matches a fresh prefilter exactly.
        broad = index.prefilter("ha")
        self.assertEqual(index.prefilter("handlr", broad), index.prefilter("handlr"))

    def test_search_finds_target(self):
        index = FuzzyIndex(self.paths)
        results = index.search("server/utils.py")
        self.assertEqual(results[0][1], "src/server/utils.py")

    def test_results_sorted(self):
        results = FuzzyIndex(self.paths).search("login")
        scores = [item[0] for item in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLessEqual(len(results), 200)

    def test_performance(self):
        index = FuzzyIndex(self.paths)
        timings = {}
        for query in ["srvut", "main", "cfgld", "handlr", "usr", "idx"]:
            index._last_query = None  # measure a cold (non-incremental) search
            start = time.perf_counter()
            index.search(query)
            timings[query] = time.perf_counter() - start
        print("\nfuzzy search over %d paths: %s" % (
            len(self.paths), ", ".join(f"{q}={t * 1000:.1f}ms" for q, t in timings.items())))
        for query, elapsed in timings.items():
            self.assertLess(elapsed, 0.25, query)


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="metafiles-fuzzy-")
        files = [
            "src/server/utils.py",
            "src/app.py",
            "README.md",
            ".hidden/secret.txt",
            ".env",
            "node_modules/pkg/index.js",
            "build/out.o",
        ]
        for rel in files:
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("x\n")
        os.makedirs(os.path.join(self.tmp, "empty"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_walk_without_git(self):
        paths = build_index(self.tmp, use_git=False)
        self.assertIn("src/server/utils.py", paths)
        self.assertIn("src/", paths)
        self.assertIn("src/server/", paths)
        self.assertIn("empty/", paths)
        self.assertIn("node_modules/pkg/index.js", paths)
        self.assertNotIn(".env", paths)
        self.assertFalse(any(p.startswith(".hidden") for p in paths))

        with_hidden = build_index(self.tmp, use_git=False, show_hidden=True)
        self.assertIn(".env", with_hidden)
        self.assertIn(".hidden/secret.txt", with_hidden)

    def test_walk_skips_dot_git_and_symlinks(self):
        os.makedirs(os.path.join(self.tmp, ".git", "objects"))
        os.symlink(os.path.join(self.tmp, "src"), os.path.join(self.tmp, "link"))
        paths = build_index(self.tmp, use_git=False, show_hidden=True)
        self.assertFalse(any(p.startswith(".git/") or p == ".git/" for p in paths))
        self.assertIn("link", paths)
        self.assertFalse(any(p.startswith("link/") for p in paths))

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_index(self):
        _git(self.tmp, "init", "-q")
        with open(os.path.join(self.tmp, ".gitignore"), "w") as handle:
            handle.write("node_modules/\nbuild/\n")
        paths = build_index(self.tmp, use_git=True)
        self.assertIn("src/server/utils.py", paths)
        self.assertIn("src/server/", paths)
        self.assertIn("src/", paths)
        self.assertIn("README.md", paths)
        self.assertFalse(any(p.startswith("node_modules") for p in paths))
        self.assertFalse(any(p.startswith("build") for p in paths))
        self.assertNotIn(".gitignore", paths)
        self.assertNotIn(".env", paths)
        self.assertFalse(any(p.startswith(".git/") for p in paths))
        # Directories are listed before their content.
        self.assertLess(paths.index("src/"), paths.index("src/app.py"))

        with_hidden = build_index(self.tmp, use_git=True, show_hidden=True)
        self.assertIn(".gitignore", with_hidden)
        self.assertIn(".hidden/secret.txt", with_hidden)
        self.assertFalse(any(p.startswith("node_modules") for p in with_hidden))

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_index_skips_deleted_tracked_files(self):
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "add", "src/app.py")
        os.remove(os.path.join(self.tmp, "src", "app.py"))
        paths = build_index(self.tmp, use_git=True)
        self.assertNotIn("src/app.py", paths)

    def test_async(self):
        from gi.repository import GLib

        loop = GLib.MainLoop()
        received = []

        def on_done(paths):
            received.append(paths)
            loop.quit()

        fuzzy.build_index_async(self.tmp, False, False, on_done)
        GLib.timeout_add_seconds(10, loop.quit)
        loop.run()
        self.assertEqual(len(received), 1)
        self.assertIn("src/app.py", received[0])


if __name__ == "__main__":
    unittest.main()
