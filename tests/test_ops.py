"""Tests for the launchd schedule and the ops scripts.

Every run of ``ops/run_collect.sh`` or ``ops/install_launchd.sh`` goes through
``/usr/bin/env -i`` with a temporary shim directory holding ``uv`` and ``etfh``
doubles, so nothing comes from the host: no inherited ``PATH`` (a real ``uv``
installed on the machine is never found), no repository ``.env`` outside the
sandbox, and no environment variable the test did not set. ``ETFH_UV_DIRS`` is
set explicitly wherever the script's own uv search must not probe the host.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = REPO_ROOT / "ops"
PLIST_PATH = OPS_DIR / "launchd" / "com.etfh.collect.plist"
ENV_BIN = Path("/usr/bin/env")
BASH = Path("/bin/bash")

_UV_SHIM = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "$ETFH_TEST_UV_LOG"
printf 'leak=%s path=%s\\n' "${ETFH_TEST_LEAK-<unset>}" "$PATH" >> "$ETFH_TEST_ENV_LOG"
if [ "${1:-}" = "run" ]; then
    shift
    if [ "${1:-}" = "etfh" ]; then
        shift
    fi
    exec "$ETFH_TEST_BIN/etfh" "$@"
fi
exit 0
"""

_ETFH_SHIM = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "$ETFH_TEST_ETFH_LOG"
case "${1:-}" in
    collect) exit "${ETFH_TEST_COLLECT_EXIT:-0}" ;;
    build) exit "${ETFH_TEST_BUILD_EXIT:-0}" ;;
    qa) exit "${ETFH_TEST_QA_EXIT:-0}" ;;
esac
exit 0
"""


@dataclass(frozen=True)
class Sandbox:
    """A throwaway checkout plus the logs written by the shims."""

    home: Path
    repo: Path
    bin_dir: Path
    uv_log: Path
    etfh_log: Path
    env_log: Path


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> Sandbox:
    """A fake checkout with the real ops scripts and fake ``uv``/``etfh``."""
    home = tmp_path / "home"
    repo = home / "repo"
    bin_dir = tmp_path / "bin"
    (repo / "ops" / "launchd").mkdir(parents=True)
    bin_dir.mkdir()
    for name in ("run_collect.sh", "install_launchd.sh"):
        shutil.copy(OPS_DIR / name, repo / "ops" / name)
    shutil.copy(PLIST_PATH, repo / "ops" / "launchd" / PLIST_PATH.name)
    _write_executable(bin_dir / "uv", _UV_SHIM)
    _write_executable(bin_dir / "etfh", _ETFH_SHIM)
    return Sandbox(
        home=home,
        repo=repo,
        bin_dir=bin_dir,
        uv_log=tmp_path / "uv.log",
        etfh_log=tmp_path / "etfh.log",
        env_log=tmp_path / "env.log",
    )


def _run(
    script: Path,
    sandbox: Sandbox,
    *,
    path: str | None = None,
    uv_dirs: str | None = "",
    build_exit: int = 0,
    qa_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under ``env -i`` with only the values set below.

    ``uv_dirs=None`` leaves ``ETFH_UV_DIRS`` unset so the script uses its
    built-in search list; the default empty string switches that search off.
    """
    env_args = [
        f"HOME={sandbox.home}",
        f"PATH={path if path is not None else f'{sandbox.bin_dir}:/usr/bin:/bin'}",
        "LC_ALL=C",
        f"ETFH_TEST_BIN={sandbox.bin_dir}",
        f"ETFH_TEST_UV_LOG={sandbox.uv_log}",
        f"ETFH_TEST_ETFH_LOG={sandbox.etfh_log}",
        f"ETFH_TEST_ENV_LOG={sandbox.env_log}",
        "ETFH_TEST_COLLECT_EXIT=0",
        f"ETFH_TEST_BUILD_EXIT={build_exit}",
        f"ETFH_TEST_QA_EXIT={qa_exit}",
    ]
    if uv_dirs is not None:
        env_args.append(f"ETFH_UV_DIRS={uv_dirs}")
    command = [str(ENV_BIN), "-i", *env_args, str(BASH), str(script)]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=sandbox.home,
    )


