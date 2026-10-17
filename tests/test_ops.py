"""Tests for the launchd schedule and the ops scripts."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = REPO_ROOT / "ops"
PLIST_PATH = OPS_DIR / "launchd" / "com.etfh.collect.plist"


def _load_plist() -> dict[str, object]:
    return plistlib.loads(PLIST_PATH.read_bytes())


def test_plist_has_the_expected_job_and_schedule() -> None:
    plist = _load_plist()
    assert plist["Label"] == "com.etfh.collect"
    assert plist["ProgramArguments"] == ["/bin/bash", "__REPO__/ops/run_collect.sh"]
    assert plist["WorkingDirectory"] == "__REPO__"
    assert plist["StartCalendarInterval"] == {"Hour": 19, "Minute": 15}
    assert plist["RunAtLoad"] is False


def test_plist_log_paths_use_the_home_placeholder() -> None:
    plist = _load_plist()
    assert plist["StandardOutPath"] == "__HOME__/Library/Logs/etfh/collect.log"
    assert plist["StandardErrorPath"] == "__HOME__/Library/Logs/etfh/collect.err.log"


def test_plist_documents_placeholders_and_hardcodes_no_paths() -> None:
    text = PLIST_PATH.read_text(encoding="utf-8")
    assert "__REPO__" in text
    assert "__HOME__" in text
    assert str(REPO_ROOT) not in text
    assert "/Users/" not in text


def test_scripts_pass_bash_syntax_check() -> None:
    for name in ("run_collect.sh", "install_launchd.sh"):
        result = subprocess.run(
            ["/bin/bash", "-n", str(OPS_DIR / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_run_collect_script_calls_collect_build_and_qa() -> None:
    text = (OPS_DIR / "run_collect.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert 'uv run etfh collect --date today --raw "$REPO/raw"' in text
    assert 'uv run etfh build --raw "$REPO/raw" --out "$REPO/build"' in text
    assert 'uv run etfh qa --db "$REPO/build/etfh.duckdb"' in text
    assert 'REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' in text


def test_install_script_prints_bootstrap_without_running_launchctl(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "launchctl-was-called"
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(f"#!/bin/bash\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake_launchctl.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(OPS_DIR / "install_launchd.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert f"launchctl bootstrap gui/{os.getuid()}" in result.stdout
    assert str(tmp_path / "Library" / "LaunchAgents" / "com.etfh.collect.plist") in result.stdout


def test_install_script_substitutes_placeholders(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(OPS_DIR / "install_launchd.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr

    installed = tmp_path / "Library" / "LaunchAgents" / "com.etfh.collect.plist"
    assert installed.is_file()
    data = plistlib.loads(installed.read_bytes())
    assert data["ProgramArguments"] == ["/bin/bash", f"{REPO_ROOT}/ops/run_collect.sh"]
    assert data["WorkingDirectory"] == str(REPO_ROOT)
    assert data["StandardOutPath"] == f"{tmp_path}/Library/Logs/etfh/collect.log"
    assert data["StandardErrorPath"] == f"{tmp_path}/Library/Logs/etfh/collect.err.log"
    text = installed.read_text(encoding="utf-8")
    assert "__REPO__" not in text
    assert "__HOME__" not in text
    assert (tmp_path / "Library" / "Logs" / "etfh").is_dir()
