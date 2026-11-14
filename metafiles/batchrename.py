"""Batch rename logic: computing new names, validating them and renaming safely.

This module is pure Python (no GTK). The dialog computes a live preview with
:func:`compute_new_names` and :func:`validate`, then performs the rename with
:func:`plan_renames` + :func:`apply_plan`. Undo is the same pipeline fed with
:func:`undo_pairs`.

Order of the transformation steps applied to every name:

1. find / replace (on the stem, or on the full name with ``include_extension``),
2. case change (same target as step 1),
3. prefix and suffix (the suffix goes before the extension),
4. numbering (``number_format`` applied to the stem),
5. extension change.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "RenameOptions",
    "split_name",
    "compute_new_names",
    "validate",
    "plan_renames",
    "apply_plan",
    "undo_pairs",
    "TEMP_PREFIX",
]

COMPOUND_EXTENSIONS = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")
TEMP_PREFIX = ".metafiles-rename-"
NAME_MAX = 255


@dataclass
class RenameOptions:
    find: str = ""
    replace: str = ""
    regex: bool = False
    case_sensitive: bool = True
    prefix: str = ""
    suffix: str = ""  # added before the extension
    numbering: bool = False
    # Template applied to the stem; placeholders: {name} (name after the
    # previous steps), {n} (padded number), {i} (unpadded number).
    number_format: str = "{name} {n}"
    start: int = 1
    padding: int = 1
    case: str = "none"  # none | lower | upper | title (applies to the stem)
    # keep | lower | upper | remove | any other string = new extension (no dot)
    extension: str = "keep"
    # Whether find/replace and case operate on the full name instead of the stem.
    include_extension: bool = False


# ---------------------------------------------------------------------------
# Name splitting
# ---------------------------------------------------------------------------

def split_name(name: str) -> tuple[str, str]:
    """Split ``name`` into ``(stem, extension)``; the extension keeps its dot.

    Follows Nautilus' rules: a leading dot does not start an extension
    (``".bashrc"`` -> ``(".bashrc", "")``), a trailing dot is not an
    extension, and ``.tar.gz``/``.tar.xz``/``.tar.bz2``/``.tar.zst`` are one
    extension. Additionally, a "extension" containing whitespace is not
    treated as one (``"Notes v1.2 final"`` has no extension).
    """
    lower = name.lower()
    for compound in COMPOUND_EXTENSIONS:
        if lower.endswith(compound) and len(name) > len(compound):
            stem = name[: -len(compound)]
            if stem.strip("."):
                return stem, name[-len(compound):]
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    ext = name[dot:]
    if any(ch.isspace() for ch in ext):
        return name, ""
    return name[:dot], ext


# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------

_REF_RE = re.compile(
    r"\\(\\|\d+|g<([^>]*)>)"  # \\  \1  \g<1>  \g<name>
    r"|\$(\$|\d+|\{([^}]*)\})"  # $$  $1  ${1}  ${name}
)


def _parse_replacement(template: str, pattern: re.Pattern) -> list:
    """Parse a replacement template into literal strings and group refs.

    Supports ``\\1``, ``\\g<1>``, ``\\g<name>``, ``$1``, ``${1}``,
    ``${name}``; ``\\\\`` and ``$$`` produce a literal backslash / dollar.
    Other backslashes are kept literally. Raises ``ValueError`` for
    references to groups the pattern does not have.
    """
    parts: list = []
    pos = 0
    for m in _REF_RE.finditer(template):
        parts.append(template[pos:m.start()])
        pos = m.end()
        if m.group(1) is not None:
            token, ref = m.group(1), m.group(2)
            if token == "\\":
                parts.append("\\")
                continue
            ref = ref if ref is not None else token
        else:
            token, ref = m.group(3), m.group(4)
            if token == "$":
                parts.append("$")
                continue
            ref = ref if ref is not None else token
        if ref.isdigit():
            index = int(ref)
            if index > pattern.groups:
                raise ValueError(f"Invalid group reference: {m.group(0)}")
            parts.append(index)
        else:
            if ref not in pattern.groupindex:
                raise ValueError(f"Unknown group name: {m.group(0)}")
            parts.append(pattern.groupindex[ref])
    parts.append(template[pos:])
    return parts


def _make_replacer(parts: list):
    def replacer(match: re.Match) -> str:
        out = []
        for part in parts:
            if isinstance(part, int):
                out.append(match.group(part) or "")
            else:
                out.append(part)
        return "".join(out)

    return replacer


def _build_substitution(options: RenameOptions):
    """Return a function str -> str implementing find/replace, or None."""
    if not options.find:
        return None
    flags = 0 if options.case_sensitive else re.IGNORECASE
    if options.regex:
        try:
            pattern = re.compile(options.find, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        replacer = _make_replacer(_parse_replacement(options.replace, pattern))
    else:
        pattern = re.compile(re.escape(options.find), flags)
        literal = options.replace
        replacer = lambda _m: literal  # noqa: E731
    return lambda text: pattern.sub(replacer, text)


_TITLE_WORD_RE = re.compile(r"[^\s\-_.]+")


def _title_case(text: str) -> str:
    return _TITLE_WORD_RE.sub(lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), text)


def _apply_case(text: str, case: str) -> str:
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    if case == "title":
        return _title_case(text)
    return text


def _apply_extension(ext: str, mode: str) -> str:
    if mode in ("", "keep"):
        return ext
    if mode == "lower":
        return ext.lower()
    if mode == "upper":
        return ext.upper()
    if mode == "remove":
        return ""
    new = mode.lstrip(".")
    return "." + new if new else ""


# ---------------------------------------------------------------------------
# Public API: computing and validating names
# ---------------------------------------------------------------------------

def compute_new_names(names: list[str], options: RenameOptions) -> list[str]:
    """Return the new names for ``names`` (same order) according to ``options``.

    Raises ``ValueError`` for an invalid regular expression or an invalid
    group reference in the replacement.
    """
    substitute = _build_substitution(options)
    result = []
    for index, name in enumerate(names):
        if options.include_extension:
            work = name
            if substitute:
                work = substitute(work)
            work = _apply_case(work, options.case)
            stem, ext = split_name(work)
        else:
            stem, ext = split_name(name)
            if substitute:
                stem = substitute(stem)
            stem = _apply_case(stem, options.case)
        stem = options.prefix + stem + options.suffix
        if options.numbering:
            number = options.start + index
            padded = str(number).zfill(max(1, options.padding)) if number >= 0 \
                else "-" + str(-number).zfill(max(1, options.padding))
            stem = (
                options.number_format.replace("{name}", "\0NAME\0")
                .replace("{n}", padded)
                .replace("{i}", str(number))
                .replace("\0NAME\0", stem)
            )
        result.append(stem + _apply_extension(ext, options.extension))
    return result


def validate(old_names: list[str], new_names: list[str], existing: set[str]) -> list[str | None]:
    """Return an error code (or None) for every new name.

    Codes: ``"empty"``, ``"invalid"`` (contains ``/`` or NUL, is ``.``/``..``
    or longer than 255 bytes), ``"duplicate"`` (several items would get the
    same name; all of them are flagged), ``"exists"`` (the name is taken in
    the folder by something that is not being renamed away).
    """
    counts: dict[str, int] = {}
    for new in new_names:
        counts[new] = counts.get(new, 0) + 1
    renamed_away = set(old_names)
    errors: list[str | None] = []
    for old, new in zip(old_names, new_names):
        if not new:
            errors.append("empty")
        elif "/" in new or "\0" in new or new in (".", "..") \
                or len(new.encode("utf-8", "surrogateescape")) > NAME_MAX:
            errors.append("invalid")
        elif counts[new] > 1:
            errors.append("duplicate")
        elif new != old and new in existing and new not in renamed_away:
            errors.append("exists")
        else:
            errors.append(None)
    return errors


# ---------------------------------------------------------------------------
# Public API: planning and performing renames
# ---------------------------------------------------------------------------

def _temp_name(counter: int, old: str, taken: set[str]) -> str:
    while True:
        base = f"{TEMP_PREFIX}{counter}-"
        budget = NAME_MAX - len(base.encode())
        tail = old.encode("utf-8", "surrogateescape")[:budget].decode("utf-8", "ignore")
        candidate = base + tail
        if candidate not in taken:
            return candidate
        counter += 1


def plan_renames(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order ``(old, new)`` renames in one folder so none overwrites another.

    Unchanged pairs are dropped. A rename whose target is not the current
    name of another pending item is done directly; cycles (swaps, rotations)
    are broken by moving one item to a temporary name
    (``.metafiles-rename-<n>-<old>``) first. Raises ``ValueError`` when two
    items share a target or one item appears twice.
    """
    pending: dict[str, str] = {}
    targets: set[str] = set()
    for old, new in pairs:
        if old == new:
            continue
        if old in pending:
            raise ValueError(f"Duplicate source name: {old}")
        if new in targets:
            raise ValueError(f"Duplicate target name: {new}")
        pending[old] = new
        targets.add(new)

    taken = set(pending) | targets
    steps: list[tuple[str, str]] = []
    counter = 0
    while pending:
        progressed = False
        for old in list(pending):
            new = pending[old]
            if new not in pending:
                steps.append((old, new))
                del pending[old]
                progressed = True
        if progressed:
            continue
        # Every remaining item is part of a cycle: park one under a temp name.
        old = next(iter(pending))
        temp = _temp_name(counter, old, taken)
        counter += 1
        taken.add(temp)
        steps.append((old, temp))
        pending[temp] = pending.pop(old)
    return steps


def apply_plan(directory: str, steps: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Perform ``steps`` inside ``directory`` with :func:`os.rename`.

    Before each step the target is checked with :func:`os.path.lexists`;
    an existing target raises ``FileExistsError``. On any failure the steps
    already performed are rolled back in reverse order and the error is
    re-raised. Returns the effective ``(original_old, final_new)`` pairs
    (temporary names collapsed), suitable for :func:`undo_pairs`.
    """
    done: list[tuple[str, str]] = []
    try:
        for old, new in steps:
            src = os.path.join(directory, old)
            dst = os.path.join(directory, new)
            if os.path.lexists(dst):
                raise FileExistsError(17, "File exists", dst)
            os.rename(src, dst)
            done.append((old, new))
    except BaseException:
        for old, new in reversed(done):
            try:
                os.rename(os.path.join(directory, new), os.path.join(directory, old))
            except OSError:
                pass
        raise

    origin: dict[str, str] = {}  # current name -> original name
    for old, new in done:
        origin[new] = origin.pop(old, old)
    return [(orig, cur) for cur, orig in origin.items() if orig != cur]


def undo_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the inverse ``(new, old)`` pairs to feed into :func:`plan_renames`."""
    return [(new, old) for old, new in pairs]
