"""Map dataset security rows to US ticker symbols for the price source.

Only US-listed venues are in scope (D78: Tiingo's free plan covers US-listed
securities), so the function is deliberately total and conservative: anything
that is not a US venue, or that has no ticker, returns ``None`` and the row is
simply left out of a price request.
"""

from __future__ import annotations

from etfh.secmaster.identifiers import normalize_exchange, normalize_ticker

__all__ = ["symbol_for"]

#: MICs of the US venues the free price plan covers: the five venues named in
#: the wave-08 interface (``XNYS``, ``XNAS``, ``XASE``, ``ARCX``, ``BATS``).
#: ``etfh.secmaster`` maps the Bloomberg labels for most of them onto these
#: codes (D22); ``XASE`` is accepted as a raw MIC because the alias table does
#: not list it yet.
_US_MICS = frozenset({"XNYS", "XNAS", "XASE", "ARCX", "BATS"})

_ID_PREFIX = "TKR:"


def _mic(label: str | None) -> str | None:
    """Normalize an exchange label or raw MIC to a MIC, or ``None``."""
    if label is None:
        return None
    normalized = normalize_exchange(label)
    if normalized is not None:
        return normalized
    cleaned = label.strip().upper()
    return cleaned if cleaned in _US_MICS else None


def symbol_for(
    security_id: str | None,
    security_ticker: str | None,
    exchange: str | None,
) -> str | None:
    """Return the Tiingo symbol for a security row, or ``None``.

    ``exchange`` may be a MIC or any label ``etfh.secmaster`` understands
    (``NYSE``, ``NASDAQ GS``, ...). When the row has no exchange label but its
    D23 security id is exchange-qualified (``TKR:<MIC>:<TICKER>``), the id
    supplies both. Class shares use ``-`` for Tiingo (``BRK.B`` → ``BRK-B``).

    ISIN-only, CUSIP-only, name-only, non-US and id-less rows return ``None``:
    the event study then reports them as skipped instead of guessing a symbol.
    """
    mic = _mic(exchange)
    ticker = normalize_ticker(security_ticker)

    if security_id is not None and security_id.startswith(_ID_PREFIX):
        parts = security_id.split(":", 2)
        if len(parts) == 3:
            if mic is None:
                mic = _mic(parts[1])
            if ticker is None:
                ticker = normalize_ticker(parts[2])

    if mic is None or mic not in _US_MICS:
        return None
    if ticker is None:
        return None
    return ticker.replace(".", "-")
