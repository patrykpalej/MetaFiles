"""Small helpers shared by the views: paths, names, sorting keys, formatting."""

import os
import re
import time
import unicodedata

from gi.repository import Gio, GLib

from metafiles.i18n import _, ngettext

HOME = os.path.expanduser("~")


def short_path(path):
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def real(path):
    return os.path.realpath(os.path.expanduser(path))


def inside(path, root):
    """True if the real path lies in root (root itself included); both must be real paths."""
    if root == "/":
        return True
    return path == root or path.startswith(root + os.sep)


def inside_project(path, root):
    """Follow symlinks before checking, like the bash confinement in MetaTerminal."""
    return inside(real(path), root)


def relative_label(path, root):
    """'.' for the root itself, 'src/lib' inside, the full (~) path elsewhere."""
    if path == root:
        return "."
    if root != "/" and path.startswith(root + os.sep):
        return path[len(root) + 1:]
    return short_path(path)


def strip_accents(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("ł", "l").replace("Ł", "L")


_NUM = re.compile(r"(\d+)")


def natural_key(name):
    """Sort key: case- and accent-insensitive, digits compared as numbers ('plik2' < 'plik10')."""
    folded = GLib.utf8_collate_key_for_filename(name, -1)
    return folded


def natural_key_py(name):
    """Pure-python variant used by tests and non-UI code."""
    parts = _NUM.split(strip_accents(name).casefold())
    return [(0, int(p), p) if p.isdigit() else (1, 0, p) for p in parts]


def split_extension(name):
    """Like Nautilus: dotfiles have no extension; .tar.* counts as one extension."""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    lower = name.lower()
    for compound in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar.lz", ".tar.lzma"):
        if lower.endswith(compound) and len(name) > len(compound):
            return name[: -len(compound)], name[-len(compound):]
    stem, ext = os.path.splitext(name)
    if not stem:
        return name, ""
    return stem, ext


def copy_name(name, existing):
    """Nautilus-style duplicate name: 'plik (kopia).txt', 'plik (kopia 2).txt'."""
    stem, ext = split_extension(name)
    m = re.match(r"^(.*) \(" + re.escape(_("copy")) + r"(?: (\d+))?\)$", stem)
    base = m.group(1) if m else stem
    n = int(m.group(2) or 1) + 1 if m else 1
    while True:
        label = _("copy") if n == 1 else f"{_('copy')} {n}"
        candidate = f"{base} ({label}){ext}"
        if candidate not in existing:
            return candidate
        n += 1


def unique_name(name, existing):
    """'name', 'name 2', 'name 3'… for new files and folders."""
    if name not in existing:
        return name
    stem, ext = split_extension(name)
    n = 2
    while f"{stem} {n}{ext}" in existing:
        n += 1
    return f"{stem} {n}{ext}"


def validate_filename(name, existing, is_folder=False):
    """Return an error message (Nautilus' wording) or None."""
    if not name.strip():
        return None if not name else _("A name cannot be only spaces.")
    if "/" in name:
        return _("Folder names cannot contain “/”.") if is_folder else _("File names cannot contain “/”.")
    if name == ".":
        return _("A folder cannot be called “.”.") if is_folder else _("A file cannot be called “.”.")
    if name == "..":
        return _("A folder cannot be called “..”.") if is_folder else _("A file cannot be called “..”.")
    if len(name.encode()) > 255:
        return _("Name is too long.")
    if name in existing:
        return _("A folder with that name already exists.") if is_folder else _("A file with that name already exists.")
    return None


def format_size(size):
    return GLib.format_size(size) if size is not None and size >= 0 else ""


def format_count(n):
    return ngettext("{n} item", "{n} items", n).format(n=n)


def format_date(timestamp, fmt="simple"):
    """Nautilus' simple format: time for today, 'Yesterday', weekday for this week, else date."""
    if not timestamp:
        return ""
    dt = GLib.DateTime.new_from_unix_local(int(timestamp))
    if fmt == "detailed":
        return dt.format("%x %X")
    now = GLib.DateTime.new_now_local()
    today = GLib.DateTime.new_local(now.get_year(), now.get_month(), now.get_day_of_month(), 0, 0, 0)
    days = today.difference(GLib.DateTime.new_local(dt.get_year(), dt.get_month(), dt.get_day_of_month(), 0, 0, 0)) // GLib.TIME_SPAN_DAY
    if days == 0:
        return dt.format("%H:%M")
    if days == 1:
        return _("Yesterday") + dt.format(" %H:%M")
    if 1 < days < 7:
        return dt.format("%a %H:%M")
    if dt.get_year() == now.get_year():
        return dt.format("%-d %b")
    return dt.format("%-d %b %Y")


def format_permissions(mode, is_dir=False):
    chars = "d" if is_dir else "-"
    for who in range(2, -1, -1):
        bits = (mode >> (who * 3)) & 7
        chars += ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
    return chars


def uri_to_path(uri):
    return Gio.File.new_for_uri(uri).get_path()


def path_to_uri(path):
    return Gio.File.new_for_path(path).get_uri()


def now():
    return time.time()


def translate(widget, target, x, y):
    """widget coordinates -> target coordinates (PyGObject returns (x, y) or None)."""
    result = widget.translate_coordinates(target, x, y)
    if result is None:
        return x, y
    if len(result) == 3:
        return result[1], result[2]
    return result
