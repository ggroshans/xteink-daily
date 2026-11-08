"""Source registry: built-in sources plus plugins.

A project's "type" in config.json names a source. Sources come from:
  1. built-ins (rss, command)
  2. installed packages advertising the "xteink_daily.sources" entry point group
  3. modules listed under "plugins" in config.json (found via "plugin_paths"),
     each exposing SOURCES = {"type-name": SourceClass}
Later entries override earlier ones with the same name (with a warning).
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any, Dict, Optional

from ..api import API_VERSION
from .command import CommandSource
from .rss import RssSource

ENTRY_POINT_GROUP = "xteink_daily.sources"
BUILTIN: Dict[str, Any] = {"rss": RssSource, "command": CommandSource}


def _entry_points():
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return []
    eps = entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=ENTRY_POINT_GROUP))
    return list(eps.get(ENTRY_POINT_GROUP, []))  # Python 3.9


def load_sources(cfg=None, log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    log = log or logging.getLogger("xteink-daily")
    registry: Dict[str, Any] = dict(BUILTIN)
    origin = {name: "built-in" for name in registry}

    def add(name: str, obj: Any, where: str) -> None:
        if name in registry and origin[name] != where:
            log.warning("source type %r from %s overrides the one from %s", name, where, origin[name])
        registry[name] = obj
        origin[name] = where

    for ep in _entry_points():
        try:
            add(ep.name, ep.load(), f"package {ep.value.split(':')[0]}")
        except Exception as e:
            log.warning("could not load source %r from entry point %s: %s", ep.name, ep.value, e)

    if cfg is not None:
        for path in cfg.plugin_paths:
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        for module_name in cfg.plugins:
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                log.error("could not import plugin module %r: %s", module_name, e)
                continue
            required = getattr(module, "REQUIRES_API", 1)
            if required > API_VERSION:
                log.error("plugin %r needs plugin API %s; this xteink-daily provides %s. Update xteink-daily.",
                          module_name, required, API_VERSION)
                continue
            for name, obj in (getattr(module, "SOURCES", None) or {}).items():
                add(name, obj, f"plugin {module_name}")
    return registry


def instantiate(registry: Dict[str, Any], kind: str, cfg=None):
    try:
        obj = registry[kind]
    except KeyError:
        raise ValueError(f"unknown project type {kind!r}; available: {', '.join(sorted(registry))}"
                         " (see `xteink-daily sources`)") from None
    if obj is CommandSource and cfg is not None:
        return CommandSource(cfg.path.parent)
    return obj() if isinstance(obj, type) else obj
