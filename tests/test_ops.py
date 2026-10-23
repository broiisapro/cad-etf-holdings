"""Tests for the launchd schedule, the daily guard and the ops scripts.

Every run of ``ops/run_collect.sh``, ``ops/collect_guard.sh`` or
``ops/install_launchd.sh`` goes through ``/usr/bin/env -i`` with a temporary
shim directory holding ``uv`` and ``etfh`` doubles, so nothing comes from the
host: no inherited ``PATH`` (a real ``uv`` installed on the machine is never
found), no repository ``.env`` outside the sandbox, and no environment
variable the test did not set. ``ETFH_UV_DIRS`` is set explicitly wherever the
script's own uv search must not probe the host. The guard's clock is fake
through ``ETFH_GUARD_NOW``, so these tests never read the real time.
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
    collect)
        printf '%s' "${ETFH_TEST_COLLECT_OUTPUT-}"
        exit "${ETFH_TEST_COLLECT_EXIT:-0}"
        ;;
    collect-aum)
        printf '%s' "${ETFH_TEST_METRICS_OUTPUT-}"
        exit "${ETFH_TEST_METRICS_EXIT:-0}"
        ;;
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

    @property
    def stamp(self) -> Path:
        return self.repo / ".cache" / "etfh" / "last-collect-date"

    @property
    def guard_log(self) -> Path:
        return self.home / "Library" / "Logs" / "etfh" / "guard.log"


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
    for name in ("run_collect.sh", "collect_guard.sh", "install_launchd.sh"):
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
    extra_env: dict[str, str] | None = None,
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
        "ETFH_TEST_METRICS_EXIT=0",
        f"ETFH_TEST_BUILD_EXIT={build_exit}",
        f"ETFH_TEST_QA_EXIT={qa_exit}",
    ]
    if uv_dirs is not None:
        env_args.append(f"ETFH_UV_DIRS={uv_dirs}")
    for key, value in (extra_env or {}).items():
        env_args.append(f"{key}={value}")
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
    collect_output: str = "",
    metrics_output: str = "",
) -> subprocess.CompletedProcess[str]:
    return _run(
        sandbox.repo / "ops" / "run_collect.sh",
        sandbox,
        path=path,
        uv_dirs=uv_dirs,
        build_exit=build_exit,
        qa_exit=qa_exit,
        extra_env={
            "ETFH_TEST_COLLECT_OUTPUT": collect_output,
            "ETFH_TEST_METRICS_OUTPUT": metrics_output,
        },
    )


def _run_guard(
    sandbox: Sandbox,
    *,
    now: str,
    collect_output: str = "",
    metrics_output: str = "",
    qa_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    return _run(
        sandbox.repo / "ops" / "collect_guard.sh",
        sandbox,
        qa_exit=qa_exit,
        extra_env={
            "ETFH_GUARD_NOW": now,
            "ETFH_TEST_COLLECT_OUTPUT": collect_output,
            "ETFH_TEST_METRICS_OUTPUT": metrics_output,
        },
    )


def _seed_env(sandbox: Sandbox) -> None:
    """A repository ``.env`` with the user agent ``run_collect.sh`` requires."""
    (sandbox.repo / ".env").write_text(
        'ETFH_USER_AGENT="etfh-tests/1 (tests@example.com)"\n', encoding="utf-8"
    )


def _stamp(sandbox: Sandbox) -> str | None:
    return sandbox.stamp.read_text(encoding="utf-8").strip() if sandbox.stamp.is_file() else None


def _load_plist() -> dict[str, object]:
    return plistlib.loads(PLIST_PATH.read_bytes())


def test_plist_has_the_expected_job_and_schedule() -> None:
    plist = _load_plist()
    assert plist["Label"] == "com.etfh.collect"
    assert plist["ProgramArguments"] == ["/bin/bash", "__REPO__/ops/collect_guard.sh"]
    assert plist["WorkingDirectory"] == "__REPO__"
    assert plist["StartInterval"] == 1800
    assert plist["RunAtLoad"] is True
    assert "StartCalendarInterval" not in plist


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
    for name in ("run_collect.sh", "collect_guard.sh", "install_launchd.sh"):
        result = subprocess.run(
            [str(BASH), "-n", str(OPS_DIR / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_guard_script_is_executable() -> None:
    assert os.access(OPS_DIR / "collect_guard.sh", os.X_OK)


def test_run_collect_script_calls_collect_metrics_build_and_qa() -> None:
    text = (OPS_DIR / "run_collect.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert 'uv run etfh collect --date today --raw "$REPO/raw"' in text
    assert 'uv run etfh collect-aum --date today --raw "$REPO/raw"' in text
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
    _seed_env(sandbox)
    result = _run_collect(sandbox, path="/usr/bin:/bin", uv_dirs=None)
    assert result.returncode == 0, result.stderr
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[0] == (
        f"collect --date today --raw {sandbox.repo}/raw"
    )
    assert f"path={local_bin}:" in sandbox.env_log.read_text(encoding="utf-8")


def test_run_collect_loads_env_and_runs_collect_metrics_build_qa(tmp_path: Path) -> None:
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
        f"collect-aum --date today --raw {sandbox.repo}/raw",
        f"build --raw {sandbox.repo}/raw --out {sandbox.repo}/build",
        f"qa --db {sandbox.repo}/build/etfh.duckdb",
    ]
    assert "--openfigi" not in sandbox.uv_log.read_text(encoding="utf-8")


def test_run_collect_child_environment_is_only_what_the_test_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
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
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[2] == (
        f"build --raw {sandbox.repo}/raw --out {sandbox.repo}/build --openfigi"
    )


def test_run_collect_continues_to_qa_after_a_metrics_problem(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    result = _run(
        sandbox.repo / "ops" / "run_collect.sh",
        sandbox,
        extra_env={"ETFH_TEST_METRICS_EXIT": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "collect-aum exited 1" in result.stderr
    assert any(
        line.startswith("build --raw")
        for line in sandbox.etfh_log.read_text(encoding="utf-8").splitlines()
    )


def test_run_collect_continues_to_qa_after_a_build_problem(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    result = _run_collect(sandbox, build_exit=1)
    assert result.returncode == 0, result.stderr
    assert "build exited 1" in result.stderr
    assert any(
        line.startswith("qa --db")
        for line in sandbox.etfh_log.read_text(encoding="utf-8").splitlines()
    )


def test_run_collect_exits_with_the_qa_status(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
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
    assert data["ProgramArguments"] == ["/bin/bash", f"{sandbox.repo}/ops/collect_guard.sh"]
    assert data["WorkingDirectory"] == str(sandbox.repo)
    assert data["StartInterval"] == 1800
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == f"{sandbox.home}/Library/Logs/etfh/collect.log"
    assert data["StandardErrorPath"] == f"{sandbox.home}/Library/Logs/etfh/collect.err.log"
    text = installed.read_text(encoding="utf-8")
    assert "__REPO__" not in text
    assert "__HOME__" not in text
    assert (sandbox.home / "Library" / "Logs" / "etfh").is_dir()


# ---------------------------------------------------------------------------
# Daily guard (D64)
# ---------------------------------------------------------------------------


def test_guard_does_nothing_when_today_is_already_stamped(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    sandbox.stamp.parent.mkdir(parents=True)
    sandbox.stamp.write_text("2026-10-23\n", encoding="utf-8")

    result = _run_guard(sandbox, now="2026-10-23 20:15")

    assert result.returncode == 0, result.stderr
    assert not sandbox.etfh_log.exists()
    assert _stamp(sandbox) == "2026-10-23"


def test_guard_waits_for_1800_when_yesterday_was_collected(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    sandbox.stamp.parent.mkdir(parents=True)
    sandbox.stamp.write_text("2026-10-22\n", encoding="utf-8")

    result = _run_guard(sandbox, now="2026-10-23 09:30")

    assert result.returncode == 0, result.stderr
    assert not sandbox.etfh_log.exists()
    assert _stamp(sandbox) == "2026-10-22"


def test_guard_collects_after_1800_and_stamps_the_day(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    sandbox.stamp.parent.mkdir(parents=True)
    sandbox.stamp.write_text("2026-10-22\n", encoding="utf-8")

    result = _run_guard(
        sandbox,
        now="2026-10-23 18:00",
        collect_output="fetched source=bmo_daily fund=ZEM raw/x.xlsx\n",
    )

    assert result.returncode == 0, result.stderr
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[0] == (
        f"collect --date today --raw {sandbox.repo}/raw"
    )
    assert _stamp(sandbox) == "2026-10-23"
    log = sandbox.guard_log.read_text(encoding="utf-8")
    assert "starting collection (last=2026-10-22, now=2026-10-23 18:00)" in log
    assert "stamped 2026-10-23" in log


def test_guard_collects_immediately_after_a_missed_day(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)
    sandbox.stamp.parent.mkdir(parents=True)
    sandbox.stamp.write_text("2026-10-21\n", encoding="utf-8")

    result = _run_guard(
        sandbox,
        now="2026-10-23 09:30",
        collect_output="unchanged source=purpose_daily fund=PDF\n",
    )

    assert result.returncode == 0, result.stderr
    assert _stamp(sandbox) == "2026-10-23"
    assert "stamped 2026-10-23" in sandbox.guard_log.read_text(encoding="utf-8")


def test_guard_collects_on_first_run_without_a_stamp(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    result = _run_guard(
        sandbox,
        now="2026-10-23 10:00",
        collect_output="fetched source=syn_csv fund=SYNA raw/x.csv\n",
    )

    assert result.returncode == 0, result.stderr
    assert _stamp(sandbox) == "2026-10-23"


def test_guard_does_not_stamp_when_nothing_was_fetched(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    result = _run_guard(sandbox, now="2026-10-23 19:00")

    assert result.returncode == 0, result.stderr
    assert _stamp(sandbox) is None
    log = sandbox.guard_log.read_text(encoding="utf-8")
    assert "not stamped; retry in 30 min" in log
    # the build and QA still ran, so the next 30-minute tick retries the day
    assert sandbox.etfh_log.read_text(encoding="utf-8").splitlines()[-1].startswith("qa --db")


def test_guard_stamps_when_only_metrics_were_collected(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    result = _run_guard(
        sandbox,
        now="2026-10-23 19:00",
        metrics_output="unchanged source=bmo_metrics fund=ZEM\n",
    )

    assert result.returncode == 0, result.stderr
    assert _stamp(sandbox) == "2026-10-23"


def test_guard_keeps_the_day_open_when_qa_fails(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    result = _run_guard(sandbox, now="2026-10-23 19:00", qa_exit=3)

    assert result.returncode == 0, result.stderr
    assert "qa_exit=3" in result.stdout
    assert "done rc=3; nothing fetched" in sandbox.guard_log.read_text(encoding="utf-8")


def test_guard_rejects_an_invalid_fake_clock(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    result = _run_guard(sandbox, now="23/10/2026 09:00")

    assert result.returncode == 2
    assert "ETFH_GUARD_NOW must be 'YYYY-MM-DD HH:MM'" in result.stderr
    assert not sandbox.etfh_log.exists()
    assert _stamp(sandbox) is None


def test_guard_state_file_lives_under_the_repo_cache(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _seed_env(sandbox)

    _run_guard(
        sandbox,
        now="2026-10-23 19:00",
        collect_output="unchanged source=syn_csv fund=SYNA\n",
    )

    assert sandbox.stamp == sandbox.repo / ".cache" / "etfh" / "last-collect-date"
    assert sandbox.stamp.is_file()
    assert Path(f"{sandbox.repo}/.cache/etfh").is_dir()
