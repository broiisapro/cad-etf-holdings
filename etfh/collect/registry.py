"""Collector discovery, source binding and the shared collection error.

Collector modules live in ``etfh/collect/<name>.py`` and export ``COLLECTORS``
(a list of :class:`etfh.types.Collector` instances). Discovery is automatic and
deterministic: every module in this package except ``registry`` itself is
imported in sorted module-name order, and the exported lists are concatenated
in that order, preserving each module's own order.

:func:`collector_for` returns a **bound copy** of the registered collector with
its ``source`` attribute set to the :class:`~etfh.registry.Source` the caller
looked up, so ``etfh collect --config DIR`` gates every request (in the client
and in the collector) with DIR's terms record rather than the default config
the module was imported with.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from copy import copy
from types import ModuleType
from typing import Any, cast

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
    """Return a bound copy of the first collector registered for ``source``.

    The copy carries ``source`` as an attribute so the collector gates its
    requests with exactly the registry entry the caller passed (D3, interfaces
    §1). The registered instance is never mutated, so two callers with
    different ``Source`` objects cannot interfere.
    """
    for collector in all_collectors():
        if collector.source_id == source.id:
            bound = copy(collector)
            cast(Any, bound).source = source
            return bound
    return None
