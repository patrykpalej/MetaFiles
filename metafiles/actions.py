"""User-defined custom actions: parsing, matching and command expansion.

Actions live in the ``"actions"`` list of ``config.json``. Each item is a dict::

    {
      "label": "Zmniejsz do 50%",            # required
      "command": ["convert", "%f", "-resize", "50%%", "%f"],  # required; list or string
      "context": ["files"],                   # required; string or list of CONTEXTS
      "id": "resize",                         # optional, default: slug of label
      "icon": "image-x-generic-symbolic",     # optional
      "mime": ["image/*"],                    # optional fnmatch globs
      "extensions": ["png", "jpg"],           # optional, no dots, case-insensitive
      "min": 1, "max": null,                  # optional selection count bounds
      "shortcut": "<Control><Alt>r",          # optional
      "terminal": false, "confirm": false,    # optional
      "shell": false, "enabled": true         # optional
    }

A string command is split with :func:`shlex.split` unless ``"shell": true``,
in which case it is kept as a single shell script (``command == [script]``).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import unicodedata
from dataclasses import dataclass, field

__all__ = [
    "CONTEXTS",
    "CustomAction",
    "DEFAULT_ACTIONS",
    "parse_actions",
    "load_actions_file",
    "expand_command",
    "matches",
    "is_available",
]

# files: selection of files; folders: selection of folders;
# folder: empty area / current folder; project: project menu & empty area in a project.
CONTEXTS = ("files", "folders", "folder", "project")

SHELL = "/bin/sh"
SHELL_ARGV0 = "metafiles-action"


@dataclass
class CustomAction:
    id: str
    label: str
    command: list[str]
    contexts: set[str]
    icon: str = ""
    mime_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    min_count: int = 1
    max_count: int | None = None
    shortcut: str = ""
    terminal: bool = False
    confirm: bool = False
    shell: bool = False
    enabled: bool = True


DEFAULT_ACTIONS: list[dict] = [
    {
        "id": "vscode-project",
        "label": "Otwórz projekt w VS Code",
        "command": ["code", "%p"],
        "context": ["project"],
        "icon": "text-editor-symbolic",
    },
    {
        "id": "vscode",
        "label": "Otwórz w VS Code",
        "command": ["code", "%F"],
        "context": ["files", "folders"],
        "icon": "text-editor-symbolic",
    },
    {
        "id": "text-editor",
        "label": "Otwórz w edytorze tekstu",
        "command": ["gnome-text-editor", "%F"],
        "context": ["files"],
        "mime": ["text/*"],
        "icon": "text-x-generic-symbolic",
    },
    {
        "id": "metaterminal",
        "label": "Otwórz w MetaTerminalu",
        "command": ["metaterminal", "%p"],
        "context": ["project"],
        "icon": "utilities-terminal-symbolic",
    },
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SLUG_TRANSLATE = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "ß": "ss"})


def _slugify(label: str) -> str:
    text = unicodedata.normalize("NFKD", label.translate(_SLUG_TRANSLATE))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "action"


def _string_list(value, what: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError(f"'{what}' must be a string or a list of strings")


def _uses_placeholder(script: str, letters: str) -> bool:
    return any(m.group(1) in letters for m in _PLACEHOLDER_RE.finditer(script))


def _parse_one(item, used_ids: set[str]) -> CustomAction:
    if not isinstance(item, dict):
        raise ValueError("definition must be an object")
    label = item.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("missing 'label'")

    def flag(key: str, default: bool) -> bool:
        value = item.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"'{key}' must be true or false")
        return value

    shell = flag("shell", False)
    terminal = flag("terminal", False)
    confirm = flag("confirm", False)
    enabled = flag("enabled", True)

    if "command" not in item:
        raise ValueError("missing 'command'")
    raw = item["command"]
    if isinstance(raw, str):
        if shell:
            command = [raw]
        else:
            try:
                command = shlex.split(raw)
            except ValueError as exc:
                raise ValueError(f"cannot parse 'command': {exc}") from None
    elif isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        command = [shlex.join(raw)] if shell else list(raw)
    else:
        raise ValueError("'command' must be a string or a list of strings")
    if not command or not command[0].strip():
        raise ValueError("'command' is empty")
    if shell and _uses_placeholder(command[0], "n") and _uses_placeholder(command[0], "Ff"):
        raise ValueError("a shell command cannot combine %n with %F or %f")

    if "context" not in item:
        raise ValueError("missing 'context'")
    contexts = _string_list(item["context"], "context")
    if not contexts:
        raise ValueError("'context' is empty")
    for ctx in contexts:
        if ctx not in CONTEXTS:
            raise ValueError(f"unknown context '{ctx}'")

    icon = item.get("icon", "")
    if not isinstance(icon, str):
        raise ValueError("'icon' must be a string")
    shortcut = item.get("shortcut", "")
    if not isinstance(shortcut, str):
        raise ValueError("'shortcut' must be a string")
    mime = _string_list(item.get("mime", []), "mime")
    extensions = [e.strip().lstrip(".").lower() for e in _string_list(item.get("extensions", []), "extensions")]
    extensions = [e for e in extensions if e]

    min_count = item.get("min", 1)
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 0:
        raise ValueError("'min' must be a non-negative integer")
    max_count = item.get("max")
    if max_count is not None:
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < min_count:
            raise ValueError("'max' must be an integer not smaller than 'min'")

    action_id = item.get("id")
    if action_id is not None:
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("'id' must be a non-empty string")
        if action_id in used_ids:
            raise ValueError(f"duplicate id '{action_id}'")
    else:
        base = _slugify(label)
        action_id, n = base, 2
        while action_id in used_ids:
            action_id, n = f"{base}-{n}", n + 1

    return CustomAction(
        id=action_id, label=label, command=command, contexts=set(contexts),
        icon=icon, mime_types=mime, extensions=extensions,
        min_count=min_count, max_count=max_count, shortcut=shortcut,
        terminal=terminal, confirm=confirm, shell=shell, enabled=enabled,
    )


def parse_actions(data) -> tuple[list[CustomAction], list[str]]:
    """Parse action definitions.

    ``data`` is the list of definitions, or a dict holding it under
    ``"actions"`` (the whole config). Invalid definitions are skipped and
    reported as human-readable messages such as
    ``Action “X”: unknown context 'foo'``; valid ones are still returned.
    """
    if isinstance(data, dict):
        data = data.get("actions", [])
    if data is None:
        return [], []
    if not isinstance(data, list):
        return [], ["'actions' must be a list"]
    actions: list[CustomAction] = []
    errors: list[str] = []
    used_ids: set[str] = set()
    for index, item in enumerate(data):
        label = item.get("label") if isinstance(item, dict) else None
        name = label if isinstance(label, str) and label.strip() else f"#{index + 1}"
        try:
            action = _parse_one(item, used_ids)
        except ValueError as exc:
            errors.append(f"Action “{name}”: {exc}")
            continue
        used_ids.add(action.id)
        actions.append(action)
    return actions, errors


def load_actions_file(path: str) -> tuple[list[CustomAction], list[str]]:
    """Read the ``"actions"`` key of the JSON file at ``path``.

    A missing file yields ``([], [])``; unreadable or malformed JSON yields
    ``([], [message])``. Defaults are not written here.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [f"Cannot read {path}: {exc}"]
    if not isinstance(data, dict):
        return [], [f"Cannot read {path}: top level must be an object"]
    return parse_actions(data.get("actions", []))


