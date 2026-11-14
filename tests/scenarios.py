"""Harness scenarios (see tests/harness.py)."""

import os

from gi.repository import Gio

from metafiles import features


def _add(h, path):
    h.step(300, lambda: h.win.add_project(path), f"add {path}")


def basic(h, project):
    _add(h, project)
    h.shot("01-list", 1200)
    h.step(200, lambda: h.key("view-grid"))
    h.shot("02-grid", 900)
    h.step(200, lambda: h.key("view-list"))

    def checks():
        pane = h.win.active_pane
        h.check(pane is not None, "active pane exists")
        h.check(pane.filter_model.get_n_items() > 0, f"items listed: {pane.filter_model.get_n_items()}")
        h.check(not pane.can_go_up(), "cannot go up from the locked project root")
    h.step(300, checks)
    h.step(200, lambda: h.win.active_pane.navigate(Gio.File.new_for_path("/etc").get_uri()))
    h.step(300, lambda: h.check(h.win.active_pane.path() == os.path.realpath(project), "lock refused /etc"))
    h.shot("03-refused", 300)


def features_tour(h, project):
    """Exercise most features and take screenshots of each."""
    root = os.path.realpath(project)
    import shutil

    shutil.rmtree(os.path.join(root, "created-by-test"), ignore_errors=True)
    _add(h, project)
    pane = lambda: h.win.active_pane  # noqa: E731

    # Tree view, expanded src
    h.step(800, lambda: pane().set_view_mode("tree"))

    def expand():
        for i in range(pane().filter_model.get_n_items()):
            row = pane().filter_model.get_item(i)
            if row.get_item().get_name() == "src":
                row.set_expanded(True)
    h.step(500, expand)
    h.shot("10-tree", 700)
    h.step(100, lambda: h.check(any(pane().info_at(i).get_name() == "app.py"
                                    for i in range(pane().filter_model.get_n_items())), "tree shows src/app.py"))
    h.step(100, lambda: pane().set_view_mode("list"))

    # Split view + preview panel
    h.step(300, lambda: h.key("toggle-split"))
    h.step(500, lambda: h.win.current_tab.active.navigate(Gio.File.new_for_path(os.path.join(root, "src")).get_uri()))
    h.step(400, lambda: pane().select_uris([Gio.File.new_for_path(os.path.join(root, "src", "app.py")).get_uri()]))
    h.step(200, lambda: h.key("toggle-preview"))
    h.shot("11-split-preview", 1200)
    h.step(100, lambda: h.check(h.win.current_tab.is_split, "split view open"))
    h.step(100, lambda: h.key("toggle-preview"))
    h.step(100, lambda: h.key("toggle-split"))

    # Search the whole project
    h.step(300, lambda: h.win.start_search(whole_project=True, text="util"))
    h.step(1200, lambda: h.check(any(pane().info_at(i).get_name() == "util.py"
                                     for i in range(pane().filter_model.get_n_items())), "search finds util.py"))
    h.shot("12-search", 200)
    h.step(200, lambda: h.win.pathbar.close_search())

    # Context menu on a file
    h.step(400, lambda: pane().select_uris([Gio.File.new_for_path(os.path.join(root, "README.md")).get_uri()]))
    h.step(300, lambda: pane().go_home())
    h.step(500, lambda: pane().select_uris([Gio.File.new_for_path(os.path.join(root, "README.md")).get_uri()]))
    h.step(300, lambda: h.win.show_context_menu(pane(), 300, 200))
    h.step(700, lambda: h.popover_shot("13-context-menu"))
    h.step(100, h.close_popovers)

    # New folder via local operations, then trash + undo
    h.step(300, lambda: h.app.ops.create_folder(Gio.File.new_for_path(root).get_uri(), "created-by-test"))
    h.step(900, lambda: h.check(os.path.isdir(os.path.join(root, "created-by-test")), "folder created"))
    h.step(100, lambda: h.app.ops.trash([Gio.File.new_for_path(os.path.join(root, "created-by-test")).get_uri()]))
    h.step(900, lambda: h.check(not os.path.exists(os.path.join(root, "created-by-test")), "folder trashed"))
    h.step(100, lambda: h.app.ops.undo())
    h.step(1500, lambda: h.check(os.path.isdir(os.path.join(root, "created-by-test")), "undo restored folder"))

    # Places: tree mode, changed files, recent
    h.step(200, lambda: h.win.places.set_mode("tree"))
    h.step(300, lambda: pane().navigate("git-changes:///"))
    h.shot("14-changes-tree", 1200)
    h.step(100, lambda: h.check(pane().filter_model.get_n_items() >= 2,
                                f"changed files listed: {pane().filter_model.get_n_items()}"))
    h.step(100, lambda: h.win.places.set_mode("places"))
    h.step(100, lambda: pane().go_home())

    # Quick open
    h.step(300, lambda: h.key("quick-open"))
    h.step(900, lambda: _type_quick_open(h, "srcapp"))
    h.shot("15-quick-open", 900)
    h.step(100, lambda: h.win.get_visible_dialog().close() if h.win.get_visible_dialog() else None)

    # Groups and second project
    h.step(300, lambda: h.win.add_project(os.path.join(root, "docs")))
    h.step(400, lambda: h.win.session.groups.append("Klienci") or h.win.sidebar.resort())
    h.step(300, lambda: h.win.toggle_focus())
    h.shot("16-sidebar-focus", 600)
    h.step(200, lambda: h.win.toggle_focus())

    # Shortcuts and preferences dialogs
    h.step(300, lambda: h.key("show-shortcuts"))
    h.shot("17-shortcuts", 900)
    h.step(100, lambda: h.win.get_visible_dialog().close())
    h.step(300, lambda: h.key("preferences"))
    h.shot("18-preferences", 900)
    h.step(100, lambda: h.win.get_visible_dialog().close())
    h.step(100, lambda: shutil.rmtree(os.path.join(root, "created-by-test"), ignore_errors=True))


