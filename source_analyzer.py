from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence


ANALYZER_VERSION = "m2-source-analyzer-v1"
DECISION_SCHEMA_VERSION = 1
DECISION_VERSION = "m2-source-decision-v1"

USE_EXISTING_ZH_TW = "USE_EXISTING_ZH_TW"
NORMALIZE_ZH_HANT = "NORMALIZE_ZH_HANT"
CONVERT_ZH_CN = "CONVERT_ZH_CN"
TRANSLATE_JA_SUBTITLE = "TRANSLATE_JA_SUBTITLE"
ASR_JA_AUDIO = "ASR_JA_AUDIO"
NEEDS_REVIEW = "NEEDS_REVIEW"
UNSUPPORTED = "UNSUPPORTED"

SUPPORTED_STRATEGIES = (
    USE_EXISTING_ZH_TW,
    NORMALIZE_ZH_HANT,
    CONVERT_ZH_CN,
    TRANSLATE_JA_SUBTITLE,
    ASR_JA_AUDIO,
    NEEDS_REVIEW,
    UNSUPPORTED,
)

_SUPPORTED_TEXT_SUBTITLE_CODECS = frozenset(
    {"ass", "ssa", "subrip", "srt", "webvtt", "vtt", "mov_text", "text"}
)
_UNKNOWN_LANGUAGE_TAGS = frozenset({"", "und", "unk", "unknown", "mis", "mul", "zxx"})
_TRADITIONAL_MARKERS = frozenset(
    "體臺灣這裡後發為與門開關讓說話時來個們嗎還過從應該現麼學習歡樂聲畫見聽讀寫愛親氣網電腦軟動龍風萬專業檔儲傳統總優質選擇"
)
_SIMPLIFIED_MARKERS = frozenset(
    "体台湾这里后发为与门开关让说话时来个们吗还过从应该现么学习欢乐声画见听读写爱亲气网电脑软动龙风万专业档储传统总优质选择"
)
_ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SRT_TIMING_RE = re.compile(
    r"^\s*\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
    r"\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}"
)


@dataclass(frozen=True)
class AnalyzerThresholds:
    """All policy thresholds used by the deterministic analyzer.

    The defaults are intentionally policy inputs rather than module-level
    branches so later corpus calibration can replace them without changing the
    analyzer algorithm or invalidating unrelated checkpoints.
    """

    auto_accept_confidence: float = 0.90
    review_confidence: float = 0.60
    min_subtitle_events: int = 20
    min_subtitle_coverage_ratio: float = 0.60
    min_valid_timing_ratio: float = 0.90
    max_empty_event_ratio: float = 0.20
    max_forced_probability: float = 0.60
    max_signs_only_probability: float = 0.65
    max_songs_only_probability: float = 0.70
    min_dialogue_completeness_score: float = 0.68
    min_content_characters: int = 12
    min_cjk_characters: int = 8
    min_kana_characters: int = 2
    min_japanese_character_ratio: float = 0.08
    min_audio_duration_ratio: float = 0.60
    close_candidate_score_margin: float = 0.025
    exact_tie_score_epsilon: float = 0.002
    metadata_conflict_penalty: float = 0.06
    japanese_audio_tag_confidence: float = 0.91

    def __post_init__(self) -> None:
        probabilities = (
            self.auto_accept_confidence,
            self.review_confidence,
            self.min_subtitle_coverage_ratio,
            self.min_valid_timing_ratio,
            self.max_empty_event_ratio,
            self.max_forced_probability,
            self.max_signs_only_probability,
            self.max_songs_only_probability,
            self.min_dialogue_completeness_score,
            self.min_japanese_character_ratio,
            self.min_audio_duration_ratio,
            self.close_candidate_score_margin,
            self.exact_tie_score_epsilon,
            self.metadata_conflict_penalty,
            self.japanese_audio_tag_confidence,
        )
        if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in probabilities):
            raise ValueError("analyzer probability thresholds must be finite values in [0, 1]")
        if self.review_confidence >= self.auto_accept_confidence:
            raise ValueError("review_confidence must be lower than auto_accept_confidence")
        if self.min_subtitle_events < 1:
            raise ValueError("min_subtitle_events must be positive")
        if self.min_content_characters < 1 or self.min_cjk_characters < 1:
            raise ValueError("content thresholds must be positive")
        if self.min_kana_characters < 1:
            raise ValueError("min_kana_characters must be positive")
        if self.exact_tie_score_epsilon > self.close_candidate_score_margin:
            raise ValueError("exact_tie_score_epsilon cannot exceed close_candidate_score_margin")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True)
