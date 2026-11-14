"""File operations: delegated to Nautilus' D-Bus service when available, done locally otherwise.

Both implementations share one interface (FileOperations) and one merged undo history:
operations Nautilus performs live in Nautilus' own (single-level, shared) undo history, the
rest in a local stack; "Undo" picks whichever happened last.
"""

import os
import threading
import time

import gi
from gi.repository import Gio, GLib, GObject

from metafiles.i18n import _, ngettext

# Load the backend modules before any window exists, so surfaces get their full Python classes.
try:
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland
except (ImportError, ValueError):
    GdkWayland = None
try:
    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11
except (ImportError, ValueError):
    GdkX11 = None

NAUTILUS_BUS = "org.gnome.Nautilus"
NAUTILUS_PATH = "/org/gnome/Nautilus/FileOperations2"
NAUTILUS_IFACE = "org.gnome.Nautilus.FileOperations2"
FILEMANAGER_BUS = "org.freedesktop.FileManager1"
FILEMANAGER_PATH = "/org/freedesktop/FileManager1"

UNDO_NONE, UNDO_UNDO, UNDO_REDO = 0, 1, 2


def gfile(uri_or_path):
    if "://" in uri_or_path:
        return Gio.File.new_for_uri(uri_or_path)
    return Gio.File.new_for_path(uri_or_path)


class WindowHandle:
    """Exports the toplevel so Nautilus can parent its dialogs to our window."""

    def __init__(self, window):
        self.window = window
        self.handle = ""

    def ensure(self):
        if self.handle:
            return
        surface = self.window.get_surface()
        if surface is None:
            return
        if GdkWayland is not None and isinstance(surface, GdkWayland.WaylandToplevel):
            surface.export_handle(self._on_exported)
            return
        if GdkX11 is not None and isinstance(surface, GdkX11.X11Surface):
            self.handle = "x11:%x" % surface.get_xid()

    def _on_exported(self, _toplevel, handle, *_):
        self.handle = f"wayland:{handle}"

    def platform_data(self):
        self.ensure()
        data = {"timestamp": GLib.Variant("u", 0)}
        if self.handle:
            data["parent-handle"] = GLib.Variant("s", self.handle)
            data["window-position"] = GLib.Variant("s", "center")
        return data


class UndoEntry:
    def __init__(self, label, undo, redo=None):
        self.label = label
        self.undo = undo  # callable(done_callback)
        self.redo = redo
        self.time = time.monotonic()


