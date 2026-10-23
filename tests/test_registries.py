"""Tests for parser and collector discovery registries."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from etfh.collect.registry import CollectError, all_collectors, collector_for
from etfh.parse.registry import (
    all_metrics_parsers,
    all_parsers,
    find_metrics_parsers,
    find_parser,
)
from etfh.types import FileFormat, RawFile

_METRICS_PARSER_BODY = """
from etfh.types import FileFormat


class FakeMetricsParser:
    def __init__(self, tag, matches=True):
        self.tag = tag
        self.formats = frozenset({{FileFormat.csv, FileFormat.json}})
        self.matches = matches

    def can_parse(self, raw):
        return self.matches

    def parse(self, raw):
        raise NotImplementedError


METRICS_PARSERS = [FakeMetricsParser({tag!r}, {matches!r})]
"""

_PARSER_BODY = """
from etfh.types import FileFormat


class FakeParser:
    def __init__(self, tag, formats, matches=True):
        self.tag = tag
        self.formats = formats
        self.matches = matches

    def can_parse(self, raw):
        return self.matches

    def parse(self, raw):
        raise NotImplementedError


PARSERS = [FakeParser({tag!r}, frozenset({{FileFormat.csv}}), {matches!r})]
"""

_COLLECTOR_BODY = """
class FakeCollector:
    def __init__(self, source_id):
        self.source_id = source_id

    def fetch(self, fund, day, client, store):
        raise NotImplementedError


COLLECTORS = [FakeCollector({source_id!r})]
"""


@pytest.fixture
def install_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Write fake modules into a temp package path and clean up imports after."""
    created: list[str] = []

    def _install(package_name: str, modules: Mapping[str, str]) -> None:
        directory = tmp_path / package_name.replace(".", "_")
        directory.mkdir(exist_ok=True)
        for name, source in modules.items():
            (directory / f"{name}.py").write_text(source, encoding="utf-8")
        package = importlib.import_module(package_name)
        monkeypatch.setattr(package, "__path__", [str(directory)])
        created.extend(f"{package_name}.{name}" for name in modules)

    yield _install
    for key in created:
        sys.modules.pop(key, None)


def _raw(tmp_path: Path, ext: str = "csv", source_id: str = "syn_csv") -> RawFile:
    path = tmp_path / f"file.{ext}"
    path.write_bytes(b"x")
    return RawFile(
        source_id=source_id,
        issuer="synthetic",
        fund_ticker="SYNA",
        as_of_hint=date(2026, 10, 29),
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        url=f"synthetic://fixtures/SYNA.{ext}",
        path=path,
        sha256="0" * 64,
        content_type="text/csv",
        ext=ext,
    )


def test_all_parsers_discoveries_follow_sorted_module_order(install_modules: Any) -> None:
    install_modules(
        "etfh.parse",
        {
            "zz_second": _PARSER_BODY.format(tag="second", matches=True),
            "aa_first": _PARSER_BODY.format(tag="first", matches=True),
        },
    )
    assert [parser.tag for parser in all_parsers()] == ["first", "second"]  # type: ignore[attr-defined]


def test_modules_without_parsers_are_ignored(install_modules: Any) -> None:
    install_modules(
        "etfh.parse",
        {
            "helper": "VALUE = 1\n",
            "only_parser": _PARSER_BODY.format(tag="only", matches=True),
        },
    )
    assert [parser.tag for parser in all_parsers()] == ["only"]  # type: ignore[attr-defined]


def test_find_parser_returns_first_matching_parser(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {
            "aa_first": _PARSER_BODY.format(tag="first", matches=True),
            "zz_second": _PARSER_BODY.format(tag="second", matches=True),
        },
    )
    parser = find_parser(_raw(tmp_path), source_factory("syn_csv"))
    assert parser is not None
    assert parser.tag == "first"  # type: ignore[attr-defined]


def test_find_parser_skips_non_matching_parsers(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {
            "aa_first": _PARSER_BODY.format(tag="first", matches=False),
            "zz_second": _PARSER_BODY.format(tag="second", matches=True),
        },
    )
    parser = find_parser(_raw(tmp_path), source_factory("syn_csv"))
    assert parser is not None
    assert parser.tag == "second"  # type: ignore[attr-defined]


def test_find_parser_returns_none_when_nothing_matches(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {"aa_first": _PARSER_BODY.format(tag="first", matches=False)},
    )
    assert find_parser(_raw(tmp_path), source_factory("syn_csv")) is None


def test_find_parser_skips_parsers_that_do_not_declare_the_format(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {"aa_first": _PARSER_BODY.format(tag="first", matches=True)},
    )
    assert find_parser(_raw(tmp_path, ext="txt"), source_factory("syn_csv")) is None


def test_non_list_parsers_attribute_raises(install_modules: Any) -> None:
    install_modules("etfh.parse", {"bad": "PARSERS = 'not a list'\n"})
    with pytest.raises(TypeError, match="PARSERS must be a list"):
        all_parsers()


def _tags(parsers: list[Any]) -> list[str]:
    return [parser.tag for parser in parsers if hasattr(parser, "tag")]


