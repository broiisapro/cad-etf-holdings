"""Helpers that build fictional quarterly-disclosure PDFs for parser tests.

The PDFs are created at test time with ReportLab (``invariant=1``) so no
binary fixtures are committed and every run is byte-for-byte deterministic.
Layouts mirror the structures issuers use: unruled text tables, ruled tables,
multi-page tables with repeated headers, two side-by-side lists, sector groups
with subtotal lines, and French-style decimal commas.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from etfh.registry import Fund, Source, TermsRecord
from etfh.types import AssetClass, Decision, FileFormat, RawFile, SourceKind

PdfRow = tuple[str, str] | str

_PAGE_WIDTH, _PAGE_HEIGHT = letter
_LEFT = 72.0
_RIGHT = _PAGE_WIDTH - 72.0
_MIDDLE_LEFT = 300.0
_MIDDLE_RIGHT = 320.0
_TOP = _PAGE_HEIGHT - 72.0


def write_qpd_pdf(
    path: Path,
    *,
    rows: Sequence[PdfRow],
    as_of: str = "September 30, 2026",
    fund_name: str = "Synthetic Fjord Equity Fund",
    published: str | None = "October 28, 2026",
    total: str | None = "Total of the holdings listed above: 82.35% of net asset value",
    header: str = "Holding % of net asset value",
    two_column: bool = False,
    ruled: bool = False,
    page_break_after: int | None = None,
    repeat_header: bool = True,
    title: str | None = None,
) -> Path:
    """Write a fictional disclosure PDF and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    head = title if title is not None else f"Summary of Investment Portfolio as at {as_of}"
    split = page_break_after if page_break_after is not None else len(rows)
    pages = [(rows[:split], True), (rows[split:], False)] if split < len(rows) else [(rows, True)]

    for page_rows, first_page in pages:
        y = _TOP
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(_LEFT, y, head)
        y -= 18
        pdf.setFont("Helvetica", 10)
        pdf.drawString(_LEFT, y, fund_name)
        y -= 14
        if published is not None:
            pdf.drawString(_LEFT, y, f"Published {published}")
            y -= 22
        else:
            y -= 8
        if header:
            pdf.setFont("Helvetica-Bold", 9)
            match = re.match(r"^(.*?)\s+(%.*)$", header)
            left_header = match.group(1) if match else header
            right_header = match.group(2) if match else "% of net asset value"
            pdf.drawString(_LEFT, y, left_header)
            pdf.drawRightString(_RIGHT, y, right_header)
            y -= 6
            if ruled:
                pdf.line(_LEFT, y, _RIGHT, y)
            y -= 14
        pdf.setFont("Helvetica", 9)
        if two_column:
            y = _draw_two_columns(pdf, page_rows, y, ruled)
        else:
            y = _draw_rows(pdf, page_rows, y, ruled)
        if not first_page and repeat_header is False:
            pass
        if total is not None:
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(_LEFT, y - 8, total)
        pdf.showPage()
    pdf.save()
    return path


def _draw_rows(
    pdf: canvas.Canvas,
    rows: Sequence[PdfRow],
    y: float,
    ruled: bool,
) -> float:
    bottom = y
    for row in rows:
        if isinstance(row, tuple):
            name, weight = row
            pdf.drawString(_LEFT, y, name)
            pdf.drawRightString(_RIGHT, y, weight)
        else:
            pdf.drawString(_LEFT, y, row)
        bottom = y
        if ruled:
            pdf.line(_LEFT, y - 4, _RIGHT, y - 4)
        y -= 15
    if ruled:
        pdf.line(_LEFT, bottom + 10, _LEFT, bottom - 4)
        pdf.line(_RIGHT, bottom + 10, _RIGHT, bottom - 4)
    return y


