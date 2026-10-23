"""Helpers that build fictional quarterly-disclosure PDFs for parser tests.

The PDFs are created at test time with ReportLab (``invariant=1``) so no
binary fixtures are committed and every run is byte-for-byte deterministic.
Layouts mirror the structures issuers use: unruled text tables, ruled tables,
multi-page tables with repeated headers, two side-by-side lists, sector groups
with subtotal lines, and French-style decimal commas.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from etfh.registry import Fund, Source, TermsRecord
from etfh.storage import RawStore
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


def write_globalx_lookalike(path: Path, *, as_of: str = "March 31, 2026") -> Path:
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
    pdf.drawString(_LEFT, _TOP - 32, f"As at {as_of}")

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


def write_purpose_lookalike(
    path: Path, *, as_of: str = "June 30, 2026", heading: str = "TOP 25 HOLDINGS"
) -> Path:
    """Write a fictional Purpose-style two-column disclosure.

    The sector and asset mixes sit down the left column and the Top 25
    Holdings down the right; the holdings have no stated total. One bond name
    wraps so its weight lands on its own line, as happens in the real
    documents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 320.0

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Fictional Meridian Dividend Fund")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 16, f"As At {as_of}")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, _TOP - 16, heading)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right, _TOP - 30, "% of Fund's Net")
    pdf.drawString(right, _TOP - 42, "Asset Value")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 64, "SECTOR MIX")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 76, "Financials 32.45")
    pdf.drawString(_LEFT, _TOP - 90, "Energy 18.81")
    pdf.drawString(_LEFT, _TOP - 104, "Cash and Cash Equivalents 0.43")
    pdf.drawString(_LEFT, _TOP - 118, "Net Other Assets -0.04")
    pdf.drawString(_LEFT, _TOP - 132, "Total Net Asset Value 100.00")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 154, "ASSET MIX")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 166, "Canadian Equities 72.90")
    pdf.drawString(_LEFT, _TOP - 180, "United States Equities 25.57")

    holdings = [
        ("Northgate Bank", "5.39"),
        ("Maplecrest Properties", "4.87"),
        ("Pineview Software Corp.", "4.50"),
        ("Juniper Health Corp.", "4.14"),
    ]
    y = _TOP - 56
    for name, weight in holdings:
        pdf.drawString(right, y, name)
        pdf.drawRightString(_RIGHT, y, weight)
        y -= 14
    pdf.drawString(right, y, "Fjord Shipping Holdings Ltd., Callable,")
    y -= 12
    pdf.drawRightString(_RIGHT, y, "3.20")
    y -= 12
    pdf.drawString(right, y, "2041/01/31")

    pdf.setFont("Helvetica", 7)
    pdf.drawString(_LEFT, y - 30, "The summary of investment portfolio may change due to")
    pdf.drawString(_LEFT, y - 40, "ongoing portfolio transactions of the investment fund.")
    pdf.drawString(right, y - 30, "Top Holdings table includes the fund's 25 largest")
    pdf.drawString(right, y - 40, "positions. If the fund holds fewer positions, all are shown.")
    pdf.save()
    return path