def _run_collect(
    sandbox: Sandbox,
    *,
    path: str | None = None,
    uv_dirs: str | None = "",
    build_exit: int = 0,
    qa_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    return _run(
        sandbox.repo / "ops" / "run_collect.sh",
        sandbox,
        path=path,
        uv_dirs=uv_dirs,
        build_exit=build_exit,
        qa_exit=qa_exit,
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
            [str(BASH), "-n", str(OPS_DIR / name)],
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


def test_run_collect_prepends_the_usual_uv_locations_to_path() -> None:
    text = (OPS_DIR / "run_collect.sh").read_text(encoding="utf-8")
    assert (
        'UV_DIRS="${ETFH_UV_DIRS-$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin}"'
        in text
    )
    assert 'if [ -n "$UV_DIRS" ]' in text
    assert 'export PATH="$UV_DIRS:${PATH}"' in text


def test_run_collect_fails_clearly_without_a_user_agent(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run_collect(sandbox)
    assert result.returncode == 2
    assert "ETFH_USER_AGENT is not set" in result.stderr
    assert not sandbox.uv_log.exists()


def test_run_collect_ignores_a_user_agent_from_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(tmp_path)
    monkeypatch.setenv("ETFH_USER_AGENT", "host-agent/1 (host@example.com)")
    result = _run_collect(sandbox)
    assert result.returncode == 2
    assert "ETFH_USER_AGENT is not set" in result.stderr
    assert not sandbox.uv_log.exists()


def test_run_collect_fails_clearly_when_uv_is_missing(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.bin_dir / "uv").unlink()
    result = _run_collect(sandbox, uv_dirs="")
    assert result.returncode == 2
    assert "uv is not on PATH" in result.stderr
    assert not sandbox.uv_log.exists()


def test_run_collect_finds_uv_in_the_user_local_bin(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    local_bin = sandbox.home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (sandbox.bin_dir / "uv").rename(local_bin / "uv")
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(sandbox, path="/usr/bin:/bin", uv_dirs=None)
    assert result.returncode == 0, result.stderr
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[0] == (
        f"collect --date today --raw {sandbox.repo}/raw"
    )
    assert f"path={local_bin}:" in sandbox.env_log.read_text(encoding="utf-8")


def test_run_collect_loads_env_and_runs_collect_build_qa(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\nOPENFIGI_API_KEY=super-secret-value\n',
        encoding="utf-8",
    )
    result = _run_collect(sandbox)
    assert result.returncode == 0, result.stderr
    assert "super-secret-value" not in result.stdout + result.stderr
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines() == [
        f"collect --date today --raw {sandbox.repo}/raw",
        f"build --raw {sandbox.repo}/raw --out {sandbox.repo}/build",
        f"qa --db {sandbox.repo}/build/etfh.duckdb",
    ]
    assert "--openfigi" not in sandbox.uv_log.read_text(encoding="utf-8")


def test_run_collect_child_environment_is_only_what_the_test_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    decoy = tmp_path / "host-bin"
    decoy.mkdir()
    _write_executable(decoy / "uv", "#!/bin/bash\nexit 99\n")
    monkeypatch.setenv("ETFH_TEST_LEAK", "from-the-host")
    monkeypatch.setenv("PATH", f"{decoy}:{os.environ['PATH']}")

    result = _run_collect(sandbox)

    assert result.returncode == 0, result.stderr
    lines = sandbox.env_log.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(line == f"leak=<unset> path={sandbox.bin_dir}:/usr/bin:/bin" for line in lines)


def test_run_collect_passes_openfigi_when_the_env_asks_for_it(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\nETFH_OPENFIGI=1\n',
        encoding="utf-8",
    )
    result = _run_collect(sandbox)
    assert result.returncode == 0, result.stderr
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[1] == (
        f"build --raw {sandbox.repo}/raw --out {sandbox.repo}/build --openfigi"
    )


def test_run_collect_continues_to_qa_after_a_build_problem(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(sandbox, build_exit=1)
    assert result.returncode == 0, result.stderr
    assert "build exited 1" in result.stderr
    assert any(
        line.startswith("qa --db")
        for line in sandbox.etfh_log.read_text(encoding="utf-8").splitlines()
    )


def test_run_collect_exits_with_the_qa_status(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )
    result = _run_collect(sandbox, qa_exit=3)
    assert result.returncode == 3
    assert "qa_exit=3" in result.stdout


def test_install_script_prints_bootstrap_without_running_launchctl(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    marker = tmp_path / "launchctl-was-called"
    _write_executable(sandbox.bin_dir / "launchctl", f"#!/bin/bash\ntouch {marker}\nexit 0\n")

    result = _run(sandbox.repo / "ops" / "install_launchd.sh", sandbox)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert f"launchctl bootstrap gui/{os.getuid()}" in result.stdout
    assert (
        str(sandbox.home / "Library" / "LaunchAgents" / "com.etfh.collect.plist") in result.stdout
    )


def test_install_script_substitutes_placeholders(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox.repo / "ops" / "install_launchd.sh", sandbox)
    assert result.returncode == 0, result.stderr

    installed = sandbox.home / "Library" / "LaunchAgents" / "com.etfh.collect.plist"
    assert installed.is_file()
    data = plistlib.loads(installed.read_bytes())
    assert data["ProgramArguments"] == ["/bin/bash", f"{sandbox.repo}/ops/run_collect.sh"]
    assert data["WorkingDirectory"] == str(sandbox.repo)
    assert data["StandardOutPath"] == f"{sandbox.home}/Library/Logs/etfh/collect.log"
    assert data["StandardErrorPath"] == f"{sandbox.home}/Library/Logs/etfh/collect.err.log"
    text = installed.read_text(encoding="utf-8")
    assert "__REPO__" not in text
    assert "__HOME__" not in text
    assert (sandbox.home / "Library" / "Logs" / "etfh").is_dir()
