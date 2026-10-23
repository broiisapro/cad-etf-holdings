"""Parser discovery.

Parser modules live in ``etfh/parse/<name>.py``. A module that parses holdings
files exports ``PARSERS`` (a list of :class:`etfh.types.Parser` instances); a
module that parses fund-metrics files exports ``METRICS_PARSERS``. A module may
export both. Discovery is automatic and deterministic: every module in this
package except ``registry`` itself is imported in sorted module-name order, and
the exported lists are concatenated in that order, preserving each module's own
order.

The two lists answer different questions. ``all_parsers``/``find_parser`` pick
the one holdings parser for a raw file. ``find_metrics_parsers`` returns *every*
metrics parser that claims a raw file, including parsers registered for a
different source than the file's own (``purpose_metrics`` reads files collected
as ``purpose_daily``, interfaces-06 §2).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from etfh.registry import Source
from etfh.types import Parser, RawFile

__all__ = ["all_metrics_parsers", "all_parsers", "find_metrics_parsers", "find_parser"]

_PACKAGE = "etfh.parse"
_REGISTRY_MODULE = "registry"
_PARSERS_ATTRIBUTE = "PARSERS"
_METRICS_PARSERS_ATTRIBUTE = "METRICS_PARSERS"


def _iter_modules() -> Iterator[ModuleType]:
    package = importlib.import_module(_PACKAGE)
    infos = sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name)
    for info in infos:
        if info.name == _REGISTRY_MODULE or info.name.startswith("_"):
            continue
        yield importlib.import_module(f"{_PACKAGE}.{info.name}")


def _discover(attribute: str) -> list[Parser]:
    parsers: list[Parser] = []
    for module in _iter_modules():
        exported = getattr(module, attribute, None)
        if exported is None:
            continue
        if not isinstance(exported, list):
            raise TypeError(f"{module.__name__}.{attribute} must be a list")
        parsers.extend(exported)
    return parsers


def all_parsers() -> list[Parser]:
    """Return every discovered holdings parser instance in deterministic order."""
    return _discover(_PARSERS_ATTRIBUTE)


def all_metrics_parsers() -> list[Parser]:
    """Return every discovered fund-metrics parser in deterministic order."""
    return _discover(_METRICS_PARSERS_ATTRIBUTE)


def find_parser(raw: RawFile, source: Source) -> Parser | None:
    """Return the first discovered parser that claims ``raw``, else ``None``.

    A parser is only asked about files whose extension is one of the formats
    it declares, then the first parser whose ``can_parse`` returns true wins.
    ``source`` is the registry entry the file was collected for; it is passed
    for context and future use (for example issuer-specific settings).
    """
    extension = raw.ext.lower().lstrip(".")
    for parser in all_parsers():
        if not any(extension == fmt.value for fmt in parser.formats):
            continue
        if parser.can_parse(raw):
            return parser
    return None


def find_metrics_parsers(raw: RawFile, source: Source) -> list[Parser]:
    """Return every metrics parser whose ``can_parse`` accepts ``raw``.

    Unlike :func:`find_parser` there is no first-match rule and no extension
    pre-filter: metrics parsing is additive (a file may feed several metrics
    sources), and the file's own source does not have to be the metrics source
    a parser is registered for. ``source`` is the registry entry the file was
    collected for; it is passed for context, like :func:`find_parser`.
    """
    return [parser for parser in all_metrics_parsers() if parser.can_parse(raw)]
