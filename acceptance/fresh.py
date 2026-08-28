from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable

from completed_delivery import (
    CompletedDeliveryError,
    completed_delivery_destination,
    completed_delivery_marker_path,
    completed_delivery_receipt_path,
)
from output_manifest import delivery_identity, output_manifest_path
from processing_provenance import provenance_path_for_video
from safe_files import sha256_file
from series_metadata import canonical_local_path, series_root_for_video, stable_series_id

from .harness import (
    ACCEPTANCE_CONTRACT,
    FRESH_PLAN_SCHEMA_VERSION,
    PRE_ADMISSION_CONTRACT,
    REQUIRED_CASE_COUNT,
    ROUTES,
    RUN_CLAIM_CONTRACT,
    AcceptanceInputError,
    duration_bucket,
    fresh_run_id,
    read_json_object,
    validate_plan_structure,
)
from .planner import (
    FAULT_ORDER,
    FAULT_TRIGGERS,
    CorpusCandidate,
    _assign_planned_faults,
    _configuration_gaps,
    _select_container,
    corpus_coverage,
    coverage_gaps,
    probe_corpus_media,
)


PRE_ADMISSION_CORPUS_CONTRACT = "anime-unattended-pre-admission-corpus-v1"
PRE_ADMISSION_CORPUS_SCHEMA_VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class FreshCandidate:
    case_id: str
    corpus: CorpusCandidate
    source_sha256: str


