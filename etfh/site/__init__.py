"""Static site for the weekly digest (FR-3.1, interfaces wave 08 §4).

``render_site`` writes an offline-friendly GitHub Pages directory from digest
documents: no JavaScript, inline CSS with light and dark themes, relative
links only, and the same bytes for the same input. See
:mod:`etfh.site.render` for the file list.
"""

from __future__ import annotations

from etfh.site.render import render_site

__all__ = ["render_site"]