# ---------------------------------------------------------------------------
# Command expansion
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"%(.)", re.DOTALL)


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def expand_command(action: CustomAction, paths: list[str], folder: str,
                   project: str | None) -> tuple[list[str], dict[str, str]]:
    """Build ``(argv, env_extra)`` for running ``action``; no shell involved.

    ``env_extra`` always contains ``METAFILES_FOLDER`` (= ``folder``) and
    ``METAFILES_PROJECT`` (= ``project``, or ``folder`` outside a project),
    to be merged into the child's environment.

    Normal actions (``shell`` false), per argument of ``action.command``:

    * an argument that is exactly ``%F`` (``%n``) expands into one argv entry
      per selected path (file name); with no selection it disappears;
    * an argument that is exactly ``%f`` becomes the first path, or
      disappears with no selection;
    * otherwise placeholders are substituted inside the argument:
      ``%F``/``%n`` -> paths/names joined with one space, ``%f`` -> first
      path (or empty), ``%d`` -> folder, ``%p`` -> project root (or folder),
      ``%%`` -> ``%``. Unknown ``%x`` sequences are kept as they are.

    Values are copied verbatim into argv entries, so spaces, quotes, ``$``,
    ``;`` or newlines in paths are never interpreted.

    Shell actions (``shell`` true) return
    ``["/bin/sh", "-c", script, "metafiles-action", *args]`` where the
    placeholders in ``script`` are replaced by shell references only:

    * ``%F`` -> ``"$@"``, ``%f`` -> ``"$1"``, and ``args`` are the paths;
    * ``%n`` -> ``"$@"`` and ``args`` are the file names (a script can't use
      ``%n`` together with ``%F``/``%f``; parse_actions rejects that);
    * ``%d`` -> ``"$METAFILES_FOLDER"``, ``%p`` -> ``"$METAFILES_PROJECT"``;
    * ``%%`` -> ``%``.

    Paths are therefore never interpolated into the script text.
    """
    project_root = project or folder
    names = [_basename(p) for p in paths]
    env_extra = {"METAFILES_FOLDER": folder, "METAFILES_PROJECT": project_root}

    if action.shell:
        script = action.command[0]
        if _uses_placeholder(script, "n") and _uses_placeholder(script, "Ff"):
            raise ValueError("a shell command cannot combine %n with %F or %f")
        mapping = {
            "F": '"$@"', "f": '"$1"', "n": '"$@"',
            "d": '"$METAFILES_FOLDER"', "p": '"$METAFILES_PROJECT"', "%": "%",
        }
        script = _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), script)
        args = names if _uses_placeholder(action.command[0], "n") else paths
        return [SHELL, "-c", script, SHELL_ARGV0, *args], env_extra

    values = {
        "F": " ".join(paths), "n": " ".join(names), "f": paths[0] if paths else "",
        "d": folder, "p": project_root, "%": "%",
    }
    argv: list[str] = []
    for arg in action.command:
        if arg == "%F":
            argv.extend(paths)
        elif arg == "%n":
            argv.extend(names)
        elif arg == "%f":
            if paths:
                argv.append(paths[0])
        else:
            argv.append(_PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), arg))
    return argv, env_extra