def write_purpose_options_lookalike(path: Path) -> Path:
    """Write a fictional Purpose-style disclosure with wrapped option names.

    Each written-call option is three physical lines: the name fragment, the
    weight in the right-hand weight column, then the strike price printed
    inside the name column. The strike continuation must never be read as a
    second weight (the real failure: a fund's top-25 sum became 3.07).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 320.0

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Fictional Meridian Financial Income Fund")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 16, "As At March 31, 2026")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, _TOP - 16, "TOP 25 HOLDINGS")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right, _TOP - 30, "% of Fund's Net")
    pdf.drawString(right, _TOP - 42, "Asset Value")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 64, "SECTOR MIX")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 76, "Financials 99.66")
    pdf.drawString(_LEFT, _TOP - 90, "Cash and Cash Equivalents 0.36")
    pdf.drawString(_LEFT, _TOP - 104, "Net Other Assets 0.01")
    pdf.drawString(_LEFT, _TOP - 118, "Equity Call Options -0.03")
    pdf.drawString(_LEFT, _TOP - 132, "Total Net Asset Value 100.00")

    holdings = [
        ("Northgate Bank", "12.67"),
        ("Fairhaven Trust", "12.27"),
        ("Cedarline Financial Corp.", "11.92"),
        ("Kestrel Bancorp", "11.71"),
        ("Maplecrest Bank", "11.49"),
        ("Elmsworth Savings", "10.85"),
        ("Juniper Life Insurance", "7.54"),
        ("Oakhurst Financial Group", "7.46"),
        ("Pineview Asset Management", "6.94"),
        ("Quarrystone Insurance", "6.81"),
        ("Cash and Cash Equivalents", "0.36"),
    ]
    y = _TOP - 56
    for name, weight in holdings:
        pdf.drawString(right, y, name)
        pdf.drawRightString(_RIGHT, y, weight)
        y -= 14

    options = [
        ("Northgate Bank, Written Call Options on April 2026 at CAD", "0.00", "205.00"),
        ("Fairhaven Trust, Written Call Options on April 2026 at CAD", "0.00", "140.00"),
        ("Cedarline Financial Corp., Written Call Options on April", "0.00", "2026 at CAD 195.00"),
        ("Kestrel Bancorp, Written Call Options on April 2026 at CAD", "0.00", "92.00"),
        ("Elmsworth Savings, Written Call Options on April 2026 at", "-0.01", "CAD 49.00"),
        ("Pineview Asset Management, Written Call Options on April", "-0.01", "2026 at CAD 66.00"),
    ]
    for name, weight, strike in options:
        pdf.drawString(right, y, name)
        y -= 6
        pdf.drawRightString(_RIGHT, y, weight)
        y -= 10
        pdf.drawString(right, y, strike)
        y -= 14

    pdf.setFont("Helvetica", 7)
    pdf.drawString(_LEFT, y - 20, "The summary of investment portfolio may change due to")
    pdf.drawString(_LEFT, y - 30, "ongoing portfolio transactions of the investment fund.")
    pdf.save()
    return path


def write_purpose_wrapped_neighbours(path: Path) -> Path:
    """Write a fictional disclosure with two adjacent wrapped bond names.

    The first row's name tail sits right above the second row's name head; the
    join must attach each fragment to its own row and never merge the two
    holdings.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 320.0

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Fictional Meridian Bond Fund")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 16, "As At June 30, 2026")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, _TOP - 16, "TOP 25 HOLDINGS")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right, _TOP - 30, "% of Fund's Net")
    pdf.drawString(right, _TOP - 42, "Asset Value")

    y = _TOP - 56
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right, y, "Fjord Shipping Holdings Ltd., Callable,")
    y -= 6
    pdf.drawRightString(_RIGHT, y, "3.20")
    y -= 10
    pdf.drawString(right, y, "2041/01/31")
    y -= 14
    pdf.drawString(right, y, "Kestrel Aerospace Inc., Callable, 4.100%,")
    y -= 6
    pdf.drawRightString(_RIGHT, y, "2.50")
    y -= 10
    pdf.drawString(right, y, "2038/05/01")
    y -= 14
    pdf.drawString(right, y, "Northgate Bank")
    pdf.drawRightString(_RIGHT, y, "5.00")
    y -= 14
    pdf.drawString(right, y, "Pineview Software Corp.")
    pdf.drawRightString(_RIGHT, y, "4.30")
    pdf.save()
    return path


