from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

from completed_delivery import (
    CompletedDeliveryCollisionError,
    CompletedDeliveryError,
    _strict_publication,
    completed_delivery_destination,
    completed_delivery_marker_path,
    completed_delivery_receipt_path,
    deliver_completed_mkv,
    validate_completed_delivery,
)
from output_manifest import output_manifest_path
from safe_files import sha256_file


TERMINAL_QUEUE_STATUSES = frozenset({"done", "completed", "succeeded"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or commit exactly one strict completed-MKV delivery without enabling "
            "completed delivery in the long-running Worker. Preview is the default."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--video",
        type=Path,
        action="append",
        required=True,
        help="Exact source media path; repeated --video values are rejected",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Read-only strict preflight (default)")
    mode.add_argument("--commit", action="store_true", help="Commit only this one completed MKV")
    return parser


def load_canary_config(config_path: Path) -> tuple[Any, bool, str]:
    """Enable delivery only in this process and rerun the full config guard."""

    from config import _validate_config, load_config

    persisted = load_config(config_path)
    persisted_enabled = bool(getattr(persisted, "completed_delivery_enabled", False))
    persisted_source_policy = str(
        getattr(persisted, "completed_delivery_source_policy", "retain") or "retain"
    )
    canary = replace(
        persisted,
        completed_delivery_enabled=True,
        completed_delivery_source_policy="retain",
    )
    _validate_config(canary)
    return canary, persisted_enabled, persisted_source_policy


def inspect_completed_delivery_canary(video: str | Path, config: Any) -> dict[str, Any]:
    """Read-only proof that one exact terminal queue item is safe to deliver."""

    source = Path(video).resolve()
    if not source.is_file():
        raise CompletedDeliveryError(f"source media is unavailable: {source}")
    queue = _terminal_queue_evidence(source, config)
    publication = _strict_publication(source, config)
    destination = completed_delivery_destination(source, config)
    receipt = completed_delivery_receipt_path(source, config)
    marker = completed_delivery_marker_path(source, config)
    manifest = output_manifest_path(source, config).resolve()

    already_committed = False
    if receipt.exists() or destination.exists():
        already_committed = validate_completed_delivery(source, config, verify_streams=True)
        if receipt.exists() and not already_committed:
            raise CompletedDeliveryError(
                f"existing completed-delivery receipt failed strict revalidation: {receipt}"
            )
        if destination.exists() and not already_committed and not marker.exists():
            raise CompletedDeliveryCollisionError(
                f"completed destination exists without a matching receipt or owned recovery marker: {destination}"
            )

    recovery_pending = marker.exists() and not already_committed
    return {
        "mode": "completed-delivery-canary-preview",
        "readonly": True,
        "write_performed": False,
        "target_count": 1,
        "target": str(source),
        "queue": queue,
        "strict_publication": {
            "manifest": str(manifest),
            "manifest_sha256": sha256_file(manifest),
            "languages": sorted(
                str(track.get("language") or "") for track in publication.get("tracks", [])
            ),
        },
        "destination": str(destination),
        "receipt": str(receipt),
        "recovery_marker": str(marker),
        "destination_exists": destination.exists(),
        "receipt_exists": receipt.exists(),
        "recovery_pending": recovery_pending,
        "already_committed": already_committed,
        "source_retained": True,
        "ready": not recovery_pending,
        "reason_code": "recovery_pending" if recovery_pending else "",
    }


def commit_completed_delivery_canary(video: str | Path, config: Any) -> dict[str, Any]:
    preview = inspect_completed_delivery_canary(video, config)
    if preview.get("ready") is not True:
        reason = str(preview.get("reason_code") or "preflight_not_ready")
        raise CompletedDeliveryError(
            f"completed-delivery canary preflight is not ready: reason={reason}"
        )
    source = Path(video).resolve()
    result = deliver_completed_mkv(source, config)
    if not validate_completed_delivery(source, config, verify_streams=True):
        raise CompletedDeliveryError(
            "completed-delivery canary commit failed full receipt and stream revalidation"
        )
    return {
        "mode": "completed-delivery-canary-commit",
        "readonly": False,
        "write_performed": True,
        "target_count": 1,
        "target": preview["target"],
        "queue": preview["queue"],
        "strict_publication": preview["strict_publication"],
        "source_retained": True,
        "result": result.to_dict(),
    }


def _terminal_queue_evidence(source: Path, config: Any) -> dict[str, Any]:
    from acceptance.planner import read_queue_candidates

    matches = []
    for row in read_queue_candidates(config):
        try:
            if Path(row.path).resolve() == source:
                matches.append(row)
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        raise CompletedDeliveryError(
            f"exactly one scanner queue row is required for canary target; found={len(matches)}"
        )
    row = matches[0]
    status = str(row.status or "").strip().casefold()
    if status not in TERMINAL_QUEUE_STATUSES:
        raise CompletedDeliveryError(f"scanner queue row is not terminal: status={status or 'missing'}")
    stat = source.stat()
    if int(row.mtime_ns or 0) <= 0 or int(row.mtime_ns) != int(stat.st_mtime_ns):
        raise CompletedDeliveryError("scanner queue media identity does not match the current source mtime")
    return {
        "status": status,
        "source": str(row.source or ""),
        "mtime_ns": int(row.mtime_ns),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.video) != 1:
        sys.stderr.write("completed-delivery canary requires exactly one --video\n")
        return 2
    try:
        config, persisted_enabled, persisted_source_policy = load_canary_config(args.config)
        if args.commit:
            payload = commit_completed_delivery_canary(args.video[0], config)
        else:
            payload = inspect_completed_delivery_canary(args.video[0], config)
        payload["persistent_completed_delivery_enabled"] = persisted_enabled
        payload["persistent_completed_delivery_source_policy"] = persisted_source_policy
        payload["process_local_enable_only"] = not persisted_enabled
        payload["process_local_source_policy"] = "retain"
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 0 if payload.get("ready", True) is True else 2
    except (CompletedDeliveryError, OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"completed-delivery canary refused: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