def _type_quick_open(h, text):
    dialog = h.win.get_visible_dialog()
    h.check(dialog is not None, "quick open dialog shown")
    if dialog is not None:
        dialog.entry.set_text(text)


def panels_tour(h, project):
    """Terminal panel, places views, preview of an image and the main dialogs."""
    root = os.path.realpath(project)
    uri = lambda *p: Gio.File.new_for_path(os.path.join(root, *p)).get_uri()  # noqa: E731
    pane = lambda: h.win.active_pane  # noqa: E731
    _add(h, project)

    # Terminal panel follows the view, and the view follows `cd` in the terminal.
    # Optional: needs gir1.2-vte-3.91.
    if features.has("terminal"):
        h.step(600, lambda: h.key("toggle-terminal"))
        h.step(1500, lambda: pane().navigate(uri("src")))
        h.step(1200, lambda: h.check(h.win.current_tab.terminal is not None and
                                     h.win.current_tab.terminal.cwd == os.path.join(root, "src"),
                                     f"terminal followed the view: {getattr(h.win.current_tab.terminal, 'cwd', None)}"))
        h.step(100, lambda: h.win.current_tab.terminal.term.feed_child(b"cd lib\n"))
        h.step(1200, lambda: h.check(pane().path() == os.path.join(root, "src", "lib"), f"view followed cd: {pane().path()}"))
        h.step(100, lambda: h.win.current_tab.terminal.term.feed_child(b"cd /\n"))
        h.shot("20-terminal", 1200)
        h.step(100, lambda: h.check(pane().path() == os.path.join(root, "src", "lib"), "cd / refused, view stays"))
        h.step(100, lambda: h.key("toggle-terminal"))
    else:
        h.step(100, lambda: h.log.append("skip terminal steps (VTE not installed)"))

    # Star a file and open Starred
    h.step(300, lambda: pane().navigate(uri()))
    h.step(500, lambda: pane().toggle_star([uri("README.md")]))
    h.step(300, lambda: pane().navigate("starred:///"))
    h.step(900, lambda: h.check(pane().filter_model.get_n_items() >= 1, "starred view lists README.md"))
    h.shot("21-starred", 200)

    # Recent (scoped to the project)
    h.step(200, lambda: __import__("gi.repository.Gtk", fromlist=["Gtk"]).RecentManager.get_default().add_item(uri("notes.txt")))
    h.step(600, lambda: pane().navigate("recent:///"))
    h.shot("22-recent", 1200)
    # The recent list is shared with the whole desktop: remove the test entry again.
    h.step(100, lambda: __import__("gi.repository.Gtk", fromlist=["Gtk"]).RecentManager.get_default().remove_item(uri("notes.txt")))

    # Trash a file, view the project trash, restore it
    h.step(200, lambda: pane().navigate(uri()))
    h.step(500, lambda: open(os.path.join(root, "trash-me.txt"), "w").write("x"))
    h.step(700, lambda: h.app.ops.trash([uri("trash-me.txt")]))
    h.step(700, lambda: pane().navigate("trash:///"))
    h.shot("23-trash", 1200)
    h.step(100, lambda: h.check(any(pane().info_at(i).get_display_name() == "trash-me.txt"
                                    for i in range(pane().filter_model.get_n_items())), "trash lists the project item"))
    h.step(100, lambda: h.app.ops.restore_from_trash([item for item in
        [__import__("metafiles.fileview", fromlist=["item_file"]).item_file(pane().info_at(i)).get_uri()
         for i in range(pane().filter_model.get_n_items())] if item.endswith("trash-me.txt")]))
    h.step(800, lambda: h.check(os.path.exists(os.path.join(root, "trash-me.txt")), "restored from trash"))
    h.step(100, lambda: os.unlink(os.path.join(root, "trash-me.txt")))

    # Preview of an image in grid view
    h.step(300, lambda: pane().navigate(uri("docs")))
    h.step(700, lambda: pane().set_view_mode("grid"))
    h.step(300, lambda: pane().select_uris([uri("docs", "image.png")]))
    h.step(200, lambda: h.key("toggle-preview"))
    h.shot("24-grid-preview", 1500)
    h.step(100, lambda: h.key("toggle-preview"))
    h.step(100, lambda: pane().set_view_mode("list"))

    # Rename popover, new folder dialog, batch rename
    h.step(300, lambda: pane().navigate(uri()))
    h.step(600, lambda: pane().select_uris([uri("notes.txt")]))
    h.step(200, lambda: h.key("rename"))
    h.step(800, lambda: h.popover_shot("25-rename"))
    h.step(100, h.close_popovers)
    h.step(300, lambda: h.key("new-folder"))
    h.shot("26-new-folder", 800)
    h.step(100, lambda: h.win.get_visible_dialog().close())
    h.step(300, lambda: pane().navigate(uri("docs")))
    h.step(600, lambda: pane().select_all())
    h.step(200, lambda: h.key("rename"))
    h.step(600, lambda: h.win.get_visible_dialog().find.set_text("png"))
    h.step(100, lambda: h.win.get_visible_dialog().replace.set_text("PNG"))
    h.shot("27-batch-rename", 800)
    h.step(100, lambda: h.win.get_visible_dialog().close())
    h.step(300, lambda: h.key("toggle-places"))
    h.step(100, lambda: h.key("toggle-projects"))
    h.shot("28-no-sidebars", 700)


