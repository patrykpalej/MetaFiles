"""Persistent session and configuration stored under ~/.config/metafiles."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field

from metafiles.i18n import _

# Shortcuts not taken from Nautilus' table: projects, panels and the extra features.
DEFAULT_SHORTCUTS = {
    "prev-project": ["<Alt>Page_Up"],
    "next-project": ["<Alt>Page_Down"],
    "toggle-projects": ["<Control><Shift>s"],
    "toggle-focus": ["<Control>Tab"],
    "switch-to-loose": ["<Control><Alt>0"],
    **{f"switch-to-project-{i}": [f"<Control><Alt>{i}"] for i in range(1, 10)},
    "quick-open": ["<Control>p"],
    "toggle-preview": ["<Alt>p"],
    "toggle-split": ["F3"],
    "switch-pane": ["F6"],
    "copy-to-other-pane": ["<Shift>F5"],
    "move-to-other-pane": ["<Shift>F6"],
    "toggle-terminal": ["F4"],
    "new-file": ["<Control><Alt>n"],
    "duplicate": ["<Control><Shift>d"],
    "copy-full-path": ["<Control><Shift>c"],
    "copy-relative-path": ["<Control><Alt>c"],
    "toggle-git-ignored": ["<Control><Shift>h"],
}

VIEW_MODES = ("list", "tree", "grid")


def config_dir():
    if os.environ.get("METAFILES_CONFIG_DIR"):
        return os.environ["METAFILES_CONFIG_DIR"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "metafiles")


def new_tab_state(location, view=""):
    """A tab as stored in the session: one or two panes plus its panels."""
    return {
        "panes": [{"location": location, "view": view}],
        "active_pane": 0,
        "name": "",
        "preview": False,
        "terminal": False,
        "terminal_height": 220,
        "preview_width": 320,
    }


def normalize_tab(tab):
    """Accept partial or legacy tab entries (a bare location string)."""
    if isinstance(tab, str):
        tab = {"panes": [{"location": tab}]}
    if not isinstance(tab, dict):
        return None
    panes = [p for p in tab.get("panes", []) if isinstance(p, dict) and isinstance(p.get("location"), str)][:2]
    if not panes:
        return None
    result = new_tab_state(panes[0]["location"])
    result.update({k: v for k, v in tab.items() if k in result and k != "panes"})
    result["panes"] = [{"location": p["location"], "view": p.get("view") if p.get("view") in VIEW_MODES else ""}
                       for p in panes]
    if not isinstance(result["active_pane"], int) or not 0 <= result["active_pane"] < len(result["panes"]):
        result["active_pane"] = 0
    if not isinstance(result["name"], str):
        result["name"] = ""
    return result


@dataclass
class ProjectState:
    path: str
    name: str
    # Navigation lock: tabs cannot leave the project directory.
    lock: bool = True
    tabs: list = field(default_factory=list)
    active_tab: int = 0
    # The general entry: browsing not tied to a project, never locked.
    loose: bool = False
    # Optional group name; "" means the project is not in a group.
    group: str = ""
    # Project bookmarks: absolute paths of folders inside the project.
    bookmarks: list = field(default_factory=list)
    hide_git_ignored: bool = False


LOOSE_NAME = _("Unassigned tabs")
OLD_LOOSE_NAMES = ("Unassigned tabs", "Nieprzypisane karty")


def loose_project():
    return ProjectState(path=os.path.expanduser("~"), name=LOOSE_NAME, lock=False, loose=True)


@dataclass
class SessionState:
    projects: list = field(default_factory=list)
    active_project: int = 0
    window_width: int = 1280
    window_height: int = 800
    maximized: bool = False
    sidebar_width: int = 300
    sidebar_visible: bool = True
    places_width: int = 220
    places_visible: bool = True
    places_mode: str = "places"
    statusbar_visible: bool = True
    # None: follow Nautilus' show-hidden-files; True/False: the user's choice in MetaFiles.
    show_hidden: object = None
    # Display order of project groups.
    groups: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        projects = []
        for p in data.get("projects", []):
            if not isinstance(p, dict) or not isinstance(p.get("path"), str):
                continue
            state = ProjectState(**{k: v for k, v in p.items() if k in ProjectState.__dataclass_fields__})
            state.name = state.name if isinstance(state.name, str) and state.name else (
                os.path.basename(state.path.rstrip("/")) or state.path)
            state.tabs = [t for t in (normalize_tab(t) for t in state.tabs if t is not None) if t]
            state.bookmarks = [b for b in state.bookmarks if isinstance(b, str)]
            projects.append(state)
        known["projects"] = projects
        known["groups"] = [g for g in known.get("groups", []) if isinstance(g, str)]
        if known.get("places_mode") not in ("places", "tree"):
            known.pop("places_mode", None)
        if known.get("show_hidden") not in (None, True, False):
            known.pop("show_hidden", None)
        state = cls(**known)
        state.ensure_loose()
        return state

    def ensure_loose(self):
        """Normalize: one general entry first, then ungrouped projects, then each group."""
        loose = [p for p in self.projects if p.loose]
        rest = [p for p in self.projects if not p.loose]
        entry = loose[0] if loose else loose_project()
        entry.lock = False
        entry.group = ""
        if entry.name in OLD_LOOSE_NAMES:
            entry.name = LOOSE_NAME
        for p in rest:
            p.group = p.group.strip() if isinstance(p.group, str) else ""
        used = []
        for p in rest:
            if p.group and p.group not in used:
                used.append(p.group)
        groups = []
        for g in self.groups:
            g = g.strip() if isinstance(g, str) else ""
            if g and g not in groups:
                groups.append(g)
        self.groups = groups + [g for g in used if g not in groups]
        ungrouped = [p for p in rest if not p.group]
        grouped = [p for g in self.groups for p in rest if p.group == g]
        self.projects = [entry] + ungrouped + grouped
        return self


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"metafiles: ignoring unreadable {path}: {e}")
        return None


def load_session(directory=None):
    data = read_json(os.path.join(directory or config_dir(), "session.json"))
    if not isinstance(data, dict):
        return SessionState().ensure_loose()
    try:
        return SessionState.from_dict(data)
    except TypeError as e:
        print(f"metafiles: invalid session file, starting fresh: {e}")
        return SessionState().ensure_loose()


def save_session(state, directory=None):
    atomic_write_json(os.path.join(directory or config_dir(), "session.json"), asdict(state))


def config_path(directory=None):
    return os.path.join(directory or config_dir(), "config.json")


def default_config():
    from metafiles.actions import DEFAULT_ACTIONS

    return {"shortcuts": DEFAULT_SHORTCUTS, "actions": DEFAULT_ACTIONS}


def load_config(directory=None):
    """Return {"shortcuts": {...}, "actions": [raw dicts]}, creating the file on first run."""
    path = config_path(directory)
    data = read_json(path)
    if not isinstance(data, dict):
        data = default_config()
        if not os.path.exists(path):
            atomic_write_json(path, data)
    shortcuts = {k: list(v) for k, v in DEFAULT_SHORTCUTS.items()}
    for action, accels in (data.get("shortcuts") or {}).items():
        if isinstance(accels, str):
            accels = [accels]
        if isinstance(accels, list):
            shortcuts[action] = [a for a in accels if isinstance(a, str) and a != "disabled"]
    actions = data.get("actions")
    return {"shortcuts": shortcuts, "actions": actions if isinstance(actions, list) else []}
