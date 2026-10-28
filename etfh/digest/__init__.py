"""Weekly "what changed" document derived from a built database (FR-3.1).

The digest answers, for one week, what every tracked fund added, dropped and
reweighted, how much it turned over, how concentrated it is, how much money its
share count implies moved, and which fund pairs became more or less similar. It
is computed from the point-in-time tables (D7, D30), so it never sees a file
published after the week end, and it serialises to the versioned document of
interfaces-08 §3::

    from etfh.digest import build_digest, default_week_end, validate_digest

    doc = build_digest(con, week_end, public=True, sources=sources, funds=funds)
    validate_digest(doc)
    json.dumps(doc, sort_keys=True)

Publication policy (D79) is applied while building: a public document shows
security names, identifiers and weights only for synthetic funds, or for funds
whose every source permits redistribution. Every other fund still reports its
counts, turnover, concentration, flows and overlap values, but its ``changes``
list is empty.

The package is deliberately small and layered:

- :mod:`etfh.digest.window` — the Friday week and its D30 cutoff;
- :mod:`etfh.digest.policy` — the D79 disclosure rule;
- :mod:`etfh.digest.compare` — snapshot selection and the week comparison;
- :mod:`etfh.digest.schema` — the document schema and ``validate_digest``;
- :mod:`etfh.digest.document` — assembling the document.
"""

from __future__ import annotations

from etfh.digest.document import build_digest
from etfh.digest.schema import SCHEMA_VERSION, validate_digest
from etfh.digest.window import default_week_end

__all__ = ["SCHEMA_VERSION", "build_digest", "default_week_end", "validate_digest"]
