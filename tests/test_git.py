"""Tests for metafiles.git."""

import os
import shutil
import subprocess
import tempfile
import unittest

from metafiles import git as mgit
from metafiles.git import (
    GitManager,
    GitRepo,
    RepoStatus,
    build_folder_map,
    find_repo_root,
    is_ignored_rel,
    parse_ignored,
    parse_ignored_entries,
    parse_status_v2,
    run_status_sync,
    status_from_xy,
)

HAS_GIT = shutil.which("git") is not None
OID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
H = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def _ordinary(xy, path):
    return f"1 {xy} N... 100644 100644 100644 {H} {H} {path}".encode()


def _status_bytes(*records):
    return b"\0".join(records) + b"\0"


class ParseStatusTest(unittest.TestCase):
    def test_branch_headers(self):
        data = _status_bytes(
            f"# branch.oid {OID}".encode(),
            b"# branch.head feature/login",
            b"# branch.upstream origin/feature/login",
            b"# branch.ab +2 -0",
        )
        status = parse_status_v2(data)
        self.assertEqual(status.branch, "feature/login")
        self.assertEqual(status.oid, OID)
        self.assertEqual(status.upstream, "origin/feature/login")
        self.assertEqual((status.ahead, status.behind), (2, 0))
        self.assertEqual(status.branch_label(), "feature/login ↑2")
        self.assertFalse(status.dirty)
        self.assertEqual(status.entries, {})

    def test_ahead_behind_label(self):
        status = parse_status_v2(_status_bytes(
            f"# branch.oid {OID}".encode(), b"# branch.head main",
            b"# branch.upstream origin/main", b"# branch.ab +1 -3"))
        self.assertEqual(status.branch_label(), "main ↑1 ↓3")

    def test_detached(self):
        status = parse_status_v2(_status_bytes(
            f"# branch.oid {OID}".encode(), b"# branch.head (detached)"))
        self.assertIsNone(status.branch)
        self.assertEqual(status.branch_label(), "a1b2c3d")

    def test_initial_commit(self):
        status = parse_status_v2(_status_bytes(b"# branch.oid (initial)", b"# branch.head main"))
        self.assertIsNone(status.oid)
        self.assertEqual(status.branch_label(), "main")

    def test_ordinary_entries(self):
        data = _status_bytes(
            f"# branch.oid {OID}".encode(),
            b"# branch.head main",
            _ordinary(".M", "src/app.py"),
            _ordinary("M.", "staged.py"),
            _ordinary("A.", "new.py"),
            _ordinary("AM", "new_modified.py"),
            _ordinary(".D", "gone.py"),
            _ordinary("D.", "removed.py"),
            _ordinary(".T", "typechange"),
            _ordinary("MM", "both.py"),
            _ordinary("AD", "added_then_deleted.py"),
            _ordinary(".M", "dir with spaces/file name.txt"),
        )
        entries = parse_status_v2(data).entries
        self.assertEqual(entries["src/app.py"], "modified")
        self.assertEqual(entries["staged.py"], "modified")
        self.assertEqual(entries["new.py"], "added")
        self.assertEqual(entries["new_modified.py"], "modified")
        self.assertEqual(entries["gone.py"], "deleted")
        self.assertEqual(entries["removed.py"], "deleted")
        self.assertEqual(entries["typechange"], "modified")
        self.assertEqual(entries["both.py"], "modified")
        self.assertEqual(entries["added_then_deleted.py"], "deleted")
        self.assertEqual(entries["dir with spaces/file name.txt"], "modified")

    def test_rename_entries(self):
        data = _status_bytes(
            f"# branch.oid {OID}".encode(),
            b"# branch.head main",
            f"2 R. N... 100644 100644 100644 {H} {H} R100 docs/new name.md".encode(),
            b"docs/old name.md",
            f"2 RM N... 100644 100644 100644 {H} {H} R87 b.py".encode(),
            b"a.py",
            f"2 C. N... 100644 100644 100644 {H} {H} C100 copy.txt".encode(),
            b"orig.txt",
            _ordinary(".M", "after.py"),
        )
        status = parse_status_v2(data)
        self.assertEqual(status.entries["docs/new name.md"], "renamed")
        self.assertEqual(status.entries["b.py"], "modified")
        self.assertEqual(status.entries["copy.txt"], "renamed")
        self.assertEqual(status.entries["after.py"], "modified")
        self.assertNotIn("docs/old name.md", status.entries)
        self.assertNotIn("a.py", status.entries)
        self.assertEqual(status.renames["docs/new name.md"], "docs/old name.md")
        self.assertEqual(status.renames["b.py"], "a.py")

    def test_unmerged_entries(self):
        records = [
            f"u {xy} N... 100644 100644 100644 100644 {H} {H} {H} c{i}.txt".encode()
            for i, xy in enumerate(["UU", "AA", "DD", "AU", "UA", "DU", "UD"])
        ]
        entries = parse_status_v2(_status_bytes(*records)).entries
        self.assertEqual(len(entries), 7)
        self.assertTrue(all(value == "conflicted" for value in entries.values()))

    def test_untracked_and_ignored(self):
        data = _status_bytes(b"? new dir/file.txt", b"! node_modules/", b"! debug.log")
        status = parse_status_v2(data)
        self.assertEqual(status.entries["new dir/file.txt"], "untracked")
        self.assertEqual(status.entries["node_modules"], "ignored")
        self.assertEqual(status.entries["debug.log"], "ignored")
        self.assertTrue(status.dirty)
        only_ignored = parse_status_v2(_status_bytes(b"! debug.log"))
        self.assertFalse(only_ignored.dirty)

    def test_utf8_paths(self):
        status = parse_status_v2(_status_bytes("? Żółw/łąka.txt".encode()))
        self.assertIn("Żółw/łąka.txt", status.entries)

    def test_empty(self):
        status = parse_status_v2(b"")
        self.assertEqual(status, RepoStatus())

    def test_status_from_xy(self):
        self.assertEqual(status_from_xy("UU"), "conflicted")
        self.assertEqual(status_from_xy("R."), "renamed")
        self.assertEqual(status_from_xy("A."), "added")
        self.assertEqual(status_from_xy("M."), "modified")
        self.assertEqual(status_from_xy(".M"), "modified")
        self.assertEqual(status_from_xy("RD"), "deleted")


