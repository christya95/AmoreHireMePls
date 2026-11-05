"""ATS form adapters. Importing the package registers the built-in adapters."""

from __future__ import annotations

from .ashby import AshbyAdapter
from .base import AtsAdapter, QuestionSet, Registry, registry
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

registry.register(GreenhouseAdapter())
registry.register(LeverAdapter())
registry.register(AshbyAdapter())


def adapter_for(url: str | None, route: str | None = None) -> AtsAdapter | None:
    return registry.for_url(url, route)


__all__ = ["AshbyAdapter", "AtsAdapter", "GreenhouseAdapter", "LeverAdapter", "QuestionSet", "Registry", "adapter_for", "registry"]
