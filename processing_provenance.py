from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
import hashlib
import json
import time
from typing import Any

from safe_files import atomic_write_text
from acceptance_queue_lane import acceptance_run_id_for_video


PROVENANCE_SCHEMA_VERSION = 1


class ProvenanceRecorder:
    def __init__(self, config: Any, video_path: str | Path) -> None:
        self.config = config
        self.video = Path(video_path)
        self.path = provenance_path_for_video(config, self.video)
        current = self._initial_payload()
        existing = _read_json(self.path)
        self.payload = (
            existing
            if _provenance_identity_matches(existing, current)
            else current
        )
        self.payload["run_started_at"] = time.time()
        self.payload["status"] = "running"
        self._write()

    def record_stage(self, stage: str, status: str, message: str = "") -> None:
        now = time.time()
        stages = self.payload.setdefault("stages", [])
        if stages and stages[-1].get("stage") == stage and stages[-1].get("status") == status:
            stages[-1].update({"message": message, "updated_at": now})
        else:
            stages.append({"stage": stage, "status": status, "message": message, "updated_at": now})
        if len(stages) > 300:
            del stages[:-300]
        self.payload["current_stage"] = stage
        self.payload["current_stage_status"] = status
        self.payload["updated_at"] = now
        self._write()

    def update(self, section: str, value: Any) -> None:
        self.payload[str(section)] = _json_value(value)
        self.payload["updated_at"] = time.time()
        self._write()

    def merge(self, **values: Any) -> None:
        for key, value in values.items():
            self.payload[str(key)] = _json_value(value)
        self.payload["updated_at"] = time.time()
        self._write()

    def finish(self, *, ok: bool, outcome: Any | None = None, error: BaseException | None = None) -> None:
        now = time.time()
        self.payload["status"] = "complete" if ok else "failed"
        self.payload["finished_at"] = now
        self.payload["updated_at"] = now
        if outcome is not None:
            self.payload["outcome"] = _json_value(outcome)
        if error is not None:
            self.payload["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        self._write()

    def _initial_payload(self) -> dict[str, Any]:
        try:
            stat = self.video.stat()
            video_info = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            video_info = {"size": 0, "mtime_ns": 0}
        payload = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "video_path": str(self.video),
            "video": video_info,
            "config_signature": processing_config_signature(self.config),
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "created",
            "stages": [],
        }
        acceptance_run_id = acceptance_run_id_for_video(self.config, self.video)
        if acceptance_run_id:
            payload["acceptance_run_id"] = acceptance_run_id
        return payload

    def _write(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _provenance_identity_matches(
    existing: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    """Resume only an exact current media and processing-policy identity."""

    if not isinstance(existing, dict):
        return False
    return (
        existing.get("schema_version") == current["schema_version"]
        and existing.get("video_path") == current["video_path"]
        and existing.get("video") == current["video"]
        and existing.get("config_signature") == current["config_signature"]
        and existing.get("acceptance_run_id") == current.get("acceptance_run_id")
    )


def provenance_path_for_video(config: Any, video_path: str | Path) -> Path:
    video = Path(video_path)
    configured = Path(str(getattr(config, "processing_provenance_path", "provenance") or "provenance"))
    root = configured if configured.is_absolute() else Path(config.work_path) / configured
    digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:20]
    return root / f"{digest}.json"


def processing_config_signature(config: Any) -> str:
    from source_decision import SOURCE_DECISION_CONTRACT
    from subtitle_remediation import SUBTITLE_REMEDIATION_CONTRACT
    from translation_memory import (
        TRANSLATION_MEMORY_CONTEXT_CONTRACT,
        TRANSLATION_MEMORY_SCHEMA_VERSION,
    )
    from translation_memory_bridge import (
        TRANSLATION_MEMORY_LINEAGE_CONTRACT,
        TRANSLATION_MEMORY_SPLIT_CONTRACT,
    )
    from translation_memory_outbox import TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION
    from translator import TRANSLATION_PROMPT_VERSION

    fields = (
        "transcription_backend",
        "japanese_transcription_backend",
        "japanese_transcription_fallback_backend",
        "japanese_transcription_final_fallback_backend",
        "non_japanese_transcription_backend",
        "non_japanese_transcription_fallback_backend",
        "non_japanese_transcription_final_fallback_backend",
        "whisper_model",
        "japanese_transcription_model",
        "japanese_transcription_fallback_model",
        "japanese_transcription_fallback_compute_type",
        "japanese_transcription_final_fallback_model",
        "japanese_transcription_final_fallback_compute_type",
        "non_japanese_transcription_model",
        "non_japanese_transcription_fallback_model",
        "non_japanese_transcription_fallback_compute_type",
        "non_japanese_transcription_final_fallback_model",
        "non_japanese_transcription_final_fallback_compute_type",
        "whisper_compute_type",
        "whisper_language",
        "whisper_task",
        "whisper_vad_filter",
        "whisper_condition_on_previous_text",
        "whisper_temperature",
        "whisper_beam_size",
        "whisper_best_of",
        "whisper_initial_prompt",
        "whisper_no_speech_threshold",
        "whisper_log_prob_threshold",
        "whisper_compression_ratio_threshold",
        "transcription_quality_check_enabled",
        "transcription_quality_min_coverage_percent",
        "transcription_quality_min_blocks_per_minute",
        "transcription_quality_min_avg_logprob",
        "transcription_quality_max_low_confidence_percent",
        "transcription_quality_max_leading_gap_seconds",
        "translator_model",
        "batch_size",
        "translation_max_line_chars",
        "translation_max_line_expansion_ratio",
        "translation_reject_residual_kana",
        "translation_metadata_context_enabled",
        "translation_context_enabled",
        "metadata_context_max_chars",
        "translation_glossary",
        "translation_memory_enabled",
        "translation_memory_auto_apply_enabled",
        "opencc_config",
        "subtitle_quality_check_enabled",
        "subtitle_quality_fail_job",
        "subtitle_quality_max_duration_seconds",
        "subtitle_quality_max_primary_chars",
        "subtitle_quality_hard_max_primary_chars",
        "subtitle_quality_max_gap_seconds",
        "subtitle_quality_max_leading_gap_seconds",
        "subtitle_quality_warn_cps",
        "subtitle_quality_fail_cps",
        "subtitle_quality_min_duration_seconds",
        "subtitle_quality_hard_min_duration_seconds",
        "subtitle_quality_max_overlap_seconds",
        "subtitle_remediation_punctuation_repeat_limit",
        "subtitle_remediation_wrap_max_chars",
        "subtitle_remediation_max_visual_lines",
        "subtitle_remediation_max_timing_shift_seconds",
        "subtitle_remediation_max_total_timing_shift_seconds",
        "subtitle_remediation_max_overlap_repair_seconds",
        "language_gate_enabled",
        "allowed_source_languages",
        "skip_non_allowed_language",
        "transcribe_non_allowed_languages",
        "translate_non_japanese_sources",
        "language_detect_model",
        "language_detect_min_probability",
        "language_detect_sample_count",
        "language_detect_sample_seconds",
        "language_uncertain_policy",
        "audio_content_probe_enabled",
        "audio_content_probe_max_streams",
        "audio_content_probe_sample_count",
        "audio_content_probe_sample_seconds",
        "force_ai_bypass_language_gate",
        "resource_admission_enabled",
        "resource_admission_telemetry_stale_seconds",
        "resource_admission_cpu_yellow_percent",
        "resource_admission_cpu_red_percent",
        "resource_admission_ram_yellow_available_ratio",
        "resource_admission_ram_red_available_ratio",
        "resource_admission_gpu_yellow_percent",
        "resource_admission_gpu_red_percent",
        "resource_admission_vram_reserve_mib",
        "resource_admission_primary_vram_mib",
        "resource_admission_lower_memory_vram_mib",
        "resource_admission_recovery_samples",
    )
    payload = {field: _json_value(getattr(config, field, None)) for field in fields}
    payload["translation_prompt_version"] = TRANSLATION_PROMPT_VERSION
    payload["subtitle_source_decision_contract"] = SOURCE_DECISION_CONTRACT
    payload["subtitle_remediation_contract"] = SUBTITLE_REMEDIATION_CONTRACT
    payload["translation_memory_schema_version"] = TRANSLATION_MEMORY_SCHEMA_VERSION
    payload["translation_memory_context_contract"] = TRANSLATION_MEMORY_CONTEXT_CONTRACT
    payload["translation_memory_split_contract"] = TRANSLATION_MEMORY_SPLIT_CONTRACT
    payload["translation_memory_lineage_contract"] = TRANSLATION_MEMORY_LINEAGE_CONTRACT
    payload["translation_memory_outbox_schema_version"] = TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prompt_signature(*prompts: str) -> str:
    raw = "\n\0\n".join(prompts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_provenance(config: Any, video_path: str | Path) -> dict[str, Any] | None:
    return _read_json(provenance_path_for_video(config, video_path))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
