"""Tests for the synthetic demo mode (D62) and Community Cloud readiness."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from etfh.dashboard import demo
from tests._dashboard_helpers import APP_PATH, REPO_ROOT


def test_is_demo_reads_the_environment() -> None:
    assert demo.is_demo({"ETFH_DEMO": "1"}, argv=[])
    assert demo.is_demo({"ETFH_DEMO": "TRUE"}, argv=[])
    assert demo.is_demo({"ETFH_DEMO": " on "}, argv=[])
    assert not demo.is_demo({"ETFH_DEMO": "0"}, argv=[])
    assert not demo.is_demo({}, argv=[])


def test_is_demo_reads_the_command_line_flag() -> None:
    assert demo.is_demo({}, argv=["streamlit", "app.py", "--", "--demo"])
    assert not demo.is_demo({}, argv=["streamlit", "app.py", "--demo=false"])


def test_demo_fixtures_are_committed() -> None:
    assert demo.DEMO_RAW_DIR.is_dir()
    assert (demo.DEMO_CONFIG_DIR / "sources.yaml").is_file()


def test_demo_build_is_offline_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("demo build attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    store = demo.build_demo_database()

    assert store.db_path.is_file()
    con = duckdb.connect(str(store.db_path), read_only=True)
    try:
        funds = [
            row[0] for row in con.execute("SELECT DISTINCT fund_ticker FROM holdings").fetchall()
        ]
        files = con.execute("SELECT count(*) FROM file_versions").fetchone()
    finally:
        con.close()
    assert sorted(funds) == ["SYNA", "SYNB", "SYNE", "SYNF"]
    assert files is not None and files[0] == 15
    assert store.raw_dir == demo.DEMO_RAW_DIR


def test_demo_build_reports_missing_fixtures(tmp_path: Path) -> None:
    with pytest.raises(demo.DemoError, match="Demo fixtures not found"):
        demo.build_demo_database(raw_dir=tmp_path / "missing")


def test_demo_apptest_shows_banner_and_synthetic_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("demo app attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    demo.demo_database.clear()
    monkeypatch.setenv("ETFH_DEMO", "1")
    monkeypatch.setenv("ETFH_DB", str(tmp_path / "real-store.duckdb"))
    app = AppTest.from_file(str(APP_PATH), default_timeout=120).run()

    assert not app.exception
    assert app.title[0].value == "Canadian ETF holdings"
    assert app.info[0].value.startswith("Demo data — synthetic funds")
    assert app.selectbox(key="fund").options == ["SYNA", "SYNB", "SYNE", "SYNF"]
    assert not app.error
    assert any("synthetic demo" in caption.value for caption in app.caption)

    manifest = json.loads((REPO_ROOT / "tests" / "fixtures" / "manifest.json").read_text())
    latest = max(date.fromisoformat(day) for day in manifest["funds"]["SYNA"]["as_of_dates"])
    assert app.date_input(key="asof").value == latest
    metrics = {metric.label: metric.value for metric in app.metric}
    assert int(metrics["Holdings"]) > 0
    assert metrics["Portfolio date"] == latest.isoformat()
    assert metrics["Coverage"] == "Full portfolio"


def test_demo_apptest_with_the_command_line_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    demo.demo_database.clear()
    monkeypatch.delenv("ETFH_DEMO", raising=False)
    monkeypatch.setenv("ETFH_DB", "/nonexistent/real-store.duckdb")
    monkeypatch.setattr("sys.argv", ["streamlit", "run", "app.py", "--", "--demo"])
    app = AppTest.from_file(str(APP_PATH), default_timeout=120).run()

    assert not app.exception
    assert app.info[0].value.startswith("Demo data — synthetic funds")
    assert app.selectbox(key="fund").options == ["SYNA", "SYNB", "SYNE", "SYNF"]


def test_cloud_entrypoint_forces_demo_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    demo.demo_database.clear()
    monkeypatch.delenv("ETFH_DEMO", raising=False)
    monkeypatch.setenv("ETFH_DB", str(tmp_path / "real-store.duckdb"))
    app = AppTest.from_file(str(REPO_ROOT / "streamlit_app.py"), default_timeout=120).run()

    assert not app.exception
    assert app.info[0].value.startswith("Demo data — synthetic funds")
    assert app.selectbox(key="fund").options == ["SYNA", "SYNB", "SYNE", "SYNF"]
    assert not app.error


CLEAN_IMPORT_SCRIPT = """
import os
import socket
import sys
import tempfile
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError("the demo entry point attempted network access")

