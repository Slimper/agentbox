"""Extension points for editions built on top of the open-source core.

An extension is a Python package that exposes an :class:`Extension` object through the ``agentbox.extensions`` entry
point group. The core discovers installed extensions at runtime and lets them add routers, templates, job kinds, CLI
commands, migrations, ORM models, a Settings subclass and hooks at a handful of well-defined points. An extension
declares which editions it serves (``AGENTBOX_EDITION``); its routers, templates, hooks and periodic jobs are only used
when the running edition matches, while job handlers, migrations and models are always available so that a worker or
a migration can never meet an unknown kind or table.

Hook names and signatures (all async):

- ``dashboard.principal(request, session, runtime) -> Principal | None``: resolve a console login before the API-key
  cookie is tried.
- ``dashboard.shell(request, session, principal, ctx) -> dict``: extra template context for every console page.
- ``dashboard.logout(request, session) -> None``
- ``inbox.before_create(session, organization_id, settings)``, ``message.before_send(...)``,
  ``domain.before_create(...)`` (same arguments): raise :class:`agentbox.api.errors.APIError` to refuse.
- ``outbound.provider(session, runtime, inbox) -> (provider, mail_from) | None``: take over sending for an inbox.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any

ENTRY_POINT_GROUP = "agentbox.extensions"
DEFAULT_PATHS = {"login": "/dashboard/login", "logout": "/dashboard/logout", "usage": "/dashboard/usage"}


@dataclass
class Extension:
    name: str
    editions: frozenset[str] = frozenset({"cloud"})
    routers: list[Any] = field(default_factory=list)
    template_dirs: list[str] = field(default_factory=list)
    job_handlers: dict[str, Callable] = field(default_factory=dict)
    backoff: dict[str, list[int]] = field(default_factory=dict)
    periodic: dict[str, timedelta] = field(default_factory=dict)
    hooks: dict[str, list[Callable]] = field(default_factory=dict)
    cli: list[tuple[str, Any]] = field(default_factory=list)
    migration_dirs: list[str] = field(default_factory=list)
    model_modules: list[str] = field(default_factory=list)
    paths: dict[str, str] = field(default_factory=dict)
    settings_class: type | None = None

    def active(self, settings) -> bool:
        return settings is not None and getattr(settings, "edition", "oss") in self.editions


class Registry:
    def __init__(self, extensions: list[Extension]) -> None:
        self.extensions = extensions

    def active(self, settings) -> list[Extension]:
        return [e for e in self.extensions if e.active(settings)]

    def hooks(self, settings, name: str) -> list[Callable]:
        return [h for e in self.active(settings) for h in e.hooks.get(name, [])]

    def routers(self, settings) -> list[Any]:
        return [r for e in self.active(settings) for r in e.routers]

    def template_dirs(self, settings) -> list[str]:
        return [d for e in self.active(settings) for d in e.template_dirs]

    def paths(self, settings) -> dict[str, str]:
        merged = dict(DEFAULT_PATHS)
        for e in self.active(settings):
            merged.update(e.paths)
        return merged

    def periodic(self, settings) -> dict[str, timedelta]:
        merged: dict[str, timedelta] = {}
        for e in self.active(settings):
            merged.update(e.periodic)
        return merged

    def job_handlers(self) -> dict[str, Callable]:
        merged: dict[str, Callable] = {}
        for e in self.extensions:
            merged.update(e.job_handlers)
        return merged

    def backoff(self) -> dict[str, list[int]]:
        merged: dict[str, list[int]] = {}
        for e in self.extensions:
            merged.update(e.backoff)
        return merged

    def cli(self) -> list[tuple[str, Any]]:
        return [c for e in self.extensions for c in e.cli]

    def migration_dirs(self) -> list[str]:
        return [d for e in self.extensions for d in e.migration_dirs]

    def model_modules(self) -> list[str]:
        return [m for e in self.extensions for m in e.model_modules]

    def settings_class(self):
        from agentbox.config import Settings

        cls = Settings
        for e in self.extensions:
            if e.settings_class is not None and issubclass(e.settings_class, cls):
                cls = e.settings_class
        return cls


@lru_cache
def registry() -> Registry:
    found: list[Extension] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        obj = ep.load()
        ext = obj() if callable(obj) and not isinstance(obj, Extension) else obj
        if isinstance(ext, Extension):
            found.append(ext)
    found.sort(key=lambda e: e.name)
    return Registry(found)
