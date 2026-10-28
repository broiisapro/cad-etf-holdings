"""Inline stylesheet for the static digest site.

One stylesheet, inlined into every page: no fonts, images or scripts are
loaded from anywhere, so the pages work offline and behind any CSP. Colours
come from tokens with a light and a dark set chosen by
``prefers-color-scheme``; numbers use tabular figures and every table scrolls
inside its own wrapper instead of breaking the 360 px layout.
"""

from __future__ import annotations

__all__ = ["STYLESHEET"]

STYLESHEET = """\
:root {
  color-scheme: light dark;
  --bg: #fbfbfa;
  --surface: #ffffff;
  --text: #16191c;
  --muted: #535a61;
  --rule: #d9dde1;
  --accent: #0a5c86;
  --zebra: #f3f4f5;
  --demo-bg: #e4eff7;
  --demo-text: #0a3c58;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121517;
    --surface: #191d21;
    --text: #e9ecef;
    --muted: #a7afb7;
    --rule: #2e353b;
    --accent: #8dc8ee;
    --zebra: #1e2328;
    --demo-bg: #10293a;
    --demo-text: #c6e3f7;
  }
}
*,
*::before,
*::after {
  box-sizing: border-box;
}
html {
  -webkit-text-size-adjust: 100%;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
}
a {
  color: var(--accent);
  text-underline-offset: 0.15em;
}
a:hover {
  text-decoration: none;
}
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 2px;
}
.skip-link {
  position: absolute;
  left: -999px;
  top: 0;
  z-index: 2;
  padding: 0.5rem 0.75rem;
  background: var(--surface);
  color: var(--text);
}
.skip-link:focus {
  left: 0.5rem;
  top: 0.5rem;
}
.site-header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem 1.5rem;
  max-width: 72rem;
  margin: 0 auto;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--rule);
}
.wordmark {
  margin: 0;
  font-size: 1rem;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.wordmark a {
  color: inherit;
  text-decoration: none;
}
nav ul {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 1rem;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 0.95rem;
}
nav a[aria-current="page"] {
  color: var(--text);
  font-weight: 600;
  text-decoration: none;
}
main {
  max-width: 72rem;
  margin: 0 auto;
  padding: 1.5rem 1.25rem 3.5rem;
}
h1,
h2,
h3 {
  line-height: 1.2;
  letter-spacing: -0.01em;
}
h1 {
  margin: 0.5rem 0;
  font-size: clamp(1.55rem, 3.4vw, 2.05rem);
}
h2 {
  margin: 2.5rem 0 0.5rem;
  font-size: 1.2rem;
}
p {
  margin: 0.4rem 0 1rem;
}
.lede,
.week-meta,
.note,
caption {
  color: var(--muted);
}
.lede {
  max-width: 52rem;
  margin-bottom: 0.35rem;
}
.week-meta {
  margin: 0 0 0.75rem;
  font-size: 0.925rem;
}
.note {
  max-width: 62rem;
  font-size: 0.875rem;
}
.banner {
  display: inline-block;
  margin: 0.75rem 0 0;
  padding: 0.3rem 0.6rem;
  border-radius: 3px;
  background: var(--demo-bg);
  color: var(--demo-text);
  font-size: 0.9rem;
  font-weight: 600;
}
.table-wrap {
  overflow-x: auto;
  margin: 0.75rem 0 1.25rem;
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--surface);
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.925rem;
}
caption {
  padding: 0.65rem 0.75rem 0.5rem;
  text-align: left;
  font-size: 0.875rem;
}
th,
td {
  padding: 0.45rem 0.6rem;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
  text-align: left;
}
thead th {
  border-bottom: 1px solid var(--rule);
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
tbody tr:last-child > * {
  border-bottom: 0;
}
tbody tr:nth-child(even) {
  background: var(--zebra);
}
.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
th.num {
  text-align: right;
}
.id,
code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85em;
}
.tag {
  display: inline-block;
  padding: 0 0.35rem;
  border: 1px solid var(--rule);
  border-radius: 3px;
  color: var(--muted);
  font-size: 0.75rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.fund-block {
  margin: 1.25rem 0 1.75rem;
}
.fund-block h3 {
  margin: 0 0 0.25rem;
  font-size: 1rem;
}
.fund-block h3 .ticker {
  margin-right: 0.4rem;
}
.pager {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.5rem 1rem;
  margin: 2rem 0 0;
  padding-top: 1rem;
  border-top: 1px solid var(--rule);
  font-size: 0.95rem;
}
.pager a {
  text-decoration: none;
}
.pager a:hover {
  text-decoration: underline;
}
.site-footer {
  max-width: 72rem;
  margin: 0 auto;
  padding: 1.25rem 1.25rem 2.5rem;
  border-top: 1px solid var(--rule);
  color: var(--muted);
  font-size: 0.875rem;
}
.site-footer p {
  margin: 0 0 0.25rem;
}
@media (max-width: 30rem) {
  .site-header,
  main,
  .site-footer {
    padding-left: 0.85rem;
    padding-right: 0.85rem;
  }
  body {
    font-size: 15px;
  }
  table {
    font-size: 0.875rem;
  }
  th,
  td {
    padding: 0.4rem 0.5rem;
  }
}
"""