class FileOperations(GObject.Object):
    """Front-end used by the UI. `ui` provides confirm/ask_conflict/error/toast callbacks."""

    __gsignals__ = {
        # Something on disk changed because of us (views refresh, Git refreshes).
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "undo-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "progress-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "nautilus-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, ui, use_nautilus=True):
        super().__init__()
        self.ui = ui
        self.handle = None
        self.local_undo = []
        self.local_redo = []
        self.jobs = []  # running local jobs
        self._proxy = None
        self._nautilus_time = 0.0  # when we last delegated an undoable operation
        self._nautilus_state = UNDO_NONE
        self._watch = None
        if use_nautilus:
            self._watch = Gio.bus_watch_name(Gio.BusType.SESSION, NAUTILUS_BUS,
                                             Gio.BusNameWatcherFlags.AUTO_START,
                                             self._on_appeared, self._on_vanished)
            self._connect_proxy()

    # --- Nautilus service ---------------------------------------------------

    def _connect_proxy(self):
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, NAUTILUS_BUS,
                                  NAUTILUS_PATH, NAUTILUS_IFACE, None, self._on_proxy)

    def _on_proxy(self, _source, result):
        try:
            proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as e:
            print(f"metafiles: Nautilus file operations unavailable: {e.message}")
            return
        self._proxy = proxy
        proxy.connect("g-properties-changed", self._on_props)
        state = proxy.get_cached_property("UndoStatus")
        self._nautilus_state = state.unpack() if state is not None else UNDO_NONE
        self.emit("nautilus-changed", True)
        self.emit("undo-changed")

    def _on_appeared(self, *_):
        if self._proxy is None:
            self._connect_proxy()

    def _on_vanished(self, *_):
        # The proxy keeps working through D-Bus activation; only drop it when activation fails.
        pass

    def _on_props(self, proxy, changed, _invalidated):
        changed = changed.unpack()
        if "UndoStatus" in changed:
            self._nautilus_state = changed["UndoStatus"]
            self.emit("undo-changed")

    @property
    def delegated(self):
        """True when operations go through Nautilus' service."""
        return self._proxy is not None

    def set_window(self, window):
        self.handle = WindowHandle(window)
        GLib.idle_add(lambda: self.handle.ensure() and False)

    def _platform(self):
        return self.handle.platform_data() if self.handle else {"timestamp": GLib.Variant("u", 0)}

    def _call(self, method, signature, args, on_error, undoable=True, touched=()):
        """Call a FileOperations2 method; fall back to `on_error` when the call fails."""
        def done(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error as e:
                print(f"metafiles: Nautilus {method} failed ({e.message}); using local operations")
                on_error()
                return
            self.emit("changed", list(touched))

        if undoable:
            self._nautilus_time = time.monotonic()
            self.local_redo.clear()
        self._proxy.call(method, GLib.Variant(signature, (*args, self._platform())),
                         Gio.DBusCallFlags.NONE, -1, None, done)

    # --- public operations --------------------------------------------------

    def copy(self, uris, dest_uri):
        if not uris:
            return
        if self._proxy:
            self._call("CopyURIs", "(assa{sv})", (list(uris), dest_uri),
                       lambda: self._local_transfer(uris, dest_uri, move=False), touched=[dest_uri])
        else:
            self._local_transfer(uris, dest_uri, move=False)

    def move(self, uris, dest_uri):
        uris = [u for u in uris if gfile(u).get_parent() is None or gfile(u).get_parent().get_uri() != dest_uri]
        if not uris:
            return
        if self._proxy:
            self._call("MoveURIs", "(assa{sv})", (list(uris), dest_uri),
                       lambda: self._local_transfer(uris, dest_uri, move=True),
                       touched=[dest_uri] + [gfile(u).get_parent().get_uri() for u in uris])
        else:
            self._local_transfer(uris, dest_uri, move=True)

    def trash(self, uris, on_done=None):
        if not uris:
            return
        if self._proxy:
            self._call("TrashURIs", "(asa{sv})", (list(uris),), lambda: self._local_trash(uris, on_done),
                       touched=[gfile(u).get_parent().get_uri() for u in uris])
            if on_done:
                on_done(True)
        else:
            self._local_trash(uris, on_done)

    def delete(self, uris):
        """Permanent deletion; the caller has already asked for confirmation."""
        if not uris:
            return
        self._run_job(_("Deleting"), lambda job: self._delete_worker(job, uris), touched=uris)

    def empty_trash(self):
        if self._proxy:
            self._call("EmptyTrash", "(ba{sv})", (True,), self._local_empty_trash, undoable=False)
        else:
            self._local_empty_trash()

    def create_folder(self, parent_uri, name, on_done=None):
        target = gfile(parent_uri).get_child(name)

        def local():
            try:
                target.make_directory(None)
            except GLib.Error as e:
                self.ui.error(_("Cannot create folder “{name}”").format(name=name), e.message)
                return
            self._push(_("Create folder"), lambda cb: self._trash_quiet([target.get_uri()], cb))
            self.emit("changed", [parent_uri])
            if on_done:
                on_done(target.get_uri())

        if self._proxy:
            def after():
                self.emit("changed", [parent_uri])
                if on_done:
                    # Nautilus creates it asynchronously; report once it exists.
                    self._when_exists(target, lambda: on_done(target.get_uri()))
            self._nautilus_time = time.monotonic()
            self._proxy.call("CreateFolder", GLib.Variant("(ssa{sv})", (parent_uri, name, self._platform())),
                             Gio.DBusCallFlags.NONE, -1, None,
                             lambda p, r: self._finish_or(p, r, local, after))
        else:
            local()

    def rename(self, uri, new_name, on_done=None):
        source = gfile(uri)
        old_name = source.get_basename()
        if new_name == old_name:
            return

        def local():
            try:
                renamed = source.set_display_name(new_name, None)
            except GLib.Error as e:
                self.ui.error(_("Cannot rename “{name}”").format(name=old_name), e.message)
                return
            self._push(_("Rename"), lambda cb: self._rename_quiet(renamed, old_name, cb),
                       lambda cb: self._rename_quiet(source, new_name, cb))
            self._moved(uri, renamed.get_uri())
            if on_done:
                on_done(renamed.get_uri())

        if self._proxy:
            target = source.get_parent().get_child(new_name)

            def after():
                self._moved(uri, target.get_uri())
                if on_done:
                    self._when_exists(target, lambda: on_done(target.get_uri()))
            self._nautilus_time = time.monotonic()
            self._proxy.call("RenameURI", GLib.Variant("(ssa{sv})", (uri, new_name, self._platform())),
                             Gio.DBusCallFlags.NONE, -1, None, lambda p, r: self._finish_or(p, r, local, after))
        else:
            local()

    def _finish_or(self, proxy, result, fallback, after):
        try:
            proxy.call_finish(result)
        except GLib.Error as e:
            print(f"metafiles: Nautilus call failed ({e.message}); using local operations")
            fallback()
            return
        after()

    def _when_exists(self, gf, callback, tries=40):
        if gf.query_exists(None) or tries <= 0:
            callback()
            return
        GLib.timeout_add(50, lambda: self._when_exists(gf, callback, tries - 1) and False)

    def _moved(self, old_uri, new_uri):
        self.emit("changed", [gfile(old_uri).get_parent().get_uri()])
        if hasattr(self.ui, "file_moved"):
            self.ui.file_moved(old_uri, new_uri)

    # --- operations Nautilus does not offer (always local) ------------------

    def new_file(self, parent_uri, name, template_uri=None, on_done=None):
        target = gfile(parent_uri).get_child(name)
        try:
            if template_uri:
                gfile(template_uri).copy(target, Gio.FileCopyFlags.NONE, None, None)
            else:
                target.create(Gio.FileCreateFlags.NONE, None).close(None)
        except GLib.Error as e:
            self.ui.error(_("Cannot create “{name}”").format(name=name), e.message)
            return
        self._push(_("Create file"), lambda cb: self._trash_quiet([target.get_uri()], cb))
        self.emit("changed", [parent_uri])
        if on_done:
            on_done(target.get_uri())

    def duplicate(self, uris, on_done=None):
        from metafiles.fileutil import copy_name

        pairs = []
        for uri in uris:
            src = gfile(uri)
            parent = src.get_parent()
            existing = set(os.listdir(parent.get_path())) if parent.get_path() else set()
            name = copy_name(src.get_basename(), existing)
            pairs.append((src, parent.get_child(name)))
        self._run_job(_("Duplicating"), lambda job: self._copy_pairs_worker(job, pairs),
                      undo=(_("Duplicate"), lambda cb: self._trash_quiet([d.get_uri() for _s, d in pairs], cb)),
                      touched=[s.get_parent().get_uri() for s, _d in pairs],
                      on_done=lambda ok: on_done and on_done([d.get_uri() for _s, d in pairs]))

    def make_links(self, uris, dest_uri, on_done=None):
        """Create symbolic links to uris in dest_uri ('Link to x' names like Nautilus)."""
        from metafiles.fileutil import unique_name

        dest = gfile(dest_uri)
        existing = set(os.listdir(dest.get_path())) if dest.get_path() else set()
        created = []
        for uri in uris:
            src = gfile(uri)
            name = src.get_basename()
            if src.get_parent() and src.get_parent().equal(dest):
                name = _("Link to {name}").format(name=name)
            name = unique_name(name, existing)
            existing.add(name)
            link = dest.get_child(name)
            try:
                link.make_symbolic_link(src.get_path() or uri, None)
                created.append(link.get_uri())
            except GLib.Error as e:
                self.ui.error(_("Cannot create link"), e.message)
        if created:
            self._push(_("Create link"), lambda cb: self._delete_quiet(created, cb))
            self.emit("changed", [dest_uri])
        if on_done:
            on_done(created)

    def restore_from_trash(self, trash_uris):
        """Move trash items back to their original location."""
        moved = []
        for uri in trash_uris:
            item = gfile(uri)
            try:
                info = item.query_info("trash::orig-path", Gio.FileQueryInfoFlags.NONE, None)
                orig = info.get_attribute_byte_string("trash::orig-path")
                if not orig:
                    continue
                target = Gio.File.new_for_path(orig)
                parent = target.get_parent()
                if parent is not None and not parent.query_exists(None):
                    parent.make_directory_with_parents(None)
                if target.query_exists(None):
                    self.ui.error(_("Cannot restore “{name}”").format(name=os.path.basename(orig)),
                                  _("An item with the same name already exists in the original location."))
                    continue
                item.move(target, Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None)
                moved.append(target.get_uri())
            except GLib.Error as e:
                self.ui.error(_("Cannot restore item"), e.message)
        if moved:
            self._push(_("Restore from trash"), lambda cb: self._trash_quiet(moved, cb))
            self.emit("changed", ["trash:///"] + [gfile(u).get_parent().get_uri() for u in moved])

    def push_undo(self, label, undo, redo=None):
        """Let other modules (batch rename, archives) record their own undo steps."""
        self._push(label, undo, redo)

    # --- undo ---------------------------------------------------------------

    def _push(self, label, undo, redo=None):
        self.local_undo.append(UndoEntry(label, undo, redo))
        del self.local_undo[:-50]
        self.local_redo.clear()
        self.emit("undo-changed")

    def _nautilus_undo_newer(self):
        if self._proxy is None or self._nautilus_state != UNDO_UNDO:
            return False
        return not self.local_undo or self._nautilus_time >= self.local_undo[-1].time

    def can_undo(self):
        return bool(self.local_undo) or (self._proxy is not None and self._nautilus_state == UNDO_UNDO)

    def can_redo(self):
        return bool(self.local_redo) or (self._proxy is not None and self._nautilus_state == UNDO_REDO)

    def undo_label(self):
        if self._nautilus_undo_newer() or not self.local_undo:
            return _("Undo (shared with Nautilus)") if self._proxy else _("Undo")
        return _("Undo: {what}").format(what=self.local_undo[-1].label)

    def undo(self):
        if self._nautilus_undo_newer() or (not self.local_undo and self._proxy is not None):
            if self._nautilus_state == UNDO_UNDO:
                self._proxy.call("Undo", GLib.Variant("(a{sv})", (self._platform(),)),
                                 Gio.DBusCallFlags.NONE, -1, None, None)
            return
        if not self.local_undo:
            return
        entry = self.local_undo.pop()

        def done(ok=True):
            if entry.redo:
                self.local_redo.append(entry)
            self.emit("undo-changed")
            self.emit("changed", [])
        entry.undo(done)

    def redo(self):
        if self.local_redo:
            entry = self.local_redo.pop()

            def done(ok=True):
                self.local_undo.append(entry)
                entry.time = time.monotonic()
                self.emit("undo-changed")
                self.emit("changed", [])
            entry.redo(done)
            return
        if self._proxy is not None and self._nautilus_state == UNDO_REDO:
            self._proxy.call("Redo", GLib.Variant("(a{sv})", (self._platform(),)),
                             Gio.DBusCallFlags.NONE, -1, None, None)

    # --- local implementation -----------------------------------------------

    def _rename_quiet(self, gf, name, callback):
        try:
            gf.set_display_name(name, None)
        except GLib.Error as e:
            self.ui.error(_("Cannot undo"), e.message)
        callback()

    def _trash_quiet(self, uris, callback):
        for uri in uris:
            try:
                gfile(uri).trash(None)
            except GLib.Error:
                pass
        callback()

    def _delete_quiet(self, uris, callback):
        for uri in uris:
            try:
                gfile(uri).delete(None)
            except GLib.Error:
                pass
        callback()

    def _local_trash(self, uris, on_done=None):
        trashed = []
        errors = []
        unsupported = []
        for uri in uris:
            gf = gfile(uri)
            try:
                gf.trash(None)
                trashed.append(gf.get_path())
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                    unsupported.append(uri)
                else:
                    errors.append((gf.get_basename(), e.message))
        if unsupported:
            # Like Nautilus: offer permanent deletion where there is no trash.
            n = len(unsupported)
            self.ui.confirm_async(
                ngettext("Cannot move {n} item to the trash", "Cannot move {n} items to the trash", n).format(n=n),
                _("This location has no trash. Do you want to delete permanently instead?"), _("_Delete"),
                lambda ok: ok and self.delete(unsupported))
        if errors:
            name, message = errors[0]
            self.ui.error(_("Cannot move “{name}” to the trash").format(name=name), message)
        if trashed:
            self._push(_("Move to trash"), lambda cb: self._untrash_paths(trashed, cb),
                       lambda cb: self._trash_quiet([Gio.File.new_for_path(p).get_uri() for p in trashed], cb))
            self.emit("changed", [Gio.File.new_for_path(os.path.dirname(p)).get_uri() for p in trashed])
        if on_done:
            on_done(bool(trashed))

    def _untrash_paths(self, paths, callback):
        wanted = set(paths)
        found = []
        try:
            enum = Gio.File.new_for_uri("trash:///").enumerate_children(
                "standard::name,trash::orig-path,trash::deletion-date", Gio.FileQueryInfoFlags.NONE, None)
            candidates = {}
            for info in enum:
                orig = info.get_attribute_byte_string("trash::orig-path")
                if orig in wanted:
                    date = info.get_deletion_date()
                    stamp = date.to_unix() if date else 0
                    if orig not in candidates or stamp > candidates[orig][0]:
                        candidates[orig] = (stamp, enum.get_child(info).get_uri())
            found = [uri for _stamp, uri in candidates.values()]
        except GLib.Error as e:
            self.ui.error(_("Cannot undo"), e.message)
        if found:
            self.restore_from_trash(found)
            if self.local_undo and self.local_undo[-1].label == _("Restore from trash"):
                self.local_undo.pop()  # restoring is the undo itself
        callback()

    def _local_empty_trash(self):
        def answer(ok):
            if ok:
                self._run_job(_("Emptying trash"), self._empty_trash_worker, touched=["trash:///"])

        self.ui.confirm_async(_("Empty all items from Trash?"),
                              _("All items in the Trash will be permanently deleted."), _("_Empty Trash"), answer)

    def _empty_trash_worker(self, job):
        trash = Gio.File.new_for_uri("trash:///")
        for info in trash.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None):
            child = trash.get_child(info.get_name())
            self._delete_recursive(child, job)

    def _delete_recursive(self, gf, job):
        info = gf.query_info("standard::type", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        if info.get_file_type() == Gio.FileType.DIRECTORY:
            for child_info in gf.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None):
                if job.cancelled:
                    return
                self._delete_recursive(gf.get_child(child_info.get_name()), job)
        gf.delete(None)
        job.step()

    def _delete_worker(self, job, uris):
        for uri in uris:
            if job.cancelled:
                return
            gf = gfile(uri)
            if gf.get_uri_scheme() == "trash":
                gf.delete(None)
            else:
                self._delete_recursive(gf, job)

    def _local_transfer(self, uris, dest_uri, move):
        dest = gfile(dest_uri)
        pairs = [(gfile(u), dest.get_child(gfile(u).get_basename())) for u in uris]
        label = _("Moving") if move else _("Copying")

        def undo(cb):
            if move:
                back = [(d, s) for s, d in pairs]
                self._run_job(_("Moving"), lambda job: self._move_pairs_worker(job, back, ask=False), on_done=lambda ok: cb())
            else:
                self._trash_quiet([d.get_uri() for _s, d in pairs], cb)

        self._run_job(label, lambda job: (self._move_pairs_worker if move else self._copy_pairs_worker)(job, pairs),
                      undo=(_("Move") if move else _("Copy"), undo),
                      touched=[dest_uri] + ([s.get_parent().get_uri() for s, _d in pairs] if move else []))

    def _resolve_conflict(self, job, src, dst):
        """Returns (action, target): action in replace/skip/rename/merge/cancel."""
        if not dst.query_exists(None):
            return "write", dst
        if src.equal(dst):
            from metafiles.fileutil import copy_name

            existing = set(os.listdir(dst.get_parent().get_path()))
            return "write", dst.get_parent().get_child(copy_name(dst.get_basename(), existing))
        if job.apply_all:
            decision = job.apply_all
        else:
            decision = job.ask_main(lambda done: self.ui.ask_conflict(src, dst, done))
        action, new_name, apply_all = decision
        if apply_all and action != "rename":
            job.apply_all = decision
        if action == "rename":
            return "write", dst.get_parent().get_child(new_name)
        return action, dst

    def _copy_pairs_worker(self, job, pairs):
        for src, dst in pairs:
            if job.cancelled:
                return
            action, dst = self._resolve_conflict(job, src, dst)
            if action == "cancel":
                job.cancelled = True
                return
            if action == "skip":
                continue
            self._copy_one(job, src, dst, replace=action == "replace", merge=action == "merge")

    def _copy_one(self, job, src, dst, replace=False, merge=False):
        info = src.query_info("standard::type,standard::size", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        if info.get_file_type() == Gio.FileType.DIRECTORY:
            if replace and dst.query_exists(None):
                self._delete_recursive(dst, job)
            if not dst.query_exists(None):
                dst.make_directory(None)
            for child in src.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None):
                if job.cancelled:
                    return
                name = child.get_name()
                csrc, cdst = src.get_child(name), dst.get_child(name)
                if cdst.query_exists(None):
                    action, cdst = self._resolve_conflict(job, csrc, cdst)
                    if action == "skip":
                        continue
                    if action == "cancel":
                        job.cancelled = True
                        return
                    self._copy_one(job, csrc, cdst, replace=action == "replace", merge=action == "merge")
                else:
                    self._copy_one(job, csrc, cdst)
            return
        flags = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA
        if replace:
            flags |= Gio.FileCopyFlags.OVERWRITE
        src.copy(dst, flags, job.cancellable, job.progress_cb, None)
        job.done_bytes += info.get_size()

    def _move_pairs_worker(self, job, pairs, ask=True):
        for src, dst in pairs:
            if job.cancelled:
                return
            if ask:
                action, dst = self._resolve_conflict(job, src, dst)
            else:
                action = "write"
            if action == "cancel":
                job.cancelled = True
                return
            if action == "skip":
                continue
            flags = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA
            if action == "replace":
                flags |= Gio.FileCopyFlags.OVERWRITE
            try:
                src.move(dst, flags, job.cancellable, job.progress_cb, None)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WOULD_MERGE) or \
                        e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WOULD_RECURSE):
                    self._copy_one(job, src, dst, merge=True)
                    self._delete_recursive(src, job)
                else:
                    raise
            GLib.idle_add(lambda s=src.get_uri(), d=dst.get_uri(): (self.ui.file_moved(s, d)
                                                                  if hasattr(self.ui, "file_moved") else None) and False)

    def _run_job(self, label, worker, undo=None, touched=(), on_done=None):
        job = Job(self, label)
        self.jobs.append(job)
        self.emit("progress-changed")

        def finish(error):
            self.jobs.remove(job)
            self.emit("progress-changed")
            if error is not None:
                self.ui.error(_("Error while {what}").format(what=label.lower()), error)
            if undo is not None and not job.cancelled and error is None:
                self._push(*undo)
            self.emit("changed", list(touched))
            if on_done:
                on_done(error is None and not job.cancelled)
            return False

        def run():
            error = None
            try:
                worker(job)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    error = e.message
            except OSError as e:
                error = str(e)
            GLib.idle_add(finish, error)

        threading.Thread(target=run, daemon=True).start()
        return job

    def shutdown(self):
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = None

    @property
    def running(self):
        return list(self.jobs)


