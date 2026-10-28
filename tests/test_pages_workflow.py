"""Tests for the GitHub Pages workflow and its build script.

The workflow only ships disabled (D76) and the site is rendered by code that
another lane owns, so these tests stay offline and never run ``etfh``. Every
run of ``ops/build_digest.sh`` goes through ``/usr/bin/env -i`` with a temporary
shim directory holding a fake ``uv``, so nothing comes from the host: no
inherited ``PATH`` (a real ``uv`` installed on the machine is never found) and
no environment variable the test did not set.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PAGES_WORKFLOW = WORKFLOWS / "pages.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
OPS_DIR = REPO_ROOT / "ops"
SCRIPT = OPS_DIR / "build_digest.sh"
ENV_BIN = Path("/usr/bin/env")
BASH = Path("/bin/bash")
SHARED_ACTIONS = ("actions/checkout", "astral-sh/setup-uv")

_UV_SHIM = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "$ETFH_TEST_UV_LOG"
if [ "${1:-}" != "run" ]; then
    exit 0
fi
shift
if [ "${1:-}" = "etfh" ]; then
    shift
fi
command="${1:-}"
shift || true

out=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--out" ]; then
        out="${2:-}"
        shift 2 || shift
    else
        shift
    fi
done

case "$command" in
    build)
        mkdir -p "$out"
        : > "$out/etfh.duckdb"
        ;;
    weekly)
        if [ "${ETFH_TEST_NO_INDEX:-0}" = "1" ]; then
            exit 0
        fi
        mkdir -p "$out"
        printf '<!doctype html><title>weekly digest</title>\\n' > "$out/index.html"
        if [ -n "${ETFH_TEST_PLANT:-}" ]; then
            mkdir -p "$out/data"
            printf '{"source": "%s"}\\n' "$ETFH_TEST_PLANT" > "$out/data/leak.json"
        fi
        ;;
esac
exit 0
"""