def write_vanguard_wrapped_lookalike(path: Path) -> Path:
    """Write a fictional Vanguard-style disclosure with a wrapped fund name.

    Mirrors VXC: the first holding's name wraps onto the line that carries the
    weight, the first weight is printed with a ``%`` sign and later ones
    without (their right edge sits about six points further left), and the
    unlabelled listed total and footnotes follow the table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 330.0

    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP, "Quarterly Portfolio Disclosure | June 30, 2026")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 16, "Vanguard Fictional All Cap ETF")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 30, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(_LEFT, _TOP - 50, "Sector Allocation")
    pdf.drawString(right, _TOP - 50, "Top 25 Holdings")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 62, "As at June 30, 2026")
    pdf.drawString(right, _TOP - 62, "As at June 30, 2026")
    pdf.drawString(_LEFT, _TOP - 74, "% of")
    pdf.drawRightString(_RIGHT, _TOP - 74, "% of Underlying Fund")
    pdf.drawString(_RIGHT - 90, _TOP - 86, "Underlying Fund")

    sectors = [("Financials", "32.9%"), ("Energy", "18.0")]
    y = _TOP - 100
    for name, weight in sectors:
        pdf.drawString(_LEFT, y, name)
        pdf.drawRightString(290.0, y, weight)
        y -= 14

    pdf.drawString(right, y, "Vanguard Fictional Large-Cap ETF*")
    pdf.drawRightString(_RIGHT, y, "12.5%")
    y -= 10
    pdf.drawString(right, y, "Vanguard Fictional Developed All Cap ex North America")
    y -= 10
    pdf.drawString(right, y, "Index ETF**")
    pdf.drawRightString(_RIGHT - 6.6, y, "26.4")
    y -= 10
    pdf.drawString(right, y, "Vanguard Fictional Emerging Markets ETF*")
    pdf.drawRightString(_RIGHT - 6.6, y, "11.0")
    y -= 10
    pdf.drawString(right, y, "Cash")
    pdf.drawRightString(_RIGHT - 6.6, y, "0.3")
    y -= 10
    pdf.drawString(right, y, "50.2")
    y -= 10
    pdf.drawString(right, y, "Total net asset value $12,345,678,901")
    pdf.setFont("Helvetica", 7)
    pdf.drawString(_LEFT, y - 12, "* U.S.-domiciled fictional ETF")
    pdf.drawString(_LEFT, y - 22, "** Canada-domiciled fictional ETF")
    pdf.save()
    return path


def write_purpose_overweight_lookalike(path: Path) -> Path:
    """Write a fictional Purpose-style disclosure with a position above 100% NAV.

    Mirrors KILO: a gold position of 101.00% is offset by negative other net
    assets, so the printed weight itself exceeds 100 while the fund totals 100.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 320.0

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(_LEFT, _TOP, "Fictional Meridian Gold Fund")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 16, "As At March 31, 2026")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, _TOP - 16, "TOP 25 HOLDINGS")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right, _TOP - 30, "% of Fund's Net")
    pdf.drawString(right, _TOP - 42, "Asset Value")

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(_LEFT, _TOP - 64, "SECTOR MIX")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 76, "Gold Holdings 101.00")
    pdf.drawString(_LEFT, _TOP - 90, "Cash and Cash Equivalents 0.03")
    pdf.drawString(_LEFT, _TOP - 104, "Net Other Assets -1.03")
    pdf.drawString(_LEFT, _TOP - 118, "Total Net Asset Value 100.00")

    y = _TOP - 56
    pdf.drawString(right, y, "Gold Bars")
    pdf.drawRightString(_RIGHT, y, "101.00")
    y -= 14
    pdf.drawString(right, y, "Cash and Cash Equivalents")
    pdf.drawRightString(_RIGHT, y, "0.03")
    pdf.save()
    return path


# Right-column x positions of the real Evolve layout: names at 360, the
# sector weights right-aligned at 568, and the Top 25 weights right-aligned
# at 336; the left/right split sits between 336 and 360.
_EVOLVE_LEFT_WEIGHT = 336.0
_EVOLVE_RIGHT = 360.0
_EVOLVE_RIGHT_WEIGHT = 568.0


