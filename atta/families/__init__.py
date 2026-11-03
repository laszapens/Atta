# -*- coding: utf-8 -*-
"""Extended shape families — one module per category.

Each module registers its recipes on import::

    from ..registry import recipe

    @recipe("mechanical.flange", "法兰盘", "...", params={...}, ...)
    def _build_flange(v, quality="draft"):
        ...
        return mesh, finish

Adding a family therefore means *adding a file*, not editing the core.  The
loader discovers modules at import time; by default a broken module is
recorded and skipped so one bad family cannot take the engine down
(``ATTA_FAMILIES_STRICT=1`` makes it a hard error — the test suite runs with
it set, so every shipped family must load cleanly).
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import traceback

__all__ = ["load", "load_errors", "modules"]

#: module name -> traceback string, for modules that failed to import
_load_errors: dict[str, str] = {}
_loaded: list[str] = []


def modules() -> list[str]:
    """Names of the family modules shipped inside this package."""
    return sorted(
        info.name
        for info in pkgutil.iter_modules(__path__)
        if not info.name.startswith("_")
    )


def load_errors() -> dict[str, str]:
    """Import failures from the most recent :func:`load` call."""
    return dict(_load_errors)


def load(strict: bool | None = None) -> list[str]:
    """Import every family module once; return the ones that loaded."""
    if strict is None:
        strict = str(os.environ.get("ATTA_FAMILIES_STRICT", "")).lower() in (
            "1", "true", "yes", "on")
    _load_errors.clear()
    if not _loaded:
        names = modules()
    else:  # allow a later call to pick up freshly added modules
        names = [n for n in modules() if n not in _loaded]
    for name in names:
        modname = "%s.%s" % (__name__, name)
        try:
            importlib.import_module(modname)
        except Exception:  # pragma: no cover - defensive
            _load_errors[name] = traceback.format_exc()
            if strict:
                raise
            continue
        _loaded.append(name)
    return list(_loaded)