class SubtitleCandidateInput:
    track_index: int
    codec: str
    source_kind: str = ""
    source_reference: str = ""
    source_size: int | None = None
    source_mtime_ns: int | None = None
    source_sha256: str = ""
    content_sha256: str = ""
    container_language_tag: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool | None = None
    event_count: int = 0
    first_timestamp_seconds: float | None = None
    last_timestamp_seconds: float | None = None
    valid_timing_count: int | None = None
    empty_event_count: int | None = None
    sample_text: str = ""
    extraction_error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, str) or not isinstance(self.source_reference, str):
            raise TypeError("source_kind and source_reference must be strings")
        for field_name in ("source_size", "source_mtime_ns"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{field_name} must be null or a non-negative integer")
        for field_name in ("source_sha256", "content_sha256"):
            digest = str(getattr(self, field_name) or "").strip().casefold()
            if digest and re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError(f"{field_name} must be empty or a SHA-256 hex digest")
            object.__setattr__(self, field_name, digest)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SubtitleCandidateInput":
        return cls(
            track_index=_as_int(value.get("track_index", value.get("index", -1)), -1),
            codec=str(value.get("codec", value.get("codec_name", "")) or ""),
            source_kind=str(value.get("source_kind", "") or ""),
            source_reference=str(value.get("source_reference", "") or ""),
            source_size=_strict_optional_nonnegative_int(
                value.get("source_size"), "source_size"
            ),
            source_mtime_ns=_strict_optional_nonnegative_int(
                value.get("source_mtime_ns"), "source_mtime_ns"
            ),
            source_sha256=str(value.get("source_sha256", "") or ""),
            content_sha256=str(value.get("content_sha256", "") or ""),
            container_language_tag=str(
                value.get("container_language_tag", value.get("language", "")) or ""
            ),
            title=str(value.get("title", "") or ""),
            default=_as_bool(value.get("default", value.get("default_flag", False))),
            forced=_as_bool(value.get("forced", value.get("forced_flag", False))),
            hearing_impaired=_as_optional_bool(
                value.get("hearing_impaired", value.get("hearing_impaired_flag"))
            ),
            event_count=max(0, _as_int(value.get("event_count", 0), 0)),
            first_timestamp_seconds=_as_optional_float(value.get("first_timestamp_seconds")),
            last_timestamp_seconds=_as_optional_float(value.get("last_timestamp_seconds")),
            valid_timing_count=_as_optional_int(value.get("valid_timing_count")),
            empty_event_count=_as_optional_int(value.get("empty_event_count")),
            sample_text=str(value.get("sample_text", "") or ""),
            extraction_error=str(value.get("extraction_error", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_index": self.track_index,
            "codec": self.codec,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
            "container_language_tag": self.container_language_tag,
            "title": self.title,
            "default": self.default,
            "forced": self.forced,
            "hearing_impaired": self.hearing_impaired,
            "event_count": self.event_count,
            "first_timestamp_seconds": self.first_timestamp_seconds,
            "last_timestamp_seconds": self.last_timestamp_seconds,
            "valid_timing_count": self.valid_timing_count,
            "empty_event_count": self.empty_event_count,
            "sample_text": self.sample_text,
            "extraction_error": self.extraction_error,
        }


@dataclass(frozen=True)
class AudioCandidateInput:
    track_index: int
    codec: str
    source_kind: str = ""
    source_reference: str = ""
    container_language_tag: str = ""
    title: str = ""
    default: bool = False
    commentary: bool = False
    channels: int | None = None
    sample_rate: int | None = None
    duration_seconds: float | None = None
    detected_language: str = ""
    language_confidence: float = 0.0
    detection_source: str = ""
    probing_error: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AudioCandidateInput":
        return cls(
            track_index=_as_int(value.get("track_index", value.get("index", -1)), -1),
            codec=str(value.get("codec", value.get("codec_name", "")) or ""),
            source_kind=str(value.get("source_kind", "") or ""),
            source_reference=str(value.get("source_reference", "") or ""),
            container_language_tag=str(
                value.get("container_language_tag", value.get("language", "")) or ""
            ),
            title=str(value.get("title", "") or ""),
            default=_as_bool(value.get("default", value.get("default_flag", False))),
            commentary=_as_bool(value.get("commentary", False)),
            channels=_as_optional_int(value.get("channels")),
            sample_rate=_as_optional_int(value.get("sample_rate")),
            duration_seconds=_as_optional_float(value.get("duration_seconds", value.get("duration"))),
            detected_language=str(value.get("detected_language", "") or ""),
            language_confidence=_probability(value.get("language_confidence", 0.0)),
            detection_source=str(value.get("detection_source", "") or ""),
            probing_error=str(
                value.get("probing_error", value.get("extraction_error", "")) or ""
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_index": self.track_index,
            "codec": self.codec,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
            "container_language_tag": self.container_language_tag,
            "title": self.title,
            "default": self.default,
            "commentary": self.commentary,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "detected_language": self.detected_language,
            "language_confidence": self.language_confidence,
            "detection_source": self.detection_source,
            "probing_error": self.probing_error,
        }


@dataclass(frozen=True)
class SubtitleCandidateAnalysis:
    track_index: int
    codec: str
    source_kind: str
    source_reference: str
    source_size: int | None
    source_mtime_ns: int | None
    source_sha256: str
    content_sha256: str
    container_language_tag: str
    normalized_language_tag: str
    title: str
    default: bool
    forced: bool
    hearing_impaired: bool | None
    event_count: int
    first_timestamp_seconds: float | None
    last_timestamp_seconds: float | None
    coverage_ratio: float
    valid_timing_ratio: float
    empty_event_ratio: float
    detected_language: str
    language_confidence: float
    chinese_script: str
    traditional_marker_count: int
    simplified_marker_count: int
    japanese_character_ratio: float
    forced_probability: float
    signs_only_probability: float
    songs_only_probability: float
    dialogue_completeness_score: float
    score: float
    eligible: bool
    processable: bool
    extraction_error: str
    rejection_reasons: tuple[str, ...]
    evidence: Mapping[str, Any]
    selected: bool = False

    @property
    def kind(self) -> str:
        return "subtitle"

    @property
    def index(self) -> int:
        return self.track_index

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "index": self.track_index,
            "score": _rounded(self.score),
            "selected": self.selected,
            "codec": self.codec,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
            "container_language_tag": self.container_language_tag,
            "normalized_language_tag": self.normalized_language_tag,
            "title": self.title,
            "default": self.default,
            "forced": self.forced,
            "hearing_impaired": self.hearing_impaired,
            "event_count": self.event_count,
            "first_timestamp_seconds": _rounded_optional(self.first_timestamp_seconds),
            "last_timestamp_seconds": _rounded_optional(self.last_timestamp_seconds),
            "coverage_ratio": _rounded(self.coverage_ratio),
            "valid_timing_ratio": _rounded(self.valid_timing_ratio),
            "empty_event_ratio": _rounded(self.empty_event_ratio),
            "detected_language": self.detected_language,
            "language_confidence": _rounded(self.language_confidence),
            "chinese_script": self.chinese_script,
            "traditional_marker_count": self.traditional_marker_count,
            "simplified_marker_count": self.simplified_marker_count,
            "japanese_character_ratio": _rounded(self.japanese_character_ratio),
            "forced_probability": _rounded(self.forced_probability),
            "signs_only_probability": _rounded(self.signs_only_probability),
            "songs_only_probability": _rounded(self.songs_only_probability),
            "dialogue_completeness_score": _rounded(self.dialogue_completeness_score),
            "eligible": self.eligible,
            "processable": self.processable,
            "extraction_error": self.extraction_error,
            "rejection_reasons": list(self.rejection_reasons),
            "evidence": _canonical_data(self.evidence),
        }


@dataclass(frozen=True)
class AudioCandidateAnalysis:
    track_index: int
    codec: str
    source_kind: str
    source_reference: str
    container_language_tag: str
    normalized_language_tag: str
    title: str
    default: bool
    commentary: bool
    channels: int | None
    sample_rate: int | None
    duration_seconds: float | None
    duration_ratio: float
    detected_language: str
    language_confidence: float
    detection_source: str
    score: float
    eligible: bool
    processable: bool
    probing_error: str
    rejection_reasons: tuple[str, ...]
    evidence: Mapping[str, Any]
    selected: bool = False

    @property
    def kind(self) -> str:
        return "audio"

    @property
    def index(self) -> int:
        return self.track_index

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "index": self.track_index,
            "score": _rounded(self.score),
            "selected": self.selected,
            "codec": self.codec,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
            "container_language_tag": self.container_language_tag,
            "normalized_language_tag": self.normalized_language_tag,
            "title": self.title,
            "default": self.default,
            "commentary": self.commentary,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "duration_seconds": _rounded_optional(self.duration_seconds),
            "duration_ratio": _rounded(self.duration_ratio),
            "detected_language": self.detected_language,
            "language_confidence": _rounded(self.language_confidence),
            "detection_source": self.detection_source,
            "eligible": self.eligible,
            "processable": self.processable,
            "probing_error": self.probing_error,
            "rejection_reasons": list(self.rejection_reasons),
            "evidence": _canonical_data(self.evidence),
        }


CandidateAnalysis = SubtitleCandidateAnalysis | AudioCandidateAnalysis


@dataclass(frozen=True)
class SourceDecision:
    strategy: str
    confidence: float
    reason_code: str
    evidence: Mapping[str, Any]
    selected_subtitle_track: int | None
    selected_audio_track: int | None
    candidates: tuple[CandidateAnalysis, ...]
    unselected_reasons: tuple[tuple[str, tuple[str, ...]], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable, JSON-safe persistence contract."""

        selected_subtitle = next(
            (
                candidate.to_dict()
                for candidate in self.candidates
                if candidate.kind == "subtitle"
                and candidate.index == self.selected_subtitle_track
            ),
            None,
        )
        selected_audio = next(
            (
                candidate.to_dict()
                for candidate in self.candidates
                if candidate.kind == "audio"
                and candidate.index == self.selected_audio_track
            ),
            None,
        )
        return {
            "strategy": self.strategy,
            "confidence": _rounded(self.confidence),
            "reason_code": self.reason_code,
            "evidence": _canonical_data(self.evidence),
            "selected_subtitle_track": selected_subtitle,
            "selected_audio_track": selected_audio,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "unselected_reasons": [
                {"candidate": key, "reasons": list(reasons)}
                for key, reasons in sorted(self.unselected_reasons)
            ],
        }


def normalize_language_tag(value: Any) -> str:
    tag = str(value or "").strip().casefold().replace("_", "-")
    if tag in _UNKNOWN_LANGUAGE_TAGS:
        return "und"
    aliases = {
        "cht": "zh-hant",
        "traditional-chinese": "zh-hant",
        "zh-traditional": "zh-hant",
        "zh-hk": "zh-hant",
        "zh-mo": "zh-hant",
        "chs": "zh-hans",
        "simplified-chinese": "zh-hans",
        "zh-simplified": "zh-hans",
        "zh-sg": "zh-hans",
        "chi": "zh",
        "zho": "zh",
        "cmn": "zh",
        "chinese": "zh",
        "jp": "ja",
        "jpn": "ja",
        "jap": "ja",
        "japanese": "ja",
        "eng": "en",
        "english": "en",
    }
    if tag in aliases:
        return aliases[tag]
    if tag.startswith("zh-tw"):
        return "zh-tw"
    if tag.startswith("zh-hant"):
        return "zh-hant"
    if tag.startswith("zh-cn"):
        return "zh-cn"
    if tag.startswith("zh-hans"):
        return "zh-hans"
    if tag == "zh" or tag.startswith("zh-"):
        return "zh"
    if tag == "ja" or tag.startswith("ja-"):
        return "ja"
    return tag


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fingerprint_inputs(
    subtitle_candidates: Iterable[SubtitleCandidateInput | Mapping[str, Any]],
    audio_candidates: Iterable[AudioCandidateInput | Mapping[str, Any]],
    *,
    media_duration_seconds: float | None,
    subtitle_inventory_complete: bool = True,
    audio_inventory_complete: bool = True,
) -> str:
    """Fingerprint the canonical candidate inventory, independent of probe order."""

    subtitles = [_coerce_subtitle(item) for item in subtitle_candidates]
    audios = [_coerce_audio(item) for item in audio_candidates]
    payload = {
        "subtitle_inventory_complete": bool(subtitle_inventory_complete),
        "audio_inventory_complete": bool(audio_inventory_complete),
        "media_duration_seconds": _rounded_optional(_positive_float(media_duration_seconds)),
        "subtitles": sorted(
            (item.to_dict() for item in subtitles),
            key=lambda item: (item["track_index"], canonical_json(item)),
        ),
        "audio": sorted(
            (item.to_dict() for item in audios),
            key=lambda item: (item["track_index"], canonical_json(item)),
        ),
    }
    return canonical_json_sha256(payload)


def decision_sha256(decision: SourceDecision | Mapping[str, Any]) -> str:
    payload = decision.to_dict() if isinstance(decision, SourceDecision) else decision
    return canonical_json_sha256(payload)


def analyze_sources(
    subtitle_candidates: Iterable[SubtitleCandidateInput | Mapping[str, Any]] = (),
    audio_candidates: Iterable[AudioCandidateInput | Mapping[str, Any]] = (),
    *,
    media_duration_seconds: float | None,
    thresholds: AnalyzerThresholds | None = None,
    subtitle_inventory_complete: bool = True,
    audio_inventory_complete: bool = True,
) -> SourceDecision:
    """Analyze already-observed source candidates without invoking ASR.

    This function has no media, model, clock, network, or filesystem side
    effects.  The caller owns probing/extraction and can persist the canonical
    result together with its authoritative Job/media identity.
    """

    policy = thresholds or AnalyzerThresholds()
    duration = _positive_float(media_duration_seconds)
    subtitle_inputs = tuple(_coerce_subtitle(item) for item in subtitle_candidates)
    audio_inputs = tuple(_coerce_audio(item) for item in audio_candidates)
    _require_unique_indices(subtitle_inputs, "subtitle")
    _require_unique_indices(audio_inputs, "audio")

    analyzed_subtitles = tuple(
        _analyze_subtitle(item, duration, policy) for item in subtitle_inputs
    )
    subtitles = tuple(
        sorted(_deduplicate_subtitle_content(analyzed_subtitles), key=_subtitle_sort_key)
    )
    audios = tuple(
        sorted(
            (_analyze_audio(item, duration, policy) for item in audio_inputs),
            key=_audio_sort_key,
        )
    )
    base_evidence: dict[str, Any] = {
        "analyzer_version": ANALYZER_VERSION,
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "decision_version": DECISION_VERSION,
        "policy": policy.to_dict(),
        "subtitle_inventory_complete": bool(subtitle_inventory_complete),
        "audio_inventory_complete": bool(audio_inventory_complete),
        "candidate_counts": {"subtitle": len(subtitles), "audio": len(audios)},
        "priority_order": ["zh-tw", "zh-hant", "zh-cn", "zh-hans", "ja-subtitle", "ja-audio"],
        "asr_invoked": False,
    }

    eligible_subtitles = [item for item in subtitles if item.eligible]
    if eligible_subtitles:
        selected = eligible_subtitles[0]
        base_confidence = _subtitle_decision_confidence(selected)
        additional = _subtitle_additional_checks(selected, eligible_subtitles, base_confidence, policy)
        final_confidence = float(additional["postcheck_confidence"])
        if final_confidence >= policy.auto_accept_confidence and additional["result"] != "insufficient":
            strategy = _strategy_for_subtitle_language(selected.detected_language)
            marked_subtitles = tuple(
                replace(item, selected=item.track_index == selected.track_index) for item in subtitles
            )
            candidates: tuple[CandidateAnalysis, ...] = marked_subtitles + audios
            reason = {
                USE_EXISTING_ZH_TW: "complete_zh_tw_subtitle",
                NORMALIZE_ZH_HANT: "complete_zh_hant_subtitle_requires_normalization",
                CONVERT_ZH_CN: "complete_zh_cn_subtitle",
                TRANSLATE_JA_SUBTITLE: "complete_japanese_dialogue_subtitle",
            }[strategy]
            evidence = {
                **base_evidence,
                "selected_candidate": _candidate_key(selected),
                "selected_language": selected.detected_language,
                "additional_checks": additional,
            }
            return SourceDecision(
                strategy=strategy,
                confidence=final_confidence,
                reason_code=reason,
                evidence=evidence,
                selected_subtitle_track=selected.track_index,
                selected_audio_track=None,
                candidates=candidates,
                unselected_reasons=_unselected_reasons(candidates, selected),
            )
        evidence = {
            **base_evidence,
            "provisional_candidate": _candidate_key(selected),
            "additional_checks": additional,
        }
        return _review_decision(subtitles + audios, final_confidence, "subtitle_selection_ambiguous", evidence)

    eligible_audios = [item for item in audios if item.eligible]
    if eligible_audios:
        selected_audio = eligible_audios[0]
        base_confidence = _audio_decision_confidence(selected_audio)
        additional = _audio_additional_checks(selected_audio, eligible_audios, base_confidence, policy)
        final_confidence = float(additional["postcheck_confidence"])
        if final_confidence >= policy.auto_accept_confidence and additional["result"] != "insufficient":
            marked_audios = tuple(
                replace(item, selected=item.track_index == selected_audio.track_index) for item in audios
            )
            candidates = subtitles + marked_audios
            evidence = {
                **base_evidence,
                "selected_candidate": _candidate_key(selected_audio),
                "selected_language": "ja",
                "additional_checks": additional,
            }
            return SourceDecision(
                strategy=ASR_JA_AUDIO,
                confidence=final_confidence,
                reason_code="trusted_japanese_audio_no_usable_subtitle",
                evidence=evidence,
                selected_subtitle_track=None,
                selected_audio_track=selected_audio.track_index,
                candidates=candidates,
                unselected_reasons=_unselected_reasons(candidates, selected_audio),
            )
        evidence = {
            **base_evidence,
            "provisional_candidate": _candidate_key(selected_audio),
            "additional_checks": additional,
        }
        return _review_decision(subtitles + audios, final_confidence, "audio_selection_ambiguous", evidence)

    all_candidates: tuple[CandidateAnalysis, ...] = subtitles + audios
    incomplete_inventory = not subtitle_inventory_complete or not audio_inventory_complete
    processable = [item for item in all_candidates if item.processable]
    if incomplete_inventory or processable:
        confidence = max((item.score for item in processable), default=0.0)
        evidence = {
            **base_evidence,
            "additional_checks": {
                "required": True,
                "performed": ["candidate_usability", "inventory_completeness"],
                "result": "insufficient",
                "precheck_confidence": _rounded(confidence),
                "postcheck_confidence": _rounded(confidence),
            },
        }
        reason = "candidate_analysis_inconclusive" if processable else "source_inventory_incomplete"
        return _review_decision(all_candidates, confidence, reason, evidence)

    evidence = {
        **base_evidence,
        "additional_checks": {
            "required": False,
            "performed": [],
            "result": "not_required",
            "precheck_confidence": 0.99,
            "postcheck_confidence": 0.99,
        },
    }
    return SourceDecision(
        strategy=UNSUPPORTED,
        confidence=0.99,
        reason_code="no_supported_subtitle_or_audio_source",
        evidence=evidence,
        selected_subtitle_track=None,
        selected_audio_track=None,
        candidates=all_candidates,
        unselected_reasons=_unselected_reasons(all_candidates, None),
    )


def analyze_subtitle_candidate(
    candidate: SubtitleCandidateInput | Mapping[str, Any],
    *,
    media_duration_seconds: float | None,
    thresholds: AnalyzerThresholds | None = None,
) -> SubtitleCandidateAnalysis:
    return _analyze_subtitle(
        _coerce_subtitle(candidate),
        _positive_float(media_duration_seconds),
        thresholds or AnalyzerThresholds(),
    )


def analyze_audio_candidate(
    candidate: AudioCandidateInput | Mapping[str, Any],
    *,
    media_duration_seconds: float | None,
    thresholds: AnalyzerThresholds | None = None,
) -> AudioCandidateAnalysis:
    return _analyze_audio(
        _coerce_audio(candidate),
        _positive_float(media_duration_seconds),
        thresholds or AnalyzerThresholds(),
    )


def _analyze_subtitle(
    candidate: SubtitleCandidateInput,
    media_duration: float | None,
    policy: AnalyzerThresholds,
) -> SubtitleCandidateAnalysis:
    events = max(0, int(candidate.event_count))
    normalized_tag = normalize_language_tag(candidate.container_language_tag)
    plain_text = _plain_subtitle_text(candidate.sample_text)
    language, language_confidence, language_evidence = _subtitle_language_evidence(
        normalized_tag,
        candidate.title,
        plain_text,
        policy,
    )
    timing_metrics_present = candidate.valid_timing_count is not None
    empty_metrics_present = candidate.empty_event_count is not None
    valid_count = 0 if candidate.valid_timing_count is None else max(0, candidate.valid_timing_count)
    empty_count = events if candidate.empty_event_count is None else max(0, candidate.empty_event_count)
    valid_timing_ratio = _ratio(valid_count, events)
    empty_event_ratio = _ratio(empty_count, events)
    coverage_ratio = _coverage_ratio(
        candidate.first_timestamp_seconds,
        candidate.last_timestamp_seconds,
        media_duration,
    )
    forced_probability, signs_probability, songs_probability = _subtitle_risks(
        candidate,
        plain_text,
        coverage_ratio,
        policy,
    )
    completeness = _dialogue_completeness(
        events,
        coverage_ratio,
        valid_timing_ratio,
        empty_event_ratio,
        forced_probability,
        signs_probability,
        songs_probability,
        policy,
    )
    score = _clamp(
        0.54 * completeness
        + 0.35 * language_confidence
        + 0.10 * valid_timing_ratio
        + (0.01 if candidate.default else 0.0)
    )
    codec = str(candidate.codec or "").strip().casefold()
    rejection: list[str] = []
    if candidate.extraction_error:
        rejection.append("extraction_error")
    if codec not in _SUPPORTED_TEXT_SUBTITLE_CODECS:
        rejection.append("unsupported_subtitle_codec")
    if events <= 0:
        rejection.append("empty_subtitle")
    elif events < policy.min_subtitle_events:
        rejection.append("too_few_events")
    if not timing_metrics_present:
        rejection.append("timing_metrics_missing")
    if not empty_metrics_present:
        rejection.append("empty_event_metrics_missing")
    if coverage_ratio < policy.min_subtitle_coverage_ratio:
        rejection.append("insufficient_coverage")
    if valid_timing_ratio < policy.min_valid_timing_ratio:
        rejection.append("invalid_timing_ratio")
    if empty_event_ratio > policy.max_empty_event_ratio:
        rejection.append("too_many_empty_events")
    if forced_probability >= policy.max_forced_probability:
        rejection.append("forced_track_risk")
    if signs_probability >= policy.max_signs_only_probability:
        rejection.append("signs_only_risk")
    if songs_probability >= policy.max_songs_only_probability:
        rejection.append("songs_only_risk")
    if completeness < policy.min_dialogue_completeness_score:
        rejection.append("low_dialogue_completeness")
    if language not in {"zh-tw", "zh-hant", "zh-cn", "zh-hans", "ja"}:
        rejection.append("unsupported_or_unknown_language")
    elif language_confidence < policy.review_confidence:
        rejection.append("language_confidence_below_review_threshold")
    processable = codec in _SUPPORTED_TEXT_SUBTITLE_CODECS and (
        language in {"zh-tw", "zh-hant", "zh-cn", "zh-hans", "ja"}
        or normalized_tag in {"zh-tw", "zh-hant", "zh-cn", "zh-hans", "zh", "ja", "und"}
        or bool(candidate.extraction_error)
    )
    evidence = {
        **language_evidence,
        "timeline": {
            "media_duration_seconds": _rounded_optional(media_duration),
            "coverage_ratio": _rounded(coverage_ratio),
            "valid_timing_ratio": _rounded(valid_timing_ratio),
            "empty_event_ratio": _rounded(empty_event_ratio),
        },
        "risk": {
            "forced_probability": _rounded(forced_probability),
            "signs_only_probability": _rounded(signs_probability),
            "songs_only_probability": _rounded(songs_probability),
        },
    }
    return SubtitleCandidateAnalysis(
        track_index=candidate.track_index,
        codec=codec,
        source_kind=candidate.source_kind,
        source_reference=candidate.source_reference,
        source_size=candidate.source_size,
        source_mtime_ns=candidate.source_mtime_ns,
        source_sha256=candidate.source_sha256,
        content_sha256=candidate.content_sha256,
        container_language_tag=candidate.container_language_tag,
        normalized_language_tag=normalized_tag,
        title=candidate.title,
        default=candidate.default,
        forced=candidate.forced,
        hearing_impaired=candidate.hearing_impaired,
        event_count=events,
        first_timestamp_seconds=candidate.first_timestamp_seconds,
        last_timestamp_seconds=candidate.last_timestamp_seconds,
        coverage_ratio=coverage_ratio,
        valid_timing_ratio=valid_timing_ratio,
        empty_event_ratio=empty_event_ratio,
        detected_language=language,
        language_confidence=language_confidence,
        chinese_script=str(language_evidence["script_distribution"]["chinese_script"]),
        traditional_marker_count=int(language_evidence["script_distribution"]["traditional_markers"]),
        simplified_marker_count=int(language_evidence["script_distribution"]["simplified_markers"]),
        japanese_character_ratio=float(language_evidence["script_distribution"]["japanese_character_ratio"]),
        forced_probability=forced_probability,
        signs_only_probability=signs_probability,
        songs_only_probability=songs_probability,
        dialogue_completeness_score=completeness,
        score=score,
        eligible=not rejection,
        processable=processable,
        extraction_error=candidate.extraction_error,
        rejection_reasons=tuple(dict.fromkeys(rejection)),
        evidence=evidence,
    )


def _analyze_audio(
    candidate: AudioCandidateInput,
    media_duration: float | None,
    policy: AnalyzerThresholds,
) -> AudioCandidateAnalysis:
    normalized_tag = normalize_language_tag(candidate.container_language_tag)
    title_language = _title_language(candidate.title)
    detected = normalize_language_tag(candidate.detected_language)
    source = str(candidate.detection_source or "").strip() or "metadata"
    conflict = False
    if detected != "und":
        language = detected
        confidence = _probability(candidate.language_confidence)
        if language == "ja" and (normalized_tag == "ja" or title_language == "ja"):
            confidence = min(0.995, confidence + 0.02)
        if normalized_tag not in {"und", "zh", language} and normalized_tag != language:
            conflict = True
            confidence = max(0.0, confidence - policy.metadata_conflict_penalty)
    elif normalized_tag == "ja":
        language = "ja"
        confidence = policy.japanese_audio_tag_confidence
        if title_language == "ja":
            confidence = min(0.995, confidence + 0.05)
    elif title_language == "ja":
        language = "ja"
        confidence = max(policy.review_confidence, policy.japanese_audio_tag_confidence - 0.05)
    else:
        language = normalized_tag
        confidence = 0.0 if normalized_tag == "und" else 0.55

    duration = _positive_float(candidate.duration_seconds)
    if duration is None:
        duration_ratio = 0.0
    elif media_duration is None:
        duration_ratio = 1.0
    else:
        duration_ratio = _clamp(duration / media_duration)
    channel_score = _clamp(float(candidate.channels or 0) / 2.0)
    score = _clamp(
        0.65 * confidence
        + 0.25 * duration_ratio
        + 0.07 * channel_score
        + (0.03 if candidate.default else 0.0)
        - (0.45 if candidate.commentary else 0.0)
    )
    rejection: list[str] = []
    if candidate.probing_error:
        rejection.append("audio_probe_error")
    if candidate.commentary:
        rejection.append("commentary_audio")
    if language != "ja":
        rejection.append("not_confident_japanese_audio")
    elif confidence < policy.review_confidence:
        rejection.append("language_confidence_below_review_threshold")
    if duration_ratio < policy.min_audio_duration_ratio:
        rejection.append("insufficient_audio_duration")
    processable = (
        language == "ja"
        or normalized_tag in {"ja", "und"}
        or title_language == "ja"
        or bool(candidate.probing_error)
    )
    evidence = {
        "metadata_language": normalized_tag,
        "title_language": title_language,
        "provided_detection_language": detected,
        "metadata_content_conflict": conflict,
        "duration_ratio": _rounded(duration_ratio),
    }
    return AudioCandidateAnalysis(
        track_index=candidate.track_index,
        codec=str(candidate.codec or "").strip().casefold(),
        source_kind=candidate.source_kind,
        source_reference=candidate.source_reference,
        container_language_tag=candidate.container_language_tag,
        normalized_language_tag=normalized_tag,
        title=candidate.title,
        default=candidate.default,
        commentary=candidate.commentary,
        channels=candidate.channels,
        sample_rate=candidate.sample_rate,
        duration_seconds=duration,
        duration_ratio=duration_ratio,
        detected_language=language,
        language_confidence=confidence,
        detection_source=source,
        score=score,
        eligible=not rejection,
        processable=processable,
        probing_error=candidate.probing_error,
        rejection_reasons=tuple(dict.fromkeys(rejection)),
        evidence=evidence,
    )


def _subtitle_language_evidence(
    normalized_tag: str,
    title: str,
    plain_text: str,
    policy: AnalyzerThresholds,
) -> tuple[str, float, dict[str, Any]]:
    title_language = _title_language(title)
    meaningful = [character for character in plain_text if _is_meaningful_character(character)]
    han_count = sum(1 for character in meaningful if _is_han(character))
    kana_count = sum(1 for character in meaningful if _is_kana(character))
    traditional = sum(1 for character in meaningful if character in _TRADITIONAL_MARKERS)
    simplified = sum(1 for character in meaningful if character in _SIMPLIFIED_MARKERS)
    japanese_ratio = _ratio(kana_count, max(1, han_count + kana_count))
    chinese_script = "undetermined"
    content_language = "und"
    content_confidence = 0.0
    if kana_count >= policy.min_kana_characters and japanese_ratio >= policy.min_japanese_character_ratio:
        content_language = "ja"
        content_confidence = min(0.995, 0.90 + min(0.075, japanese_ratio * 0.12) + min(0.02, kana_count / 500.0))
    elif traditional >= 2 and traditional >= simplified + 2:
        chinese_script = "traditional"
        content_language = "zh-hant"
        content_confidence = min(0.995, 0.92 + min(0.06, traditional / 100.0))
    elif simplified >= 2 and simplified >= traditional + 2:
        chinese_script = "simplified"
        content_language = "zh-hans"
        content_confidence = min(0.995, 0.92 + min(0.06, simplified / 100.0))
    elif han_count >= policy.min_cjk_characters:
        content_language = "zh"
        content_confidence = 0.58

    metadata_conflict = False
    normalized_language = content_language
    confidence = content_confidence
    if content_language == "ja":
        if normalized_tag in {"zh-tw", "zh-hant", "zh-cn", "zh-hans", "zh"}:
            metadata_conflict = True
            confidence = max(0.0, confidence - policy.metadata_conflict_penalty)
        elif normalized_tag == "ja" or title_language == "ja":
            confidence = min(0.995, confidence + 0.01)
    elif content_language == "zh-hant":
        if normalized_tag in {"zh-cn", "zh-hans", "ja"}:
            metadata_conflict = True
            confidence = max(0.0, confidence - policy.metadata_conflict_penalty)
        if normalized_tag == "zh-tw" or title_language == "zh-tw":
            normalized_language = "zh-tw"
            confidence = min(0.995, confidence + 0.01)
        elif normalized_tag in {"zh-hant", "zh", "und"}:
            normalized_language = "zh-hant"
    elif content_language == "zh-hans":
        if normalized_tag in {"zh-tw", "zh-hant", "ja"}:
            metadata_conflict = True
            confidence = max(0.0, confidence - policy.metadata_conflict_penalty)
        normalized_language = "zh-cn" if normalized_tag == "zh-cn" else "zh-hans"
        if normalized_tag in {"zh-cn", "zh-hans"} or title_language in {"zh-cn", "zh-hans"}:
            confidence = min(0.995, confidence + 0.01)
    elif content_language == "zh":
        preferred = normalized_tag if normalized_tag in {"zh-tw", "zh-hant", "zh-cn", "zh-hans"} else title_language
        if preferred in {"zh-tw", "zh-hant", "zh-cn", "zh-hans"}:
            normalized_language = preferred
            confidence = 0.72
        else:
            normalized_language = "zh"
    elif len(meaningful) >= policy.min_content_characters:
        normalized_language = "und"
    elif normalized_tag in {"zh-tw", "zh-hant", "zh-cn", "zh-hans", "ja"}:
        normalized_language = normalized_tag
        confidence = 0.45

    if title_language not in {"und", "zh"} and normalized_language not in {"und", "zh"}:
        title_matches = _language_family(title_language) == _language_family(normalized_language)
        if not title_matches:
            metadata_conflict = True
        elif content_language != "und":
            confidence = min(0.995, confidence + 0.005)

    evidence = {
        "metadata_language": normalized_tag,
        "title_language": title_language,
        "content_language": content_language,
        "metadata_content_conflict": metadata_conflict,
        "sample_character_count": len(meaningful),
        "script_distribution": {
            "han_characters": han_count,
            "kana_characters": kana_count,
            "traditional_markers": traditional,
            "simplified_markers": simplified,
            "chinese_script": chinese_script,
            "japanese_character_ratio": _rounded(japanese_ratio),
        },
    }
    return normalized_language, _clamp(confidence), evidence


def _subtitle_risks(
    candidate: SubtitleCandidateInput,
    plain_text: str,
    coverage_ratio: float,
    policy: AnalyzerThresholds,
) -> tuple[float, float, float]:
    lowered_title = str(candidate.title or "").casefold()
    forced_probability = 1.0 if candidate.forced else 0.0
    if any(marker in lowered_title for marker in ("forced", "forçado", "強制", "强制")):
        forced_probability = max(forced_probability, 0.95)

    signs_probability = 0.0
    if any(marker in lowered_title for marker in ("signs", "signs & songs", "sign/song", "招牌", "標誌", "标志")):
        signs_probability = 0.95
    if candidate.forced:
        signs_probability = max(signs_probability, 0.85)
    if candidate.event_count < policy.min_subtitle_events:
        signs_probability = max(signs_probability, 0.78)
    if coverage_ratio < policy.min_subtitle_coverage_ratio * 0.5:
        signs_probability = max(signs_probability, 0.72)

    songs_probability = 0.0
    if any(
        marker in lowered_title
        for marker in ("songs", "lyrics", "karaoke", "opening", "ending", "op/ed", "歌詞", "歌词")
    ):
        songs_probability = 0.95
    music_marks = plain_text.count("♪") + plain_text.count("♫") + plain_text.count("♬")
    nonempty_lines = max(1, sum(1 for line in plain_text.splitlines() if line.strip()))
    if music_marks >= 2 and music_marks / nonempty_lines >= 0.25:
        songs_probability = max(songs_probability, 0.82)
    return forced_probability, signs_probability, songs_probability


def _dialogue_completeness(
    event_count: int,
    coverage_ratio: float,
    valid_timing_ratio: float,
    empty_event_ratio: float,
    forced_probability: float,
    signs_probability: float,
    songs_probability: float,
    policy: AnalyzerThresholds,
) -> float:
    event_score = _clamp(event_count / float(policy.min_subtitle_events))
    nonempty_score = 1.0 - empty_event_ratio
    base = (
        0.30 * coverage_ratio
        + 0.25 * valid_timing_ratio
        + 0.20 * nonempty_score
        + 0.25 * event_score
    )
    risk_penalty = max(
        0.65 * forced_probability,
        0.48 * signs_probability,
        0.48 * songs_probability,
    )
    return _clamp(base * (1.0 - risk_penalty))


def _subtitle_additional_checks(
    selected: SubtitleCandidateAnalysis,
    eligible: Sequence[SubtitleCandidateAnalysis],
    base_confidence: float,
    policy: AnalyzerThresholds,
) -> dict[str, Any]:
    runner_up = eligible[1] if len(eligible) > 1 else None
    close = runner_up is not None and abs(selected.score - runner_up.score) <= policy.close_candidate_score_margin
    middle_confidence = policy.review_confidence <= base_confidence < policy.auto_accept_confidence
    required = close or middle_confidence
    performed: list[dict[str, Any]] = []
    insufficient = False
    if required:
        performed.extend(
            [
                {"check": "content_language_confidence", "passed": selected.language_confidence >= policy.auto_accept_confidence},
                {"check": "dialogue_completeness", "passed": selected.dialogue_completeness_score >= policy.auto_accept_confidence},
                {"check": "timing_integrity", "passed": selected.valid_timing_ratio >= policy.min_valid_timing_ratio},
                {
                    "check": "special_track_risk",
                    "passed": max(
                        selected.forced_probability,
                        selected.signs_only_probability,
                        selected.songs_only_probability,
                    ) < min(
                        policy.max_forced_probability,
                        policy.max_signs_only_probability,
                        policy.max_songs_only_probability,
                    ),
                },
            ]
        )
        if runner_up is not None:
            priority_resolves = _subtitle_language_priority(selected.detected_language) < _subtitle_language_priority(
                runner_up.detected_language
            )
            score_margin = selected.score - runner_up.score
            materially_better = score_margin > policy.exact_tie_score_epsilon
            performed.append(
                {
                    "check": "close_candidate_disambiguation",
                    "passed": priority_resolves or materially_better,
                    "runner_up": _candidate_key(runner_up),
                    "score_margin": _rounded(score_margin),
                    "priority_resolves": priority_resolves,
                }
            )
            if not (priority_resolves or materially_better):
                insufficient = True
        if middle_confidence and not all(bool(item["passed"]) for item in performed):
            insufficient = True
    post_confidence = base_confidence
    if insufficient:
        post_confidence = min(post_confidence, max(0.0, policy.auto_accept_confidence - 0.01))
    return {
        "required": required,
        "performed": performed,
        "result": "insufficient" if insufficient else ("passed" if required else "not_required"),
        "precheck_confidence": _rounded(base_confidence),
        "postcheck_confidence": _rounded(post_confidence),
    }


def _audio_additional_checks(
    selected: AudioCandidateAnalysis,
    eligible: Sequence[AudioCandidateAnalysis],
    base_confidence: float,
    policy: AnalyzerThresholds,
) -> dict[str, Any]:
    runner_up = eligible[1] if len(eligible) > 1 else None
    close = runner_up is not None and abs(selected.score - runner_up.score) <= policy.close_candidate_score_margin
    middle_confidence = policy.review_confidence <= base_confidence < policy.auto_accept_confidence
    required = close or middle_confidence
    performed: list[dict[str, Any]] = []
    insufficient = False
    if required:
        performed.extend(
            [
                {"check": "japanese_language_confidence", "passed": selected.language_confidence >= policy.auto_accept_confidence},
                {"check": "audio_duration", "passed": selected.duration_ratio >= policy.min_audio_duration_ratio},
                {"check": "not_commentary", "passed": not selected.commentary},
            ]
        )
        if runner_up is not None:
            score_margin = selected.score - runner_up.score
            materially_better = score_margin > policy.exact_tie_score_epsilon
            performed.append(
                {
                    "check": "close_candidate_disambiguation",
                    "passed": materially_better,
                    "runner_up": _candidate_key(runner_up),
                    "score_margin": _rounded(score_margin),
                }
            )
            if not materially_better:
                insufficient = True
        if middle_confidence and not all(bool(item["passed"]) for item in performed):
            insufficient = True
    post_confidence = base_confidence
    if insufficient:
        post_confidence = min(post_confidence, max(0.0, policy.auto_accept_confidence - 0.01))
    return {
        "required": required,
        "performed": performed,
        "result": "insufficient" if insufficient else ("passed" if required else "not_required"),
        "precheck_confidence": _rounded(base_confidence),
        "postcheck_confidence": _rounded(post_confidence),
    }


def _review_decision(
    candidates: tuple[CandidateAnalysis, ...],
    confidence: float,
    reason_code: str,
    evidence: Mapping[str, Any],
) -> SourceDecision:
    unmarked = tuple(replace(item, selected=False) for item in candidates)
    return SourceDecision(
        strategy=NEEDS_REVIEW,
        confidence=_clamp(confidence),
        reason_code=reason_code,
        evidence=evidence,
        selected_subtitle_track=None,
        selected_audio_track=None,
        candidates=unmarked,
        unselected_reasons=_unselected_reasons(unmarked, None),
    )


def _unselected_reasons(
    candidates: Sequence[CandidateAnalysis],
    selected: CandidateAnalysis | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    results: list[tuple[str, tuple[str, ...]]] = []
    for candidate in candidates:
        if selected is not None and candidate.kind == selected.kind and candidate.index == selected.index:
            continue
        reasons = list(candidate.rejection_reasons)
        if not reasons:
            if selected is None:
                reasons.append("decision_not_auto_accepted")
            elif candidate.kind == "audio" and selected.kind == "subtitle":
                reasons.append("usable_subtitle_precedes_audio")
            elif candidate.kind == "subtitle" and selected.kind == "subtitle":
                candidate_priority = _subtitle_language_priority(candidate.detected_language)
                selected_priority = _subtitle_language_priority(selected.detected_language)
                if candidate_priority > selected_priority:
                    reasons.append("lower_language_priority")
                else:
                    reasons.append("lower_candidate_score")
            else:
                reasons.append("lower_candidate_score")
        results.append((_candidate_key(candidate), tuple(dict.fromkeys(reasons))))
    return tuple(sorted(results))


def _strategy_for_subtitle_language(language: str) -> str:
    mapping = {
        "zh-tw": USE_EXISTING_ZH_TW,
        "zh-hant": NORMALIZE_ZH_HANT,
        "zh-cn": CONVERT_ZH_CN,
        "zh-hans": CONVERT_ZH_CN,
        "ja": TRANSLATE_JA_SUBTITLE,
    }
    return mapping[language]


def _subtitle_decision_confidence(candidate: SubtitleCandidateAnalysis) -> float:
    return _clamp(0.52 * candidate.language_confidence + 0.48 * candidate.dialogue_completeness_score)


def _audio_decision_confidence(candidate: AudioCandidateAnalysis) -> float:
    weighted = _clamp(0.60 * candidate.language_confidence + 0.40 * candidate.duration_ratio)
    # Duration proves usability, not language.  It must never promote weak
    # title/metadata language evidence over the auto-accept threshold.
    return min(candidate.language_confidence, weighted)


def _deduplicate_subtitle_content(
    candidates: Sequence[SubtitleCandidateAnalysis],
) -> tuple[SubtitleCandidateAnalysis, ...]:
    """Reject duplicate semantic subtitle content without creating a false tie.

    The inventory's semantic content digest intentionally survives subtitle
    format changes.  A usable sidecar is preferred because it is independently
    materializable on restart; otherwise candidate quality remains the primary
    safety constraint.  Input order never influences the representative.
    """

    groups: dict[str, list[SubtitleCandidateAnalysis]] = {}
    for candidate in candidates:
        if candidate.content_sha256:
            groups.setdefault(candidate.content_sha256, []).append(candidate)

    replacements: dict[int, SubtitleCandidateAnalysis] = {}
    for digest, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        representative = min(members, key=_duplicate_subtitle_representative_key)
        for candidate in members:
            if candidate.track_index == representative.track_index:
                continue
            evidence = dict(candidate.evidence)
            evidence["duplicate_content"] = {
                "content_sha256": digest,
                "representative": _candidate_key(representative),
            }
            replacements[candidate.track_index] = replace(
                candidate,
                eligible=False,
                rejection_reasons=tuple(
                    dict.fromkeys((*candidate.rejection_reasons, "duplicate_subtitle_content"))
                ),
                evidence=evidence,
            )

    return tuple(replacements.get(candidate.track_index, candidate) for candidate in candidates)


def _duplicate_subtitle_representative_key(
    candidate: SubtitleCandidateAnalysis,
) -> tuple[Any, ...]:
    return (
        0 if candidate.eligible else 1,
        0 if candidate.source_kind.strip().casefold() == "sidecar" else 1,
        _subtitle_language_priority(candidate.detected_language),
        -_rounded(candidate.score),
        -candidate.event_count,
        candidate.track_index,
        candidate.source_reference.casefold(),
        candidate.codec,
        candidate.title.casefold(),
    )


def _subtitle_sort_key(candidate: SubtitleCandidateAnalysis) -> tuple[Any, ...]:
    return (
        0 if candidate.eligible else 1,
        _subtitle_language_priority(candidate.detected_language),
        -_rounded(candidate.score),
        -candidate.event_count,
        candidate.track_index,
        candidate.codec,
        candidate.title.casefold(),
    )


def _audio_sort_key(candidate: AudioCandidateAnalysis) -> tuple[Any, ...]:
    return (
        0 if candidate.eligible else 1,
        0 if candidate.detected_language == "ja" else 1,
        -_rounded(candidate.score),
        candidate.track_index,
        candidate.codec,
        candidate.title.casefold(),
    )


def _subtitle_language_priority(language: str) -> int:
    return {"zh-tw": 0, "zh-hant": 1, "zh-cn": 2, "zh-hans": 2, "ja": 3}.get(language, 9)


def _title_language(title: str) -> str:
    lowered = str(title or "").strip().casefold().replace("_", "-")
    if any(marker in lowered for marker in ("zh-tw", "taiwan", "taiwanese", "臺灣", "台灣")):
        return "zh-tw"
    if any(marker in lowered for marker in ("zh-hant", "traditional", "繁體", "繁体", "cht")):
        return "zh-hant"
    if any(marker in lowered for marker in ("zh-cn", "simplified", "简体", "簡體", "chs")):
        return "zh-cn"
    if any(marker in lowered for marker in ("zh-hans",)):
        return "zh-hans"
    if any(marker in lowered for marker in ("japanese", "jpn", "日本語", "日語", "日语")):
        return "ja"
    if any(marker in lowered for marker in ("chinese", "中文", "chi", "zho")):
        return "zh"
    return "und"


def _language_family(language: str) -> str:
    if language in {"zh-tw", "zh-hant"}:
        return "zh-hant"
    if language in {"zh-cn", "zh-hans"}:
        return "zh-hans"
    return language


def _plain_subtitle_text(value: str) -> str:
    dialogue_lines: list[str] = []
    fallback_lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        if lowered.startswith("dialogue:"):
            payload = line.split(":", 1)[1].lstrip()
            parts = payload.split(",", 9)
            dialogue_lines.append(parts[9] if len(parts) >= 10 else parts[-1])
            continue
        if line.isdigit() or _SRT_TIMING_RE.match(line):
            continue
        if lowered.startswith(("[", "format:", "style:", "comment:", "note:", "webvtt")):
            continue
        fallback_lines.append(line)
    lines = dialogue_lines or fallback_lines
    cleaned: list[str] = []
    for line in lines:
        text = line.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        text = _ASS_OVERRIDE_RE.sub("", text)
        text = _HTML_TAG_RE.sub("", text)
        cleaned.extend(part.strip() for part in text.splitlines() if part.strip())
    return "\n".join(cleaned)


def _coverage_ratio(start: float | None, end: float | None, duration: float | None) -> float:
    start_value = _nonnegative_float(start)
    end_value = _nonnegative_float(end)
    if start_value is None or end_value is None or duration is None or end_value <= start_value:
        return 0.0
    return _clamp((end_value - start_value) / duration)


def _ratio(numerator: int | float, denominator: int | float) -> float:
    try:
        if float(denominator) <= 0:
            return 0.0
        return _clamp(float(numerator) / float(denominator))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _is_han(character: str) -> bool:
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _is_kana(character: str) -> bool:
    code = ord(character)
    return 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF


def _is_meaningful_character(character: str) -> bool:
    return character.isalnum() or _is_han(character) or _is_kana(character)


def _coerce_subtitle(value: SubtitleCandidateInput | Mapping[str, Any]) -> SubtitleCandidateInput:
    if isinstance(value, SubtitleCandidateInput):
        return value
    if isinstance(value, Mapping):
        return SubtitleCandidateInput.from_mapping(value)
    raise TypeError(f"unsupported subtitle candidate type: {type(value).__name__}")


def _coerce_audio(value: AudioCandidateInput | Mapping[str, Any]) -> AudioCandidateInput:
    if isinstance(value, AudioCandidateInput):
        return value
    if isinstance(value, Mapping):
        return AudioCandidateInput.from_mapping(value)
    raise TypeError(f"unsupported audio candidate type: {type(value).__name__}")


def _require_unique_indices(candidates: Sequence[Any], kind: str) -> None:
    indices = [int(candidate.track_index) for candidate in candidates]
    if len(indices) != len(set(indices)):
        raise ValueError(f"duplicate {kind} track index in candidate inventory")


def _candidate_key(candidate: CandidateAnalysis) -> str:
    return f"{candidate.kind}:{candidate.index}"


def _canonical_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support non-finite floats")
        return _rounded(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_data(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_data(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_canonical_data(item) for item in value]
        return sorted(converted, key=canonical_json)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _canonical_data(to_dict())
    if is_dataclass(value):
        return _canonical_data({field.name: getattr(value, field.name) for field in fields(value)})
    raise TypeError(f"value is not canonical JSON serializable: {type(value).__name__}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _as_bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _as_int(value, 0)


def _strict_optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be null or a non-negative integer")
    return value


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_float(value: Any) -> float | None:
    parsed = _as_optional_float(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def _nonnegative_float(value: Any) -> float | None:
    parsed = _as_optional_float(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _probability(value: Any) -> float:
    parsed = _as_optional_float(value)
    return _clamp(parsed if parsed is not None else 0.0)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _rounded_optional(value: float | None) -> float | None:
    return None if value is None else _rounded(value)