class FolderMapTest(unittest.TestCase):
    def test_aggregation(self):
        entries = {
            "src/app.py": "modified",
            "src/new.py": "untracked",
            "src/deep/x/conflict.txt": "conflicted",
            "docs/a.md": "added",
            "docs/b.md": "untracked",
            "README.md": "deleted",
            "node_modules": "ignored",
            "build/out/x.o": "ignored",
        }
        folders = build_folder_map(entries)
        self.assertEqual(folders[""], "conflicted")
        self.assertEqual(folders["src"], "conflicted")
        self.assertEqual(folders["src/deep"], "conflicted")
        self.assertEqual(folders["src/deep/x"], "conflicted")
        self.assertEqual(folders["docs"], "added")
        self.assertNotIn("build", folders)
        self.assertNotIn("build/out", folders)
        self.assertNotIn("node_modules", folders)

    def test_order_independent(self):
        entries = [("a/b/c.txt", "untracked"), ("a/b/d.txt", "modified"), ("a/e.txt", "added")]
        forward = build_folder_map(dict(entries))
        backward = build_folder_map(dict(reversed(entries)))
        self.assertEqual(forward, backward)
        self.assertEqual(forward, {"": "modified", "a": "modified", "a/b": "modified"})

    def test_empty(self):
        self.assertEqual(build_folder_map({}), {})


class IgnoredTest(unittest.TestCase):
    def test_parse(self):
        data = b"node_modules/\0build/out/\0debug.log\0"
        self.assertEqual(parse_ignored(data), {"node_modules", "build/out", "debug.log"})
        ignored, dirs = parse_ignored_entries(data)
        self.assertEqual(dirs, {"node_modules", "build/out"})
        self.assertEqual(parse_ignored(b""), set())

    def test_is_ignored_rel(self):
        ignored = {"node_modules", "build/out", "debug.log"}
        self.assertTrue(is_ignored_rel("node_modules", ignored))
        self.assertTrue(is_ignored_rel("node_modules/pkg/index.js", ignored))
        self.assertTrue(is_ignored_rel("build/out/a/b.o", ignored))
        self.assertTrue(is_ignored_rel("debug.log", ignored))
        self.assertFalse(is_ignored_rel("build", ignored))
        self.assertFalse(is_ignored_rel("build/outside.txt", ignored))
        self.assertFalse(is_ignored_rel("src/node_modules.txt", ignored))
        self.assertFalse(is_ignored_rel("", ignored))


class FindRepoRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="metafiles-git-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dir_and_file(self):
        os.makedirs(os.path.join(self.tmp, "repo", ".git"))
        os.makedirs(os.path.join(self.tmp, "repo", "src", "deep"))
        target = os.path.join(self.tmp, "repo", "src", "deep", "a.py")
        open(target, "w").close()
        root = os.path.join(self.tmp, "repo")
        self.assertEqual(find_repo_root(root), root)
        self.assertEqual(find_repo_root(os.path.join(root, "src", "deep")), root)
        self.assertEqual(find_repo_root(target), root)
        self.assertIsNone(find_repo_root(os.path.join(root, ".git")))

    def test_dotgit_file_and_nested(self):
        outer = os.path.join(self.tmp, "outer")
        inner = os.path.join(outer, "sub")
        os.makedirs(os.path.join(outer, ".git"))
        os.makedirs(os.path.join(inner, "lib"))
        with open(os.path.join(inner, ".git"), "w") as handle:
            handle.write("gitdir: ../.git/modules/sub\n")
        self.assertEqual(find_repo_root(os.path.join(inner, "lib")), inner)
        self.assertEqual(find_repo_root(outer), outer)

    def test_symlink_resolves(self):
        os.makedirs(os.path.join(self.tmp, "repo", ".git"))
        link = os.path.join(self.tmp, "link")
        os.symlink(os.path.join(self.tmp, "repo"), link)
        self.assertEqual(find_repo_root(link), os.path.join(self.tmp, "repo"))

    def test_outside(self):
        if find_repo_root(os.path.dirname(self.tmp)) is None:
            self.assertIsNone(find_repo_root(self.tmp))
        self.assertIsNone(find_repo_root(""))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


