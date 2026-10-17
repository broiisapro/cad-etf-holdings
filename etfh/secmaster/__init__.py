"""Security master: identifier validation, resolution and reporting.

The public API is re-exported here as each module lands. Identifier
primitives (issue #21), reviewed overrides and row resolution (issue #22),
the unmatched report with fuzzy proposals (issue #23) and the optional cached
OpenFIGI lookup (issue #24) are all in.
"""

from __future__ import annotations

from etfh.secmaster.identifiers import (
    cusip_is_valid,
    cusip_to_isin,
    isin_is_valid,
    normalize_exchange,
    normalize_ticker,
)
from etfh.secmaster.openfigi import FigiLookup
from etfh.secmaster.overrides import Override, load_overrides
from etfh.secmaster.reports import fuzzy_proposals, normalize_name, unmatched_report
from etfh.secmaster.resolve import SecurityMaster

__all__ = [
    "FigiLookup",
    "Override",
    "SecurityMaster",
    "cusip_is_valid",
    "cusip_to_isin",
    "fuzzy_proposals",
    "isin_is_valid",
    "load_overrides",
    "normalize_exchange",
    "normalize_name",
    "normalize_ticker",
    "unmatched_report",
]
