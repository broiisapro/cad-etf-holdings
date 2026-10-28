"""Publication policy for digest documents (D79, interfaces-08 §3).

A public document may name a security (its name, identifier or weight) only
when redistribution is permitted for every file behind it. Three cases qualify:

- the caller asked for an internal document (``public=False``);
- the fund's issuer is ``synthetic``, so the holdings are fictional fixtures
  committed with the code (D31);
- every source of the fund records ``terms.redistribution_allowed: true``.

No real issuer records that permission today (D40/D65/D84), so in practice only
synthetic funds and ``public=False`` documents disclose securities. A source id
that is missing from the registry is treated as *not* permitting
redistribution: the policy fails closed rather than guessing.
"""

from __future__ import annotations

from collections.abc import Mapping

from etfh.registry import Fund, Source

__all__ = ["SYNTHETIC_ISSUER", "fund_is_synthetic", "securities_disclosed"]

#: Issuer label of the committed synthetic fixtures (D31).
SYNTHETIC_ISSUER = "synthetic"


def fund_is_synthetic(fund: Fund) -> bool:
    """True for the synthetic fixture funds, which are always publishable."""
    return fund.issuer == SYNTHETIC_ISSUER


def securities_disclosed(fund: Fund, sources: Mapping[str, Source], *, public: bool) -> bool:
    """Return whether ``fund``'s security-level lines may appear in the document.

    ``sources`` is the loaded ``config/sources.yaml`` registry; ``public`` is
    the caller's choice (public output for Pages/demo, internal for local use).
    """
    if not public:
        return True
    if fund_is_synthetic(fund):
        return True
    for source_id in fund.source_ids:
        source = sources.get(source_id)
        if source is None or not source.terms.redistribution_allowed:
            return False
    return True
