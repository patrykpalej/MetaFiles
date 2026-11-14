"""The user's default gnome-terminal profile and copy/paste keys (ported from MetaTerminal)."""

from dataclasses import dataclass, field

from gi.repository import Gio

PROFILES_LIST_SCHEMA = "org.gnome.Terminal.ProfilesList"
PROFILE_SCHEMA = "org.gnome.Terminal.Legacy.Profile"
PROFILE_PATH = "/org/gnome/terminal/legacy/profiles:/:{uuid}/"
KEYBINDINGS_SCHEMA = "org.gnome.Terminal.Legacy.Keybindings"
KEYBINDINGS_PATH = "/org/gnome/terminal/legacy/keybindings/"


def _schema_exists(schema_id):
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


def build_accel_map(lookup):
    """Return {action: [accels]} using lookup(gnome_key) -> accel string."""
    result = {}
    for gnome_key, action in GNOME_ACTION_MAP.items():
        accel = lookup(gnome_key)
        accels = [] if not accel or accel == "disabled" else [accel]
        if accels:
            accels += EXTRA_ACCELS.get(gnome_key, [])
        result[action] = accels
    return result


def terminal_keys():
    """{'copy': accel, 'paste': accel} from gnome-terminal, with its stock defaults."""
    keys = {"copy": "<Control><Shift>c", "paste": "<Control><Shift>v"}
    if _schema_exists(KEYBINDINGS_SCHEMA):
        settings = Gio.Settings.new_with_path(KEYBINDINGS_SCHEMA, KEYBINDINGS_PATH)
        for key in keys:
            value = settings.get_string(key)
            if value and value != "disabled":
                keys[key] = value
    return keys


def _rgb_or_none(value):
    return value if isinstance(value, str) and value else None


@dataclass
class TerminalProfile:
    font: str = "Monospace 12"
    use_theme_colors: bool = True
    foreground: str = None
    background: str = None
    palette: list = field(default_factory=list)
    bold_color: str = None
    cursor_colors: tuple = None
    highlight_colors: tuple = None
    bold_is_bright: bool = False
    background_alpha: float = 1.0
    cursor_shape: str = "block"
    cursor_blink_mode: str = "system"
    audible_bell: bool = True
    scrollback_lines: int = 10000
    scroll_on_output: bool = False
    scroll_on_keystroke: bool = True
    cell_height_scale: float = 1.0
    cell_width_scale: float = 1.0


class GnomeProfile:
    """The default gnome-terminal profile, re-read whenever it changes."""

    def __init__(self, on_changed):
        self._on_changed = on_changed
        self._profile = None
        self._interface = None
        if _schema_exists("org.gnome.desktop.interface"):
            self._interface = Gio.Settings.new("org.gnome.desktop.interface")
            self._interface.connect("changed::monospace-font-name", lambda *_: on_changed())
        if not (_schema_exists(PROFILES_LIST_SCHEMA) and _schema_exists(PROFILE_SCHEMA)):
            return
        uuid = Gio.Settings.new(PROFILES_LIST_SCHEMA).get_string("default")
        if uuid:
            self._profile = Gio.Settings.new_with_path(PROFILE_SCHEMA, PROFILE_PATH.format(uuid=uuid))
            self._profile.connect("changed", lambda *_: on_changed())

    def _system_font(self):
        if self._interface is not None:
            return self._interface.get_string("monospace-font-name") or "Monospace 12"
        return "Monospace 12"

    def read(self):
        s = self._profile
        if s is None:
            return TerminalProfile(font=self._system_font())
        transparent = s.get_boolean("use-transparent-background") and not s.get_boolean(
            "use-theme-transparency"
        )
        alpha = 1.0 - s.get_int("background-transparency-percent") / 100.0 if transparent else 1.0
        return TerminalProfile(
            font=self._system_font() if s.get_boolean("use-system-font") else s.get_string("font"),
            use_theme_colors=s.get_boolean("use-theme-colors"),
            foreground=_rgb_or_none(s.get_string("foreground-color")),
            background=_rgb_or_none(s.get_string("background-color")),
            palette=list(s.get_strv("palette")),
            bold_color=None if s.get_boolean("bold-color-same-as-fg") else s.get_string("bold-color"),
            cursor_colors=(
                (s.get_string("cursor-foreground-color"), s.get_string("cursor-background-color"))
                if s.get_boolean("cursor-colors-set")
                else None
            ),
            highlight_colors=(
                (s.get_string("highlight-foreground-color"), s.get_string("highlight-background-color"))
                if s.get_boolean("highlight-colors-set")
                else None
            ),
            bold_is_bright=s.get_boolean("bold-is-bright"),
            background_alpha=max(0.0, min(1.0, alpha)),
            cursor_shape=s.get_string("cursor-shape"),
            cursor_blink_mode=s.get_string("cursor-blink-mode"),
            audible_bell=s.get_boolean("audible-bell"),
            scrollback_lines=-1 if s.get_boolean("scrollback-unlimited") else s.get_int("scrollback-lines"),
            scroll_on_output=s.get_boolean("scroll-on-output"),
            scroll_on_keystroke=s.get_boolean("scroll-on-keystroke"),
            cell_height_scale=s.get_double("cell-height-scale"),
            cell_width_scale=s.get_double("cell-width-scale"),
        )