def _draw_two_columns(
    pdf: canvas.Canvas,
    rows: Sequence[PdfRow],
    y: float,
    ruled: bool,
) -> float:
    left_rows = rows[: (len(rows) + 1) // 2]
    right_rows = rows[(len(rows) + 1) // 2 :]
    start = y
    y_left = y
    for row in left_rows:
        if isinstance(row, tuple):
            name, weight = row
            pdf.drawString(_LEFT, y_left, name)
            pdf.drawRightString(_MIDDLE_LEFT, y_left, weight)
        else:
            pdf.drawString(_LEFT, y_left, row)
        if ruled:
            pdf.line(_LEFT, y_left - 4, _MIDDLE_LEFT, y_left - 4)
        y_left -= 15
    y_right = start
    for row in right_rows:
        if isinstance(row, tuple):
            name, weight = row
            pdf.drawString(_MIDDLE_RIGHT, y_right, name)
            pdf.drawRightString(_RIGHT, y_right, weight)
        else:
            pdf.drawString(_MIDDLE_RIGHT, y_right, row)
        if ruled:
            pdf.line(_MIDDLE_RIGHT, y_right - 4, _RIGHT, y_right - 4)
        y_right -= 15
    if ruled:
        for x in (_LEFT, _MIDDLE_LEFT, _MIDDLE_RIGHT, _RIGHT):
            pdf.line(x, y - 4, x, start + 10)
    return min(y_left, y_right)


def write_raw_text_pdf(path: Path, lines: Sequence[str]) -> Path:
    """Write a PDF with plain left-aligned lines (no table treatment)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setFont("Helvetica", 9)
    y = _TOP
    for line in lines:
        if y < 72:
            pdf.showPage()
            pdf.setFont("Helvetica", 9)
            y = _TOP
        pdf.drawString(_LEFT, y, line)
        y -= 15
    pdf.save()
    return path


def raw_file_for_pdf(
    path: Path,
    *,
    source_id: str = "syn_pdf",
    fund_ticker: str = "SYNF",
    issuer: str = "synthetic",
) -> RawFile:
    """Build the :class:`RawFile` a parser would receive for ``path``."""
    from datetime import UTC, datetime

    content = path.read_bytes() if path.exists() else path.name.encode("utf-8")
    return RawFile(
        source_id=source_id,
        issuer=issuer,
        fund_ticker=fund_ticker,
        as_of_hint=None,
        fetched_at=datetime(2026, 10, 29, 22, 0, tzinfo=UTC),
        url=f"https://example.invalid/{path.name}",
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type="application/pdf",
        ext="pdf",
    )


def qpd_source(
    source_id: str = "vanguard_ca_qpd",
    *,
    issuer: str = "vanguard_ca",
    decision: Decision = Decision.allowed_with_conditions,
    supported: bool = True,
    min_interval_seconds: float | None = None,
) -> Source:
    """Build a quarterly-disclosure Source for collector tests."""
    conditions = (
        ["internal research use only"] if decision is Decision.allowed_with_conditions else []
    )
    return Source(
        id=source_id,
        issuer=issuer,
        kind=SourceKind.quarterly_disclosure,
        format=FileFormat.pdf,
        url_patterns=["https://example.invalid/qpd.pdf"],
        update_frequency="quarterly",
        supported=supported,
        terms=TermsRecord(
            url="https://example.invalid/terms",
            reviewer="Test Reviewer",
            reviewed_on=date(2026, 10, 17),
            decision=decision,
            conditions=conditions,
            min_interval_seconds=min_interval_seconds,
        ),
    )


def qpd_fund(
    ticker: str = "NORTH",
    *,
    issuer: str = "vanguard_ca",
    source_id: str = "vanguard_ca_qpd",
    asset_class: AssetClass = AssetClass.equity,
) -> Fund:
    """Build a Fund for collector tests without touching config files."""
    return Fund(
        ticker=ticker,
        issuer=issuer,
        name=f"Fictional {ticker} Fund",
        asset_class=asset_class,
        source_ids=(source_id,),
    )


def write_globalx_lookalike(path: Path) -> Path:
    """Write a fictional Global X-style disclosure with several tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")

    def amount_row(y: float, name: str, dollars: str | None, percent: str | None) -> None:
        pdf.drawString(_LEFT, y, name)
        if dollars is not None:
            pdf.drawRightString(440, y, dollars)
        if percent is not None:
            pdf.drawRightString(_RIGHT, y, percent)

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Global X Fictional 60 Index Corporate Class ETF (GXF, GXF.U:TSX)")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 18, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 32, "As at March 31, 2026")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 62, "Asset Mix")
    pdf.drawString(_LEFT, _TOP - 76, "% of ETF's")
    pdf.drawRightString(440, _TOP - 76, "Net Asset Value")
    pdf.drawRightString(_RIGHT, _TOP - 76, "Net Asset Value")
    pdf.setFont("Helvetica", 9)
    amount_row(
        _TOP - 92,
        "Fictional Equity Exposure through Index Swaps",
        "$ 4,736,760,236",
        "99.89%",
    )
    amount_row(_TOP - 106, "Cash - Other", "3,057,087", "0.06%")
    amount_row(_TOP - 120, "Other Assets less Liabilities", "2,067,217", "0.05%")
    pdf.setFont("Helvetica-Bold", 9)
    amount_row(_TOP - 136, "", "$ 4,741,884,540", "100.00%")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 166, "Top Holdings")
    pdf.drawString(_LEFT, _TOP - 180, "% of ETF's")
    pdf.drawRightString(440, _TOP - 180, "Net Asset Value")
    pdf.drawRightString(_RIGHT, _TOP - 180, "Net Asset Value")
    pdf.setFont("Helvetica", 9)
    amount_row(
        _TOP - 196,
        "Fair Value of Index Swaps (notional value $2,153,681,434)(1)",
        None,
        "54.51%",
    )
    amount_row(_TOP - 210, "Cash held for Collateral", None, "45.38%")
    amount_row(_TOP - 224, "Cash - Other", None, "0.06%")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(
        _LEFT,
        _TOP - 254,
        "Top 25 Securities in the Underlying Index - Fictional 60 Index",
    )
    pdf.drawRightString(_RIGHT, _TOP - 254, "% Weighting in Underlying Index")
    pdf.setFont("Helvetica", 9)
    underlying = [
        ("Northgate Bank", "8.45%"),
        ("Maplecrest Properties", "5.86%"),
        ("Pineview Software Corp.", "5.44%"),
        ("Cedarline Energy Corp.", "4.42%"),
    ]
    for index, (name, weight) in enumerate(underlying, start=1):
        amount_row(_TOP - 254 - index * 14, name, None, weight)

    pdf.setFont("Helvetica", 7)
    pdf.drawString(
        _LEFT,
        _TOP - 340,
        "(1) The fair value of index swap(s) represents the amount receivable "
        "from the counterparty.",
    )
    pdf.drawString(
        _LEFT,
        _TOP - 352,
        "The summary of investment portfolio may change due to ongoing portfolio transactions.",
    )
    pdf.save()
    return path


def write_globalx_direct_lookalike(path: Path) -> Path:
    """Write a fictional Global X direct-holding disclosure (top-25 layout)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Global X Fictional Nasdaq ETF (GXD, GXD.U:TSX)")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 18, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 32, "As at March 31, 2026")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 62, "Asset Mix")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 76, "U.S. Equities")
    pdf.drawRightString(440, _TOP - 76, "$ 904,735,751")
    pdf.drawRightString(_RIGHT, _TOP - 76, "95.90%")
    pdf.drawString(_LEFT, _TOP - 90, "Cash and Cash Equivalents")
    pdf.drawRightString(440, _TOP - 90, "644,595")
    pdf.drawRightString(_RIGHT, _TOP - 90, "4.10%")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawRightString(440, _TOP - 104, "$ 943,415,807")
    pdf.drawRightString(_RIGHT, _TOP - 104, "100.00%")

    pdf.showPage()
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Global X Fictional Nasdaq ETF (GXD, GXD.U:TSX)")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 18, "Summary of Investment Portfolio (continued)")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 32, "As at March 31, 2026")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 62, "Top 25 Holdings")
    pdf.drawRightString(_RIGHT, _TOP - 62, "% of ETF's Net Asset Value")
    pdf.setFont("Helvetica", 9)
    holdings = [
        ("Juniper Health Corp.", "8.69%"),
        ("Northwind Materials Inc.", "7.64%"),
        ("Pineview Software Corp.", "6.63%"),
        ("Uplands Chemicals Inc.", "5.63%"),
        ("Elmsworth Capital Corp.", "4.58%"),
    ]
    for index, (name, weight) in enumerate(holdings, start=1):
        pdf.drawString(_LEFT, _TOP - 62 - index * 14, name)
        pdf.drawRightString(_RIGHT, _TOP - 62 - index * 14, weight)

    pdf.setFont("Helvetica", 7)
    pdf.drawString(
        _LEFT,
        _TOP - 150,
        "The summary of investment portfolio may change due to ongoing portfolio transactions.",
    )
    pdf.save()
    return path


def write_bmo_lookalike(path: Path) -> Path:
    """Write a fictional BMO-style disclosure with shares and dollar columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")

    def row(y: float, name: str, shares: str, value: str, percent: str) -> None:
        pdf.drawString(_LEFT, y, name)
        pdf.drawRightString(340, y, shares)
        pdf.drawRightString(470, y, value)
        pdf.drawRightString(_RIGHT, y, percent)

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "BMO Fictional Canadian Equity Index ETF (ZFN)")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 18, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 32, "As at June 30, 2026")

    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(_LEFT, _TOP - 62, "Security Name")
    pdf.drawRightString(340, _TOP - 62, "Number of Shares")
    pdf.drawRightString(470, _TOP - 62, "Market Value ($)")
    pdf.drawRightString(_RIGHT, _TOP - 62, "% of Net Assets")
    pdf.setFont("Helvetica", 9)
    rows = [
        ("Northgate Bank", "1,234,500", "$ 45,678,900", "44.56%"),
        ("Maplecrest Properties", "987,654", "$ 23,456,700", "32.34%"),
        ("Pineview Software Corp.", "456,789", "$ 12,345,600", "21.23%"),
        ("Cash and Cash Equivalents", "-", "$ 1,000,000", "0.97%"),
    ]
    for index, data in enumerate(rows, start=1):
        row(_TOP - 62 - index * 14, *data)

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawRightString(_RIGHT, _TOP - 150, "Total Investments 99.10%")
    pdf.save()
    return path
