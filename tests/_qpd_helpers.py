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
