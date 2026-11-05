"""ATS form adapters. Importing the package registers the built-in adapters."""

from __future__ import annotations

from .base import AtsAdapter, QuestionSet, Registry, registry
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

registry.register(GreenhouseAdapter())
registry.register(LeverAdapter())


def adapter_for(url: str | None, route: str | None = None) -> AtsAdapter | None:
    return registry.for_url(url, route)


__all__ = ["AtsAdapter", "GreenhouseAdapter", "LeverAdapter", "QuestionSet", "Registry", "adapter_for", "registry"]
