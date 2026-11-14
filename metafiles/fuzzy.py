"""Fuzzy path matching for quick open (Ctrl+P).

Matching is case- and accent-insensitive. Every original character is normalized to
exactly one character, so match positions in the normalized string are also valid
indices into the original path (used for highlighting).

Searching a ``FuzzyIndex`` is done in two steps:

1. a C-level regex pass (one compiled possessive pattern mapped over all normalized
   paths) keeps only the paths that contain the query characters in order;
2. only those candidates are scored with a dynamic-programming matcher. When there
   are too many candidates, a basename-favoured subset is scored (paths whose file
   name alone matches the query first, then the shortest remaining paths).
"""

from __future__ import annotations

import functools
import heapq
import operator
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from itertools import compress
from typing import Callable, Iterable

# -- scoring weights ---------------------------------------------------------
SCORE_MATCH = 16
BONUS_SEGMENT = 10      # match at path start or right after "/"
BONUS_WORD = 8          # match after "_", "-", ".", " "
BONUS_CAMEL = 7         # lower -> upper transition, letter -> digit
BONUS_CONSECUTIVE = 6   # minimal bonus for a match adjacent to the previous one
BONUS_BASENAME_CHAR = 4  # every match inside the file name
BASENAME_BOUNDARY_FACTOR = 2  # boundary bonuses inside the file name are multiplied
GAP_OPEN = 3
GAP_EXTEND = 2
LENGTH_DIVISOR = 2      # penalty: len(path) // LENGTH_DIVISOR

# Maximum number of prefiltered candidates that get the full (slow) score.
MAX_SCORED = 2000
DEFAULT_LIMIT = 200
DEFAULT_MAX_ENTRIES = 300_000

_WORD_SEPARATORS = frozenset("_-. ")
_EXTRA_FOLD = {
    "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "s",
    "æ": "a", "Æ": "a", "œ": "o", "Œ": "o", "ı": "i", "þ": "t", "Þ": "t", "ð": "d", "Ð": "d",
}
_char_cache: dict[str, str] = {}


def _fold_char(char: str) -> str:
    folded = _char_cache.get(char)
    if folded is None:
        base = _EXTRA_FOLD.get(char)
        if base is None:
            decomposed = unicodedata.normalize("NFKD", char)
            base = next((c for c in decomposed if not unicodedata.combining(c)), char)
        folded = base.lower()[:1] or char
        if folded == "\n":
            folded = " "
        _char_cache[char] = folded
    return folded


def normalize(text: str) -> str:
    """Lower-case, strip diacritics; the result has the same length as ``text``."""
    if text.isascii():
        lowered = text.lower()
        return lowered.replace("\n", " ") if "\n" in lowered else lowered
    return "".join([_fold_char(c) for c in text])


def normalize_query(query: str) -> str:
    """Normalized query without whitespace."""
    return "".join(normalize(query).split())


def _basename_start(path: str) -> int:
    end = len(path) - 1 if path.endswith("/") else len(path)
    return path.rfind("/", 0, end) + 1


@functools.lru_cache(maxsize=64)
def _subseq_pattern(query: str) -> re.Pattern:
    """Possessive "characters in order" pattern; possessive classes never backtrack."""
    parts = []
    for char in query:
        escaped = re.escape(char)
        parts.append(f"[^{escaped}]*+{escaped}")
    return re.compile("".join(parts))


def _boundary_bonus(orig: str, norm: str, pos: int) -> int:
    if pos == 0:
        return BONUS_SEGMENT
    prev = norm[pos - 1]
    if prev == "/":
        return BONUS_SEGMENT
    if prev in _WORD_SEPARATORS:
        return BONUS_WORD
    cur_o = orig[pos]
    prev_o = orig[pos - 1]
    if cur_o.isupper() and prev_o.islower():
        return BONUS_CAMEL
    if cur_o.isdigit() and not prev_o.isdigit():
        return BONUS_CAMEL
    return 0


