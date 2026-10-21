"""Tests for the generic quarterly portfolio disclosure PDF parser."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from etfh.parse import qpd_pdf
from etfh.parse.qpd_pdf import QpdPdfError, QpdPdfParser, parse_qpd_date
from etfh.schema import ParsedHoldings
from tests._qpd_helpers import raw_file_for_pdf, write_qpd_pdf, write_raw_text_pdf

SYNF_PDF = Path("tests/fixtures/raw_synthetic/synthetic/SYNF/2026-09-30/20261028T220000Z.pdf")


def _note(parsed_notes: list[str], prefix: str) -> str:
    for note in parsed_notes:
        if note.startswith(prefix):
            return note.removeprefix(prefix)
    raise AssertionError(f"note {prefix!r} not found in {parsed_notes!r}")


def _parse(path: Path, **kwargs: str) -> object:
    raw = raw_file_for_pdf(path, **kwargs)  # type: ignore[arg-type]
    parsed = QpdPdfParser().parse(raw)
    ParsedHoldings.validate(parsed.frame)
    return parsed


def test_synf_total_equals_stated_total() -> None:
    parsed = _parse(SYNF_PDF)
    assert parsed.as_of_date == date(2026, 9, 30)  # type: ignore[attr-defined]
    assert parsed.published_at == datetime(2026, 10, 28, 4, 0, tzinfo=UTC)  # type: ignore[attr-defined]
    assert "declared_unit=percent" in parsed.notes  # type: ignore[attr-defined]
    assert "partial_list=true" in parsed.notes  # type: ignore[attr-defined]
    assert "stated_total_fraction=0.8235" in parsed.notes  # type: ignore[attr-defined]
    stated = float(_note(parsed.notes, "stated_total_fraction="))  # type: ignore[attr-defined]
    frame: pl.DataFrame = parsed.frame  # type: ignore[attr-defined]
    assert stated == pytest.approx(0.8235)
    assert frame.height == 25
    assert round(float(frame["weight"].sum()), 4) == 0.8235
    cash = frame.filter(pl.col("security_name") == "Cash and Cash Equivalents")
    assert cash.height == 1
    assert cash["asset_class"].to_list() == ["cash"]
    assert float(cash["weight"][0]) == pytest.approx(0.0735)
    assert set(frame["asset_class"].unique().to_list()) == {"equity", "cash"}
    assert frame["sector"].unique().to_list() == ["Unknown"]
    assert frame["shares"].null_count() == frame.height
    assert frame["market_value"].null_count() == frame.height


def test_multi_page_table_with_repeated_headers(tmp_path: Path) -> None:
    rows = [
        ("Northwind Materials Inc.", "6.00"),
        ("Northgate Telecom Corp.", "7.00"),
        ("Maplecrest Properties", "5.50"),
        ("Harbourside Logistics Corp.", "4.50"),
        ("Elmsworth Capital Corp.", "9.00"),
        ("Pineview Software Corp.", "8.00"),
        ("Oakhurst Insurance Group", "5.00"),
        ("Kestrel Aerospace Inc.", "3.00"),
    ]
    path = write_qpd_pdf(
        tmp_path / "multipage.pdf",
        rows=rows,
        page_break_after=4,
        total="Total of the holdings listed above: 48.00% of net asset value",
    )
    parsed = _parse(path)
    assert parsed.frame.height == 8  # type: ignore[attr-defined]
    assert float(parsed.frame["weight"].sum()) == pytest.approx(0.48)  # type: ignore[attr-defined]
    assert parsed.frame["security_name"].to_list()[-1] == "Kestrel Aerospace Inc."  # type: ignore[attr-defined]


def test_two_column_layout_on_one_page(tmp_path: Path) -> None:
    rows = [
        ("Northwind Materials Inc.", "6.00"),
        ("Northgate Telecom Corp.", "7.00"),
        ("Maplecrest Properties", "5.50"),
        ("Harbourside Logistics Corp.", "4.50"),
        ("Elmsworth Capital Corp.", "9.00"),
        ("Pineview Software Corp.", "8.00"),
    ]
    path = write_qpd_pdf(
        tmp_path / "two_column.pdf",
        rows=rows,
        two_column=True,
        total="Total of the holdings listed above: 40.00% of net asset value",
    )
    parsed = _parse(path)
    assert parsed.frame.height == 6  # type: ignore[attr-defined]
    names = parsed.frame["security_name"].to_list()  # type: ignore[attr-defined]
    assert set(names) == {row[0] for row in rows}
    assert float(parsed.frame["weight"].sum()) == pytest.approx(0.40)  # type: ignore[attr-defined]


def test_sector_headers_and_subtotals_skipped(tmp_path: Path) -> None:
    rows = [
        "Financials",
        ("Northgate Bank", "10.00"),
        ("Fairhaven Insurance Group", "5.00"),
        ("Total Financials", "15.00"),
        "Energy",
        ("Cedarline Energy Corp.", "8.00"),
        ("Total Energy Corp.", "2.00"),
        ("Total Energy", "10.00"),
    ]
    path = write_qpd_pdf(
        tmp_path / "sectors.pdf",
        rows=rows,
        total="Total of the holdings listed above: 25.00% of net asset value",
    )
    parsed = _parse(path)
    frame: pl.DataFrame = parsed.frame  # type: ignore[attr-defined]
    assert frame.height == 4
    assert set(frame["security_name"].to_list()) == {
        "Northgate Bank",
        "Fairhaven Insurance Group",
        "Cedarline Energy Corp.",
        "Total Energy Corp.",
    }
    by_name = dict(zip(frame["security_name"].to_list(), frame["sector"].to_list(), strict=True))
    assert by_name["Northgate Bank"] == "Financials"
    assert by_name["Fairhaven Insurance Group"] == "Financials"
    assert by_name["Cedarline Energy Corp."] == "Energy"
    assert by_name["Total Energy Corp."] == "Energy"


def test_cash_other_and_negative_other_net_assets(tmp_path: Path) -> None:
    rows = [
        ("Maplecrest Properties", "60.00"),
        ("Cash and Cash Equivalents", "2.00"),
        ("Other net assets", "(1.50)"),
    ]
    path = write_qpd_pdf(
        tmp_path / "cash_other.pdf",
        rows=rows,
        total="Total of the holdings listed above: 60.50% of net asset value",
    )
    parsed = _parse(path)
    frame: pl.DataFrame = parsed.frame  # type: ignore[attr-defined]
    assert frame.height == 3
    names = frame["security_name"].to_list()
    classes = dict(zip(names, frame["asset_class"].to_list(), strict=True))
    weights = dict(zip(names, frame["weight"].to_list(), strict=True))
    assert classes["Cash and Cash Equivalents"] == "cash"
    assert classes["Other net assets"] == "other"
    assert weights["Other net assets"] == pytest.approx(-0.015)
    assert float(frame["weight"].sum()) == pytest.approx(0.605)


def test_french_decimal_commas_never_mis_scaled(tmp_path: Path) -> None:
    rows = [
        ("Banque Royale du Canada", "12,5 %"),
        ("Compagnie Minière Nord", "7,25 %"),
    ]
    path = write_qpd_pdf(
        tmp_path / "french.pdf",
        rows=rows,
        total="Total of the holdings listed above: 19,75 % of net asset value",
    )
    parsed = _parse(path)
    weights = parsed.frame["weight"].to_list()  # type: ignore[attr-defined]
    assert weights == pytest.approx([0.125, 0.0725])
    assert float(_note(parsed.notes, "stated_total_fraction=")) == pytest.approx(0.1975)  # type: ignore[attr-defined]


def test_weight_decimals_note_reports_the_smallest_printed_unit(tmp_path: Path) -> None:
    one_decimal = write_qpd_pdf(
        tmp_path / "one_decimal.pdf",
        rows=[("Maplecrest Properties", "12.5"), ("Northgate Bank", "7.5")],
        total="Total of the holdings listed above: 20.0% of net asset value",
    )
    assert "weight_decimals=1" in _parse(one_decimal).notes  # type: ignore[attr-defined]

    two_decimals = write_qpd_pdf(
        tmp_path / "two_decimals.pdf",
        rows=[("Maplecrest Properties", "12.50"), ("Northgate Bank", "7.50")],
        total="Total of the holdings listed above: 20.00% of net asset value",
    )
    assert "weight_decimals=2" in _parse(two_decimals).notes  # type: ignore[attr-defined]

    whole = write_qpd_pdf(
        tmp_path / "whole.pdf",
        rows=[("Maplecrest Properties", "12"), ("Northgate Bank", "8")],
        total="Total of the holdings listed above: 20% of net asset value",
    )
    assert "weight_decimals=0" in _parse(whole).notes  # type: ignore[attr-defined]


def test_wrong_stated_total_is_reported_not_hidden(tmp_path: Path) -> None:
    rows = [("Maplecrest Properties", "20.00")]
    path = write_qpd_pdf(
        tmp_path / "wrong_total.pdf",
        rows=rows,
        total="Total of the holdings listed above: 99.00% of net asset value",
    )
    parsed = _parse(path)
    stated = float(_note(parsed.notes, "stated_total_fraction="))  # type: ignore[attr-defined]
    assert stated == pytest.approx(0.99)
    assert float(parsed.frame["weight"].sum()) == pytest.approx(0.20)  # type: ignore[attr-defined]


def test_table_and_two_column_with_repeated_header(tmp_path: Path) -> None:
    rows = [(f"Holding Number {index}", f"{index}.00") for index in range(1, 7)]
    path = write_qpd_pdf(
        tmp_path / "ruled.pdf",
        rows=rows,
        ruled=True,
        total="Total of the holdings listed above: 21.00% of net asset value",
    )
    parsed = _parse(path)
    assert parsed.frame.height == 6  # type: ignore[attr-defined]


def test_missing_portfolio_date_raises(tmp_path: Path) -> None:
    path = write_raw_text_pdf(
        tmp_path / "no_date.pdf",
        ["Holding % of net asset value", "Maplecrest Properties 5.53"],
    )
    with pytest.raises(QpdPdfError, match="no portfolio date"):
        _parse(path)


def test_missing_stated_total_raises(tmp_path: Path) -> None:
    path = write_raw_text_pdf(
        tmp_path / "no_total.pdf",
        [
            "Summary of Investment Portfolio as at September 30, 2026",
            "Holding % of net asset value",
            "Maplecrest Properties 5.53",
        ],
    )
    with pytest.raises(QpdPdfError, match="no stated total"):
        _parse(path)


def test_no_holdings_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_raw_text_pdf(
        tmp_path / "empty.pdf",
        [
            "Summary of Investment Portfolio as at September 30, 2026",
            "Total of the holdings listed above: 0.00% of net asset value",
        ],
    )
    monkeypatch.setattr(qpd_pdf, "_camelot_rows", lambda _path: ([], False))
    with pytest.raises(QpdPdfError, match="no holdings table"):
        _parse(path)


def test_camelot_fallback_is_used_when_words_find_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fallback.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    text = (
        "Summary of Investment Portfolio as at September 30, 2026\n"
        "Total of the holdings listed above: 20.00% of net asset value"
    )
    monkeypatch.setattr(qpd_pdf, "_pdfplumber_rows", lambda _path, _rules: ([], text, False))
    monkeypatch.setattr(
        qpd_pdf,
        "_camelot_rows",
        lambda _path: ([qpd_pdf._Row(name="Fallback Corp.", weight=20.0, sector="Unknown")], False),
    )
    parsed = QpdPdfParser().parse(raw_file_for_pdf(path))
    assert parsed.frame["security_name"].to_list() == ["Fallback Corp."]
    assert float(parsed.frame["weight"][0]) == pytest.approx(0.20)


def test_can_parse_only_pdfs(tmp_path: Path) -> None:
    parser = QpdPdfParser()
    pdf_raw = raw_file_for_pdf(tmp_path / "x.pdf")
    assert parser.formats == frozenset({"pdf"})
    assert parser.can_parse(pdf_raw)
    csv_raw = pdf_raw.__class__(
        source_id=pdf_raw.source_id,
        issuer=pdf_raw.issuer,
        fund_ticker=pdf_raw.fund_ticker,
        as_of_hint=None,
        fetched_at=pdf_raw.fetched_at,
        url=pdf_raw.url,
        path=pdf_raw.path,
        sha256=pdf_raw.sha256,
        content_type="text/csv",
        ext="csv",
    )
    assert not parser.can_parse(csv_raw)


def test_source_scoped_parser() -> None:
    scoped = QpdPdfParser(source_ids=frozenset({"syn_pdf"}))
    raw = raw_file_for_pdf(Path("anything.pdf"))
    assert scoped.can_parse(raw)
    other = raw_file_for_pdf(Path("anything.pdf"), source_id="bmo_qpd")
    assert not scoped.can_parse(other)


def test_parse_qpd_date_variants() -> None:
    assert parse_qpd_date("September 30, 2026") == date(2026, 9, 30)
    assert parse_qpd_date("30 September 2026") == date(2026, 9, 30)
    assert parse_qpd_date("2026-09-30") == date(2026, 9, 30)
    assert parse_qpd_date("30 septembre 2026") == date(2026, 9, 30)
    assert parse_qpd_date("not a date") is None
