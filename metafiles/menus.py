"""Popover menus built from simple entry lists (context menus, sidebar menus).

Entries:
  ("item", label, callback[, sensitive[, accel_action]])
  ("check", label, active, callback)
  ("radio", label, active, callback)
  ("submenu", label, [entries])
  ("section", [entries])          # a separated group
"""

import itertools

from gi.repository import Gdk, Gio, GLib, Gtk

_counter = itertools.count()


def build_menu(entries, group, accel_for=None):
    menu = Gio.Menu()
    section = Gio.Menu()

    def flush():
        nonlocal section
        if section.get_n_items():
            menu.append_section(None, section)
        section = Gio.Menu()

    for entry in entries:
        if entry is None:
            continue
        kind = entry[0]
        if kind == "section":
            flush()
            sub = build_menu(entry[1], group, accel_for)
            if sub.get_n_items():
                menu.append_section(None, sub)
            continue
        if kind == "separator":
            flush()
            continue
        name = f"a{next(_counter)}"
        if kind == "submenu":
            section.append_submenu(entry[1], build_menu(entry[2], group, accel_for))
            continue
        if kind in ("check", "radio"):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", bool(entry[2])))
            action.connect("activate", lambda _a, _p, cb=entry[3]: GLib.idle_add(lambda: cb() and False))
        else:
            action = Gio.SimpleAction.new(name, None)
            action.set_enabled(entry[3] if len(entry) > 3 else True)
            action.connect("activate", lambda _a, _p, cb=entry[2]: GLib.idle_add(lambda: cb() and False))
        group.add_action(action)
        item = Gio.MenuItem.new(entry[1], f"m.{name}")
        if kind == "item" and len(entry) > 4 and entry[4] and accel_for is not None:
            accel = accel_for(entry[4])
            if accel:
                item.set_attribute_value("accel", GLib.Variant("s", accel))
        section.append_item(item)
    flush()
    return menu


def popup(widget, entries, x=None, y=None, accel_for=None, flat=False):
    """Show a popover menu anchored at widget (optionally at x, y in widget coordinates)."""
    group = Gio.SimpleActionGroup()
    model = build_menu(entries, group, accel_for)
    popover = Gtk.PopoverMenu.new_from_model_full(model, Gtk.PopoverMenuFlags.NESTED if flat else 0)
    popover.insert_action_group("m", group)
    popover.set_has_arrow(x is None)
    popover.set_parent(widget)
    if x is not None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_halign(Gtk.Align.START)

    def closed(p):
        GLib.idle_add(lambda: p.unparent() and False)

    popover.connect("closed", closed)
    popover.popup()
    return popover