def write_evolve_lookalike(path: Path, *, as_of: str = "June 30, 2026") -> Path:
    """Write a fictional Evolve-style two-column disclosure.

    Top 25 Positions on the left with the listed total at the foot of that
    column, and the sector mix on the right with its own 100.00% total.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Quarterly Portfolio Disclosure")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(44.0, _TOP, "Evolve Fictional Cyber Security Index Fund")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(44.0, _TOP - 16, f"As at {as_of}")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(44.0, _TOP - 32, "Top 25 Positions % of Net Asset")
    pdf.drawString(44.0, _TOP - 44, "Value")
    pdf.drawString(_EVOLVE_RIGHT, _TOP - 32, "Sector Mix % of Net Asset")
    pdf.drawString(_EVOLVE_RIGHT, _TOP - 44, "Value")

    holdings = [
        ("Northwind Materials Inc.", "25.10%"),
        ("Harbourside Logistics Corp.", "12.30%"),
        ("Foxglove Networks Inc.", "5.40%"),
        ("Quarrystone Cement Inc.", "2.60%"),
        ("Birchwood Timber Corp.", "1.40%"),
    ]
    pdf.setFont("Helvetica", 9)
    y = _TOP - 60
    for name, weight in holdings:
        pdf.drawString(44.0, y, name)
        pdf.drawRightString(_EVOLVE_LEFT_WEIGHT, y, weight)
        y -= 14
    pdf.drawString(44.0, y, "Total 46.80%")

    sectors = [
        ("Information Technology", "91.63%"),
        ("Industrials", "8.25%"),
        ("Communication Services", "0.12%"),
    ]
    y = _TOP - 60
    for name, weight in sectors:
        pdf.drawString(_EVOLVE_RIGHT, y, name)
        pdf.drawRightString(_EVOLVE_RIGHT_WEIGHT, y, weight)
        y -= 14
    pdf.drawString(_EVOLVE_RIGHT, y, "Total 100.00%")
    pdf.drawString(_EVOLVE_RIGHT, y - 12, "Total Net Asset Value $ 151,715,057")
    pdf.save()
    return path


def write_vanguard_lookalike(path: Path, *, as_of: str = "June 30, 2026") -> Path:
    """Write a fictional Vanguard-style two-column disclosure.

    Sector allocation down the left column, the Top 25 Holdings down the
    right, and the listed holdings' unlabelled total at the foot of the right
    column just above "Total net asset value".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 330.0

    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP, f"Quarterly Portfolio Disclosure | {as_of}")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 16, "Vanguard Fictional 500 Index ETF")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 30, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(_LEFT, _TOP - 50, "Sector Allocation")
    pdf.drawString(right, _TOP - 50, "Top 25 Holdings")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 62, f"As at {as_of}")
    pdf.drawString(right, _TOP - 62, f"As at {as_of}")
    pdf.drawString(_LEFT, _TOP - 74, "% of")
    pdf.drawRightString(_RIGHT, _TOP - 74, "% of Underlying Fund")
    pdf.drawString(_RIGHT - 60, _TOP - 86, "Underlying Fund")

    sectors = [
        ("Financials", "32.9%"),
        ("Energy", "18.0"),
        ("Technology", "7.7"),
        ("Other assets and liabilities (net)", "0.2"),
    ]
    y = _TOP - 100
    for name, weight in sectors:
        pdf.drawString(_LEFT, y, name)
        pdf.drawRightString(290.0, y, weight)
        y -= 14
    pdf.drawString(_LEFT, y, "100.0")

    holdings = [
        ("Northwind Materials Inc.", "12.50%"),
        ("Pineview Software Corp.", "10.00%"),
        ("Northgate Bank", "8.50%"),
        ("Maplecrest Properties", "7.00%"),
        ("Juniper Health Corp.", "6.50%"),
        ("Cedarline Energy Corp.", "5.50%"),
    ]
    y = _TOP - 100
    for name, weight in holdings:
        pdf.drawString(right, y, name)
        pdf.drawRightString(_RIGHT, y, weight)
        y -= 14
    pdf.drawString(right, y, "50.0")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, y - 12, "Total net asset value $12,345,678,901")
    pdf.save()
    return path


