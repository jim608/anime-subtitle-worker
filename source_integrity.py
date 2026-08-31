"""Read-only source identity guard for one pipeline run.

The default check is metadata-only to avoid two extra full reads of large media.
Deployments can opt into SHA-256 verification for stronger evidence.  In both
modes the descriptor and path are sampled so a replace-during-read is rejected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
from typing import Any


class SourceIntegrityError(RuntimeError):
    """The source disappeared, was replaced, or changed during processing."""


@dataclass(frozen=True)
class SourceSnapshot:
    canonical_path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    sha256: str = ""

    def as_evidence(self) -> dict[str, Any]:
        return asdict(self)


def capture_source_snapshot(
    path: str | Path,
    *,
    hash_content: bool = False,
    chunk_size: int = 1024 * 1024,
) -> SourceSnapshot:
    source = Path(path).resolve(strict=True)
    digest = hashlib.sha256() if hash_content else None
    with source.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not _is_regular_file(before.st_mode):
            raise SourceIntegrityError(f"source is not a regular file: {source}")
        if digest is not None:
            while True:
                chunk = handle.read(max(4096, int(chunk_size)))
                if not chunk:
                    break
                digest.update(chunk)
        after = os.fstat(handle.fileno())
    path_stat = source.stat()
    before_identity = _stat_identity(before)
    if before_identity != _stat_identity(after) or before_identity != _stat_identity(path_stat):
        raise SourceIntegrityError(f"source changed while its identity was captured: {source}")
    return SourceSnapshot(
        canonical_path=str(source),
        size=int(before.st_size),
        mtime_ns=int(before.st_mtime_ns),
        device=int(before.st_dev),
        inode=int(before.st_ino),
        sha256=digest.hexdigest() if digest is not None else "",
    )


def verify_source_snapshot(
    snapshot: SourceSnapshot,
    *,
    hash_content: bool | None = None,
) -> dict[str, Any]:
    require_hash = bool(snapshot.sha256) if hash_content is None else bool(hash_content)
    current = capture_source_snapshot(snapshot.canonical_path, hash_content=require_hash)
    expected_identity = (
        snapshot.canonical_path,
        snapshot.size,
        snapshot.mtime_ns,
        snapshot.device,
        snapshot.inode,
    )
    current_identity = (
        current.canonical_path,
        current.size,
        current.mtime_ns,
        current.device,
        current.inode,
    )
    if expected_identity != current_identity:
        raise SourceIntegrityError(
            f"source identity changed during processing: {snapshot.canonical_path}"
        )
    if snapshot.sha256 and require_hash and current.sha256 != snapshot.sha256:
        raise SourceIntegrityError(
            f"source checksum changed during processing: {snapshot.canonical_path}"
        )
    return {
        **current.as_evidence(),
        "verified": True,
        "verification": "sha256" if snapshot.sha256 else "metadata_identity",
    }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def _is_regular_file(mode: int) -> bool:
    # Avoid another path lookup and keep the check portable across Unix/Windows.
    import stat

    return stat.S_ISREG(mode)
