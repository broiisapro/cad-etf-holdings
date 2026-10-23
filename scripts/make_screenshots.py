#!/usr/bin/env python3
"""Capture the README screenshots from the synthetic demo dashboard (D59, D62).

The images must be reproducible and must never contain real issuer data, so the
script starts the dashboard in demo mode (``ETFH_DEMO=1``), ignores any
``ETFH_DB`` in the environment, pins the as-of date to the committed synthetic
expectation in ``tests/fixtures/expected_overlap.json`` and captures three
1400x900 PNGs into ``docs/img/``.

Playwright is a development-only tool, not a project dependency::

    uv run --with playwright playwright install chromium
    uv run --with playwright python scripts/make_screenshots.py

The script stops the Streamlit server in a ``finally`` block and fails if an
image is missing, is not 1400x900, or is larger than 400 KB.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError as exc:  # pragma: no cover - developer tooling
    raise SystemExit(
        "Playwright is required. Run: uv run --with playwright python scripts/make_screenshots.py"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "etfh" / "dashboard" / "app.py"
EXPECTED_OVERLAP_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_overlap.json"
DEFAULT_OUT = REPO_ROOT / "docs" / "img"
VIEWPORT = {"width": 1400, "height": 900}
MAX_BYTES = 400 * 1024
DEMO_BANNER = "Demo data — synthetic funds"


def free_port() -> int:
    """Reserve a free localhost port for the Streamlit server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(url: str, timeout: float) -> None:
    """Poll Streamlit's health endpoint until the server answers ``ok``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.read().strip() == b"ok":
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.25)
    raise RuntimeError(f"Streamlit did not become healthy at {url} within {timeout:.0f}s")


@contextmanager
def demo_server(timeout: float) -> Iterator[str]:
    """Run the dashboard in demo mode and yield its base URL."""
    port = free_port()
    environment = os.environ.copy()
    environment["ETFH_DEMO"] = "1"
    environment.pop("ETFH_DB", None)
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.headless",
        "true",
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
    ]
    with tempfile.TemporaryDirectory(prefix="etfh-screenshots-") as tmp:
        log_path = Path(tmp) / "streamlit.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                wait_for_health(f"{base_url}/_stcore/health", timeout)
                yield base_url
            except Exception:
                log.flush()
                print(log_path.read_text(encoding="utf-8")[-4000:], file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def calendar_label(day: date) -> str:
    """The aria-label Playwright uses for a day in Streamlit's date picker."""
    return f"{day:%A, %B} {day.day}, {day.year}"


def hide_streamlit_chrome(page: Page) -> None:
    """Hide the deploy toolbar and header so the image shows the app only."""
    page.add_style_tag(
        content=(
            'header[data-testid="stHeader"]{display:none!important}'
            '[data-testid="stToolbar"]{display:none!important}'
            '[data-testid="stDecoration"]{display:none!important}'
        )
    )
    page.mouse.move(2, VIEWPORT["height"] - 2)


def set_as_of(page: Page, day: date, expected_heading: str) -> None:
    """Pick ``day`` in the sidebar date input and wait for the new fund heading."""
    if page.get_by_text(expected_heading).count() == 0:
        page.locator('[data-testid="stDateInputCalendarButton"]').click()
        page.get_by_role("dialog").get_by_role("button", name=calendar_label(day)).click()
    page.get_by_text(expected_heading).wait_for(timeout=60_000)


def capture(page: Page, path: Path) -> None:
    """Screenshot the viewport and check the committed-image contract."""
    page.mouse.move(2, VIEWPORT["height"] - 2)
    page.screenshot(path=str(path), full_page=False)
    width, height = png_size(path)
    size = path.stat().st_size
    if (width, height) != (VIEWPORT["width"], VIEWPORT["height"]):
        raise RuntimeError(f"{path.name} is {width}x{height}, expected 1400x900")
    if size > MAX_BYTES:
        raise RuntimeError(f"{path.name} is {size} bytes, larger than {MAX_BYTES} bytes")
    print(f"wrote {path.relative_to(REPO_ROOT)} ({width}x{height}, {size} bytes)")


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def main(argv: list[str] | None = None) -> int:
    """Start the demo, capture the three README images and stop the server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="image directory")
    parser.add_argument("--timeout", type=float, default=120.0, help="server startup timeout")
    arguments = parser.parse_args(argv)

    expected = json.loads(EXPECTED_OVERLAP_PATH.read_text(encoding="utf-8"))
    as_of = date.fromisoformat(expected["asof"])
    fund_a = str(expected["fund_a"])
    fund_b = str(expected["fund_b"])
    expected_overlap = f"{float(expected['overlap']):.2%}"
    heading = f"{fund_a} — {as_of.isoformat()}"
    out_dir = arguments.out
    out_dir.mkdir(parents=True, exist_ok=True)

    with demo_server(arguments.timeout) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
            page.goto(base_url, wait_until="load")
            page.get_by_text(DEMO_BANNER).wait_for(timeout=120_000)
            hide_streamlit_chrome(page)

            set_as_of(page, as_of, heading)
            page.get_by_text("Data quality").wait_for(timeout=60_000)
            page.locator('[data-testid="stDataFrame"]').first.wait_for(timeout=60_000)
            page.wait_for_timeout(500)
            capture(page, out_dir / "fund.png")

            page.get_by_role("tab", name="Overlap").click()
            page.get_by_text("Common holdings").wait_for(timeout=60_000)
            page.get_by_text(expected_overlap).wait_for(timeout=60_000)
            page.locator('[data-testid="stTabPanel"]:visible [data-testid="stDataFrame"]').wait_for(
                timeout=60_000
            )
            page.wait_for_timeout(500)
            capture(page, out_dir / "overlap.png")

            page.get_by_role("tab", name="Heatmap").click()
            page.locator(
                '[data-testid="stTabPanel"]:visible [data-testid="stVegaLiteChart"] svg'
            ).wait_for(timeout=60_000)
            tags = page.locator('[data-testid="stMultiSelectTagsContainer"]').inner_text()
            if fund_a not in tags or fund_b not in tags:
                raise RuntimeError(f"heatmap does not include {fund_a} and {fund_b}: {tags!r}")
            page.wait_for_timeout(500)
            capture(page, out_dir / "heatmap.png")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