@dataclass(frozen=True)
class Sandbox:
    """A throwaway checkout, a fake data repository and a fake ``uv``."""

    repo: Path
    data: Path
    out: Path
    tmp: Path
    bin_dir: Path
    uv_log: Path


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _triggers(document: Mapping[str, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1, where an unquoted ``on`` key parses as True.
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


def _uses(job: Mapping[str, Any]) -> list[str]:
    return [step["uses"] for step in _steps(job) if isinstance(step.get("uses"), str)]


def _action_pins(document: Mapping[str, Any]) -> dict[str, str]:
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    pins: dict[str, str] = {}
    for job in jobs.values():
        for uses in _uses(job):
            name, _, version = uses.partition("@")
            pins[name] = version
    return pins


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> Sandbox:
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    out = tmp_path / "site"
    tmp = tmp_path / "tmp"
    bin_dir = tmp_path / "bin"
    (repo / "ops").mkdir(parents=True)
    (data / "raw").mkdir(parents=True)
    tmp.mkdir()
    bin_dir.mkdir()
    shutil.copy(SCRIPT, repo / "ops" / "build_digest.sh")
    (repo / "ops" / "build_digest.sh").chmod(0o755)
    _write_executable(bin_dir / "uv", _UV_SHIM)
    return Sandbox(
        repo=repo,
        data=data,
        out=out,
        tmp=tmp,
        bin_dir=bin_dir,
        uv_log=tmp_path / "uv.log",
    )


def _run(
    sandbox: Sandbox,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real script under ``env -i`` with only the values set below."""
    env_args = [
        f"HOME={sandbox.repo}",
        f"PATH={sandbox.bin_dir}:/usr/bin:/bin",
        "LC_ALL=C",
        f"TMPDIR={sandbox.tmp}",
        f"ETFH_TEST_UV_LOG={sandbox.uv_log}",
    ]
    for key, value in (extra_env or {}).items():
        env_args.append(f"{key}={value}")
    command = [
        str(ENV_BIN),
        "-i",
        *env_args,
        str(BASH),
        str(sandbox.repo / "ops" / "build_digest.sh"),
        *args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=sandbox.repo,
    )


# ---------------------------------------------------------------------------
# Workflow (.github/workflows/pages.yml)
# ---------------------------------------------------------------------------


def test_workflow_triggers_the_weekly_schedule_and_manual_dispatch() -> None:
    triggers = _triggers(_load(PAGES_WORKFLOW))

    assert triggers["schedule"] == [{"cron": "23 13 * * 6"}]
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers
    assert "push" not in triggers


def test_workflow_is_read_only_by_default_and_serialises_pages_runs() -> None:
    document = _load(PAGES_WORKFLOW)

    assert document["name"] == "Pages"
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"] == {"group": "pages", "cancel-in-progress": False}


def test_build_job_is_gated_behind_the_pages_switch() -> None:
    build = _load(PAGES_WORKFLOW)["jobs"]["build"]

    assert build["if"] == "vars.ETFH_PAGES == 'true'"
    assert build["runs-on"] == "ubuntu-latest"
    assert build["permissions"] == {"contents": "read"}


def test_uv_and_python_are_installed_exactly_as_in_ci() -> None:
    steps = _steps(_load(PAGES_WORKFLOW)["jobs"]["build"])
    pins = _action_pins(_load(PAGES_WORKFLOW))
    runs = [step.get("run") for step in steps]
    ci_pins = _action_pins(_load(CI_WORKFLOW))

    assert pins["astral-sh/setup-uv"] == ci_pins["astral-sh/setup-uv"]
    assert "uv python install 3.12" in runs
    assert "uv sync --frozen" in runs


def test_shared_action_pins_match_ci() -> None:
    pages_pins = _action_pins(_load(PAGES_WORKFLOW))
    ci_pins = _action_pins(_load(CI_WORKFLOW))

    for action in SHARED_ACTIONS:
        assert action in pages_pins, action
        assert pages_pins[action] == ci_pins[action], action


def test_every_action_is_pinned_to_a_version_tag() -> None:
    for job in _load(PAGES_WORKFLOW)["jobs"].values():
        for uses in _uses(job):
            assert re.fullmatch(r"[^@]+@v\d+(?:\.\d+\.\d+)?", uses), uses


def test_real_mode_checks_out_the_data_repository_with_the_deploy_key() -> None:
    steps = _steps(_load(PAGES_WORKFLOW)["jobs"]["build"])
    checkouts = [
        step
        for step in steps
        if isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/checkout@")
        and step.get("with", {}).get("path") == "data"
    ]

    assert len(checkouts) == 1
    step = checkouts[0]
    assert step["if"] == "vars.ETFH_PAGES_REAL == 'true'"
    assert step["with"]["repository"] == (
        "${{ vars.ETFH_DATA_REPO || 'broiisapro/cad-etf-holdings-data' }}"
    )
    assert step["with"]["ssh-key"] == "${{ secrets.ETFH_DATA_DEPLOY_KEY }}"


def test_digest_step_switches_between_demo_and_real_mode() -> None:
    steps = _steps(_load(PAGES_WORKFLOW)["jobs"]["build"])
    step = next(step for step in steps if step.get("name") == "Build the digest")

    assert step["env"]["ETFH_PAGES_REAL"] == "${{ vars.ETFH_PAGES_REAL }}"
    assert "ops/build_digest.sh real data site" in step["run"]
    assert "ops/build_digest.sh demo - site" in step["run"]


def test_pages_artifact_is_the_rendered_site_and_keeps_hidden_files() -> None:
    steps = _steps(_load(PAGES_WORKFLOW)["jobs"]["build"])
    step = next(
        step
        for step in steps
        if isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/upload-pages-artifact@")
    )

    assert step["uses"] == "actions/upload-pages-artifact@v5"
    assert step["with"]["path"] == "site"
    assert step["with"]["include-hidden-files"] is True


def test_deploy_job_has_the_pages_permissions_environment_and_action() -> None:
    document = _load(PAGES_WORKFLOW)
    deploy = document["jobs"]["deploy"]

    assert deploy["needs"] == "build"
    assert deploy["runs-on"] == "ubuntu-latest"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    steps = _steps(deploy)
    assert len(steps) == 1
    assert steps[0]["id"] == "deployment"
    assert steps[0]["uses"] == "actions/deploy-pages@v5"


# ---------------------------------------------------------------------------
# Build script (ops/build_digest.sh)
# ---------------------------------------------------------------------------


def test_script_passes_bash_syntax_check_and_is_executable() -> None:
    result = subprocess.run(
        [str(BASH), "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert os.access(SCRIPT, os.X_OK)
    assert "set -euo pipefail" in SCRIPT.read_text(encoding="utf-8")


def test_demo_mode_builds_one_synthetic_week(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, "demo", "-", str(sandbox.out))

    assert result.returncode == 0, result.stderr
    assert sandbox.uv_log.read_text(encoding="utf-8").splitlines() == [
        f"run etfh weekly --demo --out {sandbox.out} --weeks 1"
    ]
    assert (sandbox.out / "index.html").is_file()
    assert list(sandbox.tmp.iterdir()) == []


def test_real_mode_rebuilds_the_dataset_then_publishes_eight_public_weeks(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, "real", str(sandbox.data), str(sandbox.out))

    assert result.returncode == 0, result.stderr
    first, second = sandbox.uv_log.read_text(encoding="utf-8").splitlines()
    prefix = f"run etfh build --raw {sandbox.data}/raw --out "
    assert first.startswith(prefix)
    assert first.endswith("/build")
    build_dir = first[len(prefix) :]
    assert second == (
        f"run etfh weekly --db {build_dir}/etfh.duckdb --out {sandbox.out} --weeks 8 --public"
    )
    assert (sandbox.out / "index.html").is_file()
    assert list(sandbox.tmp.iterdir()) == []


def test_real_mode_fails_clearly_without_a_data_checkout(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, "real", str(tmp_path / "missing"), str(sandbox.out))

    assert result.returncode == 2
    assert "real mode needs the data checkout" in result.stderr
    assert not sandbox.uv_log.exists()
    assert not sandbox.out.exists()


def test_missing_index_html_fails_the_build(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(
        sandbox,
        "demo",
        "-",
        str(sandbox.out),
        extra_env={"ETFH_TEST_NO_INDEX": "1"},
    )

    assert result.returncode == 1
    assert "the digest did not write" in result.stderr
    assert "index.html" in result.stderr


def test_leak_guard_rejects_a_published_data_checkout_path(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(
        sandbox,
        "real",
        str(sandbox.data),
        str(sandbox.out),
        extra_env={"ETFH_TEST_PLANT": str(sandbox.data)},
    )

    assert result.returncode == 1
    assert "data checkout path" in result.stderr
    assert (sandbox.out / "data" / "leak.json").is_file()
    assert list(sandbox.tmp.iterdir()) == []


def test_demo_mode_does_not_need_a_data_checkout_to_check(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(
        sandbox,
        "demo",
        "-",
        str(sandbox.out),
        extra_env={"ETFH_TEST_PLANT": str(sandbox.data)},
    )

    assert result.returncode == 0, result.stderr
    assert (sandbox.out / "index.html").is_file()


def test_script_rejects_the_wrong_argument_count_and_unknown_modes(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    too_few = _run(sandbox, "demo", "-")
    unknown = _run(sandbox, "staging", "-", str(sandbox.out))

    assert too_few.returncode == 2
    assert "usage: ops/build_digest.sh" in too_few.stderr
    assert unknown.returncode == 2
    assert "usage: ops/build_digest.sh" in unknown.stderr
    assert not sandbox.uv_log.exists()