def nautilus_ops(h, project):
    """File operations delegated to Nautilus' D-Bus service (no Undo: it is shared with Nautilus)."""
    import shutil

    root = os.path.join(os.path.realpath(project), "nautilus-ops")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "dst"))
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("a")
    uri = lambda *p: Gio.File.new_for_path(os.path.join(root, *p)).get_uri()  # noqa: E731
    ops = lambda: h.app.ops  # noqa: E731
    h.step(1500, lambda: h.check(ops().delegated, "operations are delegated to Nautilus"))
    h.step(100, lambda: ops().copy([uri("a.txt")], uri("dst")))
    h.step(1500, lambda: h.check(os.path.exists(os.path.join(root, "dst", "a.txt")), "Nautilus copied a.txt"))
    h.step(100, lambda: ops().rename(uri("dst", "a.txt"), "b.txt"))
    h.step(1500, lambda: h.check(os.path.exists(os.path.join(root, "dst", "b.txt")), "Nautilus renamed to b.txt"))
    h.step(100, lambda: ops().create_folder(uri(), "made"))
    h.step(1500, lambda: h.check(os.path.isdir(os.path.join(root, "made")), "Nautilus created a folder"))
    h.step(100, lambda: ops().move([uri("dst", "b.txt")], uri("made")))
    h.step(1500, lambda: h.check(os.path.exists(os.path.join(root, "made", "b.txt")), "Nautilus moved b.txt"))
    h.step(100, lambda: ops().trash([uri("made", "b.txt")]))
    h.step(1500, lambda: h.check(not os.path.exists(os.path.join(root, "made", "b.txt")), "Nautilus trashed b.txt"))
    h.step(100, lambda: shutil.rmtree(root, ignore_errors=True))
    h.step(100, lambda: _purge_trash(root))


