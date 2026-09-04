from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from source_analyzer import (
    AnalyzerThresholds,
    AudioCandidateInput,
    SourceDecision,
    SubtitleCandidateInput,
    analyze_sources,
    canonical_json_sha256,
)


SOURCE_ANALYSIS_SERVICE_VERSION = "m2-source-analysis-service-v1"


class SourceAnalysisServiceError(RuntimeError):
    pass


class SourceAnalysisAttemptError(SourceAnalysisServiceError):
    pass


class SourceAnalysisContractError(SourceAnalysisServiceError):
    pass


@runtime_checkable
class SourceDecisionStore(Protocol):
    def reusable_source_decision(
        self,
        job_id: str,
        *,
        expected_identity: Mapping[str, Any],
        expected_media_revision: str,
        expected_source_fingerprint: str,
        expected_analyzer_version: str,
        expected_decision_schema_version: str | int,
        expected_decision_version: str,
        expected_config_fingerprint: str,
        expected_candidate_fingerprint: str,
        with_reason: bool = False,
    ) -> Any: ...

    def persist_source_decision(
        self,
        job_id: str,
        *,
        stage_attempt_id: str,
        decision: Mapping[str, Any],
        input_identity: Mapping[str, Any],
        media_revision: str,
        source_fingerprint: str,
        analyzer_version: str,
        decision_schema_version: str | int,
        decision_version: str,
        config_fingerprint: str,
        candidate_fingerprint: str,
        idempotency_key: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]: ...

    def list_stage_attempts(
        self,
        job_id: str,
        stage: str | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SourceAnalysisContext:
    """Authoritative context that controls exact checkpoint reuse."""

    job_id: str
    stage_attempt_id: str
    input_identity: Mapping[str, Any]
    media_revision: str
    source_fingerprint: str
    config_fingerprint: str
    cheap_candidate_fingerprint: str
    analyzer_version: str
    decision_schema_version: str | int
    decision_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "stage_attempt_id",
            "analyzer_version",
            "decision_version",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if isinstance(self.decision_schema_version, bool) or not str(
            self.decision_schema_version
        ).strip():
            raise ValueError("decision_schema_version must not be empty")
        if not isinstance(self.input_identity, Mapping) or not self.input_identity:
            raise ValueError("input_identity must be a non-empty mapping")
        frozen_identity = _freeze_json(self.input_identity)
        if not isinstance(frozen_identity, Mapping):
            raise ValueError("input_identity must be a JSON mapping")
        object.__setattr__(self, "input_identity", frozen_identity)
        for field_name in (
            "media_revision",
            "source_fingerprint",
            "config_fingerprint",
            "cheap_candidate_fingerprint",
        ):
            value = str(getattr(self, field_name) or "").strip().casefold()
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{field_name} must be a SHA-256 hex digest")
            object.__setattr__(self, field_name, value)

    @property
    def candidate_fingerprint(self) -> str:
        return self.cheap_candidate_fingerprint

    def persistence_kwargs(self) -> dict[str, Any]:
        return {
            "input_identity": _thaw_json(self.input_identity),
            "media_revision": self.media_revision,
            "source_fingerprint": self.source_fingerprint,
            "analyzer_version": self.analyzer_version,
            "decision_schema_version": self.decision_schema_version,
            "decision_version": self.decision_version,
            "config_fingerprint": self.config_fingerprint,
            "candidate_fingerprint": self.cheap_candidate_fingerprint,
        }

    def reuse_kwargs(self) -> dict[str, Any]:
        context = self.persistence_kwargs()
        return {
            "expected_identity": context["input_identity"],
            "expected_media_revision": context["media_revision"],
            "expected_source_fingerprint": context["source_fingerprint"],
            "expected_analyzer_version": context["analyzer_version"],
            "expected_decision_schema_version": context["decision_schema_version"],
            "expected_decision_version": context["decision_version"],
            "expected_config_fingerprint": context["config_fingerprint"],
            "expected_candidate_fingerprint": context["candidate_fingerprint"],
        }

    def idempotency_payload(self) -> dict[str, Any]:
        return {
            "contract": SOURCE_ANALYSIS_SERVICE_VERSION,
            "job_id": self.job_id,
            **self.persistence_kwargs(),
        }


@dataclass(frozen=True)
class CandidateInventory:
    subtitle_candidates: tuple[SubtitleCandidateInput | Mapping[str, Any], ...] = ()
    audio_candidates: tuple[AudioCandidateInput | Mapping[str, Any], ...] = ()
    media_duration_seconds: float | None = None
    subtitle_inventory_complete: bool = True
    audio_inventory_complete: bool = True

    @classmethod
    def from_value(cls, value: Any) -> "CandidateInventory":
        if isinstance(value, cls):
            return value
        payload = value
        if not isinstance(payload, Mapping):
            for method_name in ("analyzer_arguments", "to_analyzer_arguments"):
                method = getattr(value, method_name, None)
                if callable(method):
                    payload = method()
                    break
        if not isinstance(payload, Mapping):
            raise SourceAnalysisContractError(
                "candidate_loader must return CandidateInventory, a mapping, or an "
                "object with analyzer_arguments()"
            )
        subtitles = _tuple_candidates(payload.get("subtitle_candidates", ()), "subtitle_candidates")
        audios = _tuple_candidates(payload.get("audio_candidates", ()), "audio_candidates")
        duration = payload.get("media_duration_seconds")
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, int | float):
                raise SourceAnalysisContractError("media_duration_seconds must be numeric or null")
            duration = float(duration)
        subtitle_complete = payload.get("subtitle_inventory_complete", True)
        audio_complete = payload.get("audio_inventory_complete", True)
        if not isinstance(subtitle_complete, bool) or not isinstance(audio_complete, bool):
            raise SourceAnalysisContractError("inventory completeness fields must be booleans")
        return cls(
            subtitle_candidates=subtitles,
            audio_candidates=audios,
            media_duration_seconds=duration,
            subtitle_inventory_complete=subtitle_complete,
            audio_inventory_complete=audio_complete,
        )