# ---------------------------------------------------------------------------
# Matching and availability
# ---------------------------------------------------------------------------

def _matches_extension(path: str, extensions: list[str]) -> bool:
    name = _basename(path).lower()
    return any(name.endswith("." + ext) and len(name) > len(ext) + 1 for ext in extensions)


def matches(action: CustomAction, context: str, items: list[tuple[str, str, bool]]) -> bool:
    """Whether ``action`` applies in ``context`` to ``items`` (path, mime, is_dir).

    Disabled actions never match. For ``folder`` and ``project`` it is enough
    that the action lists the context (items and filters are ignored). For
    ``files``/``folders`` every item must be of a kind the action accepts
    (a file needs ``files``, a folder needs ``folders``; an action with both
    accepts mixed selections), the count must lie in ``[min_count,
    max_count]``, every item's MIME type must match one of the globs (if
    any) and every item's name must end with one of the extensions (if any).
    """
    if not action.enabled or context not in action.contexts:
        return False
    if context in ("folder", "project"):
        return True
    count = len(items)
    if count < max(action.min_count, 1):
        return False
    if action.max_count is not None and count > action.max_count:
        return False
    for path, mime, is_dir in items:
        if ("folders" if is_dir else "files") not in action.contexts:
            return False
        if action.mime_types and not any(
            fnmatch.fnmatchcase((mime or "").lower(), glob.lower()) for glob in action.mime_types
        ):
            return False
        if action.extensions and not _matches_extension(path, action.extensions):
            return False
    return True


def is_available(action: CustomAction) -> bool:
    """Whether the action's program exists (shell actions are always available)."""
    if action.shell:
        return True
    program = action.command[0] if action.command else ""
    if not program:
        return False
    if program.startswith("%"):
        return True  # the program is a selected file itself
    if os.path.isabs(program):
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return shutil.which(program) is not None
