"""Tests for the scheduled collection workflow and its ops script (FR-2.3).

Every run of ``ops/actions_collect.sh`` goes through ``/usr/bin/env -i`` with a
shim directory holding ``uv``, ``etfh`` and ``python`` doubles, so nothing comes
from the host: no inherited ``PATH``, no real ``uv`` and no real ``etfh``. The
private data repository is a local bare git repository that the test clones, so
the script's commit and push touch nothing but ``tmp_path`` and no network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = REPO_ROOT / "ops"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "collect.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ENV_BIN = Path("/usr/bin/env")
BASH = Path("/bin/bash")

_GIT_NAME = "Fixture Collector"
_GIT_EMAIL = "collector@example.com"
_USER_AGENT = "etfh-tests/1 (tests@example.com)"
_DAY = "2026-10-23"
_QA_MARKDOWN = "## etfh qa\n\n- files checked: 1\n- excluded files: 0\n\n**Exit status:** 3\n"

_UV_SHIM = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "$ETFH_TEST_UV_LOG"
if [ "${1:-}" = "run" ]; then
    shift
    case "${1:-}" in
        etfh)
            shift
            exec "$ETFH_TEST_BIN/etfh" "$@"
            ;;
        python)
            shift
            exec "$ETFH_TEST_BIN/python" "$@"
            ;;
    esac
fi
exit 0
"""

_PYTHON_SHIM = """#!/bin/bash
set -u
exit 0
"""

_ETFH_SHIM = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "$ETFH_TEST_ETFH_LOG"
command="${1:-}"
shift || true
day=""
raw=""
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --date) day="${2:-}"; shift 2 ;;
        --raw) raw="${2:-}"; shift 2 ;;
        --out) out="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done
case "$command" in
    collect)
        mkdir -p "$raw/synthetic/SYNA/$day"
        printf 'fake holdings\\n' > "$raw/synthetic/SYNA/$day/20261023T120000Z.csv"
        printf '{"sha256": "fake"}\\n' > "$raw/synthetic/SYNA/$day/20261023T120000Z.meta.json"
        ;;
    collect-aum)
        ;;
    build)
        mkdir -p "$out"
        printf 'fake database\\n' > "$out/etfh.duckdb"
        exit "${ETFH_TEST_BUILD_EXIT:-0}"
        ;;
    qa)
        printf '%s' "$ETFH_TEST_QA_MARKDOWN"
        exit "${ETFH_TEST_QA_EXIT:-0}"
        ;;
    export)
        mkdir -p "$out"
        printf 'fund,hhi\\nSYNA,0.1\\n' > "$out/concentration.csv"
        ;;
