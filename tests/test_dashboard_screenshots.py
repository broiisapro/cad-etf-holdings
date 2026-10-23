"""Guards for the committed README images (D59, D62).

The images must stay small, stay 1400x900 and come from the synthetic demo, not
from a real build.
"""

from __future__ import annotations

import struct
from pathlib import Path

from tests._dashboard_helpers import REPO_ROOT

IMAGE_DIR = REPO_ROOT / "docs" / "img"
IMAGES = ("fund.png", "heatmap.png", "overlap.png")
MAX_BYTES = 400 * 1024
SCRIPT = REPO_ROOT / "scripts" / "make_screenshots.py"


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def test_readme_images_are_small_and_readme_sized() -> None:
    for name in IMAGES:
        path = IMAGE_DIR / name
        assert path.is_file(), f"missing {path}"
        assert _png_size(path) == (1400, 900), f"{name} is not 1400x900"
        assert path.stat().st_size < MAX_BYTES, f"{name} is larger than 400 KB"


def test_screenshot_script_runs_the_synthetic_demo() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'environment["ETFH_DEMO"] = "1"' in source
    assert 'environment.pop("ETFH_DB", None)' in source