def write_vanguard_older_lookalike(path: Path) -> Path:
    """Write a fictional older Vanguard-style disclosure.

    Mirrors the same two-column table with an older quarter's date and the
    older total wording ("Total investments 50.0%") instead of an unlabelled
    number above "Total net asset value", so the issuer's second total pattern
    has to carry the check.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh test fixtures")
    pdf.setCreator("etfh test fixtures")
    right = 330.0

    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP, "Quarterly Portfolio Disclosure | December 31, 2024")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(_LEFT, _TOP - 16, "Vanguard Fictional 500 Index ETF")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(_LEFT, _TOP - 30, "Summary of Investment Portfolio")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(_LEFT, _TOP - 50, "Sector Allocation")
    pdf.drawString(right, _TOP - 50, "Top 25 Holdings")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(_LEFT, _TOP - 62, "As at December 31, 2024")
    pdf.drawString(right, _TOP - 62, "As at December 31, 2024")
    pdf.drawString(_LEFT, _TOP - 74, "% of")
    pdf.drawRightString(_RIGHT, _TOP - 74, "% of Underlying Fund")

    sectors = [("Financials", "32.9%"), ("Energy", "18.0"), ("Technology", "7.7")]
    y = _TOP - 96
    for name, weight in sectors:
        pdf.drawString(_LEFT, y, name)
        pdf.drawRightString(290.0, y, weight)
        y -= 14
    pdf.drawString(_LEFT, y, "100.0")

    holdings = [
        ("Northwind Materials Inc.", "12.50%"),
        ("Pineview Software Corp.", "10.00%"),
        ("Northgate Bank", "8.50%"),
        ("Maplecrest Properties", "7.00%"),
        ("Juniper Health Corp.", "6.50%"),
        ("Cedarline Energy Corp.", "5.50"),
    ]
    y = _TOP - 96
    for name, weight in holdings:
        pdf.drawString(right, y, name)
        pdf.drawRightString(_RIGHT, y, weight)
        y -= 14
    pdf.drawString(right, y, "Total investments 50.0%")
    pdf.setFont("Helvetica", 7)
    pdf.drawString(_LEFT, y - 14, "The summary of investment portfolio may change.")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right, y - 28, "Total net asset value $12,345,678,901")
    pdf.save()
    return path


def write_backfill_config(
    directory: Path,
    *,
    source_id: str,
    issuer: str,
    tickers: Sequence[str],
    decision: Decision = Decision.allowed_with_conditions,
    min_interval_seconds: float | None = None,
    kind: SourceKind = SourceKind.quarterly_disclosure,
    file_format: FileFormat = FileFormat.pdf,
    params: str = "",
) -> Path:
    """Write a minimal ``sources.yaml`` and ``funds.csv`` and return the directory."""
    directory.mkdir(parents=True, exist_ok=True)
    terms: dict[str, object] = {
        "url": "https://example.invalid/terms",
        "reviewer": "Test Reviewer",
        "reviewed_on": date(2026, 10, 17),
        "decision": decision.value,
    }
    if decision is Decision.allowed_with_conditions:
        terms["conditions"] = ["internal research use only"]
    if min_interval_seconds is not None:
        terms["min_interval_seconds"] = min_interval_seconds
    payload = {
        "sources": [
            {
                "id": source_id,
                "issuer": issuer,
                "kind": kind.value,
                "format": file_format.value,
                "url_patterns": ["https://example.invalid/qpd.pdf"],
                "update_frequency": "quarterly",
                "supported": True,
                "terms": terms,
            }
        ]
    }
    (directory / "sources.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    with (directory / "funds.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ticker", "issuer", "name", "asset_class", "source_ids", "exchange", "params"]
        )
        for ticker in tickers:
            writer.writerow(
                [ticker, issuer, f"Fictional {ticker} Fund", "equity", source_id, "XTSE", params]
            )
    return directory


def seed_qpd_raw(
    store: RawStore,
    pdf: Path,
    *,
    source_id: str,
    issuer: str,
    ticker: str,
    as_of: date | None = None,
    url: str = "https://example.invalid/qpd.pdf",
    fetched_at: datetime | None = None,
) -> RawFile:
    """Store a disclosure PDF as if a collector had fetched it."""
    return store.write(
        pdf.read_bytes(),
        source_id=source_id,
        issuer=issuer,
        fund_ticker=ticker,
        url=url,
        fetched_at=fetched_at or datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
        ext="pdf",
        content_type="application/pdf",
        as_of_hint=as_of,
    )