def _purge_trash(prefix):
    """Delete trash items that came from the test directory (Nautilus' trash is the user's)."""
    trash = Gio.File.new_for_uri("trash:///")
    for info in trash.enumerate_children("standard::name,trash::orig-path", Gio.FileQueryInfoFlags.NONE, None):
        if (info.get_attribute_byte_string("trash::orig-path") or "").startswith(prefix):
            trash.get_child(info.get_name()).delete(None)


def session_write(h, project):
    root = os.path.realpath(project)
    h.step(300, lambda: h.win.add_project(project))
    h.step(800, lambda: h.win.active_pane.navigate(Gio.File.new_for_path(os.path.join(root, "src")).get_uri()))
    h.step(300, lambda: h.key("toggle-split"))
    h.step(300, lambda: h.key("toggle-preview"))
    h.step(300, lambda: h.key("new-tab"))
    h.step(300, lambda: h.win.active_pane.set_view_mode("grid"))
    h.step(300, lambda: h.win.session.groups.append("Praca"))
    h.step(300, lambda: h.win.current_view.state.bookmarks.append(os.path.join(root, "docs")))


def session_check(h, project):
    root = os.path.realpath(project)

    def check():
        view = h.win.current_view
        h.check(view is not None and view.root == root, f"project restored: {view and view.root}")
        h.check(len(view.tabs) == 2, f"two tabs restored: {len(view.tabs)}")
        first = view.tabs[0]
        h.check(first.is_split, "split view restored")
        h.check(first.preview is not None, "preview panel restored")
        h.check(first.panes[0].path() == os.path.join(root, "src"), f"pane location restored: {first.panes[0].path()}")
        h.check(view.tabs[1].active.view_mode == "grid", "grid view of second tab restored")
        h.check("Praca" in h.win.session.groups, "empty group kept")
        h.check(os.path.join(root, "docs") in view.state.bookmarks, "project bookmark kept")
    h.step(1500, check)
    h.shot("30-restored", 300)


def perf_big_folder(h, folder):
    import time

    state = {}

    def start():
        h.win.add_project(folder)
        state["t"] = time.monotonic()

    def wait_loaded():
        pane = h.win.active_pane
        if pane is not None and pane.filter_model.get_n_items() > 0:
            state.setdefault("first", time.monotonic() - state["t"])
        if pane is not None and not pane._loading and pane.filter_model.get_n_items() >= 10000:
            state.setdefault("loaded", time.monotonic() - state["t"])

    h.step(100, start)
    for _ in range(40):
        h.step(100, wait_loaded)
    h.step(100, lambda: h.check(state.get("first", 99) < 1.0, f"first files visible after {state.get('first', -1):.2f} s"))
    h.step(100, lambda: h.check("loaded" in state and state["loaded"] < 4.0,
                                f"10 000 files listed in {state.get('loaded', -1):.2f} s"))

    def scroll():
        pane = h.win.active_pane
        adj = pane.view.list_scroller.get_vadjustment()
        t = time.monotonic()
        for i in range(30):
            adj.set_value(adj.get_upper() * i / 30)
            ctx = __import__("gi.repository.GLib", fromlist=["GLib"]).MainContext.default()
            while ctx.pending():
                ctx.iteration(False)
        state["scroll"] = time.monotonic() - t
    h.step(300, scroll)
    h.step(100, lambda: h.check(state["scroll"] < 3.0, f"30 scroll jumps in {state['scroll']:.2f} s"))
    h.shot("31-big-folder", 300)
