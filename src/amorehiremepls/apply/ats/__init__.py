"""ATS form adapters. Importing the package registers the built-in adapters."""

from __future__ import annotations

from amorehiremepls.apply.ats.ashby import AshbyAdapter
from amorehiremepls.apply.ats.base import AtsAdapter, QuestionSet, Registry, registry
from amorehiremepls.apply.ats.greenhouse import GreenhouseAdapter
from amorehiremepls.apply.ats.lever import LeverAdapter

registry.register(GreenhouseAdapter())
registry.register(LeverAdapter())
registry.register(AshbyAdapter())


def adapter_for(url: str | None, route: str | None = None) -> AtsAdapter | None:
    return registry.for_url(url, route)


__all__ = ["AshbyAdapter", "AtsAdapter", "GreenhouseAdapter", "LeverAdapter", "QuestionSet", "Registry", "adapter_for", "registry"]