@dataclass(frozen=True)
class SourceAnalysisResult:
    decision_id: str
    decision_sha256: str
    decision: Mapping[str, Any]
    checkpoint: Mapping[str, Any]
    stage_attempt_id: str
    idempotency_key: str
    reused: bool
    reuse_reason: str
    candidate_loader_called: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", _freeze_json(self.decision))
        object.__setattr__(self, "checkpoint", _freeze_json(self.checkpoint))

    @property
    def strategy(self) -> str:
        return str(self.decision.get("strategy") or "")

    @property
    def confidence(self) -> float:
        return float(self.decision.get("confidence") or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_sha256": self.decision_sha256,
            "decision": _thaw_json(self.decision),
            "checkpoint": _thaw_json(self.checkpoint),
            "stage_attempt_id": self.stage_attempt_id,
            "idempotency_key": self.idempotency_key,
            "reused": self.reused,
            "reuse_reason": self.reuse_reason,
            "candidate_loader_called": self.candidate_loader_called,
        }


CandidateLoader = Callable[[], CandidateInventory | Mapping[str, Any] | Any]
Analyzer = Callable[..., SourceDecision | Mapping[str, Any]]


def source_decision_idempotency_key(context: SourceAnalysisContext) -> str:
    """Return a restart-stable key; stage attempt identity is intentionally absent."""

    return "source-decision:" + canonical_json_sha256(context.idempotency_payload())


def run_source_analysis(
    store: SourceDecisionStore,
    context: SourceAnalysisContext,
    candidate_loader: CandidateLoader,
    *,
    thresholds: AnalyzerThresholds | None = None,
    analyzer: Analyzer = analyze_sources,
) -> SourceAnalysisResult:
    """Reuse or create one durable source decision in two explicit phases.

    Phase one is an exact cheap-context lookup.  On a hit the detailed loader is
    never evaluated; the immutable decision is rebound to the current running
    SUBTITLE_DETECTION attempt through the Job Store's idempotent persistence
    path.  Only a miss enters phase two and loads detailed candidates.
    """

    _require_running_subtitle_attempt(store, context)
    reusable, reuse_reason = store.reusable_source_decision(
        context.job_id,
        **context.reuse_kwargs(),
        with_reason=True,
    )
    stable_key = source_decision_idempotency_key(context)
    if reusable is not None:
        if not isinstance(reusable, Mapping):
            raise SourceAnalysisContractError("reusable source decision must be a mapping")
        decision = reusable.get("decision")
        if not isinstance(decision, Mapping):
            raise SourceAnalysisContractError("reusable source decision has no decision mapping")
        stored_key_value = reusable.get("idempotency_key")
        stored_key = str(stored_key_value).strip() if stored_key_value is not None else ""
        created_at = reusable.get("created_at")
        bound = store.persist_source_decision(
            context.job_id,
            stage_attempt_id=context.stage_attempt_id,
            decision=decision,
            idempotency_key=stored_key or None,
            created_at=float(created_at) if created_at is not None else None,
            **context.persistence_kwargs(),
        )
        if str(bound.get("decision_id") or "") != str(reusable.get("decision_id") or ""):
            raise SourceAnalysisContractError(
                "checkpoint reuse produced a different immutable decision id"
            )
        _require_bound_checkpoint(store, context, bound)
        return _result_from_record(
            bound,
            context,
            idempotency_key=stored_key or stable_key,
            reused=True,
            reuse_reason=str(reuse_reason),
            candidate_loader_called=False,
        )

    inventory = CandidateInventory.from_value(candidate_loader())
    analyzed = analyzer(
        inventory.subtitle_candidates,
        inventory.audio_candidates,
        media_duration_seconds=inventory.media_duration_seconds,
        thresholds=thresholds,
        subtitle_inventory_complete=inventory.subtitle_inventory_complete,
        audio_inventory_complete=inventory.audio_inventory_complete,
    )
    if isinstance(analyzed, SourceDecision):
        decision_payload = analyzed.to_dict()
    elif isinstance(analyzed, Mapping):
        decision_payload = _thaw_json(_freeze_json(analyzed))
    else:
        raise SourceAnalysisContractError("analyzer must return SourceDecision or a mapping")
    persisted = store.persist_source_decision(
        context.job_id,
        stage_attempt_id=context.stage_attempt_id,
        decision=decision_payload,
        idempotency_key=stable_key,
        **context.persistence_kwargs(),
    )
    _require_bound_checkpoint(store, context, persisted)
    return _result_from_record(
        persisted,
        context,
        idempotency_key=stable_key,
        reused=False,
        reuse_reason=str(reuse_reason),
        candidate_loader_called=True,
    )