def _score_normalized(query: str, norm: str, orig: str) -> tuple[int, list[int]] | None:
    """Best alignment of ``query`` (normalized) in ``norm`` (normalized ``orig``)."""
    m = len(query)
    n = len(norm)
    if m == 0:
        return 0, []
    if m > n:
        return None
    # Feasible window for every query character: leftmost and rightmost placement.
    lo = [0] * m
    pos = 0
    find = norm.find
    for j, char in enumerate(query):
        pos = find(char, pos)
        if pos < 0:
            return None
        lo[j] = pos
        pos += 1
    hi = [0] * m
    pos = n
    rfind = norm.rfind
    for j in range(m - 1, -1, -1):
        pos = rfind(query[j], 0, pos)
        hi[j] = pos

    base = _basename_start(norm)
    bonus_cache: dict[int, tuple[int, int]] = {}

    def char_values(p: int) -> tuple[int, int]:
        """(base score, boundary bonus) of a match at position p."""
        cached = bonus_cache.get(p)
        if cached is None:
            bonus = _boundary_bonus(orig, norm, p)
            value = SCORE_MATCH
            if p >= base:
                value += BONUS_BASENAME_CHAR
                bonus *= BASENAME_BOUNDARY_FACTOR
            cached = (value, bonus)
            bonus_cache[p] = cached
        return cached

    # Row for the first query character.
    positions: list[int] = []
    scores: list[int] = []
    chunks: list[int] = []
    p = lo[0]
    limit = hi[0]
    char = query[0]
    while 0 <= p <= limit:
        value, bonus = char_values(p)
        positions.append(p)
        scores.append(value + bonus)
        chunks.append(bonus)
        p = find(char, p + 1)
    rows_pos = [positions]
    rows_back: list[list[int]] = [[-1] * len(positions)]

    for j in range(1, m):
        prev_pos, prev_scores, prev_chunks = positions, scores, chunks
        prev_len = len(prev_pos)
        char = query[j]
        positions, scores, chunks, back = [], [], [], []
        k = 0
        best_gap = None  # max(prev_score + GAP_EXTEND * prev_pos)
        best_gap_idx = -1
        p = lo[j]
        limit = hi[j]
        while 0 <= p <= limit:
            # Fold every previous position that leaves at least one gap character.
            while k < prev_len and prev_pos[k] < p - 1:
                candidate = prev_scores[k] + GAP_EXTEND * prev_pos[k]
                if best_gap is None or candidate > best_gap:
                    best_gap = candidate
                    best_gap_idx = k
                k += 1
            value, bonus = char_values(p)
            best = None
            best_idx = -1
            chunk = bonus
            if best_gap is not None:
                best = best_gap - GAP_OPEN - GAP_EXTEND * (p - 1) + value + bonus
                best_idx = best_gap_idx
            if k < prev_len and prev_pos[k] == p - 1:
                prev_chunk = prev_chunks[k]
                consecutive_bonus = max(bonus, prev_chunk, BONUS_CONSECUTIVE)
                candidate = prev_scores[k] + value + consecutive_bonus
                if best is None or candidate >= best:
                    best = candidate
                    best_idx = k
                    chunk = max(prev_chunk, bonus)
            if best is not None:
                positions.append(p)
                scores.append(best)
                chunks.append(chunk)
                back.append(best_idx)
            p = find(char, p + 1)
        if not positions:
            return None
        rows_pos.append(positions)
        rows_back.append(back)

    best_idx = max(range(len(scores)), key=scores.__getitem__)
    total = scores[best_idx] - n // LENGTH_DIVISOR
    result = [0] * m
    for j in range(m - 1, -1, -1):
        result[j] = rows_pos[j][best_idx]
        best_idx = rows_back[j][best_idx]
    return total, result


def score(query: str, candidate: str) -> tuple[int, list[int]] | None:
    """Fuzzy-match ``query`` against ``candidate`` (a "/"-separated relative path).

    Returns ``(score, positions)`` with positions indexing the original candidate,
    or None when the query characters do not all occur in order. Higher is better.
    """
    return _score_normalized(normalize_query(query), normalize(candidate), candidate)


