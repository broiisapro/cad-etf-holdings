"""Tests for ``ops/seed_data_repo.sh``, the data-repository seeding script.

Every run goes through ``/usr/bin/env -i`` with a temporary ``HOME`` holding
the git identity of the throwaway data checkout, and a shim directory whose
``uv`` forwards to the real interpreter that runs the tests. The repository is
never touched: the script resolves its own checkout from its location.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from etfh.rawcheck import verify_raw_store
from etfh.storage import RawStore

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ops" / "seed_data_repo.sh"
ENV_BIN = Path("/usr/bin/env")
BASH = Path("/bin/bash")
GIT = shutil.which("git")

FETCHED = datetime(2026, 10, 29, 22, 0, 0, tzinfo=UTC)
DAY = date(2026, 10, 29)

_UV_SHIM = """#!/bin/bash
set -u
if [ "${1:-}" = "run" ]; then shift; fi
if [ "${1:-}" = "python" ]; then shift; exec __PYTHON__ "$@"; fi
echo "fake uv: unsupported arguments: $*" >&2
exit 2
"""


@dataclass(frozen=True)
class Sandbox:
    home: Path
    bin_dir: Path
    checkout: Path
    source: Path


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert GIT is not None
    return subprocess.run(
        [GIT, *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def _write_source(source: Path) -> None:
    store = RawStore(source)
    for index, ticker in enumerate(("SYNA", "SYNB")):
        store.write(
            f"ticker,weight\nMAPL,0.{index + 1}\n".encode(),
            source_id="syn_csv",
            issuer="synthetic",
            fund_ticker=ticker,
            url=f"synthetic://fixtures/{ticker}.csv",
            fetched_at=FETCHED + timedelta(hours=index),
            ext="csv",
            content_type="text/csv",
            as_of_hint=DAY,
        )


def _sandbox(tmp_path: Path, *, git_checkout: bool = True) -> Sandbox:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Seed Test\n\temail = seed@example.invalid\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(_UV_SHIM.replace("__PYTHON__", shlex.quote(sys.executable)), encoding="utf-8")
    uv.chmod(0o755)
    checkout = tmp_path / "data"
    checkout.mkdir()
    if git_checkout:
        result = _git("init", "-q", str(checkout))
        assert result.returncode == 0, result.stderr
    source = tmp_path / "source"
    _write_source(source)
    return Sandbox(home=home, bin_dir=bin_dir, checkout=checkout, source=source)


def _run(sandbox: Sandbox, *args: object) -> subprocess.CompletedProcess[str]:
    env_args = [
        f"HOME={sandbox.home}",
        f"PATH={sandbox.bin_dir}:/usr/bin:/bin",
        "LC_ALL=C",
        f"ETFH_UV_DIRS={sandbox.bin_dir}",
    ]
    return subprocess.run(
        [str(ENV_BIN), "-i", *env_args, str(BASH), str(SCRIPT), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
        cwd=sandbox.home,
    )


def _commits(checkout: Path) -> list[str]:
    result = _git("-C", str(checkout), "log", "--format=%s")
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _status(checkout: Path) -> str:
    result = _git("-C", str(checkout), "status", "--porcelain")
    assert result.returncode == 0, result.stderr
    return result.stdout


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_seeds_a_fresh_data_repository(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, sandbox.source, sandbox.checkout)

    assert result.returncode == 0, result.stderr
    assert "2 file(s) checked, 0 problem(s)" in result.stdout
    assert "2 copied, 0 identical, 0 conflict(s)" in result.stdout
    assert "raw store: 2 data file(s), 2 sidecar(s)" in result.stdout
    assert "committed 4 changed path(s)" in result.stdout
    assert _commits(sandbox.checkout) == ["data: seed raw store"]
    assert _status(sandbox.checkout) == ""
    destination = sandbox.checkout / "raw"
    assert verify_raw_store(destination).ok
    assert _relative_files(destination) == _relative_files(sandbox.source)


def test_uses_the_checkouts_identity_and_never_changes_git_config(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, sandbox.source, sandbox.checkout)

    assert result.returncode == 0, result.stderr
    local_name = _git("-C", str(sandbox.checkout), "config", "--local", "user.name")
    local_email = _git("-C", str(sandbox.checkout), "config", "--local", "user.email")
    assert local_name.returncode != 0
    assert local_email.returncode != 0
    author = _git("-C", str(sandbox.checkout), "log", "-1", "--format=%an <%ae>")
    assert author.stdout.strip() == "Seed Test <seed@example.invalid>"


def test_second_run_commits_nothing(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    first = _run(sandbox, sandbox.source, sandbox.checkout)
    assert first.returncode == 0, first.stderr

    second = _run(sandbox, sandbox.source, sandbox.checkout)

    assert second.returncode == 0, second.stderr
    assert "0 copied, 2 identical, 0 conflict(s)" in second.stdout
    assert "already up to date; nothing committed" in second.stdout
    assert _commits(sandbox.checkout) == ["data: seed raw store"]
    assert _status(sandbox.checkout) == ""


def test_a_broken_source_stops_before_the_checkout_is_touched(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sidecar = sandbox.source / "synthetic/SYNA/2026-10-29/20261029T220000Z.meta.json"
    sidecar.unlink()

    result = _run(sandbox, sandbox.source, sandbox.checkout)

    assert result.returncode == 1
    assert "missing_sidecar" in result.stdout
    assert not (sandbox.checkout / "raw").exists()
    assert _commits(sandbox.checkout) == []


def test_a_destination_conflict_fails_before_committing(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    first = _run(sandbox, sandbox.source, sandbox.checkout)
    assert first.returncode == 0, first.stderr
    target = sandbox.checkout / "raw/synthetic/SYNA/2026-10-29/20261029T220000Z.csv"
    target.write_bytes(b"different bytes")

    result = _run(sandbox, sandbox.source, sandbox.checkout)

    assert result.returncode == 1
    assert "conflict" in result.stdout
    assert target.read_bytes() == b"different bytes"
    assert _commits(sandbox.checkout) == ["data: seed raw store"]


def test_rejects_a_checkout_that_is_not_a_git_repository(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, git_checkout=False)

    result = _run(sandbox, sandbox.source, sandbox.checkout)

    assert result.returncode == 2
    assert "not a git checkout" in result.stderr
    assert not (sandbox.checkout / "raw").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "usage:"),
        (("only-one",), "usage:"),
    ],
)
def test_rejects_the_wrong_number_of_arguments(
    tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, *args)

    assert result.returncode == 2
    assert message in result.stderr


def test_rejects_a_missing_raw_store(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = _run(sandbox, tmp_path / "missing", sandbox.checkout)

    assert result.returncode == 2
    assert "raw store not found" in result.stderr


def test_script_is_executable_and_bash_syntax_clean() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert os.access(SCRIPT, os.X_OK)
    assert "set -euo pipefail" in text
    assert 'git -C "$DATA_CHECKOUT" add raw' in text
    assert 'git -C "$DATA_CHECKOUT" commit -m "data: seed raw store"' in text
    assert 'git -C "$DATA_CHECKOUT" push' not in text
    result = subprocess.run(
        [str(BASH), "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
