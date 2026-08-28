from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Callable

from completed_delivery import (
    CompletedDeliveryError,
    completed_delivery_destination,
    completed_delivery_receipt_path,
)
from output_manifest import (
    delivery_identity,
    manifest_publication_semantics,
    output_manifest_path,
    publication_is_traditional_chinese_delivery,
    validate_output_manifest,
)
from processing_provenance import load_provenance
from safe_files import sha256_file
from series_metadata import canonical_local_path, series_root_for_video, stable_series_id

from acceptance.harness import (
    ACCEPTANCE_CONTRACT,
    DURATION_BUCKETS,
    MINIMUM_CASES_PER_CONTAINER,
    MINIMUM_CASES_PER_DURATION_BUCKET,
    MINIMUM_CASES_PER_ROUTE,
    MINIMUM_DISTINCT_CONTAINERS,
    MINIMUM_DISTINCT_SERIES,
    PLAN_SCHEMA_VERSION,
    REQUIRED_CASE_COUNT,
    ROUTES,
    AcceptanceInputError,
    _provenance_asr_used,
    _provenance_route,
    duration_bucket,
    read_json_object,
    validate_plan_structure,
)


TERMINAL_QUEUE_STATUSES = frozenset({"done", "completed", "succeeded"})
ROUTE_ORDER = (
    "existing_zh_tw",
    "zh_cn_opencc",
    "japanese_subtitle_translation",
    "japanese_audio_asr",
)
DURATION_BUCKET_ORDER = ("short", "standard", "long")
FAULT_ORDER = (
    "worker_kill",
    "translation_timeout",
    "asr_process_crash",
    "gpu_oom",
    "model_unavailable",
    "output_publish_interrupt",
    "temporary_io_error",
    "temporary_database_busy",
    "mux_process_crash",
    "completed_publish_interrupt",
)
FAULT_TRIGGERS = {
    "worker_kill": "planned-only: after queue claim and before transcription",
    "translation_timeout": "planned-only: during the first translation request",
    "asr_process_crash": "planned-only: after ASR process start and before checkpoint commit",
    "gpu_oom": "planned-only: during GPU model inference",
    "model_unavailable": "planned-only: before the first model request",
    "output_publish_interrupt": "planned-only: after publication marker creation and before manifest commit",
    "temporary_io_error": "planned-only: during a checkpoint-safe output write",
    "temporary_database_busy": "planned-only: during scanner-ledger attempt persistence",
    "mux_process_crash": "planned-only: during completed-delivery mux",
    "completed_publish_interrupt": "planned-only: after completed marker creation and before receipt commit",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")


class CorpusCandidateError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = str(code)
        self.detail = str(detail or code)


@dataclass(frozen=True)
class QueueCandidate:
    path: str
    mtime_ns: int
    status: str
    source: str


@dataclass(frozen=True)
class CorpusCandidate:
    path: Path
    queue_source: str
    identity: dict[str, Any]
    expected_route: str
    series_id: str
    container: str
    duration_bucket: str
    audio_layout: str
    subtitle_layout: str
    release_profile: str


def prepare_corpus_plan(
    config: Any,
    *,
    queue_reader: Callable[[Any], list[QueueCandidate]] = None,
    media_probe: Callable[[Path], dict[str, Any]] = None,
    route_resolver: Callable[[Path, Any, dict[str, Any]], str] = None,
    source_hasher: Callable[[Path], str] = sha256_file,
) -> dict[str, Any]:
    """Prepare, but never execute, a deterministic fixed acceptance corpus.

    The queue and all evidence are read-only.  A plan is returned only when an
    exact 100-case schema-v2 corpus can be built without guessing any route.
    """

    queue_reader = queue_reader or read_queue_candidates
    media_probe = media_probe or probe_corpus_media
    route_resolver = route_resolver or resolve_expected_route
    rejections: Counter[str] = Counter()
    unresolved_routes: list[dict[str, str]] = []
    gaps = _configuration_gaps(config)

    try:
        rows = queue_reader(config)
    except (AcceptanceInputError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        gaps.append({"code": "queue_read_failed", "detail": str(exc)})
        return _preview_payload(
            ready=False,
            gaps=gaps,
            candidates=[],
            rejections=rejections,
            unresolved_routes=unresolved_routes,
        )

    candidates = _build_candidates(
        rows,
        config,
        media_probe=media_probe,
        route_resolver=route_resolver,
        rejections=rejections,
        unresolved_routes=unresolved_routes,
    )
    coverage = corpus_coverage(candidates)
    gaps.extend(coverage_gaps(coverage))
    if gaps:
        return _preview_payload(
            ready=False,
            gaps=gaps,
            candidates=candidates,
            rejections=rejections,
            unresolved_routes=unresolved_routes,
        )

    remaining = list(candidates)
    hash_cache: dict[str, str] = {}
    while True:
        selected = select_corpus(remaining)
        if len(selected) != REQUIRED_CASE_COUNT:
            gaps = coverage_gaps(corpus_coverage(remaining))
            if not gaps:
                gaps = [{"code": "exact_selection_failed", "found": len(selected)}]
            return _preview_payload(
                ready=False,
                gaps=gaps,
                candidates=remaining,
                rejections=rejections,
                unresolved_routes=unresolved_routes,
            )
        try:
            plan = _materialize_plan(
                selected,
                config,
                source_hasher=source_hasher,
                hash_cache=hash_cache,
            )
            break
        except CorpusCandidateError as exc:
            failed_path = exc.detail
            rejections[exc.code] += 1
            remaining = [item for item in remaining if str(item.path) != failed_path]
            if len(remaining) < REQUIRED_CASE_COUNT:
                gaps = coverage_gaps(corpus_coverage(remaining))
                if not gaps:
                    gaps = [{"code": "candidate_materialization_failed", "detail": exc.detail}]
                return _preview_payload(
                    ready=False,
                    gaps=gaps,
                    candidates=remaining,
                    rejections=rejections,
                    unresolved_routes=unresolved_routes,
                )

    structure_errors = validate_plan_structure(plan)
    if structure_errors:
        return _preview_payload(
            ready=False,
            gaps=[{"code": "generated_plan_invalid", "errors": structure_errors}],
            candidates=candidates,
            rejections=rejections,
            unresolved_routes=unresolved_routes,
        )
    return _preview_payload(
        ready=True,
        gaps=[],
        candidates=candidates,
        rejections=rejections,
        unresolved_routes=unresolved_routes,
        selected=selected,
        plan=plan,
    )


def read_queue_candidates(config: Any) -> list[QueueCandidate]:
    database = _configured_scanner_database(config)
    if not database.is_file():
        raise AcceptanceInputError(f"scanner queue database is missing: {database}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(ai_candidate_queue)").fetchall()
        }
        required = {"path", "mtime_ns", "status", "source"}
        missing = sorted(required - columns)
        if missing:
            raise AcceptanceInputError(
                f"ai_candidate_queue lacks required read-only fields: {missing}"
            )
        rows = connection.execute(
            """
            SELECT path, mtime_ns, status, source
            FROM ai_candidate_queue
            ORDER BY path COLLATE NOCASE, path
            """
        ).fetchall()
        return [
            QueueCandidate(
                path=str(row["path"] or ""),
                mtime_ns=int(row["mtime_ns"] or 0),
                status=str(row["status"] or ""),
                source=str(row["source"] or ""),
            )
            for row in rows
        ]
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        if isinstance(exc, AcceptanceInputError):
            raise
        raise AcceptanceInputError(f"cannot read scanner queue in mode=ro: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def resolve_expected_route(path: Path, config: Any, identity: dict[str, Any]) -> str:
    """Infer a route only from strict current-attempt publication evidence."""

    media = identity.get("media") if isinstance(identity, dict) else None
    if not isinstance(media, dict):
        raise CorpusCandidateError("expected_route_unproven", str(path))
    obligation_id = str(identity.get("obligation_id") or "")
    policy_revision = str(identity.get("policy_revision") or "")
    if not validate_output_manifest(
        path,
        config,
        verify_hashes=True,
        require_delivery_evidence=True,
        expected_obligation_id=obligation_id,
        expected_policy_revision=policy_revision,
        require_publication_semantics=True,
    ):
        raise CorpusCandidateError("strict_manifest_unavailable", str(path))
    try:
        manifest = read_json_object(output_manifest_path(path, config))
    except AcceptanceInputError as exc:
        raise CorpusCandidateError("strict_manifest_unavailable", str(path)) from exc
    publication = manifest_publication_semantics(manifest)
    if publication is None or not publication_is_traditional_chinese_delivery(publication):
        raise CorpusCandidateError("traditional_chinese_publication_unproven", str(path))

    provenance = load_provenance(config, path)
    if not isinstance(provenance, dict):
        raise CorpusCandidateError("expected_route_unproven", str(path))
    if provenance.get("schema_version") != 1:
        raise CorpusCandidateError("provenance_schema_invalid", str(path))
    if str(provenance.get("video_path") or "") != str(path):
        raise CorpusCandidateError("provenance_media_mismatch", str(path))
    if str(provenance.get("config_signature") or "") != policy_revision:
        raise CorpusCandidateError("provenance_policy_mismatch", str(path))
    if provenance.get("status") != "complete":
        raise CorpusCandidateError("provenance_incomplete", str(path))

    route = _provenance_route(provenance, manifest)
    asr_used = _provenance_asr_used(provenance, manifest)
    if route not in ROUTES:
        raise CorpusCandidateError("expected_route_unproven", str(path))
    if route == "japanese_audio_asr" and asr_used is not True:
        raise CorpusCandidateError("expected_route_unproven", str(path))
    if route != "japanese_audio_asr" and asr_used is not False:
        raise CorpusCandidateError("expected_route_unproven", str(path))
    return route


def probe_corpus_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,format_name:"
            "stream=index,codec_type,codec_name,channels,channel_layout:"
            "stream_tags=language:stream_disposition=default,forced"
        ),
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CorpusCandidateError("media_probe_failed", str(path)) from exc
    if completed.returncode != 0:
        raise CorpusCandidateError("media_probe_failed", str(path))
    try:
        payload = json.loads(completed.stdout or "{}")
        format_payload = payload.get("format") or {}
        duration = float(format_payload.get("duration") or 0)
        formats = sorted(
            {
                item.strip().casefold()
                for item in str(format_payload.get("format_name") or "").split(",")
                if item.strip()
            }
        )
        streams = [item for item in (payload.get("streams") or []) if isinstance(item, dict)]
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CorpusCandidateError("media_probe_failed", str(path)) from exc
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    subtitle_streams = [item for item in streams if item.get("codec_type") == "subtitle"]
    if not math.isfinite(duration) or duration <= 0 or not formats or not video_streams or not audio_streams:
        raise CorpusCandidateError("media_probe_failed", str(path))
    return {
        "duration_seconds": duration,
        "format_names": formats,
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
        "audio_layout": _stream_layout(audio_streams, audio=True),
        "subtitle_layout": _stream_layout(subtitle_streams, audio=False),
    }


def corpus_coverage(candidates: list[CorpusCandidate]) -> dict[str, Any]:
    routes = Counter(item.expected_route for item in candidates)
    containers = Counter(item.container for item in candidates)
    buckets = Counter(item.duration_bucket for item in candidates)
    return {
        "candidate_count": len(candidates),
        "routes": {route: routes.get(route, 0) for route in ROUTE_ORDER},
        "containers": dict(sorted(containers.items())),
        "duration_buckets": {
            bucket: buckets.get(bucket, 0) for bucket in DURATION_BUCKET_ORDER
        },
        "distinct_series": len({item.series_id for item in candidates}),
    }


def coverage_gaps(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    count = int(coverage.get("candidate_count") or 0)
    if count < REQUIRED_CASE_COUNT:
        gaps.append(
            {"code": "candidate_count", "required": REQUIRED_CASE_COUNT, "found": count}
        )
    route_counts = coverage.get("routes") or {}
    for route in ROUTE_ORDER:
        found = int(route_counts.get(route) or 0)
        if found < MINIMUM_CASES_PER_ROUTE:
            gaps.append(
                {
                    "code": "route_count",
                    "route": route,
                    "required": MINIMUM_CASES_PER_ROUTE,
                    "found": found,
                }
            )
    bucket_counts = coverage.get("duration_buckets") or {}
    for bucket in DURATION_BUCKET_ORDER:
        found = int(bucket_counts.get(bucket) or 0)
        if found < MINIMUM_CASES_PER_DURATION_BUCKET:
            gaps.append(
                {
                    "code": "duration_bucket_count",
                    "bucket": bucket,
                    "required": MINIMUM_CASES_PER_DURATION_BUCKET,
                    "found": found,
                }
            )
    substantial = {
        str(container): int(found)
        for container, found in (coverage.get("containers") or {}).items()
        if int(found) >= MINIMUM_CASES_PER_CONTAINER
    }
    if len(substantial) < MINIMUM_DISTINCT_CONTAINERS:
        gaps.append(
            {
                "code": "container_coverage",
                "required_distinct": MINIMUM_DISTINCT_CONTAINERS,
                "minimum_each": MINIMUM_CASES_PER_CONTAINER,
                "eligible": substantial,
            }
        )
    distinct_series = int(coverage.get("distinct_series") or 0)
    if distinct_series < MINIMUM_DISTINCT_SERIES:
        gaps.append(
            {
                "code": "series_coverage",
                "required": MINIMUM_DISTINCT_SERIES,
                "found": distinct_series,
            }
        )
    return gaps


def select_corpus(candidates: list[CorpusCandidate]) -> list[CorpusCandidate]:
    ordered = sorted(candidates, key=_candidate_sort_key)
    selected: list[CorpusCandidate] = []
    selected_paths: set[str] = set()

    def add_until(predicate: Callable[[CorpusCandidate], bool], target: int) -> None:
        current = sum(1 for item in selected if predicate(item))
        if current >= target:
            return
        for item in ordered:
            key = str(item.path)
            if key in selected_paths or not predicate(item):
                continue
            selected.append(item)
            selected_paths.add(key)
            current += 1
            if current >= target:
                return

    for route in ROUTE_ORDER:
        add_until(lambda item, route=route: item.expected_route == route, MINIMUM_CASES_PER_ROUTE)
    for bucket in DURATION_BUCKET_ORDER:
        add_until(
            lambda item, bucket=bucket: item.duration_bucket == bucket,
            MINIMUM_CASES_PER_DURATION_BUCKET,
        )
    container_counts = Counter(item.container for item in ordered)
    target_containers = [
        container
        for container, count in sorted(
            container_counts.items(), key=lambda item: (-item[1], item[0])
        )
        if count >= MINIMUM_CASES_PER_CONTAINER
    ][:MINIMUM_DISTINCT_CONTAINERS]
    for container in target_containers:
        add_until(
            lambda item, container=container: item.container == container,
            MINIMUM_CASES_PER_CONTAINER,
        )
    seen_series = {item.series_id for item in selected}
    for item in ordered:
        if len(seen_series) >= MINIMUM_DISTINCT_SERIES:
            break
        key = str(item.path)
        if key in selected_paths or item.series_id in seen_series:
            continue
        selected.append(item)
        selected_paths.add(key)
        seen_series.add(item.series_id)
    for item in ordered:
        if len(selected) >= REQUIRED_CASE_COUNT:
            break
        key = str(item.path)
        if key not in selected_paths:
            selected.append(item)
            selected_paths.add(key)
    return selected[:REQUIRED_CASE_COUNT]


def _build_candidates(
    rows: list[QueueCandidate],
    config: Any,
    *,
    media_probe: Callable[[Path], dict[str, Any]],
    route_resolver: Callable[[Path, Any, dict[str, Any]], str],
    rejections: Counter[str],
    unresolved_routes: list[dict[str, str]],
) -> list[CorpusCandidate]:
    grouped: defaultdict[str, list[QueueCandidate]] = defaultdict(list)
    for row in rows:
        grouped[_path_key(row.path)].append(row)
    candidates: list[CorpusCandidate] = []
    for key in sorted(grouped):
        group = grouped[key]
        if len(group) != 1:
            rejections["duplicate_queue_path"] += len(group)
            continue
        row = group[0]
        try:
            candidate = _build_candidate(
                row,
                config,
                media_probe=media_probe,
                route_resolver=route_resolver,
            )
        except CorpusCandidateError as exc:
            rejections[exc.code] += 1
            if exc.code in {
                "expected_route_unproven",
                "strict_manifest_unavailable",
                "traditional_chinese_publication_unproven",
                "provenance_schema_invalid",
                "provenance_media_mismatch",
                "provenance_policy_mismatch",
                "provenance_incomplete",
            } and len(unresolved_routes) < 20:
                unresolved_routes.append({"path": row.path, "reason": exc.code})
            continue
        candidates.append(candidate)
    return sorted(candidates, key=_candidate_sort_key)


def _build_candidate(
    row: QueueCandidate,
    config: Any,
    *,
    media_probe: Callable[[Path], dict[str, Any]],
    route_resolver: Callable[[Path, Any, dict[str, Any]], str],
) -> CorpusCandidate:
    if row.status.strip().casefold() not in TERMINAL_QUEUE_STATUSES:
        raise CorpusCandidateError("queue_not_terminal", row.path)
    source = _clean_label(row.source)
    if not source:
        raise CorpusCandidateError("queue_source_missing", row.path)
    raw_path = Path(row.path)
    if not raw_path.is_absolute():
        raise CorpusCandidateError("queue_path_not_absolute", row.path)
    try:
        path = raw_path.resolve(strict=True)
        if not path.is_file():
            raise CorpusCandidateError("media_missing", row.path)
        stat = path.stat()
    except OSError as exc:
        raise CorpusCandidateError("media_missing", row.path) from exc
    if row.mtime_ns <= 0 or int(stat.st_mtime_ns) != int(row.mtime_ns):
        raise CorpusCandidateError("queue_media_mtime_mismatch", row.path)
    try:
        identity = delivery_identity(path, config)
    except (OSError, TypeError, ValueError) as exc:
        raise CorpusCandidateError("media_identity_failed", row.path) from exc
    try:
        route = route_resolver(path, config, identity)
    except CorpusCandidateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise CorpusCandidateError("expected_route_unproven", row.path) from exc
    if route not in ROUTES:
        raise CorpusCandidateError("expected_route_unproven", row.path)
    try:
        probe = media_probe(path)
        duration = float(probe.get("duration_seconds") or 0)
        formats = {
            str(item).strip().casefold()
            for item in (probe.get("format_names") or [])
            if str(item).strip()
        }
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration")
        if int(probe.get("video_streams") or 0) < 1 or int(probe.get("audio_streams") or 0) < 1:
            raise ValueError("streams")
        container = _select_container(path, formats)
        audio_layout = str(probe.get("audio_layout") or "").strip()
        subtitle_layout = str(probe.get("subtitle_layout") or "").strip()
        if not audio_layout or not subtitle_layout:
            raise ValueError("layout")
    except CorpusCandidateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise CorpusCandidateError("media_probe_failed", row.path) from exc
    final_stat = path.stat()
    if (
        int(final_stat.st_size) != int(stat.st_size)
        or int(final_stat.st_mtime_ns) != int(stat.st_mtime_ns)
    ):
        raise CorpusCandidateError("media_changed_during_probe", row.path)
    series_root = series_root_for_video(path)
    series_id = stable_series_id(canonical_local_path(series_root))
    return CorpusCandidate(
        path=path,
        queue_source=row.source,
        identity=identity,
        expected_route=route,
        series_id=series_id,
        container=container,
        duration_bucket=duration_bucket(duration),
        audio_layout=audio_layout,
        subtitle_layout=subtitle_layout,
        release_profile=f"queue-source:{source}",
    )


def _materialize_plan(
    selected: list[CorpusCandidate],
    config: Any,
    *,
    source_hasher: Callable[[Path], str],
    hash_cache: dict[str, str],
) -> dict[str, Any]:
    materialized: list[tuple[CorpusCandidate, str]] = []
    for candidate in selected:
        path_key = str(candidate.path)
        before = candidate.path.stat()
        try:
            digest = hash_cache.get(path_key) or str(source_hasher(candidate.path))
        except (OSError, TypeError, ValueError) as exc:
            raise CorpusCandidateError("source_sha256_failed", path_key) from exc
        after = candidate.path.stat()
        if not _HEX64.fullmatch(digest):
            raise CorpusCandidateError("source_sha256_failed", path_key)
        if (
            int(before.st_size) != int(after.st_size)
            or int(before.st_mtime_ns) != int(after.st_mtime_ns)
            or int(after.st_size) != int(candidate.identity["media"]["media_size"])
            or int(after.st_mtime_ns) != int(candidate.identity["media"]["media_mtime_ns"])
        ):
            raise CorpusCandidateError("media_changed_during_hash", path_key)
        hash_cache[path_key] = digest
        materialized.append((candidate, digest))

    suite_seed = [
        {
            "path": str(candidate.path),
            "fingerprint": candidate.identity["media"]["media_fingerprint"],
            "policy": candidate.identity["policy_revision"],
            "sha256": digest,
            "route": candidate.expected_route,
        }
        for candidate, digest in materialized
    ]
    suite_digest = hashlib.sha256(
        json.dumps(suite_seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    cases: list[dict[str, Any]] = []
    for index, (candidate, digest) in enumerate(materialized, start=1):
        try:
            destination = completed_delivery_destination(candidate.path, config)
            receipt = completed_delivery_receipt_path(candidate.path, config)
        except (CompletedDeliveryError, OSError, TypeError, ValueError) as exc:
            raise CorpusCandidateError("completed_delivery_path_failed", str(candidate.path)) from exc
        faults: list[dict[str, str]] = []
        if index <= len(FAULT_ORDER):
            scenario = FAULT_ORDER[index - 1]
            faults.append(
                {
                    "fault_id": f"fault-{index:02d}-{scenario}",
                    "scenario": scenario,
                    "trigger": FAULT_TRIGGERS[scenario],
                }
            )
        identity = candidate.identity
        cases.append(
            {
                "case_id": (
                    f"case-{index:03d}-{str(identity['media']['media_fingerprint'])[:12]}"
                ),
                "media": {
                    **identity["media"],
                    "policy_revision": identity["policy_revision"],
                    "obligation_id": identity["obligation_id"],
                },
                "expected_route": candidate.expected_route,
                "strata": {
                    "series_id": candidate.series_id,
                    "container": candidate.container,
                    "duration_bucket": candidate.duration_bucket,
                    "audio_layout": candidate.audio_layout,
                    "subtitle_layout": candidate.subtitle_layout,
                    "release_profile": candidate.release_profile,
                },
                "completed_delivery": {
                    "source_sha256": digest,
                    "receipt_path": str(receipt.resolve()),
                    "destination": str(destination.resolve()),
                },
                "faults": faults,
            }
        )
    created_at = max(
        int(case["media"]["media_mtime_ns"]) for case in cases
    ) / 1_000_000_000
    return {
        "contract": ACCEPTANCE_CONTRACT,
        "schema_version": PLAN_SCHEMA_VERSION,
        "suite_id": f"unattended-{suite_digest[:24]}",
        "created_at": created_at,
        "cases": cases,
    }


def _configuration_gaps(config: Any) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if getattr(config, "completed_delivery_enabled", False) is not True:
        gaps.append({"code": "completed_delivery_disabled"})
    if str(getattr(config, "completed_delivery_source_policy", "retain") or "").casefold() != "retain":
        gaps.append({"code": "completed_delivery_source_policy_not_retain"})
    if not str(getattr(config, "completed_delivery_path", "") or "").strip():
        gaps.append({"code": "completed_delivery_path_missing"})
    return gaps


def _preview_payload(
    *,
    ready: bool,
    gaps: list[dict[str, Any]],
    candidates: list[CorpusCandidate],
    rejections: Counter[str],
    unresolved_routes: list[dict[str, str]],
    selected: list[CorpusCandidate] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "prepare-plan",
        "preview": True,
        "readonly_sources": True,
        "worker_started": False,
        "database_mutated": False,
        "fault_execution_performed": False,
        "ready": bool(ready),
        "gaps": gaps,
        "candidate_coverage": corpus_coverage(candidates),
        "selected_coverage": corpus_coverage(selected or []),
        "rejections": dict(sorted(rejections.items())),
        "unresolved_expected_routes": unresolved_routes,
        "planned_fault_cases": len(FAULT_ORDER) if ready else 0,
        "planned_fault_scenarios": list(FAULT_ORDER) if ready else [],
    }
    if ready and plan is not None:
        payload["plan"] = plan
    return payload


def _configured_scanner_database(config: Any) -> Path:
    configured = Path(str(getattr(config, "scanner_state_path", "scanner_state.sqlite3")))
    if not configured.is_absolute():
        configured = Path(config.work_path) / configured
    return configured.resolve()


def _path_key(value: str) -> str:
    try:
        return str(Path(value).resolve()).casefold()
    except (OSError, ValueError):
        return str(value).casefold()


def _candidate_sort_key(candidate: CorpusCandidate) -> tuple[str, str]:
    return (str(candidate.path).casefold(), str(candidate.path))


def _select_container(path: Path, formats: set[str]) -> str:
    suffix_preferences = {
        ".mkv": ("matroska",),
        ".webm": ("webm", "matroska"),
        ".mp4": ("mov", "mp4"),
        ".m4v": ("mov", "mp4"),
        ".mov": ("mov",),
        ".ts": ("mpegts",),
        ".m2ts": ("mpegts",),
        ".avi": ("avi",),
    }
    for candidate in suffix_preferences.get(path.suffix.casefold(), ()):
        if candidate in formats:
            return candidate
    if not formats:
        raise CorpusCandidateError("media_probe_failed", str(path))
    return sorted(formats)[0]


def _stream_layout(streams: list[dict[str, Any]], *, audio: bool) -> str:
    if not streams:
        return "none"
    entries: list[str] = []
    for stream in streams:
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = (
            stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        )
        values = [
            _clean_label(stream.get("codec_name") or "unknown"),
            _clean_label(tags.get("language") or "und"),
        ]
        if audio:
            values.extend(
                [
                    str(int(stream.get("channels") or 0)),
                    _clean_label(stream.get("channel_layout") or "unknown"),
                ]
            )
        values.extend(
            [
                f"default={1 if disposition.get('default') else 0}",
                f"forced={1 if disposition.get('forced') else 0}",
            ]
        )
        entries.append(":".join(values))
    return "|".join(sorted(entries))


def _clean_label(value: Any) -> str:
    return " ".join(str(value or "").split())[:120]