def _sort_key(item: tuple[int, str, list[int]]):
    return (-item[0], len(item[1]), item[1])


class FuzzyIndex:
    """Searchable set of relative paths with precomputed normalized forms."""

    def __init__(self, paths: Iterable[str]):
        self.paths: list[str] = list(paths)
        self.norm: list[str] = [normalize(p) for p in self.paths]
        self._lengths: list[int] = list(map(len, self.norm))
        self._base: list[int] | None = None
        self._last_query: str | None = None
        self._last_candidates: list[int] | None = None

    def __len__(self) -> int:
        return len(self.paths)

    # -- prefilter ---------------------------------------------------------

    def prefilter(self, query: str, candidates: list[int] | None = None) -> list[int]:
        """Sorted indices of paths containing the normalized query chars in order.

        One compiled possessive regex is applied to every normalized path with
        ``map``/``compress``, so the loop runs entirely in C. ``candidates``
        (indices that matched a prefix of ``query``) restricts the search.
        """
        if candidates is None:
            if not query:
                return list(range(len(self.paths)))
            match = _subseq_pattern(query).match
            return list(compress(range(len(self.norm)), map(match, self.norm)))
        if not query:
            return list(candidates)
        match = _subseq_pattern(query).match
        return list(compress(candidates, map(match, map(self.norm.__getitem__, candidates))))

    # -- ranking -----------------------------------------------------------

    def _basenames(self) -> list[int]:
        if self._base is None:
            self._base = [_basename_start(p) for p in self.norm]
        return self._base

    def _select(self, query: str, candidates: list[int]) -> list[int]:
        """Cap the work: basename matches first, then the shortest other paths."""
        if len(candidates) <= MAX_SCORED:
            return candidates
        base = self._basenames()
        match = _subseq_pattern(query).match
        flags = list(map(match, map(self.norm.__getitem__, candidates),
                         map(base.__getitem__, candidates)))
        in_basename = list(compress(candidates, flags))
        length = self._lengths.__getitem__
        if len(in_basename) >= MAX_SCORED:
            return heapq.nsmallest(MAX_SCORED, in_basename, key=length)
        rest = list(compress(candidates, map(operator.not_, flags)))
        return in_basename + heapq.nsmallest(MAX_SCORED - len(in_basename), rest, key=length)

    def rank(self, query: str, candidates: list[int], limit: int = DEFAULT_LIMIT
             ) -> list[tuple[int, str, list[int]]]:
        norm = self.norm
        paths = self.paths
        results = []
        for i in self._select(query, candidates):
            scored = _score_normalized(query, norm[i], paths[i])
            if scored is not None:
                results.append((scored[0], paths[i], scored[1]))
        if len(results) > limit:
            return heapq.nsmallest(limit, results, key=_sort_key)
        results.sort(key=_sort_key)
        return results

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[tuple[int, str, list[int]]]:
        """Best ``limit`` matches as ``(score, path, positions)``, best first.

        When ``query`` extends the previous query only its candidates are searched.
        """
        q = normalize_query(query)
        if not q:
            self._last_query = None
            self._last_candidates = None
            return [(0, p, []) for p in self.paths[:limit]]
        previous = None
        if self._last_query and q.startswith(self._last_query):
            previous = self._last_candidates
        candidates = self.prefilter(q, previous)
        self._last_query = q
        self._last_candidates = candidates
        return self.rank(q, candidates, limit)


_filter_cache: tuple[int, int, FuzzyIndex] | None = None