def prepare_fresh_plan(
    corpus_manifest_path: str | Path,
    config: Any,
    *,
    plan_output: str | Path | None = None,
    media_probe: Callable[[Path], dict[str, Any]] = probe_corpus_media,
    source_hasher: Callable[[Path], str] = sha256_file,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Build a fixed pre-admission plan without consulting completed outcomes."""

    corpus_path = Path(corpus_manifest_path).resolve()
    corpus = read_json_object(corpus_path)
    request_errors = validate_pre_admission_corpus(corpus)
    corpus_digest = sha256_file(corpus_path)
    nonce = str(corpus.get("nonce") or "")
    run_id = ""
    if _HEX64.fullmatch(nonce):
        run_id = fresh_run_id(nonce, corpus_digest)
    gaps: list[dict[str, Any]] = [
        {"code": "invalid_pre_admission_corpus", "detail": error}
        for error in request_errors
    ]
    gaps.extend(_configuration_gaps(config))
    if gaps:
        return _preview_payload(
            ready=False,
            run_id=run_id,
            corpus_path=corpus_path,
            corpus_digest=corpus_digest,
            candidates=[],
            gaps=gaps,
        )

    candidates: list[FreshCandidate] = []
    rejections: Counter[str] = Counter()
    source_owners: dict[str, str] = {}
    for request in corpus["cases"]:
        try:
            candidate = _build_fresh_candidate(
                request,
                config,
                media_probe=media_probe,
                source_hasher=source_hasher,
            )
        except (AcceptanceInputError, CompletedDeliveryError, OSError, TypeError, ValueError) as exc:
            rejections[str(exc) or "candidate_invalid"] += 1
            continue
        prior = source_owners.get(candidate.source_sha256)
        if prior is not None:
            rejections["duplicate_source_sha256"] += 1
            continue
        source_owners[candidate.source_sha256] = str(candidate.corpus.path)
        candidates.append(candidate)

    corpus_candidates = [item.corpus for item in candidates]
    if len(candidates) != REQUIRED_CASE_COUNT:
        gaps.append(
            {
                "code": "candidate_count",
                "required": REQUIRED_CASE_COUNT,
                "found": len(candidates),
            }
        )
    gaps.extend(coverage_gaps(corpus_coverage(corpus_candidates)))
    if gaps:
        return _preview_payload(
            ready=False,
            run_id=run_id,
            corpus_path=corpus_path,
            corpus_digest=corpus_digest,
            candidates=corpus_candidates,
            gaps=_deduplicate_gaps(gaps),
            rejections=rejections,
        )

    ordered = sorted(candidates, key=lambda item: (str(item.corpus.path).casefold(), str(item.corpus.path)))
    fault_assignments, missing_faults = _assign_planned_faults([item.corpus for item in ordered])
    if missing_faults:
        return _preview_payload(
            ready=False,
            run_id=run_id,
            corpus_path=corpus_path,
            corpus_digest=corpus_digest,
            candidates=corpus_candidates,
            gaps=[{"code": "fault_assignment_unavailable", "scenarios": missing_faults}],
            rejections=rejections,
        )

    baselines: dict[str, dict[str, Any]] = {}
    baseline_gaps: list[dict[str, Any]] = []
    for item in ordered:
        baseline, errors = _baseline_absence(item.corpus.path, item.corpus.identity, config)
        baseline["checked_at"] = _positive_clock(clock)
        baselines[str(item.corpus.path)] = baseline
        if errors:
            baseline_gaps.append(
                {
                    "code": "pre_admission_baseline_not_absent",
                    "case_id": item.case_id,
                    "path": str(item.corpus.path),
                    "reasons": errors,
                }
            )
    if baseline_gaps:
        return _preview_payload(
            ready=False,
            run_id=run_id,
            corpus_path=corpus_path,
            corpus_digest=corpus_digest,
            candidates=corpus_candidates,
            gaps=baseline_gaps,
            rejections=rejections,
        )

    baseline_checked_at = max(float(value["checked_at"]) for value in baselines.values())
    created_at = max(_positive_clock(clock), baseline_checked_at)
    claim_path = (
        fresh_run_claim_path(config, nonce)
        if plan_output is not None
        else None
    )
    plan = _materialize_fresh_plan(
        ordered,
        config,
        run_id=run_id,
        nonce=nonce,
        created_at=created_at,
        corpus_path=corpus_path,
        corpus_digest=corpus_digest,
        claim_path=claim_path,
        baselines=baselines,
        fault_assignments=fault_assignments,
    )
    structure_errors = validate_plan_structure(plan)
    if structure_errors:
        return _preview_payload(
            ready=False,
            run_id=run_id,
            corpus_path=corpus_path,
            corpus_digest=corpus_digest,
            candidates=corpus_candidates,
            gaps=[{"code": "generated_plan_invalid", "errors": structure_errors}],
            rejections=rejections,
        )
    payload = _preview_payload(
        ready=True,
        run_id=run_id,
        corpus_path=corpus_path,
        corpus_digest=corpus_digest,
        candidates=corpus_candidates,
        gaps=[],
        rejections=rejections,
    )
    payload["plan"] = plan
    payload["run_claim"] = fresh_run_claim_payload(plan)
    return payload


def validate_pre_admission_corpus(corpus: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = {"contract", "schema_version", "nonce", "cases"}
    unknown = sorted(str(key) for key in corpus if str(key) not in allowed)
    if unknown:
        errors.append(f"unknown top-level fields: {unknown}")
    if corpus.get("contract") != PRE_ADMISSION_CORPUS_CONTRACT:
        errors.append(f"contract must be {PRE_ADMISSION_CORPUS_CONTRACT}")
    if corpus.get("schema_version") != PRE_ADMISSION_CORPUS_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PRE_ADMISSION_CORPUS_SCHEMA_VERSION}")
    nonce = corpus.get("nonce")
    if not isinstance(nonce, str) or not _HEX64.fullmatch(nonce):
        errors.append("nonce must be a one-use lowercase SHA-256 value")
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        return [*errors, "cases must be a list"]
    if len(cases) != REQUIRED_CASE_COUNT:
        errors.append(f"cases must contain exactly {REQUIRED_CASE_COUNT} entries")
    case_ids: list[str] = []
    paths: list[str] = []
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        unknown_case = sorted(
            str(key)
            for key in case
            if str(key) not in {"case_id", "canonical_path", "expected_route", "release_profile"}
        )
        if unknown_case:
            errors.append(f"{prefix} has unknown fields: {unknown_case}")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not _SAFE_ID.fullmatch(case_id):
            errors.append(f"{prefix}.case_id must be a stable identifier")
        else:
            case_ids.append(case_id)
        path_value = case.get("canonical_path")
        if not isinstance(path_value, str) or not path_value or not Path(path_value).is_absolute():
            errors.append(f"{prefix}.canonical_path must be absolute")
        else:
            try:
                paths.append(str(Path(path_value).resolve()).casefold())
            except OSError:
                errors.append(f"{prefix}.canonical_path is invalid")
        if case.get("expected_route") not in ROUTES:
            errors.append(f"{prefix}.expected_route is invalid")
        if not isinstance(case.get("release_profile"), str) or not case["release_profile"].strip():
            errors.append(f"{prefix}.release_profile is required")
    if len(case_ids) != len(set(case_ids)):
        errors.append("case_id values must be unique")
    if len(paths) != len(set(paths)):
        errors.append("canonical_path values must be unique")
    return errors


def fresh_run_claim_path(config: Any, nonce: str) -> Path:
    """Use one registry per isolated Worker work root so a nonce cannot move paths."""

    return (Path(config.work_path).resolve() / "acceptance-run-claims" / f"{nonce}.json").resolve()


def fresh_run_claim_payload(plan: dict[str, Any]) -> dict[str, Any]:
    pre_admission = plan.get("pre_admission") or {}
    return {
        "contract": RUN_CLAIM_CONTRACT,
        "schema_version": 1,
        "run_id": plan.get("run_id"),
        "nonce": plan.get("nonce"),
        "corpus_manifest_sha256": pre_admission.get("corpus_manifest_sha256"),
        "created_at": plan.get("created_at"),
    }


def _build_fresh_candidate(
    request: dict[str, Any],
    config: Any,
    *,
    media_probe: Callable[[Path], dict[str, Any]],
    source_hasher: Callable[[Path], str],
) -> FreshCandidate:
    raw_path = Path(str(request["canonical_path"]))
    path = raw_path.resolve(strict=True)
    if not path.is_file():
        raise AcceptanceInputError("media_missing")
    before = path.stat()
    identity = delivery_identity(path, config)
    probe = media_probe(path)
    duration = float(probe.get("duration_seconds") or 0)
    formats = {
        str(value).strip().casefold()
        for value in (probe.get("format_names") or [])
        if str(value).strip()
    }
    if (
        not math.isfinite(duration)
        or duration <= 0
        or int(probe.get("video_streams") or 0) < 1
        or int(probe.get("audio_streams") or 0) < 1
    ):
        raise AcceptanceInputError("media_probe_failed")
    audio_layout = str(probe.get("audio_layout") or "").strip()
    subtitle_layout = str(probe.get("subtitle_layout") or "").strip()
    if not audio_layout or not subtitle_layout:
        raise AcceptanceInputError("media_layout_missing")
    digest = str(source_hasher(path))
    if not _HEX64.fullmatch(digest):
        raise AcceptanceInputError("source_sha256_failed")
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise AcceptanceInputError("media_changed_during_pre_admission")
    series_root = series_root_for_video(path)
    candidate = CorpusCandidate(
        path=path,
        queue_source="pre-admission",
        identity=identity,
        expected_route=str(request["expected_route"]),
        series_id=stable_series_id(canonical_local_path(series_root)),
        container=_select_container(path, formats),
        duration_bucket=duration_bucket(duration),
        audio_layout=audio_layout,
        subtitle_layout=subtitle_layout,
        release_profile=str(request["release_profile"]).strip(),
    )
    return FreshCandidate(
        case_id=str(request["case_id"]),
        corpus=candidate,
        source_sha256=digest,
    )


def _baseline_absence(
    video: Path,
    identity: dict[str, Any],
    config: Any,
) -> tuple[dict[str, Any], list[str]]:
    destination = completed_delivery_destination(video, config)
    receipt = completed_delivery_receipt_path(video, config)
    marker = completed_delivery_marker_path(video, config)
    manifest = output_manifest_path(video, config)
    provenance = provenance_path_for_video(config, video)
    partial_digest = hashlib.sha256(destination.name.encode()).hexdigest()[:16]
    partials = list(destination.parent.glob(f".muxing-{partial_digest}-*.mkv")) if destination.parent.exists() else []
    queue_absent, obligation_absent, database_errors = _scanner_baseline_absence(
        config,
        video=video,
        obligation_id=str(identity.get("obligation_id") or ""),
    )
    baseline = {
        "queue_row_absent": queue_absent,
        "obligation_absent": obligation_absent,
        "output_manifest_absent": not manifest.exists(),
        "processing_provenance_absent": not provenance.exists(),
        "completed_receipt_absent": not receipt.exists(),
        "completed_destination_absent": not destination.exists(),
        "completed_marker_absent": not marker.exists(),
        "completed_partial_absent": not any(path.is_file() for path in partials),
    }
    errors = list(database_errors)
    errors.extend(name for name, absent in baseline.items() if absent is not True)
    return baseline, errors


def _scanner_baseline_absence(
    config: Any,
    *,
    video: Path,
    obligation_id: str,
) -> tuple[bool, bool, list[str]]:
    configured = Path(str(getattr(config, "scanner_state_path", "scanner_state.sqlite3")))
    database = configured if configured.is_absolute() else Path(config.work_path) / configured
    if not database.is_file():
        return True, True, []
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        queue_present = False
        obligation_present = False
        if "ai_candidate_queue" in tables:
            queue_present = connection.execute(
                "SELECT 1 FROM ai_candidate_queue WHERE path=? LIMIT 1",
                (str(video),),
            ).fetchone() is not None
        if "ai_delivery_obligations" in tables:
            obligation_present = connection.execute(
                "SELECT 1 FROM ai_delivery_obligations WHERE obligation_id=? LIMIT 1",
                (obligation_id,),
            ).fetchone() is not None
        return not queue_present, not obligation_present, []
    except sqlite3.Error as exc:
        return False, False, [f"scanner_baseline_query_failed:{exc}"]
    finally:
        if connection is not None:
            connection.close()


def _materialize_fresh_plan(
    candidates: list[FreshCandidate],
    config: Any,
    *,
    run_id: str,
    nonce: str,
    created_at: float,
    corpus_path: Path,
    corpus_digest: str,
    claim_path: Path | None,
    baselines: dict[str, dict[str, Any]],
    fault_assignments: dict[str, str],
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(candidates, start=1):
        candidate = item.corpus
        destination = completed_delivery_destination(candidate.path, config)
        receipt = completed_delivery_receipt_path(candidate.path, config)
        scenario = fault_assignments.get(str(candidate.path))
        faults = []
        if scenario is not None:
            faults.append(
                {
                    "fault_id": f"fault-{index:03d}-{scenario}",
                    "scenario": scenario,
                    "trigger": FAULT_TRIGGERS[scenario],
                }
            )
        cases.append(
            {
                "case_id": item.case_id,
                "media": {
                    **candidate.identity["media"],
                    "policy_revision": candidate.identity["policy_revision"],
                    "obligation_id": candidate.identity["obligation_id"],
                    "source_sha256": item.source_sha256,
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
                    "source_sha256": item.source_sha256,
                    "receipt_path": str(receipt.resolve()),
                    "destination": str(destination.resolve()),
                },
                "pre_admission": baselines[str(candidate.path)],
                "faults": faults,
            }
        )
    return {
        "contract": ACCEPTANCE_CONTRACT,
        "schema_version": FRESH_PLAN_SCHEMA_VERSION,
        "suite_id": run_id,
        "run_id": run_id,
        "nonce": nonce,
        "created_at": created_at,
        "pre_admission": {
            "contract": PRE_ADMISSION_CONTRACT,
            "corpus_manifest_path": str(corpus_path),
            "corpus_manifest_sha256": corpus_digest,
            "baseline_checked_at": max(
                float(case["pre_admission"]["checked_at"]) for case in cases
            ),
            "run_claim_path": str(claim_path.resolve()) if claim_path is not None else "",
        },
        "cases": cases,
    }


def _preview_payload(
    *,
    ready: bool,
    run_id: str,
    corpus_path: Path,
    corpus_digest: str,
    candidates: list[CorpusCandidate],
    gaps: list[dict[str, Any]],
    rejections: Counter[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "prepare-fresh-plan",
        "preview": True,
        "readonly_sources": True,
        "worker_started": False,
        "database_mutated": False,
        "fault_execution_performed": False,
        "ready": bool(ready),
        "run_id": run_id,
        "corpus_manifest_path": str(corpus_path),
        "corpus_manifest_sha256": corpus_digest,
        "candidate_coverage": corpus_coverage(candidates),
        "gaps": gaps,
        "rejections": dict(sorted((rejections or Counter()).items())),
        "planned_fault_cases": len(FAULT_ORDER) if ready else 0,
        "planned_fault_scenarios": list(FAULT_ORDER) if ready else [],
    }


def _positive_clock(clock: Callable[[], float]) -> float:
    value = float(clock())
    if not math.isfinite(value) or value <= 0:
        raise AcceptanceInputError("clock returned an invalid UTC timestamp")
    return value


def _deduplicate_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for gap in gaps:
        key = repr(sorted(gap.items()))
        if key not in seen:
            seen.add(key)
            result.append(gap)
    return result
