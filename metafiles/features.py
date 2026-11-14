"""Detection of optional components; every optional feature asks here first."""

import functools
import shutil

import gi
from gi.repository import Gio


@functools.cache
def _typelib(namespace, version):
    try:
        gi.require_version(namespace, version)
        __import__(f"gi.repository.{namespace}")
        return True
    except (ValueError, ImportError):
        return False


# (feature, check, Ubuntu package that provides it)
_CHECKS = {
    "terminal": (lambda: _typelib("Vte", "3.91"), "gir1.2-vte-3.91"),
    "sourceview": (lambda: _typelib("GtkSource", "5"), "gir1.2-gtksource-5"),
    "poppler": (lambda: _typelib("Poppler", "0.18"), "gir1.2-poppler-0.18"),
    "thumbnails": (lambda: _typelib("GnomeDesktop", "4.0"), "gir1.2-gnomedesktop-4.0"),
    "autoar": (lambda: _typelib("GnomeAutoar", "0.1"), "gir1.2-gnomeautoar-0.1"),
    "git": (lambda: shutil.which("git") is not None, "git"),
    "meld": (lambda: shutil.which("meld") is not None, "meld"),
    "metaterminal": (lambda: shutil.which("metaterminal") is not None, None),
    "xdg-terminal-exec": (lambda: shutil.which("xdg-terminal-exec") is not None, "xdg-terminal-exec"),
    "gnome-terminal": (lambda: shutil.which("gnome-terminal") is not None, "gnome-terminal"),
}

_forced_off = set()


def disable(*names):
    """Testing hook: pretend these components are missing."""
    _forced_off.update(names)
    has.cache_clear()


@functools.cache
def has(name):
    if name in _forced_off:
        return False
    check, _package = _CHECKS[name]
    return bool(check())


def package_for(name):
    return _CHECKS[name][1]


def missing_packages():
    return [pkg for name, (_c, pkg) in _CHECKS.items() if pkg and not has(name)
            and name not in ("gnome-terminal", "metaterminal")]


def nautilus_on_bus():
    """True when Nautilus' file-operations service can be reached (running or activatable)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "ListActivatableNames", None, None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        running = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "ListNames", None, None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return "org.gnome.Nautilus" in names or "org.gnome.Nautilus" in running
    except Exception:  # noqa: BLE001 - no session bus means no Nautilus
        return False
