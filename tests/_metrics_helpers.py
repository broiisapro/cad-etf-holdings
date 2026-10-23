"""Test helpers for the fund-metrics parsers and collectors.

The issuer fixtures are fictional look-alikes: the same layout and labels as
the real documents, with invented names, tickers and numbers. The BMO
factsheet fixture is generated as a PDF at test time from the committed text
look-alike so the parser is exercised on a real PDF extraction path.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "issuers"
BMO_FACTSHEET_TEXT = FIXTURES / "bmo_metrics" / "factsheet.txt"
EVOLVE_PAGE = FIXTURES / "evolve_metrics" / "product.html"


def write_text_pdf(path: Path, lines: list[str], *, title: str | None = None) -> Path:
    """Write ``lines`` into a small deterministic PDF at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    if title:
        pdf.setTitle(title)
    y = 750
    for line in lines:
        pdf.drawString(40, y, line)
        y -= 14
    pdf.save()
    return path


def bmo_factsheet_pdf(tmp_path: Path, *, lines: list[str] | None = None) -> Path:
    """Generate the fictional BMO factsheet PDF from the committed text fixture."""
    content = lines if lines is not None else BMO_FACTSHEET_TEXT.read_text().splitlines()
    return write_text_pdf(tmp_path / "bmo_factsheet.pdf", content, title="BMO factsheet look-alike")