class RepoFixture:
    """Creates a temporary repository with every kind of change."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="metafiles-repo-"))
        root = self.root
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "commit.gpgsign", "false")
        _write(root, ".gitignore", "node_modules/\n*.log\n")
        _write(root, "README.md", "hello\n")
        _write(root, "src/app.py", "print(1)\n")
        _write(root, "src/staged.py", "a = 1\n")
        _write(root, "docs/old.md", "old\n")
        _write(root, "gone.txt", "bye\n")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "initial")
        _write(root, "src/app.py", "print(2)\n")             # modified
        _write(root, "src/staged.py", "a = 2\n")             # staged modification
        _git(root, "add", "src/staged.py")
        _write(root, "src/new/added.py", "x\n")              # staged new file
        _git(root, "add", "src/new/added.py")
        _write(root, "notes/todo.txt", "todo\n")             # untracked
        _git(root, "mv", "docs/old.md", "docs/new.md")       # renamed
        os.remove(os.path.join(root, "gone.txt"))            # deleted
        _write(root, "node_modules/pkg/index.js", "x\n")     # ignored dir
        _write(root, "debug.log", "log\n")                   # ignored file

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipUnless(HAS_GIT, "git is not installed")
class IntegrationTest(RepoFixture, unittest.TestCase):
    def test_run_status_sync(self):
        status, ignored = run_status_sync(self.root)
        self.assertEqual(status.branch, "main")
        self.assertIsNotNone(status.oid)
        self.assertTrue(status.dirty)
        self.assertEqual(status.entries["src/app.py"], "modified")
        self.assertEqual(status.entries["src/staged.py"], "modified")
        self.assertEqual(status.entries["src/new/added.py"], "added")
        self.assertEqual(status.entries["notes/todo.txt"], "untracked")
        self.assertEqual(status.entries["docs/new.md"], "renamed")
        self.assertEqual(status.renames["docs/new.md"], "docs/old.md")
        self.assertEqual(status.entries["gone.txt"], "deleted")
        self.assertNotIn("node_modules/pkg/index.js", status.entries)
        self.assertEqual(ignored, {"node_modules", "debug.log"})
        folders = build_folder_map(status.entries)
        self.assertEqual(folders["src"], "modified")
        self.assertEqual(folders["src/new"], "added")
        self.assertEqual(folders["notes"], "untracked")
        self.assertTrue(is_ignored_rel("node_modules/pkg/index.js", ignored))
        self.assertEqual(find_repo_root(os.path.join(self.root, "src", "app.py")), self.root)

    def test_conflict(self):
        root = self.root
        _git(root, "stash", "-u", "-q")
        _git(root, "checkout", "-q", "-b", "other")
        _write(root, "README.md", "other\n")
        _git(root, "commit", "-q", "-am", "other")
        _git(root, "checkout", "-q", "main")
        _write(root, "README.md", "main\n")
        _git(root, "commit", "-q", "-am", "main")
        subprocess.run(["git", "merge", "-q", "other"], cwd=root, capture_output=True)
        status, _ = run_status_sync(root)
        self.assertEqual(status.entries["README.md"], "conflicted")
        self.assertEqual(build_folder_map(status.entries)[""], "conflicted")

    def test_detached_and_upstream(self):
        root = self.root
        _git(root, "stash", "-u", "-q")
        clone = self.root + "-clone"
        self.addCleanup(shutil.rmtree, clone, True)
        subprocess.run(["git", "clone", "-q", root, clone], check=True, capture_output=True)
        _git(clone, "config", "user.name", "Test")
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "commit.gpgsign", "false")
        _write(clone, "a.txt", "1\n")
        _git(clone, "add", "a.txt")
        _git(clone, "commit", "-q", "-m", "one")
        _write(clone, "b.txt", "1\n")
        _git(clone, "add", "b.txt")
        _git(clone, "commit", "-q", "-m", "two")
        status, _ = run_status_sync(clone)
        self.assertEqual(status.upstream, "origin/main")
        self.assertEqual(status.branch_label(), "main ↑2")
        self.assertFalse(status.dirty)
        _git(clone, "checkout", "-q", "--detach", "HEAD~1")
        status, _ = run_status_sync(clone)
        self.assertIsNone(status.branch)
        self.assertEqual(status.branch_label(), status.oid[:7])


@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitRepoAsyncTest(RepoFixture, unittest.TestCase):
    """Exercise the Gio.Subprocess based GitRepo on the same repository."""

    def setUp(self):
        super().setUp()
        from gi.repository import GLib
        self.GLib = GLib
        self.repo = GitRepo(self.root)
        self.addCleanup(self.repo.stop)

    def _run_loop(self, start, timeout=10):
        loop = self.GLib.MainLoop()
        result = {}

        def quit_later(*_args):
            loop.quit()
            return False

        start(result, quit_later)
        timer = self.GLib.timeout_add_seconds(timeout, quit_later)
        loop.run()
        if "timeout" not in result:
            try:
                self.GLib.source_remove(timer)
            except Exception:  # noqa: BLE001
                pass
        return result

    def _refresh(self):
        def start(result, done):
            handler = self.repo.connect("changed", lambda *_: (result.update(changed=True), done()))
            result["handler"] = handler
            self.repo.refresh(0)
        result = self._run_loop(start)
        self.repo.disconnect(result["handler"])
        self.assertTrue(result.get("changed"))

    def _action(self, method, paths):
        def start(result, done):
            def callback(ok, output):
                result["ok"] = ok
                result["output"] = output
                done()
            method(paths, callback)
        result = self._run_loop(start)
        self.assertIn("ok", result)
        return result["ok"], result["output"]

    def test_refresh_and_queries(self):
        self._refresh()
        repo = self.repo
        self.assertEqual(repo.status.branch, "main")
        join = lambda rel: os.path.join(self.root, rel)  # noqa: E731
        self.assertEqual(repo.status_for(join("src/app.py")), "modified")
        self.assertEqual(repo.status_for(join("src"), is_dir=True), "modified")
        self.assertEqual(repo.status_for(join("src")), "modified")
        self.assertEqual(repo.status_for(self.root, is_dir=True), "modified")
        self.assertIsNone(repo.status_for(join("README.md")))
        self.assertEqual(repo.status_for(join("node_modules"), is_dir=True), "ignored")
        self.assertEqual(repo.status_for(join("node_modules/pkg/index.js")), "ignored")
        self.assertTrue(repo.is_ignored(join("node_modules/pkg/index.js")))
        self.assertTrue(repo.is_ignored(join("debug.log")))
        self.assertFalse(repo.is_ignored(join("src/app.py")))
        self.assertIsNone(repo.status_for("/definitely/outside"))
        self.assertEqual(repo.relpath(join("src/app.py")), "src/app.py")
        self.assertEqual(repo.relpath(self.root), "")
        self.assertIsNone(repo.relpath("/definitely/outside"))
        changed = set(repo.changed_files())
        self.assertIn(join("src/app.py"), changed)
        self.assertIn(join("notes/todo.txt"), changed)
        self.assertNotIn(join("debug.log"), changed)

    def test_coalesced_refresh(self):
        count = []
        self.repo.connect("changed", lambda *_: count.append(1))

        def start(result, done):
            self.repo.refresh(0)
            self.repo.refresh(0)
            self.GLib.timeout_add(1500, done)
        self._run_loop(start)
        self.assertGreaterEqual(len(count), 1)
        self.assertLessEqual(len(count), 2)

    def test_actions(self):
        self._refresh()
        join = lambda rel: os.path.join(self.root, rel)  # noqa: E731

        ok, output = self._action(self.repo.diff, [join("src/app.py")])
        self.assertTrue(ok)
        self.assertIn("-print(1)", output)
        self.assertIn("+print(2)", output)

        ok, output = self._action(self.repo.diff, [join("notes/todo.txt")])
        self.assertTrue(ok)
        self.assertIn("+todo", output)

        ok, _ = self._action(self.repo.add, [join("notes/todo.txt")])
        self.assertTrue(ok)
        self._refresh()
        self.assertEqual(self.repo.status_for(join("notes/todo.txt")), "added")

        ok, _ = self._action(self.repo.unstage, [join("notes/todo.txt")])
        self.assertTrue(ok)
        self._refresh()
        self.assertEqual(self.repo.status_for(join("notes/todo.txt")), "untracked")

        ok, output = self._action(
            self.repo.restore,
            [join("src/app.py"), join("notes/todo.txt"), join("src/new/added.py"),
             join("docs/new.md"), join("gone.txt")])
        self.assertTrue(ok, output)
        self.assertIn("notes/todo.txt", output)
        self._refresh()
        with open(join("src/app.py")) as handle:
            self.assertEqual(handle.read(), "print(1)\n")
        self.assertIsNone(self.repo.status_for(join("src/app.py")))
        self.assertTrue(os.path.exists(join("notes/todo.txt")))
        self.assertEqual(self.repo.status_for(join("notes/todo.txt")), "untracked")
        # A staged new file is only unstaged, its content is kept.
        self.assertTrue(os.path.exists(join("src/new/added.py")))
        self.assertEqual(self.repo.status_for(join("src/new/added.py")), "untracked")
        # The rename is reverted and the deleted file restored.
        self.assertTrue(os.path.exists(join("docs/old.md")))
        self.assertFalse(os.path.exists(join("docs/new.md")))
        self.assertTrue(os.path.exists(join("gone.txt")))

    def test_monitoring_triggers_refresh(self):
        self._refresh()
        self.repo.start_monitoring()

        def start(result, done):
            self.repo.connect("changed", lambda *_: (result.update(changed=True), done()))
            subprocess.run(["git", "add", "notes/todo.txt"], cwd=self.root, check=True)
        result = self._run_loop(start)
        self.assertTrue(result.get("changed"))
        self.assertEqual(
            self.repo.status_for(os.path.join(self.root, "notes/todo.txt")), "added")


@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitManagerTest(unittest.TestCase):
    def test_get(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="metafiles-mgr-"))
        self.addCleanup(shutil.rmtree, root, True)
        _git(root, "init", "-q")
        os.makedirs(os.path.join(root, "src"))
        manager = GitManager()
        self.addCleanup(manager.shutdown)
        repo = manager.get(os.path.join(root, "src"))
        self.assertIsNotNone(repo)
        self.assertEqual(repo.root, root)
        self.assertIs(manager.get(root), repo)
        outside = os.path.realpath(tempfile.mkdtemp(prefix="metafiles-nogit-"))
        self.addCleanup(shutil.rmtree, outside, True)
        if find_repo_root(outside) is None:
            self.assertIsNone(manager.get(outside))

    def test_no_git(self):
        manager = GitManager()
        manager._git_available = False
        self.assertIsNone(manager.get(os.getcwd()))


if __name__ == "__main__":
    unittest.main()
