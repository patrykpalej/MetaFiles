"""Thumbnails from the shared freedesktop cache, generated in background threads."""

import queue
import threading

from gi.repository import Gdk, GLib

try:
    import gi

    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop
except (ImportError, ValueError):  # thumbnails are optional
    GnomeDesktop = None

WORKERS = 3


class Thumbnailer:
    """request(uri, mime, mtime, pixel_size, callback) -> callback(texture or None) on the main loop."""

    def __init__(self):
        self.available = GnomeDesktop is not None
        self._factories = {}
        self._queue = queue.LifoQueue()  # newest requests first: what is on screen now
        self._pending = {}
        self._lock = threading.Lock()
        self._memory = {}
        if self.available:
            for _ in range(WORKERS):
                threading.Thread(target=self._worker, daemon=True).start()

    def _factory(self, size):
        if size not in self._factories:
            self._factories[size] = GnomeDesktop.DesktopThumbnailFactory.new(size)
        return self._factories[size]

    @staticmethod
    def _size_for(pixels):
        sizes = GnomeDesktop.DesktopThumbnailSize
        if pixels <= 128:
            return sizes.NORMAL
        if pixels <= 256:
            return sizes.LARGE
        return sizes.XLARGE

    def request(self, uri, mime, mtime, pixels, callback):
        if not self.available:
            return
        size = self._size_for(pixels)
        key = (uri, int(mtime), int(size))
        cached = self._memory.get(key)
        if cached is not None:
            callback(cached)
            return
        with self._lock:
            waiting = self._pending.setdefault(key, [])
            waiting.append(callback)
            if len(waiting) > 1:
                return
        self._queue.put((key, uri, mime, int(mtime), size))

    def cancel(self, callback):
        with self._lock:
            for waiting in self._pending.values():
                if callback in waiting:
                    waiting.remove(callback)

    def _worker(self):
        while True:
            key, uri, mime, mtime, size = self._queue.get()
            with self._lock:
                if not self._pending.get(key):
                    self._pending.pop(key, None)
                    continue
            texture = None
            try:
                texture = self._load(uri, mime, mtime, size)
            except Exception as e:  # noqa: BLE001 - a broken file must not stop the worker
                print(f"metafiles: thumbnail failed for {uri}: {e}")
            GLib.idle_add(self._deliver, key, texture)

    def _load(self, uri, mime, mtime, size):
        factory = self._factory(size)
        path = factory.lookup(uri, mtime)
        if path:
            return Gdk.Texture.new_from_filename(path)
        if factory.has_valid_failed_thumbnail(uri, mtime) or not factory.can_thumbnail(uri, mime, mtime):
            return None
        try:
            pixbuf = factory.generate_thumbnail(uri, mime, None)
        except GLib.Error:
            pixbuf = None
        if pixbuf is None:
            try:
                factory.create_failed_thumbnail(uri, mtime, None)
            except GLib.Error:
                pass
            return None
        try:
            factory.save_thumbnail(pixbuf, uri, mtime, None)
        except GLib.Error:
            pass
        return Gdk.Texture.new_for_pixbuf(pixbuf)

    def _deliver(self, key, texture):
        with self._lock:
            callbacks = self._pending.pop(key, [])
        if texture is not None:
            self._memory[key] = texture
            if len(self._memory) > 2000:
                for old in list(self._memory)[:500]:
                    del self._memory[old]
        for callback in callbacks:
            callback(texture)
        return False
