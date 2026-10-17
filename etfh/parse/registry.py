"""Parser discovery.

Parser modules live in ``etfh/parse/<name>.py`` and export ``PARSERS`` (a list
of :class:`etfh.types.Parser` instances). Discovery is automatic and
deterministic: every module in this package except ``registry`` itself is
imported in sorted module-name order, and the exported lists are concatenated
in that order, preserving each module's own order.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from etfh.registry import Source
from etfh.types import Parser, RawFile

__all__ = ["all_parsers", "find_parser"]

_PACKAGE = "etfh.parse"
_REGISTRY_MODULE = "registry"


def _iter_modules() -> Iterator[ModuleType]:
    package = importlib.import_module(_PACKAGE)
    infos = sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name)
    for info in infos:
        if info.name == _REGISTRY_MODULE or info.name.startswith("_"):
            continue
        yield importlib.import_module(f"{_PACKAGE}.{info.name}")


def all_parsers() -> list[Parser]:
    """Return every discovered parser instance in deterministic order."""
    parsers: list[Parser] = []
    for module in _iter_modules():
        exported = getattr(module, "PARSERS", None)
        if exported is None:
            continue
        if not isinstance(exported, list):
            raise TypeError(f"{module.__name__}.PARSERS must be a list")
        parsers.extend(exported)
    return parsers


def find_parser(raw: RawFile, source: Source) -> Parser | None:
    """Return the first discovered parser that claims ``raw``, else ``None``.

    ``source`` is the registry entry the file was collected for; it is passed
    for context and future use (for example issuer-specific settings).
    """
    for parser in all_parsers():
        if parser.can_parse(raw):
            return parser
    return None
