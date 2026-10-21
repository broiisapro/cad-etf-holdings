"""Tests for the launchd schedule and the ops scripts."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = REPO_ROOT / "ops"
PLIST_PATH = OPS_DIR / "launchd" / "com.etfh.collect.plist"


def _sandbox(tmp_path: Path, *, build_exit: int = 0, qa_exit: int = 0) -> tuple[Path, Path]:
    """A fake repo with run_collect.sh and a fake ``uv`` that logs its calls."""
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copy(OPS_DIR / "run_collect.sh", repo / "ops" / "run_collect.sh")
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        f'  *"etfh build"*) exit {build_exit} ;;\n'
        f'  *"etfh qa"*) exit {qa_exit} ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return repo, calls


def _run_collect(
    repo: Path, tmp_path: Path, bin_dir: Path | None = None, *, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    if bin_dir is None:
        bin_dir = tmp_path / "bin"
    env_path = path if path is not None else f"{bin_dir}:/usr/bin:/bin"
    return subprocess.run(
        ["/bin/bash", str(repo / "ops" / "run_collect.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": env_path},
    )


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
    assert 'BUILD_ARGS=(--raw "$REPO/raw" --out "$REPO/build")' in text
    assert 'uv run etfh build "${BUILD_ARGS[@]}"' in text
    assert 'uv run etfh qa --db "$REPO/build/etfh.duckdb"' in text
    assert 'REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' in text


def test_run_collect_loads_dotenv_and_requires_a_user_agent() -> None:
    text = (OPS_DIR / "run_collect.sh").read_text(encoding="utf-8")
    assert 'if [ -f "$REPO/.env" ]' in text
    assert "set -a" in text
    assert '. "$REPO/.env"' in text
    assert "set +a" in text
    assert 'if [ -z "${ETFH_USER_AGENT:-}" ]' in text
    assert "ETFH_USER_AGENT is not set" in text
    assert 'if [ "${ETFH_OPENFIGI:-0}" = "1" ]' in text
    assert "BUILD_ARGS+=(--openfigi)" in text
    assert 'exit "$QA_STATUS"' in text


def test_run_collect_fails_clearly_without_a_user_agent(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path)
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 2
    assert "ETFH_USER_AGENT is not set" in result.stderr
    assert not calls.exists()


def test_run_collect_prepends_the_usual_uv_locations_to_path() -> None:
    text = (OPS_DIR / "run_collect.sh").read_text(encoding="utf-8")
    assert (
        'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"'
        in text
    )


def test_run_collect_fails_clearly_when_uv_is_missing(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path)
    (tmp_path / "bin" / "uv").unlink()
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 2
    assert "uv is not on PATH" in result.stderr
    assert not calls.exists()


def test_run_collect_finds_uv_in_the_user_local_bin(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path)
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (tmp_path / "bin" / "uv").rename(local_bin / "uv")
    (repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(repo, tmp_path, path="/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines()[0] == (
        f"run etfh collect --date today --raw {repo}/raw"
    )


def test_run_collect_loads_env_and_runs_collect_build_qa(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path)
    (repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\nOPENFIGI_API_KEY=super-secret-value\n',
        encoding="utf-8",
    )
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "super-secret-value" not in result.stdout + result.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"run etfh collect --date today --raw {repo}/raw"
    assert lines[1] == f"run etfh build --raw {repo}/raw --out {repo}/build"
    assert lines[2] == f"run etfh qa --db {repo}/build/etfh.duckdb"
    assert "--openfigi" not in " ".join(lines)


def test_run_collect_passes_openfigi_when_the_env_asks_for_it(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path)
    (repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\nETFH_OPENFIGI=1\n',
        encoding="utf-8",
    )
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines[1] == f"run etfh build --raw {repo}/raw --out {repo}/build --openfigi"


def test_run_collect_continues_to_qa_after_a_build_problem(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path, build_exit=1, qa_exit=0)
    (repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert any("etfh qa" in line for line in lines)
    assert "build exited 1" in result.stderr


def test_run_collect_exits_with_the_qa_status(tmp_path: Path) -> None:
    repo, calls = _sandbox(tmp_path, qa_exit=3)
    (repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(repo, tmp_path)
    assert result.returncode == 3
    assert "qa_exit=3" in result.stdout


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