esac
exit 0
"""


@dataclass(frozen=True)
class Sandbox:
    """A throwaway checkout, a bare data repository and the shim logs."""

    home: Path
    repo: Path
    data: Path
    origin: Path
    bin_dir: Path
    runner_temp: Path
    summary: Path
    uv_log: Path
    etfh_log: Path

    @property
    def raw(self) -> Path:
        return self.data / "raw"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


# The fixture repositories must not pick up the host's git configuration
# (``commit.gpgsign``, hooks, templates): only what the script or the test sets.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _sandbox(tmp_path: Path) -> Sandbox:
    home = tmp_path / "home"
    repo = home / "repo"
    bin_dir = tmp_path / "bin"
    (repo / "ops").mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy(OPS_DIR / "actions_collect.sh", repo / "ops" / "actions_collect.sh")
    _write_executable(bin_dir / "uv", _UV_SHIM)
    _write_executable(bin_dir / "etfh", _ETFH_SHIM)
    _write_executable(bin_dir / "python", _PYTHON_SHIM)

    origin = tmp_path / "origin.git"
    data = tmp_path / "data"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    subprocess.run(
        ["git", "clone", str(origin), str(data)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    _git(
        "-c",
        "user.name=Fixture Seed",
        "-c",
        "user.email=seed@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "seed",
        cwd=data,
    )
    _git("push", "origin", "HEAD:main", cwd=data)

    return Sandbox(
        home=home,
        repo=repo,
        data=data,
        origin=origin,
        bin_dir=bin_dir,
        runner_temp=tmp_path / "runner",
        summary=tmp_path / "summary.md",
        uv_log=tmp_path / "uv.log",
        etfh_log=tmp_path / "etfh.log",
    )


def _run(
    sandbox: Sandbox,
    *,
    date: str | None = _DAY,
    qa_exit: int = 0,
    build_exit: int = 0,
    drop: tuple[str, ...] = (),
    summary: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the script under ``env -i`` with only the values set below."""
    values = {
        "HOME": str(sandbox.home),
        "PATH": f"{sandbox.bin_dir}:/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "RUNNER_TEMP": str(sandbox.runner_temp),
        "ETFH_TEST_BIN": str(sandbox.bin_dir),
        "ETFH_TEST_UV_LOG": str(sandbox.uv_log),
        "ETFH_TEST_ETFH_LOG": str(sandbox.etfh_log),
        "ETFH_TEST_BUILD_EXIT": str(build_exit),
        "ETFH_TEST_QA_EXIT": str(qa_exit),
        "ETFH_TEST_QA_MARKDOWN": _QA_MARKDOWN,
        "ETFH_USER_AGENT": _USER_AGENT,
        "ETFH_GIT_NAME": _GIT_NAME,
        "ETFH_GIT_EMAIL": _GIT_EMAIL,
    }
    if summary:
        values["GITHUB_STEP_SUMMARY"] = str(sandbox.summary)
    for name in drop:
        values.pop(name, None)
    env_args = [f"{name}={value}" for name, value in values.items()]
    script = sandbox.repo / "ops" / "actions_collect.sh"
    argv = [str(ENV_BIN), "-i", *env_args, str(BASH), str(script)]
    argv.append(str(sandbox.data))
    if date is not None:
        argv.append(date)
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=sandbox.home)


def _uv_calls(sandbox: Sandbox) -> list[str]:
    if not sandbox.uv_log.is_file():
        return []
    return sandbox.uv_log.read_text(encoding="utf-8").splitlines()


def _seed_commit_count(sandbox: Sandbox) -> int:
    return int(_git("rev-list", "--count", "main", cwd=sandbox.origin))


# ---------------------------------------------------------------------------
# ops/actions_collect.sh
# ---------------------------------------------------------------------------