def test_metrics_parsers_are_discovered_separately_from_holdings_parsers(
    install_modules: Any,
) -> None:
    install_modules(
        "etfh.parse",
        {
            "aa_metrics": _METRICS_PARSER_BODY.format(tag="metrics", matches=True),
            "zz_holdings": _PARSER_BODY.format(tag="holdings", matches=True),
        },
    )
    assert _tags(all_metrics_parsers()) == ["metrics"]
    assert _tags(all_parsers()) == ["holdings"]


def test_one_module_may_export_both_parser_lists(install_modules: Any) -> None:
    body = _PARSER_BODY.format(tag="holdings", matches=True) + _METRICS_PARSER_BODY.format(
        tag="metrics", matches=True
    )
    install_modules("etfh.parse", {"both": body})
    assert _tags(all_parsers()) == ["holdings"]
    assert _tags(all_metrics_parsers()) == ["metrics"]


def test_find_metrics_parsers_returns_every_match_in_module_order(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {
            "zz_second": _METRICS_PARSER_BODY.format(tag="second", matches=True),
            "aa_first": _METRICS_PARSER_BODY.format(tag="first", matches=True),
            "mm_never": _METRICS_PARSER_BODY.format(tag="never", matches=False),
        },
    )
    found = find_metrics_parsers(_raw(tmp_path), source_factory("syn_csv"))
    assert _tags(found) == ["first", "second"]


def test_find_metrics_parsers_ignores_the_file_source_and_extension(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    """A ``purpose_metrics`` parser claims ``purpose_daily`` files (interfaces-06 §2)."""
    install_modules(
        "etfh.parse",
        {"aa_metrics": _METRICS_PARSER_BODY.format(tag="metrics", matches=True)},
    )
    raw = _raw(tmp_path, ext="json", source_id="purpose_daily")
    found = find_metrics_parsers(raw, source_factory("purpose_daily"))
    assert _tags(found) == ["metrics"]


def test_find_metrics_parsers_returns_empty_when_nothing_matches(
    install_modules: Any, source_factory: Any, tmp_path: Path
) -> None:
    install_modules(
        "etfh.parse",
        {"aa_metrics": _METRICS_PARSER_BODY.format(tag="metrics", matches=False)},
    )
    assert find_metrics_parsers(_raw(tmp_path), source_factory("syn_csv")) == []


def test_non_list_metrics_parsers_attribute_raises(install_modules: Any) -> None:
    install_modules("etfh.parse", {"bad": "METRICS_PARSERS = 3\n"})
    with pytest.raises(TypeError, match="METRICS_PARSERS must be a list"):
        all_metrics_parsers()


def test_all_collectors_discovery_and_lookup(install_modules: Any, source_factory: Any) -> None:
    install_modules(
        "etfh.collect",
        {
            "zz_second": _COLLECTOR_BODY.format(source_id="syn_json"),
            "aa_first": _COLLECTOR_BODY.format(source_id="syn_csv"),
        },
    )
    collectors = all_collectors()
    assert [collector.source_id for collector in collectors] == ["syn_csv", "syn_json"]
    csv_source = source_factory("syn_json")
    bound = collector_for(csv_source)
    assert bound is not None
    assert bound.source is csv_source  # type: ignore[attr-defined]
    assert type(bound) is type(collectors[1])
    assert collector_for(source_factory("syn_pdf")) is None


def test_non_list_collectors_attribute_raises(install_modules: Any) -> None:
    install_modules("etfh.collect", {"bad": "COLLECTORS = 3\n"})
    with pytest.raises(TypeError, match="COLLECTORS must be a list"):
        all_collectors()


_BOUND_COLLECTOR_BODY = """
class BoundCollector:
    source_id = "bound_source"
    source = None

    def fetch(self, fund, day, client, store):
        raise NotImplementedError


COLLECTORS = [BoundCollector()]
"""


def test_collector_for_returns_a_fresh_copy_bound_to_the_given_source(
    install_modules: Any, source_factory: Any
) -> None:
    install_modules("etfh.collect", {"aa_bound": _BOUND_COLLECTOR_BODY})
    source = source_factory("bound_source")
    first = collector_for(source)
    second = collector_for(source)
    assert first is not None
    assert second is not None
    assert first is not second
    assert first.source is source  # type: ignore[attr-defined]
    assert second.source is source  # type: ignore[attr-defined]
    registered = all_collectors()
    assert registered[0].source is None  # type: ignore[attr-defined]
    other = source_factory("bound_source")
    assert collector_for(other).source is other  # type: ignore[union-attr,attr-defined]


def test_collect_error_carries_source_fund_and_reason() -> None:
    error = CollectError("syn_csv", "SYNA", "HTTP 500")
    assert error.source_id == "syn_csv"
    assert error.fund_ticker == "SYNA"
    assert error.reason == "HTTP 500"
    assert "syn_csv" in str(error)
    assert "SYNA" in str(error)

    fund = SimpleNamespace(ticker="SYNB")
    error = CollectError("syn_xlsx", fund, "unexpected format")
    assert error.fund is fund
    assert error.fund_ticker == "SYNB"


def test_registry_module_itself_is_not_treated_as_a_parser_module(
    install_modules: Any,
) -> None:
    install_modules("etfh.parse", {"aa_first": _PARSER_BODY.format(tag="first", matches=True)})
    assert len(all_parsers()) == 1
    assert FileFormat.csv in next(iter(all_parsers())).formats
