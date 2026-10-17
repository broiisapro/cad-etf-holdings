"""Collector discovery and the shared collection error.

Collector modules live in ``etfh/collect/<name>.py`` and export ``COLLECTORS``
(a list of :class:`etfh.types.Collector` instances). Discovery is automatic and
deterministic: every module in this package except ``registry`` itself is
imported in sorted module-name order, and the exported lists are concatenated
in that order, preserving each module's own order.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from etfh.registry import Fund, Source
from etfh.types import Collector

__all__ = ["CollectError", "all_collectors", "collector_for"]

_PACKAGE = "etfh.collect"
_REGISTRY_MODULE = "registry"


class CollectError(Exception):
    """A collector failed for one fund/source pair.

    Carries the source id, the fund (a :class:`~etfh.registry.Fund` or a bare
    ticker string) and a human-readable reason.
    """

    def __init__(self, source_id: str, fund: Fund | str, reason: str) -> None:
        self.source_id = source_id
        self.fund = fund
        self.fund_ticker = fund if isinstance(fund, str) else fund.ticker
        self.reason = reason
        super().__init__(f"collect failed source={source_id} fund={self.fund_ticker}: {reason}")


def _iter_modules() -> Iterator[ModuleType]:
    package = importlib.import_module(_PACKAGE)
    infos = sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name)
    for info in infos:
        if info.name == _REGISTRY_MODULE or info.name.startswith("_"):
            continue
        yield importlib.import_module(f"{_PACKAGE}.{info.name}")


def all_collectors() -> list[Collector]:
    """Return every discovered collector instance in deterministic order."""
    collectors: list[Collector] = []
    for module in _iter_modules():
        exported = getattr(module, "COLLECTORS", None)
        if exported is None:
            continue
        if not isinstance(exported, list):
            raise TypeError(f"{module.__name__}.COLLECTORS must be a list")
        collectors.extend(exported)
    return collectors


def collector_for(source: Source) -> Collector | None:
    """Return the first discovered collector registered for ``source``."""
    for collector in all_collectors():
        if collector.source_id == source.id:
            return collector
    return None
