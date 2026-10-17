"""Identifier primitives: ISIN/CUSIP validation, CUSIP→ISIN and MIC/ticker normalization.

Every function is pure and total: invalid input returns ``False`` or ``None``,
never a guess. Exchange labels map through an explicit alias table (D22);
anything not in the table is unknown and returns ``None``.
"""

from __future__ import annotations

__all__ = [
    "cusip_is_valid",
    "cusip_to_isin",
    "isin_is_valid",
    "normalize_exchange",
    "normalize_ticker",
]

_DIGITS = frozenset("0123456789")
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ISIN_ALPHABET = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CUSIP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"
_CUSIP_VALUES = {character: index for index, character in enumerate(_CUSIP_ALPHABET)}

_COUNTRY_FOR_CUSIP = {
    "CA": "CA",
    "CAN": "CA",
    "CANADA": "CA",
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
}

_EXCHANGE_ALIASES: dict[str, str] = {
    # Toronto Stock Exchange (XTSE)
    "TSX": "XTSE",
    "TSE": "XTSE",
    "CT": "XTSE",
    "TORONTO STOCK EXCHANGE": "XTSE",
    "XTSE": "XTSE",
    # TSX Venture Exchange (XTSX)
    "TSXV": "XTSX",
    "TSX VENTURE": "XTSX",
    "TSX VENTURE EXCHANGE": "XTSX",
    "TSX-V": "XTSX",
    "CV": "XTSX",
    "XTSX": "XTSX",
    # Cboe Canada, formerly NEO Exchange (NEOE)
    "NEO": "NEOE",
    "NEO EXCHANGE": "NEOE",
    "CBOE CANADA": "NEOE",
    "NEOE": "NEOE",
    # Canadian Securities Exchange (XCNQ)
    "CSE": "XCNQ",
    "CANADIAN SECURITIES EXCHANGE": "XCNQ",
    "XCNQ": "XCNQ",
    # New York Stock Exchange (XNYS)
    "NYSE": "XNYS",
    "NEW YORK STOCK EXCHANGE": "XNYS",
    "UN": "XNYS",
    "XNYS": "XNYS",
    # Nasdaq (XNAS)
    "NASDAQ": "XNAS",
    "NASDAQ GS": "XNAS",
    "NASDAQ GM": "XNAS",
    "NASDAQ CM": "XNAS",
    "UW": "XNAS",
    "UQ": "XNAS",
    "XNAS": "XNAS",
    # NYSE Arca (ARCX)
    "NYSE ARCA": "ARCX",
    "ARCA": "ARCX",
    "UP": "ARCX",
    "ARCX": "ARCX",
    # Cboe BZX (BATS)
    "CBOE BZX": "BATS",
    "BZX": "BATS",
    "BATS": "BATS",
    # London Stock Exchange (XLON)
    "LSE": "XLON",
    "LONDON STOCK EXCHANGE": "XLON",
    "XLON": "XLON",
}

_TICKER_EXCHANGE_SUFFIXES = (
    " CN",
    " US",
    ".TSX",
    ".TSXV",
    ".TO",
    ".V",
    ".NE",
    ".CN",
    "-TSX",
    "-TSXV",
    "-T",
    "-V",
    "-NE",
    "-CN",
)


def isin_is_valid(s: str) -> bool:
    """Return whether ``s`` is a structurally valid ISIN.

    An ISIN is exactly 12 characters: a two-letter country code, a nine
    character alphanumeric NSIN and a numeric check digit computed with the
    Luhn algorithm over the letter-expanded string (A=10 … Z=35). Validation
    is strict: lower-case or padded input is not a valid ISIN as printed.
    """
    if len(s) != 12:
        return False
    if s[0] not in _LETTERS or s[1] not in _LETTERS:
        return False
    if any(character not in _ISIN_ALPHABET for character in s):
        return False
    if s[-1] not in _DIGITS:
        return False
    return _luhn_sum(_isin_expanded(s)) % 10 == 0


def cusip_is_valid(s: str) -> bool:
    """Return whether ``s`` is a structurally valid CUSIP.

    Nine characters: eight from ``0-9A-Z*@#`` and a check digit produced by
    the Modulus 10 Double-Add-Double technique. Each character's value is its
    base-39 index; values in odd positions (0-based) are doubled; every value
    contributes the sum of its digits (so ``A`` = 10 contributes 1).
    """
    if len(s) != 9:
        return False
    if any(character not in _CUSIP_VALUES for character in s):
        return False
    if s[-1] not in _DIGITS:
        return False
    return _cusip_check_digit(s[:8]) == s[-1]


def cusip_to_isin(cusip: str, country: str) -> str | None:
    """Derive the ISIN for a CUSIP under ``country``, or ``None``.

    Structural derivation is only defined for Canada and the United States
    (D23); every other country returns ``None``. An invalid CUSIP returns
    ``None`` as well, so callers can use the result as a validity signal.
    Private-placement CUSIPs using ``*``, ``@`` or ``#`` cannot be represented
    as an ISIN NSIN and also return ``None``.
    """
    code = _COUNTRY_FOR_CUSIP.get(country.strip().upper())
    if code is None or not cusip_is_valid(cusip):
        return None
    if any(character not in _ISIN_ALPHABET for character in cusip):
        return None
    payload = code + cusip
    return payload + _isin_check_digit(payload)


def normalize_exchange(label: str | None) -> str | None:
    """Map an exchange label to its ISO 10383 MIC, or ``None``.

    Labels are matched case-insensitively after stripping and collapsing
    whitespace, against an explicit alias table (D22). Unknown labels return
    ``None``: the exchange is never guessed.
    """
    if label is None:
        return None
    cleaned = " ".join(label.strip().upper().split())
    if not cleaned:
        return None
    return _EXCHANGE_ALIASES.get(cleaned)


def normalize_ticker(t: str | None) -> str | None:
    """Normalize a ticker: upper-case, share-class separators unified to ``.``.

    Exchange suffixes that only denote the venue are stripped: Bloomberg's
    `` CN``/`` US``, the provider suffixes ``.TO``, ``.TSX``, ``.V``, ``.TSXV``,
    ``.NE``, ``.CN`` and the ``-T``/``-TSX``/``-V``/``-TSXV``/``-NE``/``-CN``
    variants. Share-class separators such as ``BRK-B`` become ``BRK.B``; a
    bare suffix leaves no ticker and returns ``None``.
    """
    if t is None:
        return None
    cleaned = t.strip().upper()
    if not cleaned:
        return None
    for suffix in _TICKER_EXCHANGE_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    cleaned = cleaned.replace("-", ".").strip(".")
    return cleaned or None


def _isin_expanded(isin: str) -> str:
    return "".join(str(int(character, 36)) for character in isin.upper())


def _luhn_sum(digits: str) -> int:
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total


def _cusip_check_digit(base8: str) -> str:
    total = 0
    for index, character in enumerate(base8):
        value = _CUSIP_VALUES[character]
        if index % 2 == 1:
            value *= 2
        total += value // 10 + value % 10
    return str((10 - total % 10) % 10)


def _isin_check_digit(payload: str) -> str:
    total = 0
    for index, character in enumerate(reversed(_isin_expanded(payload))):
        value = int(character)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)
