"""Security master: identifier validation, resolution and reporting.

The public API is re-exported here. Identifier primitives (issue #21),
reviewed overrides and row resolution (issue #22), the unmatched report with
fuzzy proposals (issue #23), the optional cached OpenFIGI lookup (issue #24),
Bloomberg exchange/composite codes (issue #73) and the optional composite-FIGI
ids (issue #74) are all in.
"""

from __future__ import annotations

from etfh.secmaster.identifiers import (
    composite_code,
    cusip_is_valid,
    cusip_to_isin,
    isin_is_valid,
    normalize_exchange,
    normalize_ticker,
)
from etfh.secmaster.names import (
    infer_kind,
    name_proposals,
    normalize_country,
    normalize_name,
    reference_names,
)
from etfh.secmaster.openfigi import FigiLookup
from etfh.secmaster.overrides import Override, load_overrides
from etfh.secmaster.reports import fuzzy_proposals, unmatched_report
from etfh.secmaster.resolve import SecurityMaster

__all__ = [
    "FigiLookup",
    "Override",
    "SecurityMaster",
    "composite_code",
    "cusip_is_valid",
    "cusip_to_isin",
    "fuzzy_proposals",
    "infer_kind",
    "isin_is_valid",
    "load_overrides",
    "name_proposals",
    "normalize_country",
    "normalize_exchange",
    "normalize_name",
    "normalize_ticker",
    "reference_names",
    "unmatched_report",
]
