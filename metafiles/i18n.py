"""UI language: Polish when the system language is Polish, English otherwise.

Source strings are English; _() returns the Polish translation when active.
The catalog lives in metafiles/i18n_pl.py.
"""

import os


def detect_language(environ=os.environ):
    """Return "pl" or "en" from the locale environment, as gettext would read it."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(var)
        if value:
            locale = value
            break
    else:
        locale = "C"
    if locale in ("C", "POSIX") or locale.startswith(("C.", "POSIX.")):
        return "en"
    # LANGUAGE (a priority list) overrides the locale, as long as one is set.
    first = (environ.get("LANGUAGE") or locale).split(":")[0]
    return "pl" if first.lower().startswith("pl") else "en"


LANGUAGE = os.environ.get("METAFILES_LANGUAGE") or detect_language()


def _load_catalog():
    if LANGUAGE != "pl":
        return {}
    from metafiles.i18n_pl import PL

    return PL


_catalog = _load_catalog()


def _(text):
    return _catalog.get(text, text)


def ngettext(singular, plural, n):
    """Pick the form for n; Polish has three forms, keyed as 'singular|plural' variants."""
    if LANGUAGE != "pl":
        return singular if n == 1 else plural
    forms = _catalog.get(plural)
    if isinstance(forms, tuple):
        if n == 1:
            return forms[0]
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return forms[1]
        return forms[2]
    return _(singular) if n == 1 else _(plural)
