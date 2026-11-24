"""Optional plugins.

Every module in this folder that defines ``build(ctx) -> APIRouter`` and a ``FEATURE`` dict is loaded at
start-up. ``GET /api/features`` lists the plugins that loaded, so the UI only shows a plugin's screens when
its server side is present. A plugin that fails to import is logged and skipped; it never stops the daemon.

Modules whose name starts with ``_`` or ``test_`` (helpers and tests) are not loaded as plugins.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from fastapi import FastAPI

log = logging.getLogger(__name__)


def discover() -> list[Any]:
    mods = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith(("_", "test_")):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:  # a broken optional plugin must not take the daemon down
            log.exception("plugin %s failed to load; skipped", info.name)
            continue
        if callable(getattr(mod, "build", None)) and isinstance(getattr(mod, "FEATURE", None), dict):
            mods.append(mod)
    return mods


def register_plugins(app: FastAPI, ctx) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for mod in discover():
        try:
            app.include_router(mod.build(ctx))
        except Exception:
            log.exception("plugin %s failed to start; skipped", mod.__name__)
            continue
        features.append(dict(mod.FEATURE))

    @app.get("/api/features")
    def list_features() -> dict[str, Any]:
        return {"features": features}

    return features
