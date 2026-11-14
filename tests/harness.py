"""Drive MetaFiles in a test compositor and save window screenshots.

Usage:
    WAYLAND_DISPLAY=mf-test python3 tests/harness.py SCENARIO OUTDIR [PROJECT]

Without PROJECT a small sample project is generated in a temporary directory.

Runs with a throw-away config/data directory and without Nautilus delegation unless
METAFILES_NO_NAUTILUS=0 is given. Scenarios are functions in tests/scenarios.py taking
(h) and using h.step(...) to queue actions.
"""

import atexit
import importlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="metafiles-harness-")
os.environ.setdefault("METAFILES_CONFIG_DIR", os.path.join(_tmp, "config"))
os.environ.setdefault("METAFILES_DATA_DIR", os.path.join(_tmp, "data"))
os.environ.setdefault("METAFILES_NO_NAUTILUS", "1")
os.environ.setdefault("METAFILES_BOOKMARKS_FILE", os.path.join(_tmp, "bookmarks"))

import metafiles  # noqa: E402,F401
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from metafiles.application import Application  # noqa: E402


class Harness:
    def __init__(self, outdir):
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.app = Application()
        # A separate application id so a running MetaFiles is never touched.
        self.app.set_application_id("io.github.metafiles.MetaFilesTest")
        self.app.set_flags(Gio.ApplicationFlags.NON_UNIQUE | Gio.ApplicationFlags.HANDLES_OPEN)
        self.steps = []
        self.log = []
        self.failures = []

    @property
    def win(self):
        return self.app.window

    def step(self, delay_ms, fn, label=""):
        self.steps.append((delay_ms, fn, label))

    def shot(self, name, delay_ms=400):
        self.step(delay_ms, lambda: self.screenshot(name), f"shot {name}")

    def check(self, condition, message):
        self.log.append(("OK " if condition else "FAIL ") + message)
        if not condition:
            self.failures.append(message)

    def screenshot(self, name):
        win = self.win
        paintable = Gtk.WidgetPaintable.new(win)
        width, height = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            self.log.append(f"FAIL screenshot {name}: empty")
            return
        texture = win.get_renderer().render_texture(node, None)
        path = os.path.join(self.outdir, f"{name}.png")
        texture.save_to_png(path)
        self.log.append(f"shot {path}")

    def _popovers(self, widget=None, found=None):
        found = [] if found is None else found
        widget = widget or self.win
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover) and child.get_visible():
                found.append(child)
            self._popovers(child, found)
            child = child.get_next_sibling()
        return found

    def popover_shot(self, name):
        popovers = self._popovers()
        self.check(bool(popovers), f"popover visible for {name}")
        for popover in popovers:
            paintable = Gtk.WidgetPaintable.new(popover)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, popover.get_width(), popover.get_height())
            node = snapshot.to_node()
            if node is not None:
                path = os.path.join(self.outdir, f"{name}.png")
                popover.get_native().get_renderer().render_texture(node, None).save_to_png(path)
                self.log.append(f"shot {path}")

    def close_popovers(self):
        for popover in self._popovers():
            popover.popdown()

    def key(self, accel_action):
        self.win.activate_action(f"win.{accel_action}", None)

    def run(self, scenario, args):
        scenario(self, *args)

        def start(*_):
            GLib.timeout_add(1200, self._next)

        self.app.connect("activate", start)
        self.app.run([sys.argv[0]])
        for line in self.log:
            print(line)
        print("RESULT:", "FAIL" if self.failures else "OK")
        return 1 if self.failures else 0

    def _next(self):
        if not self.steps:
            self.app.window._quit_now() if self.app.window else self.app.quit()
            return False
        delay, fn, label = self.steps.pop(0)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - report and continue
            import traceback

            traceback.print_exc()
            self.failures.append(f"{label}: {e}")
        GLib.timeout_add(max(delay, 50), self._next)
        return False


def sample_project():
    """A throw-away project with the files the scenarios expect."""
    from gi.repository import GdkPixbuf

    # Under the home directory: /tmp is often a tmpfs without a trash can.
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    os.makedirs(cache, exist_ok=True)
    root = os.path.join(tempfile.mkdtemp(prefix="metafiles-harness-", dir=cache), "sample")
    atexit.register(shutil.rmtree, os.path.dirname(root), True)
    os.makedirs(os.path.join(root, "src", "lib"))
    os.makedirs(os.path.join(root, "docs"))
    files = {
        "README.md": "# Sample\n\nA project generated for the MetaFiles harness.\n",
        "notes.txt": "Some notes.\n",
        os.path.join("src", "app.py"): "def main():\n    print('hello')\n",
        os.path.join("src", "lib", "util.py"): "VALUE = 42\n",
    }
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as handle:
            handle.write(text)
    image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 128, 96)
    image.fill(0x3584E4FF)
    image.savev(os.path.join(root, "docs", "image.png"), "png", [], [])
    image.savev(os.path.join(root, "docs", "image-2.png"), "png", [], [])
    if shutil.which("git"):
        # A repository with a modified and an untracked file, for the Git views.
        git = ["git", "-C", root, "-c", "user.name=Harness", "-c", "user.email=harness@example.com"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "Initial"], check=True)
        with open(os.path.join(root, "notes.txt"), "a") as handle:
            handle.write("A change.\n")
        with open(os.path.join(root, "src", "new.py"), "w") as handle:
            handle.write("NEW = True\n")
    return root


def main():
    name, outdir, *args = sys.argv[1:]
    if not args:
        args = [sample_project()]
    scenarios = importlib.import_module("tests.scenarios")
    return Harness(outdir).run(getattr(scenarios, name), args)


if __name__ == "__main__":
    sys.exit(main())
