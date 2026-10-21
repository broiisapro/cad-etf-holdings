"""Name normalization, a reference set and reviewed match proposals (D54).

Name-only holdings rows (Purpose's daily JSON, every quarterly disclosure PDF)
carry no identifier at all. They can only be identified through the reviewed
overrides file, and the human needs help deciding: this module turns every
identifier-bearing name ever seen in the raw store into a reference set and
proposes candidates for the unmatched names.

Two rules keep the proposals safe:

* Only rows whose ``security_id`` is backed by an identifier on the row
  (provided ISIN, CUSIP, or MIC+ticker) feed the reference set. Ids that came
  from a name override never reinforce themselves (D41, D54).
* ``confidence="exact"`` requires one candidate id: normalized names equal,
  countries agreeing when both are known, and exactly one security. A name
  that normalizes to several ids (share classes, dual listings) gets
  ``fuzzy`` rows at score 100 instead, which is how it shows up as ambiguous.

Nothing here merges anything: proposals become real only when a human
approves them (``etfh.secmaster.review``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import polars as pl
from rapidfuzz import fuzz, process

from etfh.secmaster.identifiers import cusip_is_valid, isin_is_valid, normalize_ticker

__all__ = [
    "infer_kind",
    "name_proposals",
    "normalize_country",
    "normalize_name",
    "reference_names",
]

NameKind = Literal["equity", "bond", "other"]

_REFERENCE_SCHEMA = {
    "normalized_name": pl.String,
    "country": pl.String,
    "security_id": pl.String,
    "example_name": pl.String,
    "n_rows": pl.Int64,
}

_PROPOSAL_SCHEMA = {
    "normalized_name": pl.String,
    "example_name": pl.String,
    "country": pl.String,
    "n_funds": pl.Int64,
    "total_weight": pl.Float64,
    "candidate_security_id": pl.String,
    "candidate_name": pl.String,
    "score": pl.Float64,
    "confidence": pl.String,
}

_REFERENCE_COLUMNS = (
    "security_id",
    "security_name",
    "isin",
    "cusip",
    "exchange",
    "security_ticker",
    "country",
)

_NON_SECURITY_PREFIXES = ("CASH:", "DERIV:")
_EMPTY_COUNTRIES = frozenset({"", "UNKNOWN", "N/A", "NA", "NONE", "NOT AVAILABLE", "-"})

# Legal-form tokens dropped from equity names, and from anywhere in a bond
# issuer's name (a bond prints the legal form in the middle: "XYZ CORP 4.5%
# 2030", where the trailing-token rule for equities would keep it).
_LEGAL_SUFFIXES = frozenset(
    {
        "AB",
        "AG",
        "ASA",
        "BHD",
        "BV",
        "CO",
        "COMPANY",
        "CORP",
        "CORPORATION",
        "GMBH",
        "HOLDING",
        "HOLDINGS",
        "INC",
        "INCORPORATED",
        "KK",
        "LIMITEE",
        "LLC",
        "LLP",
        "LP",
        "LTD",
        "LIMITED",
        "LTEE",
        "NV",
        "OYJ",
        "PLC",
        "PTE",
        "SA",
        "SE",
        "SPA",
        "TRUST",
        "ULC",
    }
)

# Share-class markers: "CLASS A", "CL B", "SERIES 1", "SER C".
_CLASS_WORDS = frozenset({"CLASS", "CL", "SERIES", "SER"})
_CLASS_VALUE = re.compile(r"^[A-Z0-9]{1,2}$")

# Tokens that describe the instrument's form rather than its issuer.
_EQUITY_NOISE = frozenset(
    {
        "COM",
        "COMMON",
        "NONVOTING",
        "NONVTG",
        "ORD",
        "ORDINARY",
        "SHARES",
        "SHS",
        "SUB",
        "VOTING",
        "VTG",
    }
)

# Bond descriptors that carry no identity: "XYZ 5% 2031 Bond" is the same
# instrument as "XYZ 5% 2031".
_BOND_DESCRIPTORS = frozenset(
    {"BILL", "BILLS", "BOND", "BONDS", "DEBENTURE", "DEBENTURES", "DUE", "MTN", "NOTE", "NOTES"}
)

_TOKEN = re.compile(r"[A-Z0-9]+(?:\.[0-9]+)?")
_COUPON = re.compile(r"\d+(?:\.\d+)?\s*%")
_YEAR = re.compile(r"\b(?:19|20|21)\d{2}\b")
_BOND_WORDS = re.compile(
    r"\b(?:BOND|BONDS|DEBENTURE|DEBENTURES|BILL|BILLS|MTN|NOTE|NOTES|DUE)\b", re.IGNORECASE
)

# Country labels seen in holdings files, mapped to ISO 3166-1 alpha-2 so that
# "United States", "USA" and "US" agree when both sides are known.
_COUNTRY_NAMES: dict[str, str] = {
    "AUSTRALIA": "AU",
    "AUSTRIA": "AT",
    "BELGIUM": "BE",
    "BERMUDA": "BM",
    "BRAZIL": "BR",
    "CANADA": "CA",
    "CAYMAN ISLANDS": "KY",
    "CHINA": "CN",
    "DENMARK": "DK",
    "FINLAND": "FI",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "GREAT BRITAIN": "GB",
    "HONG KONG": "HK",
    "INDIA": "IN",
    "INDONESIA": "ID",
    "IRELAND": "IE",
    "ISRAEL": "IL",
    "ITALY": "IT",
    "JAPAN": "JP",
    "KOREA": "KR",
    "LUXEMBOURG": "LU",
    "MEXICO": "MX",
    "NETHERLANDS": "NL",
    "NEW ZEALAND": "NZ",
    "NORWAY": "NO",
    "PHILIPPINES": "PH",
    "PORTUGAL": "PT",
    "RUSSIA": "RU",
    "SAUDI ARABIA": "SA",
    "SINGAPORE": "SG",
    "SOUTH AFRICA": "ZA",
    "SOUTH KOREA": "KR",
    "SPAIN": "ES",
    "SWEDEN": "SE",
    "SWITZERLAND": "CH",
    "TAIWAN": "TW",
    "THAILAND": "TH",
    "TURKEY": "TR",
    "UNITED ARAB EMIRATES": "AE",
    "UNITED KINGDOM": "GB",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "USA": "US",
}


@dataclass
class _NameSummary:
    """One unmatched normalized name and its aggregate weight."""

    normalized_name: str
    example_name: str
    n_funds: int
    total_weight: float
    country: str | None


def normalize_name(name: str, *, kind: NameKind = "equity") -> str:
    """Normalize a security name for review proposals.

    Case is folded, accents removed, punctuation turned into spaces and ``&``
    spelled as ``AND``, so ``"Johnson & Johnson"``, ``"Johnson and Johnson"``
    and ``"JOHNSON  &  JOHNSON"`` agree. For ``kind="equity"``, ``The``,
    share-class markers (``CLASS A``, ``CL B``, ``SERIES 1``), instrument-form
    tokens (``COM``, ``SHS``, ``SUB VTG``) and trailing legal forms (``Inc``,
    ``Corp``, ``Ltd``, ``Limited``, ``Corporation``, ``Co``, ``Plc``, ``SA``,
    ``AG``, ``NV``, ...) are dropped.

    ``kind="bond"`` keeps the coupon and maturity tokens that distinguish two
    bonds of the same issuer (``"ROYAL BANK OF CANADA 4.5 2030"``), and drops
    legal forms anywhere in the name because a bond prints them mid-string.
    ``kind="other"`` only folds case, accents and punctuation.

    This drives proposals only; it never merges a holding (D54).
    """
    if kind not in ("equity", "bond", "other"):
        raise ValueError(f"unknown name kind: {kind!r}")
    tokens = _tokens(name)
    if kind == "other":
        return " ".join(tokens)
    cleaned = [token for token in tokens if token != "THE"]
    if kind == "bond":
        cleaned = [token for token in cleaned if token not in _LEGAL_SUFFIXES]
        cleaned = [token for token in cleaned if token not in _BOND_DESCRIPTORS]
    else:
        cleaned = _drop_class_markers(cleaned)
        cleaned = [token for token in cleaned if token not in _EQUITY_NOISE]
        cleaned = _drop_trailing_legal_suffixes(cleaned)
    return " ".join(cleaned) or " ".join(tokens)


def infer_kind(name: str) -> NameKind:
    """Guess whether a security name denotes a bond or an equity.

    A coupon (``4.5%``), a four-digit year or a bond word (``BOND``,
    ``DEBENTURE``, ``NOTE``, ``MTN``, ``DUE``) marks a bond; everything else is
    treated as an equity for normalization. The caller can always pass an
    explicit ``kind`` to :func:`normalize_name`.
    """
    if _COUPON.search(name) or _YEAR.search(name) or _BOND_WORDS.search(name):
        return "bond"
    return "equity"


def normalize_country(label: str | None) -> str | None:
    """Return an ISO 3166-1 alpha-2 country code, or ``None`` when unknown.

    Holdings files write countries in different ways (``US``, ``USA``,
    ``United States``, ``Unknown``); proposals compare them, so they are
    canonicalized first. Unrecognized two-letter labels are kept upper-case.
    """
    if label is None:
        return None
    cleaned = " ".join(label.strip().upper().split())
    if cleaned in _EMPTY_COUNTRIES:
        return None
    mapped = _COUNTRY_NAMES.get(cleaned)
    if mapped is not None:
        return mapped
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    return cleaned


def reference_names(frames: Iterable[pl.DataFrame]) -> pl.DataFrame:
    """Collect identifier-backed names from holdings frames (D54).

    Only rows whose ``security_id`` is not null, not ``CASH:``/``DERIV:`` and
    is backed by an identifier on the row itself (a valid ISIN, a valid CUSIP
    or MIC+ticker) contribute: an id that came from a name override must not
    become evidence for the next proposal (D41). Names are normalized with
    :func:`normalize_name` under :func:`infer_kind`; the result has one row per
    (normalized name, country, security id) with the lexicographically first
    raw name as ``example_name`` and the number of contributing rows.

    Columns: ``normalized_name, country, security_id, example_name, n_rows``,
    sorted deterministically.
    """
    counts: dict[tuple[str, str | None, str], tuple[str, int]] = {}
    for frame in frames:
        missing = [column for column in _REFERENCE_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"holdings frame is missing columns: {', '.join(missing)}")
        for row in frame.iter_rows(named=True):
            security_id = row["security_id"]
            if security_id is None or security_id.startswith(_NON_SECURITY_PREFIXES):
                continue
            if not _identifier_backed(row):
                continue
            name = (row["security_name"] or "").strip()
            if not name:
                continue
            normalized = normalize_name(name, kind=infer_kind(name))
            if not normalized:
                continue
            key = (normalized, normalize_country(row["country"]), security_id)
            existing = counts.get(key)
            counts[key] = (min(existing[0], name), existing[1] + 1) if existing else (name, 1)
    rows = [
        {
            "normalized_name": normalized,
            "country": country,
            "security_id": security_id,
            "example_name": example_name,
            "n_rows": n_rows,
        }
        for (normalized, country, security_id), (example_name, n_rows) in counts.items()
    ]
    if not rows:
        return pl.DataFrame(schema=_REFERENCE_SCHEMA)
    return pl.DataFrame(rows, schema=_REFERENCE_SCHEMA).sort(
        ["normalized_name", "security_id", "country"], nulls_last=True
    )


def name_proposals(
    unmatched: pl.DataFrame,
    reference: pl.DataFrame,
    *,
    fuzzy_threshold: int = 92,
) -> pl.DataFrame:
    """Propose reference securities for unmatched names, for human review (D54).

    ``unmatched`` needs ``security_name``; ``fund_ticker``, ``weight`` and
    ``country`` are used when present (they become ``n_funds``,
    ``total_weight`` and the country check). ``reference`` is the output of
    :func:`reference_names`.

    One row per (unmatched normalized name, candidate security id):

    * ``exact`` when the normalized names are equal, the countries agree (or
      one is unknown) and exactly one security id answers to that name;
    * ``fuzzy`` when the best name score reaches ``fuzzy_threshold``, or when
      an exact name maps to several ids — those rows carry score 100 and are
      how an ambiguous name is listed.

    Rows are ordered by ``total_weight`` descending, then ``normalized_name``,
    then score and candidate id, so the output is deterministic. Nothing is
    applied here.
    """
    if not 0 <= fuzzy_threshold <= 100:
        raise ValueError("fuzzy_threshold must be between 0 and 100")
    if "security_name" not in unmatched.columns:
        raise ValueError("unmatched frame has no security_name column")
    missing = [
        column
        for column in ("normalized_name", "country", "security_id", "example_name")
        if column not in reference.columns
    ]
    if missing:
        raise ValueError(f"reference frame is missing columns: {', '.join(missing)}")

    summaries = _aggregate_unmatched(unmatched)
    known = _reference_index(reference)
    if not summaries or not known:
        return pl.DataFrame(schema=_PROPOSAL_SCHEMA)

    known_names = sorted(known)
    rows: list[dict[str, object]] = []
    for summary in summaries.values():
        for candidate in _candidates_for(
            summary.normalized_name, summary.country, known, known_names, fuzzy_threshold
        ):
            rows.append(
                {
                    "normalized_name": summary.normalized_name,
                    "example_name": summary.example_name,
                    "country": summary.country,
                    "n_funds": summary.n_funds,
                    "total_weight": summary.total_weight,
                    "candidate_security_id": candidate[0],
                    "candidate_name": candidate[1],
                    "score": candidate[2],
                    "confidence": candidate[3],
                }
            )
    if not rows:
        return pl.DataFrame(schema=_PROPOSAL_SCHEMA)
    return pl.DataFrame(rows, schema=_PROPOSAL_SCHEMA).sort(
        ["total_weight", "normalized_name", "score", "candidate_security_id"],
        descending=[True, False, True, False],
        nulls_last=True,
    )


def _tokens(name: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    ascii_name = ascii_name.upper().replace("&", " AND ")
    return [_canonical_number(token) for token in _TOKEN.findall(ascii_name)]


def _canonical_number(token: str) -> str:
    """Render ``4.50`` as ``4.5`` so the same coupon matches across issuers."""
    if "." not in token:
        return token
    whole, _, fraction = token.partition(".")
    fraction = fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def _drop_class_markers(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _CLASS_WORDS:
            index += 1
            if index < len(tokens) and _CLASS_VALUE.match(tokens[index]):
                index += 1
            continue
        kept.append(token)
        index += 1
    return kept


def _drop_trailing_legal_suffixes(tokens: list[str]) -> list[str]:
    kept = list(tokens)
    while kept and kept[-1] in _LEGAL_SUFFIXES:
        kept.pop()
    return kept


def _identifier_backed(row: dict[str, object]) -> bool:
    isin = row.get("isin")
    if isinstance(isin, str) and isin.strip() and isin_is_valid(isin.strip().upper()):
        return True
    cusip = row.get("cusip")
    if isinstance(cusip, str) and cusip.strip() and cusip_is_valid(cusip.strip().upper()):
        return True
    exchange = row.get("exchange")
    ticker = row.get("security_ticker")
    return (
        isinstance(exchange, str)
        and bool(exchange.strip())
        and isinstance(ticker, str)
        and normalize_ticker(ticker) is not None
    )


def _aggregate_unmatched(unmatched: pl.DataFrame) -> dict[str, _NameSummary]:
    summaries: dict[str, _NameSummary] = {}
    fund_sets: dict[str, set[str]] = {}
    countries: dict[str, set[str]] = {}
    for row in unmatched.iter_rows(named=True):
        raw_name = row.get("security_name")
        if not raw_name:
            continue
        name = str(raw_name).strip()
        if not name:
            continue
        normalized = normalize_name(name, kind=infer_kind(name))
        if not normalized:
            continue
        summary = summaries.get(normalized)
        if summary is None:
            summary = _NameSummary(
                normalized_name=normalized,
                example_name=name,
                n_funds=0,
                total_weight=0.0,
                country=None,
            )
            summaries[normalized] = summary
            fund_sets[normalized] = set()
            countries[normalized] = set()
        else:
            summary.example_name = min(summary.example_name, name)
        fund = row.get("fund_ticker")
        if fund is not None:
            fund_sets[normalized].add(str(fund))
        weight = row.get("weight")
        if weight is not None:
            summary.total_weight += float(weight)
        country = normalize_country(row.get("country")) if row.get("country") else None
        if country is not None:
            countries[normalized].add(country)
    for normalized, summary in summaries.items():
        summary.n_funds = len(fund_sets[normalized]) or 1
        known = countries[normalized]
        summary.country = next(iter(known)) if len(known) == 1 else None
    return summaries


def _reference_index(reference: pl.DataFrame) -> dict[str, list[tuple[str, str | None, str]]]:
    known: dict[str, list[tuple[str, str | None, str]]] = {}
    frame = reference.select("normalized_name", "country", "security_id", "example_name").unique()
    for normalized, country, security_id, example_name in frame.iter_rows():
        known.setdefault(normalized, []).append((security_id, country, example_name))
    return known


def _candidates_for(
    normalized: str,
    country: str | None,
    known: dict[str, list[tuple[str, str | None, str]]],
    known_names: list[str],
    fuzzy_threshold: int,
) -> list[tuple[str, str, float, str]]:
    """Candidate ``(security_id, name, score, confidence)`` rows for one name."""
    exact = sorted(
        {
            security_id
            for security_id, candidate_country, _ in known.get(normalized, [])
            if candidate_country is None or country is None or candidate_country == country
        }
    )
    if len(exact) == 1:
        candidate = exact[0]
        return [
            (
                candidate,
                _example_for(known[normalized], candidate),
                100.0,
                "exact",
            )
        ]

    matches = process.extract(
        normalized,
        known_names,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=fuzzy_threshold,
        limit=None,
    )
    best: dict[str, tuple[float, str]] = {}
    for candidate_name, score, _ in matches:
        for security_id, _, example_name in known[str(candidate_name)]:
            value = (100.0 if security_id in exact else float(score), example_name)
            previous = best.get(security_id)
            if previous is None or value[0] > previous[0]:
                best[security_id] = value
    return [
        (security_id, example_name, score, "fuzzy")
        for security_id, (score, example_name) in sorted(best.items())
    ]


def _example_for(entries: list[tuple[str, str | None, str]], security_id: str) -> str:
    return min(example for candidate, _, example in entries if candidate == security_id)