def _result_from_record(
    record: Mapping[str, Any],
    context: SourceAnalysisContext,
    *,
    idempotency_key: str,
    reused: bool,
    reuse_reason: str,
    candidate_loader_called: bool,
) -> SourceAnalysisResult:
    decision = record.get("decision")
    checkpoint = record.get("stage_checkpoint")
    if not isinstance(decision, Mapping) or not isinstance(checkpoint, Mapping):
        raise SourceAnalysisContractError("persisted source decision record is incomplete")
    decision_id = str(record.get("decision_id") or "")
    decision_sha256 = str(record.get("decision_sha256") or "")
    if not decision_id or re.fullmatch(r"[0-9a-f]{64}", decision_sha256.casefold()) is None:
        raise SourceAnalysisContractError("persisted source decision identity is invalid")
    return SourceAnalysisResult(
        decision_id=decision_id,
        decision_sha256=decision_sha256,
        decision=decision,
        checkpoint=checkpoint,
        stage_attempt_id=context.stage_attempt_id,
        idempotency_key=idempotency_key,
        reused=reused,
        reuse_reason=reuse_reason,
        candidate_loader_called=candidate_loader_called,
    )


def _require_running_subtitle_attempt(
    store: SourceDecisionStore,
    context: SourceAnalysisContext,
) -> Mapping[str, Any]:
    attempts = store.list_stage_attempts(context.job_id, "SUBTITLE_DETECTION")
    attempt = next(
        (
            item
            for item in attempts
            if str(item.get("stage_attempt_id") or "") == context.stage_attempt_id
        ),
        None,
    )
    if attempt is None:
        raise SourceAnalysisAttemptError(
            "SUBTITLE_DETECTION stage attempt does not exist for the job"
        )
    if str(attempt.get("status") or "") != "RUNNING":
        raise SourceAnalysisAttemptError(
            "source analysis requires a RUNNING SUBTITLE_DETECTION attempt"
        )
    return attempt


def _require_bound_checkpoint(
    store: SourceDecisionStore,
    context: SourceAnalysisContext,
    record: Mapping[str, Any],
) -> None:
    attempt = _require_running_subtitle_attempt(store, context)
    checkpoint = attempt.get("checkpoint")
    expected_id = str(record.get("decision_id") or "")
    if (
        not isinstance(checkpoint, Mapping)
        or checkpoint.get("kind") != "source_decision"
        or str(checkpoint.get("decision_id") or "") != expected_id
        or not bool(attempt.get("outputs_verified"))
    ):
        raise SourceAnalysisContractError(
            "source decision was not bound to the running SUBTITLE_DETECTION attempt"
        )


def _tuple_candidates(value: Any, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise SourceAnalysisContractError(f"{field_name} must be an iterable of candidates")
    if not isinstance(value, Iterable):
        raise SourceAnalysisContractError(f"{field_name} must be iterable")
    return tuple(value)


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise SourceAnalysisContractError(
        f"source analysis context contains non-JSON value: {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value