def test_script_is_executable_and_parses() -> None:
    script = OPS_DIR / "actions_collect.sh"
    assert os.access(script, os.X_OK)
    result = subprocess.run([str(BASH), "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "TZ=America/Toronto date +%Y-%m-%d" in text
    assert 'git -C "$DATA" -c user.name="$ETFH_GIT_NAME"' in text


def test_collects_verifies_pushes_builds_and_reports_in_order(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox)
    assert result.returncode == 0, result.stderr

    data = sandbox.data
    runner = sandbox.runner_temp
    assert _uv_calls(sandbox) == [
        f"run etfh collect --date {_DAY} --raw {data}/raw",
        f"run etfh collect-aum --date {_DAY} --raw {data}/raw",
        f"run python -m etfh.rawcheck verify {data}/raw",
        f"run etfh build --raw {data}/raw --out {runner}/build",
        f"run etfh qa --db {runner}/build/etfh.duckdb --format markdown",
        f"run etfh export --db {runner}/build/etfh.duckdb --out {runner}/aggregates --asof {_DAY}",
    ]

    tracked = _git("ls-tree", "-r", "--name-only", "main", cwd=sandbox.origin).splitlines()
    assert f"raw/synthetic/SYNA/{_DAY}/20261023T120000Z.csv" in tracked
    assert f"raw/synthetic/SYNA/{_DAY}/20261023T120000Z.meta.json" in tracked
    assert _git("log", "-1", "--format=%s", "main", cwd=sandbox.origin) == f"data: collect {_DAY}"
    assert _git("log", "-1", "--format=%an|%ae|%cn|%ce", "main", cwd=sandbox.origin) == (
        f"{_GIT_NAME}|{_GIT_EMAIL}|{_GIT_NAME}|{_GIT_EMAIL}"
    )
    # The script passes identity with `git -c`; the data checkout keeps no config.
    for key in ("user.name", "user.email"):
        config = subprocess.run(
            ["git", "config", "--local", "--get", key],
            cwd=data,
            check=False,
            capture_output=True,
            text=True,
        )
        assert config.returncode != 0, f"{key} was written to the data checkout config"

    assert sandbox.summary.read_text(encoding="utf-8") == _QA_MARKDOWN
    aggregates = runner / "aggregates" / "concentration.csv"
    assert aggregates.is_file()
    assert "SYNA" in aggregates.read_text(encoding="utf-8")


def test_qa_exit_status_is_propagated_after_the_push(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, qa_exit=3)
    assert result.returncode == 3
    assert "qa_exit=3" in result.stdout
    tracked = _git("ls-tree", "-r", "--name-only", "main", cwd=sandbox.origin).splitlines()
    assert f"raw/synthetic/SYNA/{_DAY}/20261023T120000Z.csv" in tracked
    assert sandbox.summary.read_text(encoding="utf-8").endswith("**Exit status:** 3\n")


def test_build_failure_keeps_the_pushed_files_and_skips_qa(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, build_exit=1)
    assert result.returncode == 1
    tracked = _git("ls-tree", "-r", "--name-only", "main", cwd=sandbox.origin).splitlines()
    assert f"raw/synthetic/SYNA/{_DAY}/20261023T120000Z.csv" in tracked
    assert not any(call.startswith("run etfh qa") for call in _uv_calls(sandbox))
    assert not sandbox.summary.exists()


def test_nothing_to_commit_is_fine(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    first = _run(sandbox)
    assert first.returncode == 0, first.stderr
    commits = _seed_commit_count(sandbox)

    second = _run(sandbox)
    assert second.returncode == 0, second.stderr
    assert f"no new raw files for {_DAY}" in second.stdout
    assert _seed_commit_count(sandbox) == commits


def test_missing_identity_variable_fails_before_collecting(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, drop=("ETFH_GIT_EMAIL",))
    assert result.returncode == 2
    assert "ETFH_GIT_EMAIL" in result.stderr
    assert "missing required environment variable(s)" in result.stderr
    assert _uv_calls(sandbox) == []


def test_missing_user_agent_fails_before_collecting(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, drop=("ETFH_USER_AGENT",))
    assert result.returncode == 2
    assert "ETFH_USER_AGENT" in result.stderr
    assert _uv_calls(sandbox) == []


def test_invalid_date_is_rejected(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, date="23/10/2026")
    assert result.returncode == 2
    assert "DATE must be YYYY-MM-DD" in result.stderr
    assert _uv_calls(sandbox) == []


def test_missing_data_checkout_is_rejected(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    script = sandbox.repo / "ops" / "actions_collect.sh"
    result = subprocess.run(
        [str(BASH), str(script), str(tmp_path / "absent")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "data checkout not found" in result.stderr


def test_omitting_the_date_uses_the_toronto_today(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, date=None)
    assert result.returncode == 0, result.stderr
    subject = _git("log", "-1", "--format=%s", "main", cwd=sandbox.origin)
    assert re.fullmatch(r"data: collect \d{4}-\d{2}-\d{2}", subject)


def test_without_a_job_summary_the_report_goes_to_stdout(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox, summary=False)
    assert result.returncode == 0, result.stderr
    assert _QA_MARKDOWN.strip() in result.stdout
    assert not sandbox.summary.exists()


# ---------------------------------------------------------------------------
# .github/workflows/collect.yml
# ---------------------------------------------------------------------------


def _workflow(path: Path = WORKFLOW) -> dict[str, Any]:
    document: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML resolves the unquoted ``on:`` key to the boolean True (YAML 1.1).
    if True in document:
        document["on"] = document.pop(True)
    return document


def _steps() -> list[dict[str, Any]]:
    return _workflow()["jobs"]["collect"]["steps"]


def _used_actions(document: dict[str, Any]) -> set[str]:
    return {
        step["uses"] for job in document["jobs"].values() for step in job["steps"] if "uses" in step
    }


def _versions(uses: set[str], action: str) -> set[str]:
    return {entry.split("@", 1)[1] for entry in uses if entry.split("@", 1)[0] == action}


def test_workflow_triggers_on_weeknights_and_dispatch() -> None:
    triggers = _workflow()["on"]
    assert [entry["cron"] for entry in triggers["schedule"]] == ["17 23 * * 1-5"]
    dispatch = triggers["workflow_dispatch"]["inputs"]["date"]
    assert dispatch["required"] is False
    assert dispatch["default"] == ""
    assert "YYYY-MM-DD" in dispatch["description"]
    assert "America/Toronto" in dispatch["description"]


def test_workflow_ships_disabled_behind_the_repository_variable() -> None:
    document = _workflow()
    job = document["jobs"]["collect"]
    assert job["if"] == "vars.ETFH_ACTIONS_COLLECT == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 45
    assert job["permissions"] == {"contents": "read"}
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"] == {"group": "collect", "cancel-in-progress": False}


def test_workflow_pins_the_same_actions_as_ci() -> None:
    ci = _used_actions(_workflow(CI_WORKFLOW))
    collect = _used_actions(_workflow())
    for action in ("actions/checkout", "astral-sh/setup-uv"):
        assert _versions(collect, action), f"{action} is missing from collect.yml"
        assert _versions(collect, action) == _versions(ci, action), action


def test_workflow_checks_out_the_private_data_repository() -> None:
    checkouts = [
        step
        for step in _steps()
        if step.get("uses") == "actions/checkout@v7" and step.get("with", {}).get("path") == "data"
    ]
    assert len(checkouts) == 1
    options = checkouts[0]["with"]
    assert options["repository"] == (
        "${{ vars.ETFH_DATA_REPO || 'broiisapro/cad-etf-holdings-data' }}"
    )
    assert options["ssh-key"] == "${{ secrets.ETFH_DATA_DEPLOY_KEY }}"


def test_workflow_runs_the_ops_script_with_the_documented_environment() -> None:
    steps = [step for step in _steps() if "ops/actions_collect.sh" in step.get("run", "")]
    assert len(steps) == 1
    step = steps[0]
    assert step["run"] == 'ops/actions_collect.sh data "$COLLECT_DATE"'
    assert step["env"]["COLLECT_DATE"] == "${{ inputs.date }}"
    assert step["env"]["ETFH_USER_AGENT"] == "${{ vars.ETFH_USER_AGENT }}"
    assert step["env"]["ETFH_GIT_NAME"] == "${{ vars.ETFH_GIT_NAME }}"
    assert step["env"]["ETFH_GIT_EMAIL"] == "${{ vars.ETFH_GIT_EMAIL }}"
    assert step["env"]["OPENFIGI_API_KEY"] == "${{ secrets.OPENFIGI_API_KEY }}"


def test_workflow_artifact_holds_only_derived_aggregates() -> None:
    uploads = [
        step for step in _steps() if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert len(uploads) == 1
    options = uploads[0]["with"]
    assert options["retention-days"] == 30
    assert options["path"] == "${{ runner.temp }}/aggregates"
    assert "data" not in options["path"]


def test_workflow_fails_early_and_never_echoes_the_configuration() -> None:
    first = _steps()[0]
    assert "run" in first
    for name in ("ETFH_USER_AGENT", "ETFH_GIT_NAME", "ETFH_GIT_EMAIL", "ETFH_DATA_DEPLOY_KEY"):
        assert name in first["env"]
        assert name in first["run"]
    assert first["env"]["ETFH_DATA_DEPLOY_KEY"] == "${{ secrets.ETFH_DATA_DEPLOY_KEY }}"

    text = WORKFLOW.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "${{ secrets." in line:
            # Secrets may feed inputs and env, never an echo.
            assert "echo" not in line, line
    assert 'echo "::error::' in first["run"]