def fuzzy_filter(query: str, paths: list[str], limit: int = DEFAULT_LIMIT,
                 candidates: list[str] | None = None) -> list[tuple[int, str, list[int]]]:
    """Fuzzy-filter ``paths``; returns ``(score, path, positions)`` sorted best first.

    ``candidates`` (paths that matched a shorter prefix of ``query``) replaces
    ``paths`` as the search pool. The index for ``paths`` is cached between calls
    with the same list object; prefer ``FuzzyIndex`` for repeated searches.
    """
    global _filter_cache
    pool = candidates if candidates is not None else paths
    q = normalize_query(query)
    if not q:
        return [(0, p, []) for p in pool[:limit]]
    cache = _filter_cache
    if cache is not None and cache[0] == id(pool) and cache[1] == len(pool):
        index = cache[2]
    else:
        index = FuzzyIndex(pool)
        _filter_cache = (id(pool), len(pool), index)
    return index.rank(q, index.prefilter(q), limit)


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def _is_hidden(rel: str) -> bool:
    return rel.startswith(".") or "/." in rel


def _git_files(root: str) -> list[str] | None:
    if shutil.which("git") is None:
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        inside = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            env=env, capture_output=True, text=True,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        listed = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "-co", "--exclude-standard"],
            env=env, capture_output=True, check=True,
        ).stdout
        deleted = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "-d"],
            env=env, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    decode = lambda raw: raw.decode("utf-8", errors="surrogateescape")  # noqa: E731
    gone = {decode(raw) for raw in deleted.split(b"\0") if raw}
    files = []
    seen = set()
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        path = decode(raw)
        if path in gone or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _with_directories(files: list[str], show_hidden: bool) -> list[str]:
    """Insert every ancestor directory (with a trailing "/") before its first file."""
    result: list[str] = []
    seen_dirs: set[str] = set()
    for path in files:
        if not show_hidden and _is_hidden(path):
            continue
        cut = path.rfind("/")
        if cut > 0 and path[:cut] not in seen_dirs:
            missing = []
            folder = path[:cut]
            while folder and folder not in seen_dirs:
                missing.append(folder)
                seen_dirs.add(folder)
                slash = folder.rfind("/")
                folder = folder[:slash] if slash > 0 else ""
            result.extend(f + "/" for f in reversed(missing))
        result.append(path)
    return result


def _walk(root: str, show_hidden: bool, max_entries: int | None) -> list[str]:
    result: list[str] = []
    stack = [("", root)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            with os.scandir(abs_dir) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            name = entry.name
            if name == ".git" or (not show_hidden and name.startswith(".")):
                continue
            rel = f"{rel_dir}{name}"
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                result.append(rel + "/")
                subdirs.append((rel + "/", entry.path))
            else:
                result.append(rel)
            if max_entries is not None and len(result) >= max_entries:
                return result
        stack.extend(reversed(subdirs))
    return result


def build_index(root: str, use_git: bool = True, show_hidden: bool = False,
                max_entries: int | None = DEFAULT_MAX_ENTRIES) -> list[str]:
    """List files and folders under ``root`` as relative "/" paths (folders end with "/").

    Blocking; run it in a thread. In a Git work tree (and with ``use_git``) the list
    comes from ``git ls-files -co --exclude-standard`` so ignored files are excluded.
    """
    root = os.path.abspath(root)
    if use_git:
        files = _git_files(root)
        if files is not None:
            paths = _with_directories(files, show_hidden)
            if max_entries is not None:
                del paths[max_entries:]
            return paths
    return _walk(root, show_hidden, max_entries)


def build_index_async(root: str, use_git: bool, show_hidden: bool,
                      callback: Callable[[list[str]], None]) -> threading.Thread:
    """Run ``build_index`` in a thread and deliver ``callback(paths)`` on the main loop."""
    from gi.repository import GLib

    def deliver(paths: list[str]) -> bool:
        callback(paths)
        return GLib.SOURCE_REMOVE

    def work() -> None:
        try:
            paths = build_index(root, use_git, show_hidden)
        except Exception:  # noqa: BLE001 - never kill the UI because of the index
            paths = []
        GLib.idle_add(deliver, paths)

    thread = threading.Thread(target=work, name="metafiles-fuzzy-index", daemon=True)
    thread.start()
    return thread
