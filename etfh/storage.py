"""Raw store: unmodified files with SHA-256 metadata sidecars.

Layout (D20): ``<root>/<issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>``
where ``<fetched_at>`` is the UTC timestamp ``YYYYMMDDTHHMMSSZ``, with a
sidecar ``<fetched_at>.meta.json`` next to each file. The directory date is the
file's as-of hint when known, otherwise the fetch date.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from etfh.types import RawFile

__all__ = ["RawStore", "read_meta"]

_EXT_PATTERN = re.compile(r"^[a-z0-9]+$")
_META_SUFFIX = ".meta.json"


def read_meta(path: str | Path) -> dict[str, Any]:
    """Read a sidecar metadata file and return it as a dict."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: sidecar metadata must be a JSON object")
    return data


class RawStore:
    """Filesystem-backed raw store rooted at ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write(
        self,
        content: bytes,
        *,
        source_id: str,
        issuer: str,
        fund_ticker: str,
        url: str,
        fetched_at: datetime,
        ext: str,
        content_type: str = "application/octet-stream",
        as_of_hint: date | None = None,
        http_status: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> RawFile:
        """Store ``content`` unmodified and return its :class:`RawFile`.

        ``fetched_at`` must be timezone-aware; it is converted to UTC. If a
        file with the same SHA-256 already exists for this issuer, ticker and
        date, nothing is written and the existing file is returned.
        """
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (UTC)")
        fetched_utc = fetched_at.astimezone(UTC)
        clean_ext = ext.lower().lstrip(".")
        if not _EXT_PATTERN.match(clean_ext):
            raise ValueError(f"invalid extension: {ext!r}")
        sha256 = hashlib.sha256(content).hexdigest()
        day = as_of_hint if as_of_hint is not None else fetched_utc.date()
        directory = self.root / issuer / fund_ticker / day.isoformat()
        stamp = fetched_utc.strftime("%Y%m%dT%H%M%SZ")

        existing = self._find_by_sha256(directory, sha256)
        if existing is not None:
            return existing

        target = directory / f"{stamp}.{clean_ext}"
        if target.exists():
            raise FileExistsError(
                f"{target} already exists with different content for the same fetch time"
            )
        directory.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory, prefix=f".{stamp}.", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except BaseException:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

        meta: dict[str, Any] = {
            "url": url,
            "source_id": source_id,
            "issuer": issuer,
            "fund_ticker": fund_ticker,
            "as_of_hint": as_of_hint.isoformat() if as_of_hint is not None else None,
            "fetched_at": fetched_utc.isoformat(),
            "sha256": sha256,
            "content_type": content_type,
            "http_status": http_status,
            "etag": etag,
            "last_modified": last_modified,
            "bytes": len(content),
        }
        sidecar = directory / f"{stamp}{_META_SUFFIX}"
        sidecar.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return RawFile(
            source_id=source_id,
            issuer=issuer,
            fund_ticker=fund_ticker,
            as_of_hint=as_of_hint,
            fetched_at=fetched_utc,
            url=url,
            path=target,
            sha256=sha256,
            content_type=content_type,
            ext=clean_ext,
        )

    def iter_raw(self) -> list[RawFile]:
        """Return every stored file, ordered by issuer, ticker, date, fetched_at."""
        results: list[tuple[str, str, str, str, RawFile]] = []
        if not self.root.exists():
            return []
        for meta_path in self.root.glob(f"*/*/*/*{_META_SUFFIX}"):
            raw = self._raw_file_from_meta(meta_path)
            results.append(
                (
                    raw.issuer,
                    raw.fund_ticker,
                    raw.as_of_hint.isoformat() if raw.as_of_hint else "",
                    raw.fetched_at.isoformat(),
                    raw,
                )
            )
        results.sort(key=lambda item: item[:4])
        return [item[4] for item in results]

    def read_meta(self, path: str | Path) -> dict[str, Any]:
        """Read a sidecar metadata file and return it as a dict."""
        return read_meta(path)

    def _raw_file_from_meta(self, meta_path: Path) -> RawFile:
        meta = read_meta(meta_path)
        stamp = meta_path.name[: -len(_META_SUFFIX)]
        candidates = [
            sibling
            for sibling in meta_path.parent.glob(f"{stamp}.*")
            if not sibling.name.endswith(_META_SUFFIX)
        ]
        if len(candidates) != 1:
            raise ValueError(f"{meta_path}: expected exactly one data file for stamp {stamp}")
        data_path = candidates[0]
        as_of_hint = meta.get("as_of_hint")
        return RawFile(
            source_id=str(meta["source_id"]),
            issuer=str(meta["issuer"]),
            fund_ticker=str(meta["fund_ticker"]),
            as_of_hint=date.fromisoformat(as_of_hint) if as_of_hint else None,
            fetched_at=datetime.fromisoformat(str(meta["fetched_at"])),
            url=str(meta["url"]),
            path=data_path,
            sha256=str(meta["sha256"]),
            content_type=str(meta["content_type"]),
            ext=data_path.suffix.lstrip("."),
        )

    def _find_by_sha256(self, directory: Path, sha256: str) -> RawFile | None:
        if not directory.exists():
            return None
        for meta_path in sorted(directory.glob(f"*{_META_SUFFIX}")):
            meta = read_meta(meta_path)
            if meta.get("sha256") == sha256:
                return self._raw_file_from_meta(meta_path)
        return None