socket.socket.connect = blocked
socket.create_connection = blocked

import streamlit
from etfh.dashboard import demo

seen = {}
original = demo.demo_database


def recording():
    store = original()
    seen["db"] = str(store.db_path)
    return store


demo.demo_database = recording
messages = []
streamlit.info = lambda body, *args, **kwargs: messages.append(str(body))

sys.argv = ["streamlit", "run", "streamlit_app.py"]
import streamlit_app  # the entry point runs on import

db = seen.get("db", "none")
print("demo_db=" + db)
print("built=" + ("yes" if db != "none" and Path(db).is_file() else "no"))
print("same_as_etfh_db=" + ("yes" if db == os.environ.get("ETFH_DB") else "no"))
print("banner=" + ("yes" if any(m.startswith("Demo data") for m in messages) else "no"))
"""
"""Import ``streamlit_app`` in a bare interpreter with no demo environment.

Community Cloud starts the app from the repository root with nothing but the
locked environment installed, so the entry point has to work on its own: it must
force demo mode, must not read ``ETFH_DB`` (a real store is never opened) and
must not reach the network while it builds the synthetic store.
"""


def test_cloud_entrypoint_imports_cleanly_in_a_clean_process(tmp_path: Path) -> None:
    real_store = tmp_path / "real-store.duckdb"
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "ETFH_DB": str(real_store),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "NO_PROXY": "*",
    }
    result = subprocess.run(
        [sys.executable, "-c", CLEAN_IMPORT_SCRIPT],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert values["banner"] == "yes"
    assert values["built"] == "yes"
    assert values["same_as_etfh_db"] == "no"
    assert Path(values["demo_db"]).name == "etfh.duckdb"
    assert Path(values["demo_db"]).parent.name.startswith("etfh-demo-")
    assert not real_store.exists(), "the entry point touched ETFH_DB"


def test_community_cloud_finds_the_uv_lock() -> None:
    """Cloud prefers uv.lock and searches the app directory before the root.

    The official dependency order (streamlit.io docs, checked 2026-10-22) is
    uv.lock, Pipfile, environment.yml, requirements.txt, pyproject.toml. The
    public entry point is ``streamlit_app.py`` at the root, next to the
    committed ``uv.lock``, so Cloud installs the locked environment; no
    requirements.txt is committed because Cloud would ignore it and it would
    only create a second source of truth.
    """
    assert (REPO_ROOT / "uv.lock").is_file()
    assert (REPO_ROOT / "streamlit_app.py").is_file()
    app_dir = REPO_ROOT / "etfh" / "dashboard"
    for name in ("uv.lock", "Pipfile", "environment.yml", "requirements.txt", "pyproject.toml"):
        assert not (app_dir / name).exists(), f"{name} would shadow the root lock file"
    assert not (REPO_ROOT / "requirements.txt").exists()

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12,<3.13"

    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover - the suite is normally run through uv
        pytest.skip("uv is not on PATH; the export check needs the uv binary")
    exported = subprocess.run(
        [uv, "export", "--locked", "--no-hashes", "--no-dev", "--no-emit-project"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    assert "streamlit==" in exported.stdout
    assert "altair==" in exported.stdout
    assert "duckdb==" in exported.stdout