class Job:
    """A locally executed operation running in a worker thread."""

    def __init__(self, ops, label):
        self.ops = ops
        self.label = label
        self.cancellable = Gio.Cancellable()
        self.cancelled = False
        self.apply_all = None
        self.done_bytes = 0
        self.current = 0
        self.total = 0
        self.steps = 0
        self.started = time.monotonic()

    def progress_cb(self, current, total, *_):
        self.current, self.total = current, total
        GLib.idle_add(lambda: self.ops.emit("progress-changed") and False)

    def step(self):
        self.steps += 1

    def cancel(self):
        self.cancelled = True
        self.cancellable.cancel()

    def fraction(self):
        return self.current / self.total if self.total else 0.0

    def ask_main(self, ask):
        """Run ask(done) on the main thread and wait for done(answer)."""
        event = threading.Event()
        box = {}

        def done(answer):
            box["answer"] = answer
            event.set()

        GLib.idle_add(lambda: ask(done) and False)
        event.wait()
        return box["answer"]


# --- org.freedesktop.FileManager1 -------------------------------------------


def _filemanager_call(method, uris, startup_id=""):
    def done(bus, result):
        try:
            bus.call_finish(result)
        except GLib.Error as e:
            print(f"metafiles: {method} failed: {e.message}")

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call(FILEMANAGER_BUS, FILEMANAGER_PATH, FILEMANAGER_BUS, method,
             GLib.Variant("(ass)", (list(uris), startup_id)), None, Gio.DBusCallFlags.NONE, -1, None, done)


def show_properties(uris):
    _filemanager_call("ShowItemProperties", uris)


def show_in_nautilus(uris, folders=False):
    _filemanager_call("ShowFolders" if folders else "ShowItems", uris)


def count_label(n):
    return ngettext("{n} item", "{n} items", n).format(n=n)

