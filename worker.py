from __future__ import annotations

from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import shutil
import time
from types import SimpleNamespace

from ai_failure_markers import clear_ai_failure_marker, mark_ai_failure
from audio import (
    AudioStreamInfo,
    audio_cache_metadata_path,
    extract_audio,
    has_japanese_audio_stream,
    preferred_audio_stream_info,
    probe_audio_streams,
    separate_vocals,
    validate_cached_audio,
)
from ass_utils import (
    ass_style_from_config,
    convert_ass_file_to_srt,
    convert_bilingual_srt_files_to_ass,
    convert_srt_file_to_ass,
    restyle_ass_file,
)
from asr_review_checkpoint import (
    create_asr_review_checkpoint,
)
from config import AppConfig
from control_state import (
    list_open_ai_quality_review_targets,
    resolve_ai_quality_reviews_for_target_if_idle,
    upsert_review_item,
)
from gpu_memory import is_cuda_oom
from language_detector import (
    LanguageDetector,
    LanguageDetectionResult,
    format_language_result,
    format_language_skip,
    should_fail_for_language,
    should_skip_for_language,
)
from lock import VideoLock
from logger import log_failure
from metadata_context import MetadataContext, build_series_metadata_context
from notifications import notify_event
from opencc_convert import convert_ass_to_zh_tw, convert_srt_to_zh_tw
from output_manifest import (
    ADOPTED_ZH_TW_PUBLICATION_KIND,
    CONVERTED_ZH_CN_PUBLICATION_KIND,
    SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT,
    TRANSLATED_PUBLICATION_KIND,
    begin_output_publication,
    delivery_identity,
    finish_output_publication,
    manifest_publication_semantics,
    output_manifest_path,
    output_publication_marker_path,
    publication_is_traditional_chinese_delivery,
    remove_output_manifest,
    validate_output_manifest,
    write_output_manifest,
)
from processing_provenance import (
    ProvenanceRecorder,
    processing_config_signature,
    prompt_signature,
)
from safe_files import (
    atomic_write_bytes,
    atomic_write_text,
    sha256_file,
    verified_copy_replace,
    verified_move,
)
from srt_utils import (
    SrtBlock,
    read_srt,
    validate_same_numbering,
    validate_srt_structure,
    validate_translation,
    write_srt,
)
from source_decision import (
    CONVERT_ZH_CN,
    TRANSLATE_JAPANESE,
    USE_ZH_TW,
    SubtitleSourceDecision,
    discover_normalized_subtitle_source,
)
from series_metadata import (
    canonical_local_path,
    season_number_for_video,
    series_root_for_video,
    stable_series_id,
)
from mikan_source import extract_episode_number
from subtitle_extract import classify_subtitle_content_file, remove_ai_srt_outputs
from subtitle_quality import (
    SubtitleQualityError,
    SubtitleQualityReport,
    add_translation_quality_events,
    analyze_subtitle_file,
    managed_quality_report_path,
    quality_report_candidates,
    quality_report_path,
    summarize_quality_report,
    write_quality_report,
)
from subtitle_remediation import (
    RemediationRoundLimitError,
    SubtitleRemediationError,
    next_remediation_round,
    remediate_srt,
    remediate_srt_in_place,
)
from translation_quality import (
    TRANSLATION_SAFE_OMISSION,
    fail_closed_translation_output,
    read_translation_quality_events,
    read_translation_quality_events_strict,
    read_translation_quality_hold_strict,
    translation_quality_events_path,
    translation_quality_hold_path,
    write_translation_quality_events,
    write_translation_quality_hold,
)
from translation_memory import MemoryScope
from translation_memory_bridge import (
    TRANSLATION_MEMORY_LINEAGE_CONTRACT,
    TranslationMemoryBridgeError,
    TranslationMemoryOrigin,
    read_translation_memory_origin_strict,
    remove_translation_memory_origin,
    split_blocks_by_readonly_translation_memory,
    translation_memory_full_plan_digest,
    translation_memory_split_digest,
    write_translation_memory_origin,
)
from translation_memory_outbox import (
    RecordedTranslationMemoryOutboxIntent,
    TranslationMemoryOutboxError,
    record_translation_memory_outbox_intent,
)
from subtitle_paths import (
    SourceTranscriptPaths,
    SubtitlePaths,
    has_ai_finished_subtitle,
    has_finished_subtitle,
    paths_for_video,
    source_transcript_paths_for_video,
)
from transcriber import (
    AsrSelectiveRepairUnavailableError,
    LowConfidenceTranscriptionError,
    TranscriptionError,
    _clean_transcribed_text,
    _is_hallucination_text,
    _normalize_review_ranges,
    _srt_timing_seconds,
    asr_audio_stream_fingerprint,
    attach_asr_diagnostics_context,
    asr_diagnostics_path,
    asr_file_fingerprint,
    asr_transcription_hold_path,
    claim_asr_repair_attempt,
    finalize_repaired_transcription,
    promote_asr_diagnostics,
    read_asr_diagnostics,
    repair_low_confidence_ranges,
    transcribe_to_srt,
    validate_transcription_srt_quality,
    verify_asr_diagnostics_context,
    write_asr_acceptance_diagnostics,
)
from translator import (
    KANA_REPAIR_SYSTEM_PROMPT,
    LINE_TRANSLATION_SYSTEM_PROMPT,
    REPETITIVE_LINE_REPAIR_SYSTEM_PROMPT,
    SINGLE_LINE_REPAIR_SYSTEM_PROMPT,
    STANDALONE_KANA_REPAIR_SYSTEM_PROMPT,
    TRANSLATION_CONTEXT_SYSTEM_PROMPT,
    TRANSLATION_PROMPT_VERSION,
    AsrReviewError,
    SubtitleTranslator,
)
from whisper_runtime import clear_whisper_model_cache
from acceptance_runtime import AcceptanceAttemptContext


LEADING_GAP_CACHE_POLICY_VERSION = 1


@dataclass(frozen=True)
class ProcessOutcome:
    stage: str = "complete"
    status: str = "ok"
    message: str = "Finished video"


@dataclass(frozen=True)
class AsrRouteOutcome:
    backend: str
    model: str
    fallback_used: bool = False
    failed_model: str | None = None
    failed_reason: str | None = None


class VideoWorker:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        *,
        acceptance_attempt_context: AcceptanceAttemptContext | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._translator: SubtitleTranslator | None = None
        self._ass_style = ass_style_from_config(config)
        self._stage_state = None
        self._translator_progress_video: Path | None = None
        self._active_transcription_video: Path | None = None
        self._suppress_asr_review_checkpoint_capture = False
        self._last_asr_route: AsrRouteOutcome | None = None
        self._selected_audio_stream: AudioStreamInfo | None = None
        self._audio_selection_payload: dict[str, object] = {}
        self._provenance: ProvenanceRecorder | None = None
        self._subtitle_source_decision: SubtitleSourceDecision | None = None
        self._translation_memory_manual_run = False
        self._resource_launch_plan: dict[str, object] | None = None
        self._acceptance_attempt_context = acceptance_attempt_context

    @property
    def acceptance_attempt_context(self) -> AcceptanceAttemptContext | None:
        """Immutable child-verified acceptance identity, absent in production."""

        return self._acceptance_attempt_context

    def process(self, video_path: str | Path) -> bool:
        video = Path(video_path)
        self._selected_audio_stream = None
        self._audio_selection_payload = {}
        self._provenance = None
        self._subtitle_source_decision = None
        self._translation_memory_manual_run = False
        self._resource_launch_plan = self._load_resource_launch_plan(video)
        lock = VideoLock(video)
        if not lock.acquire():
            try:
                wait_seconds = max(0.0, float(getattr(self.config, "video_lock_wait_seconds", 30.0) or 0))
            except (TypeError, ValueError):
                wait_seconds = 30.0
            deadline = time.monotonic() + wait_seconds
            self.logger.info(
                "Video is locked by another AI or official-subtitle operation; waiting up to %ss: %s",
                int(wait_seconds),
                video,
            )
            while wait_seconds > 0 and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                if lock.acquire():
                    break
            if not lock.acquired:
                self.logger.info("Skip locked video after wait timeout: %s", video)
                return False

        audio_path = self._audio_path(video)
        separated_audio_path = self._separated_audio_path(video)
        resource_lease = None
        try:
            try:
                self._provenance = ProvenanceRecorder(self.config, video)
                self._provenance.merge(
                    translation={
                        "model": getattr(self.config, "translator_model", ""),
                        "prompt_version": TRANSLATION_PROMPT_VERSION,
                        "prompt_signature": prompt_signature(
                            LINE_TRANSLATION_SYSTEM_PROMPT,
                            KANA_REPAIR_SYSTEM_PROMPT,
                            STANDALONE_KANA_REPAIR_SYSTEM_PROMPT,
                            SINGLE_LINE_REPAIR_SYSTEM_PROMPT,
                            REPETITIVE_LINE_REPAIR_SYSTEM_PROMPT,
                            TRANSLATION_CONTEXT_SYSTEM_PROMPT,
                        ),
                    }
                )
                if self._resource_launch_plan is not None:
                    self._provenance.update("resource_admission", self._resource_launch_plan)
            except OSError as exc:
                self.logger.warning("Unable to initialize processing provenance video=%s error=%s", video, exc)
            if bool(getattr(self.config, "resource_admission_enabled", False)):
                from gpu_lease import GpuKernelLease, GpuLeaseBusy
                from resource_runtime import validate_authorized_resource_launch_plan

                configured_lease = Path(
                    str(getattr(self.config, "resource_gpu_lease_path", "gpu_leases/gpu-0.lock"))
                )
                lease_path = (
                    configured_lease
                    if configured_lease.is_absolute()
                    else Path(self.config.work_path) / configured_lease
                )
                if self._resource_launch_plan is None:
                    raise RuntimeError("Resource admission is enabled but no launch plan was loaded")
                expires_at = float(self._resource_launch_plan.get("expires_at") or 0.0)
                remaining_plan_seconds = expires_at - time.time()
                if remaining_plan_seconds <= 0:
                    raise RuntimeError("Resource launch plan expired before GPU lease acquisition")
                configured_wait_seconds = float(
                    getattr(self.config, "resource_gpu_lease_wait_seconds", 7200.0) or 7200.0
                )
                resource_lease = GpuKernelLease(
                    lease_path.parent,
                    "single-gpu-0",
                    # A child must never wait past the admission snapshot's
                    # validity and then launch work under stale telemetry.
                    timeout_seconds=min(configured_wait_seconds, remaining_plan_seconds),
                )
                if not resource_lease.acquire():
                    raise GpuLeaseBusy("GPU resource lease remained busy beyond the bounded wait")
                # Recheck both freshness and decision identity at the exact
                # authority boundary, after contention and before any GPU work.
                self._resource_launch_plan = validate_authorized_resource_launch_plan(
                    self.config,
                    self._resource_launch_plan,
                    video,
                )
                resource_lease.heartbeat(phase="video_pipeline")
            self._set_stage(video, "worker", "running", "Locked video")
            outcome = self._process_locked(video, audio_path, separated_audio_path)
            self._deliver_completed_media_if_required(video)
            self._set_stage(video, outcome.stage, outcome.status, outcome.message)
            clear_ai_failure_marker(self.config, video)
            if self._provenance is not None:
                self._provenance.finish(ok=True, outcome=outcome)
            self._resolve_completed_ai_quality_reviews(video, outcome)
            return True
        except Exception as exc:
            if bool(getattr(self.config, "resource_admission_enabled", False)) and is_cuda_oom(exc):
                try:
                    from resource_runtime import record_resource_oom

                    record_resource_oom(self.config, video, str(exc))
                except Exception as resource_error:  # noqa: BLE001 - preserve the processing failure.
                    self.logger.warning(
                        "Unable to persist resource OOM cooldown video=%s error=%s",
                        video,
                        resource_error,
                    )
            asr_review_required = self._requires_asr_review(exc)
            if asr_review_required:
                archive_reason = self._asr_review_archive_reason(exc)
                asr_context = self._asr_review_context(video, exc)
                try:
                    self._archive_asr_review_outputs(video, reason=archive_reason)
                except Exception as archive_error:  # noqa: BLE001 - preserve the original ASR review failure.
                    self.logger.warning("Failed to archive ASR review outputs for %s: %s", video, archive_error)
                self._create_review_item(
                    video,
                    kind="asr_quality",
                    summary="Source transcription requires evidence-gated review",
                    diagnosis={
                        "error": str(exc),
                        "stage": "transcription_review",
                        "reason": archive_reason,
                        **asr_context,
                    },
                    candidates=self._asr_review_candidates(asr_context),
                    replace_candidates=True,
                )
            omission_indexes = self._translation_safe_omission_review_indexes(exc)
            if omission_indexes:
                line_spec = ",".join(str(index) for index in omission_indexes)
                self._create_review_item(
                    video,
                    kind="subtitle_quality",
                    summary=str(exc),
                    diagnosis={
                        "error": str(exc),
                        "stage": "quality_check",
                        "reports": [
                            {
                                "role": "translated",
                                "issues": [
                                    {
                                        "code": TRANSLATION_SAFE_OMISSION,
                                        "severity": "fail",
                                        "indexes": omission_indexes,
                                    }
                                ],
                            }
                        ],
                    },
                    candidates=[
                        {
                            "action": "ai.retranslate_lines",
                            "label": "Re-translate only confirmed omitted lines",
                            "lines": line_spec,
                            "indexes": omission_indexes,
                            "selective": True,
                        }
                    ],
                    replace_candidates=True,
                )
            stage = self._stage_for_exception(exc)
            self._set_stage(video, stage, "failed", str(exc))
            if stage in {"transcription", "translation"}:
                mark_ai_failure(self.config, video, stage, exc)
            log_failure(self.config.log_path, video, stage, exc)
            notify_event(
                self.config,
                "asr_review" if stage == "transcription_review" else "ai_failure",
                "AI 字幕需要人工確認" if stage == "transcription_review" else "AI 字幕處理失敗",
                str(exc),
                severity="warning" if stage == "transcription_review" else "error",
                key=str(video),
                details={"video": str(video), "stage": stage},
            )
            if self._provenance is not None:
                try:
                    self._provenance.finish(ok=False, error=exc)
                except OSError:
                    pass
            return False
        finally:
            # Audio is always temporary. Clean it on success, skip, and every
            # failure path so an interrupted ASR/translation cannot slowly fill
            # the work volume with multi-gigabyte WAV files.
            self._cleanup_audio_files(audio_path, separated_audio_path)
            self._release_translator_models()
            if resource_lease is not None and resource_lease.acquired:
                self._release_resource_lease(resource_lease)
            self._close_stage_state()
            lock.release()

    def _release_translator_models(self) -> None:
        if not bool(getattr(self.config, "translator_ollama_auto_unload_enabled", False)):
            return
        translator = self._translator
        self._translator = None
        if translator is None:
            return
        try:
            translator.unload_requested_models()
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the processing result.
            self.logger.warning("Unable to release managed translation models after job: %s", exc)

    def _release_resource_lease(self, resource_lease) -> None:
        """Clear process-local VRAM and always release kernel authority."""

        try:
            # The kernel lease cannot be released while a process-local model
            # cache still owns VRAM. Isolated mode normally exits immediately,
            # but clearing explicitly also keeps failure paths honest.
            clear_whisper_model_cache(logger=self.logger)
        finally:
            resource_lease.release()

    def _deliver_completed_media_if_required(self, video: Path) -> None:
        from completed_delivery import completed_delivery_enabled, deliver_completed_mkv

        if not completed_delivery_enabled(self.config):
            return
        # Only a strict Traditional-Chinese publication is a user-facing
        # subtitle delivery. Source-language diagnostics must never be muxed as
        # if they satisfied the Chinese product contract.
        if not self._has_strict_chinese_publication(video, force_ai=False):
            return
        self._set_stage(video, "mux", "running", "Muxing verified subtitles into completed MKV")
        result = deliver_completed_mkv(video, self.config, logger=self.logger)
        if self._provenance is not None:
            self._provenance.update("completed_delivery", result.to_dict())
        self._set_stage(
            video,
            "move_completed",
            "ok",
            f"Completed MKV committed with source retained: {result.destination}",
        )

    def _resolve_completed_ai_quality_reviews(
        self,
        video: Path,
        outcome: ProcessOutcome,
    ) -> list[str]:
        """Clear stale AI reviews only after this run published verified output."""

        if (
            outcome.stage not in {"complete", "source_translation"}
            or outcome.status != "ok"
            or not bool(getattr(self.config, "export_ai_ass", False))
        ):
            return []
        try:
            if not has_ai_finished_subtitle(video, self.config):
                return []
            resolved = resolve_ai_quality_reviews_for_target_if_idle(
                self.config,
                str(video.resolve()),
                {
                    "action": "auto_resolve_ai_quality",
                    "source": "worker",
                    "reason": "quality_gate_and_publication_succeeded",
                    "video": str(video),
                    "outcome_stage": outcome.stage,
                    "outcome_status": outcome.status,
                },
            )
        except Exception as exc:  # noqa: BLE001 - review cleanup cannot invalidate published subtitles.
            self.logger.warning(
                "Published AI subtitle but could not resolve stale quality reviews: "
                "video=%s error=%s",
                video,
                exc,
            )
            return []
        if resolved:
            self.logger.info(
                "Resolved stale AI quality reviews after verified publication: video=%s reviews=%s",
                video,
                resolved,
            )
        return resolved

    def reconcile_published_ai_quality_reviews(self, *, limit: int = 2000) -> dict[str, int]:
        """Close stale AI reviews only when a newer strict publication exists.

        This is a bounded startup crash-recovery pass, not a media-library
        scan.  It reads compact review target rows and local manifest metadata
        first; subtitle hashes are verified only for manifests newer than the
        open review.  Source-pairing ambiguity is never selected.
        """

        stats = {
            "examined": 0,
            "resolved": 0,
            "missing": 0,
            "stale": 0,
            "invalid": 0,
            "errors": 0,
        }
        input_root = Path(self.config.input_path).resolve()
        for item in list_open_ai_quality_review_targets(self.config, limit=limit):
            stats["examined"] += 1
            try:
                video = Path(str(item.get("target_key") or "")).resolve()
                video.relative_to(input_root)
                if not video.is_file():
                    stats["missing"] += 1
                    continue
                manifest = output_manifest_path(video, self.config)
                if not manifest.is_file():
                    stats["missing"] += 1
                    continue
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                delivery = payload.get("delivery") if isinstance(payload, dict) else None
                verified_at = float(delivery.get("verified_at") or 0) if isinstance(delivery, dict) else 0.0
                if verified_at < float(item.get("latest_review_at") or 0):
                    stats["stale"] += 1
                    continue
                if verified_at <= 0 or not has_ai_finished_subtitle(video, self.config):
                    stats["invalid"] += 1
                    continue
                resolved = resolve_ai_quality_reviews_for_target_if_idle(
                    self.config,
                    str(video),
                    {
                        "action": "startup_reconcile_ai_quality",
                        "source": "worker_startup",
                        "reason": "quality_gate_and_publication_succeeded",
                        "video": str(video),
                        "manifest_path": str(manifest),
                        "manifest_verified_at": verified_at,
                    },
                )
                stats["resolved"] += len(resolved)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                stats["errors"] += 1
                self.logger.warning(
                    "Could not reconcile one published AI quality review target: target=%s error=%s",
                    item.get("target_key"),
                    exc,
                )
        return stats

    def _process_locked(self, video: Path, audio_path: Path, separated_audio_path: Path) -> ProcessOutcome:
        paths = paths_for_video(video, self.config)
        force_ai = self._force_ai_requested(video)
        self._translation_memory_manual_run = force_ai or self._manual_ai_requested(video)
        self._migrate_legacy_ai_srt_paths(video, paths)
        self._recover_interrupted_output_publication(video, paths)
        if self.config.export_ai_ass:
            self._normalize_existing_ai_ass_names(paths)

        self._set_stage(video, "preflight", "running", "Checking existing subtitles and AI policy")
        if self._has_strict_chinese_publication(video, force_ai=force_ai):
            message = "Required AI subtitle already exists" if force_ai else "Required subtitle already exists"
            self._set_stage(video, "preflight", "ok", message)
            self._set_stage(video, "skipped", "ok", message)
            removed = remove_ai_srt_outputs(video, self.config)
            if removed:
                self.logger.info("Removed AI SRT intermediates after ASS output exists: video=%s count=%s", video, len(removed))
            self.logger.info("Skip finished video: %s", video)
            self._close_stage_state()
            return ProcessOutcome("skipped", "ok", message)

        # Source priority is an input-selection contract, not a cache-skip
        # preference.  Even an explicit force-AI queue item must consume a
        # usable subtitle before touching audio; force only controls whether an
        # older generated delivery may be reused.
        source_decision = discover_normalized_subtitle_source(video, self.config)
        if source_decision is not None:
            self._record_subtitle_source_decision(source_decision)
            if source_decision.strategy == USE_ZH_TW:
                return self._adopt_traditional_chinese_source(video, source_decision)
            if source_decision.strategy == CONVERT_ZH_CN:
                return self._convert_simplified_chinese_source(video, source_decision)

        # Preserve compatibility for old verified AI outputs that predate the
        # strict manifest, but never let a bare zh-CN or source-language artifact
        # suppress the Traditional-Chinese pipeline.
        if self._has_required_finished_subtitle(video, force_ai=force_ai):
            message = "Required AI subtitle already exists" if force_ai else "Required subtitle already exists"
            self._set_stage(video, "preflight", "ok", message)
            self._set_stage(video, "skipped", "ok", message)
            removed = remove_ai_srt_outputs(video, self.config)
            if removed:
                self.logger.info("Removed AI SRT intermediates after ASS output exists: video=%s count=%s", video, len(removed))
            self.logger.info("Skip finished video: %s", video)
            self._close_stage_state()
            return ProcessOutcome("skipped", "ok", message)
        self._set_stage(video, "preflight", "ok", "AI processing required")
        self._recover_pending_asr_commit(video, paths)
        if source_decision is not None and source_decision.strategy == TRANSLATE_JAPANESE:
            self._seed_japanese_subtitle_source(video, paths, source_decision)
        self._restore_japanese_srt_cache_from_ass(paths)
        self._validate_translation_cache_chain(video, paths)

        japanese_subtitle_source = (
            source_decision is not None
            and source_decision.strategy == TRANSLATE_JAPANESE
        )
        if japanese_subtitle_source:
            # The subtitle timeline is already the authoritative Japanese
            # source.  Do not even probe audio-stream metadata or validate an
            # audio cache on this route.
            preferred_audio = None
            audio_ready = False
        else:
            preferred_audio = preferred_audio_stream_info(video)
            audio_ready = validate_cached_audio(
                audio_path,
                video,
                stream_index=preferred_audio.index if preferred_audio else None,
            )
        if audio_ready:
            self._selected_audio_stream = preferred_audio
        language_result: LanguageDetectionResult | None = None
        if (
            bool(getattr(self.config, "language_gate_enabled", False))
            and not paths.zh_cn_srt.exists()
            and not paths.ja_srt.exists()
            and not (
                source_decision is not None
                and source_decision.strategy == TRANSLATE_JAPANESE
            )
        ):
            if force_ai and bool(getattr(self.config, "force_ai_bypass_language_gate", False)):
                language_result = self._detect_source_language(video, audio_path, force_ai=force_ai)
            else:
                if not audio_ready:
                    self._set_stage(video, "audio", "running", "Extracting audio")
                    self.logger.info("Extracting audio: %s", video)
                    self._extract_preferred_audio(video, audio_path)
                    audio_ready = True
                language_result = self._detect_source_language(video, audio_path, force_ai=force_ai)
                language_result = self._recover_japanese_audio_stream(
                    video,
                    audio_path,
                    language_result,
                    force_ai=force_ai,
                )
            if language_result is not None and should_fail_for_language(language_result, self.config):
                message = format_language_skip(language_result, self.config)
                self._set_stage(video, "language_uncertain", "failed", message)
                raise TranscriptionError(message)
            if (
                language_result is not None
                and not bool(getattr(language_result, "confident", False))
                and str(getattr(self.config, "language_uncertain_policy", "skip")).lower() == "continue"
                and not _uncertain_language_has_japanese_evidence(
                    language_result,
                    self.config,
                    video,
                    self._selected_audio_stream,
                )
            ):
                message = (
                    f"{format_language_skip(language_result, self.config)} "
                    "decision=insufficient_japanese_evidence"
                )
                self._set_stage(video, "language_uncertain", "skipped", message)
                self.logger.info(
                    "Skip uncertain source language without Japanese evidence: "
                    "video=%s %s",
                    video,
                    message,
                )
                self._close_stage_state()
                return ProcessOutcome("language_uncertain", "skipped", message)
            should_source_transcribe = _should_transcribe_non_allowed_language(language_result, self.config)
            if language_result is not None and should_skip_for_language(language_result, self.config) and not should_source_transcribe:
                message = format_language_skip(language_result, self.config)
                self._set_stage(video, _language_gate_stage(language_result), "skipped", message)
                self.logger.info("Skip source language gate: video=%s %s", video, message)
                self._close_stage_state()
                return ProcessOutcome(_language_gate_stage(language_result), "skipped", message)

        source_language = _source_transcription_language(language_result, self.config)
        if source_language is not None:
            if not audio_ready:
                self._set_stage(video, "audio", "running", "Extracting audio")
                self.logger.info("Extracting audio: %s", video)
                self._extract_preferred_audio(video, audio_path)
                audio_ready = True
            return self._process_source_transcription(video, audio_path, source_language)

        if (
            paths.ja_srt.exists()
            and not (
                source_decision is not None
                and source_decision.strategy == TRANSLATE_JAPANESE
            )
        ):
            audio_ready = self._repair_cached_asr_rejection(
                video,
                audio_path,
                paths,
                audio_ready=audio_ready,
            )
            audio_ready = self._refresh_cached_japanese_leading_gap(
                video,
                audio_path,
                paths,
                audio_ready=audio_ready,
            )
        else:
            self._quarantine_unverifiable_translation_cache(video, paths)

        if not paths.ja_srt.exists() and not paths.zh_cn_srt.exists():
            if not audio_ready:
                self._set_stage(video, "audio", "running", "Extracting audio")
                self.logger.info("Extracting audio: %s", video)
                self._extract_preferred_audio(video, audio_path)
                audio_ready = True

            whisper_audio_path = audio_path
            if self.config.enable_vocal_separation:
                self._set_stage(video, "vocal_separation", "running", "Separating vocals")
                self.logger.info("Running vocal separation: %s", video)
                whisper_audio_path = separate_vocals(
                    audio_path,
                    separated_audio_path,
                    self.config.vocal_separation_engine,
                    self.config.vocal_separation_output,
                )

            primary_asr_model = _japanese_transcription_model(self.config)
            primary_asr_backend = _japanese_transcription_backend(self.config)
            self._set_stage(
                video,
                "transcription",
                "running",
                f"Running {primary_asr_backend} model={primary_asr_model} language=ja",
            )
            self.logger.info(
                "Running ASR route=japanese backend=%s model=%s language=ja video=%s",
                primary_asr_backend,
                primary_asr_model,
                video,
            )
            self._active_transcription_video = video
            self._last_asr_route = None
            try:
                try:
                    self._suppress_asr_review_checkpoint_capture = (
                        whisper_audio_path != audio_path
                    )
                    try:
                        self._transcribe(whisper_audio_path, paths.ja_srt)
                    finally:
                        self._suppress_asr_review_checkpoint_capture = False
                except TranscriptionError as exc:
                    if whisper_audio_path == audio_path:
                        raise
                    self.logger.warning(
                        "ASR failed on separated vocals; retrying original audio. video=%s error=%s",
                        video,
                        exc,
                    )
                    paths.ja_srt.unlink(missing_ok=True)
                    asr_diagnostics_path(paths.ja_srt, self.config).unlink(
                        missing_ok=True
                    )
                    asr_transcription_hold_path(paths.ja_srt, self.config).unlink(
                        missing_ok=True
                    )
                    self._set_stage(video, "transcription", "running", "Retrying ASR with original audio")
                    self._last_asr_route = None
                    self._transcribe(audio_path, paths.ja_srt)
            finally:
                self._active_transcription_video = None
            self._validate_srt_output(paths.ja_srt, "Japanese transcription")
            self._set_stage(video, "transcription", "ok", self._japanese_srt_created_message())
        elif paths.ja_srt.exists():
            self._set_stage(video, "transcription", "ok", "Japanese SRT cache hit")
            self.logger.info("Japanese SRT exists, skip Whisper: %s", paths.ja_srt)
        else:
            self._set_stage(video, "transcription", "ok", "zh-CN SRT cache hit")
            self.logger.info("zh-CN SRT exists and Japanese SRT is missing, skip Whisper: %s", paths.zh_cn_srt)

        allow_translation_asr_escalation = not (
            source_decision is not None
            and source_decision.strategy == TRANSLATE_JAPANESE
        )
        translation_cache_repaired = self._repair_cached_translation_safe_omissions(
            video,
            paths,
            allow_asr_escalation=allow_translation_asr_escalation,
        )
        translation_recovery_attempted = translation_cache_repaired
        translation_asr_escalated = (
            translation_cache_repaired and not paths.zh_cn_srt.exists()
        )

        zh_cn_created = False
        translation_pass = 0
        while not paths.zh_cn_srt.exists():
            translation_pass += 1
            if translation_pass > 2:
                raise SubtitleQualityError(
                    "Translation safe-omission recovery exhausted its bounded "
                    "same-job retry budget"
                )
            self._set_stage(video, "postprocess", "running", "Cleaning Japanese SRT")
            ja_blocks = (
                read_srt(paths.ja_srt)
                if source_decision is not None
                and source_decision.strategy == TRANSLATE_JAPANESE
                else self._postprocess_ja_srt(paths.ja_srt)
            )
            series_context = self._build_series_metadata_context(video)
            self._set_stage(video, "translation", "running", "Translating Japanese SRT")
            self.logger.info("Translating SRT: %s", paths.ja_srt)
            self._translator_progress_video = video
            try:
                translator = self._get_translator()
                self._configure_translation_memory_plan(
                    video,
                    translator,
                    ja_blocks,
                    series_glossary=(series_context.glossary if series_context is not None else {}),
                )
                if series_context is not None:
                    if series_context.glossary:
                        translator.translate_blocks(
                            ja_blocks,
                            paths.ja_srt,
                            paths.zh_cn_srt,
                            series_context=series_context.text,
                            series_glossary=series_context.glossary,
                        )
                    else:
                        translator.translate_blocks(
                            ja_blocks,
                            paths.ja_srt,
                            paths.zh_cn_srt,
                            series_context=series_context.text,
                        )
                else:
                    translator.translate_blocks(ja_blocks, paths.ja_srt, paths.zh_cn_srt)
            finally:
                self._translator_progress_video = None
            self._validate_srt_output(paths.zh_cn_srt, "Simplified Chinese translation")
            zh_cn_created = True
            if not paths.ja_srt.exists():
                asr_diagnostics_path(paths.ja_srt, self.config).unlink(missing_ok=True)
                asr_transcription_hold_path(paths.ja_srt, self.config).unlink(
                    missing_ok=True
                )
                write_srt(paths.ja_srt, ja_blocks)
                self.logger.warning("Restored Japanese SRT after it disappeared during translation: %s", paths.ja_srt)
            self._set_stage(video, "translation", "ok", "zh-CN SRT created")

            fresh_events = read_translation_quality_events_strict(paths.zh_cn_srt)
            fresh_omission_indexes = sorted(
                {
                    int(event.get("index") or 0)
                    for event in fresh_events
                    if str(event.get("code") or "") == TRANSLATION_SAFE_OMISSION
                    and str(event.get("severity") or "").lower() == "fail"
                    and int(event.get("index") or 0) > 0
                }
            )
            if fresh_omission_indexes:
                if translation_asr_escalated:
                    self.logger.warning(
                        "Running final targeted translation retry after prompt-free ASR: "
                        "video=%s indexes=%s",
                        video,
                        fresh_omission_indexes,
                    )
                    final_repaired = self._repair_cached_translation_safe_omissions(
                        video,
                        paths,
                        allow_asr_escalation=False,
                    )
                    if not final_repaired or not paths.zh_cn_srt.exists():
                        raise SubtitleQualityError(
                            "Final targeted translation retry could not commit a "
                            "verified zh-CN cache after prompt-free ASR"
                        )
                    final_hold = read_translation_quality_hold_strict(
                        paths.zh_cn_srt
                    )
                    final_events = (
                        []
                        if final_hold is not None
                        else read_translation_quality_events_strict(
                            paths.zh_cn_srt
                        )
                    )
                    final_omission_indexes = sorted(
                        {
                            int(event.get("index") or 0)
                            for event in final_events
                            if str(event.get("code") or "")
                            == TRANSLATION_SAFE_OMISSION
                            and str(event.get("severity") or "").lower() == "fail"
                            and int(event.get("index") or 0) > 0
                        }
                    )
                    if final_omission_indexes:
                        raise SubtitleQualityError(
                            "Translation safe-omission remained after bounded "
                            "same-job recovery: indexes="
                            f"{final_omission_indexes}"
                        )
                    break
                if translation_recovery_attempted:
                    raise SubtitleQualityError(
                        "Translation safe-omission remained after bounded "
                        "same-job recovery: indexes="
                        f"{fresh_omission_indexes}"
                    )
                translation_recovery_attempted = True
                repaired = self._repair_cached_translation_safe_omissions(
                    video,
                    paths,
                    allow_asr_escalation=allow_translation_asr_escalation,
                )
                translation_cache_repaired = translation_cache_repaired or repaired
                if not paths.zh_cn_srt.exists():
                    translation_asr_escalated = True
                    self.logger.warning(
                        "Fresh translation omission escalated to ASR; "
                        "retranslating once in the same job: video=%s pass=%s",
                        video,
                        translation_pass,
                    )
                    continue
            break

        if not zh_cn_created:
            self._set_stage(video, "translation", "ok", "zh-CN SRT cache hit")
            self.logger.info("zh-CN SRT exists, skip translation: %s", paths.zh_cn_srt)

        # A hold is allowed only while the matching zh-TW derivative is about
        # to be regenerated.  Without a hold, every durable fail event is a
        # hard job failure even when ASS export or optional viewing heuristics
        # are disabled.
        pending_translation_hold = read_translation_quality_hold_strict(
            paths.zh_cn_srt
        )
        if pending_translation_hold is None:
            remaining_translation_events = read_translation_quality_events_strict(
                paths.zh_cn_srt
            )
            hard_translation_events = [
                event
                for event in remaining_translation_events
                if str(event.get("severity") or "").lower() == "fail"
            ]
            if hard_translation_events:
                failed_indexes = sorted(
                    {
                        int(event.get("index") or 0)
                        for event in hard_translation_events
                        if int(event.get("index") or 0) > 0
                    }
                )
                raise SubtitleQualityError(
                    "Translation quality event blocks this job before "
                    f"publication: indexes={failed_indexes}"
                )

        cps_repaired = self._repair_translation_cps_violations(
            video,
            paths,
            source_language="ja",
        )
        translation_cache_repaired = translation_cache_repaired or cps_repaired
        force_zh_tw_regeneration = translation_cache_repaired or zh_cn_created
        if force_zh_tw_regeneration or not paths.zh_tw_srt.exists():
            self._set_stage(video, "opencc", "running", "Converting zh-CN to zh-TW")
            self._convert_to_zh_tw(paths.zh_cn_srt, paths.zh_tw_srt)
            self._validate_srt_output(paths.zh_tw_srt, "Traditional Chinese conversion")
            self._set_stage(video, "opencc", "ok", "zh-TW SRT created")
        else:
            self._set_stage(video, "opencc", "ok", "zh-TW SRT cache hit")
            self.logger.info("zh-TW SRT exists, skip OpenCC: %s", paths.zh_tw_srt)
        validate_translation(read_srt(paths.zh_cn_srt), read_srt(paths.zh_tw_srt))
        if (
            source_decision is not None
            and source_decision.strategy == TRANSLATE_JAPANESE
        ):
            source_blocks = read_srt(paths.ja_srt)
            validate_translation(source_blocks, read_srt(paths.zh_cn_srt))
            validate_translation(source_blocks, read_srt(paths.zh_tw_srt))
        self._finalize_translation_cache_commit(paths)

        if self.config.export_ai_ass:
            self._set_stage(video, "ass_export", "running", "Exporting AI ASS")
            begin_output_publication(video, self.config)
            tm_origin = self._read_translation_memory_origin_for_video(video, paths)
            self._publish_ai_ass(video, paths)
            tm_origin = self._rebind_translation_memory_origin_after_qc(
                video,
                paths,
                tm_origin,
            )
            self._set_stage(video, "ass_export", "ok", "AI ASS exported")
            publication_provenance = self._current_publication_provenance()
            if tm_origin is not None:
                publication_provenance["translation_memory"] = (
                    self._translation_memory_lineage_payload(tm_origin)
                )
            manifest = write_output_manifest(
                video,
                self.config,
                [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                provenance=publication_provenance,
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )
            # A lineage-bearing automatic translation must not be committed
            # without first durably snapshotting the material needed for TM
            # replay.  If the outbox write fails, leave the publication marker,
            # SRTs, and lineage in place.  The queue retry then completes this
            # exact transaction through _recover_interrupted_output_publication
            # instead of silently losing a verified episode from memory.
            tm_outbox = self._record_translation_memory_outbox(
                video,
                paths,
                manifest,
                tm_origin,
            )
            finish_output_publication(video, self.config)
            identity = delivery_identity(video, self.config)
            if not validate_output_manifest(
                video,
                self.config,
                verify_hashes=True,
                required_outputs=(paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass),
                require_delivery_evidence=True,
                expected_obligation_id=str(identity["obligation_id"]),
                expected_policy_revision=str(identity["policy_revision"]),
                expected_publication_kind="translated_trilingual",
                expected_output_languages=("ja", "zh-CN", "zh-TW"),
                require_publication_semantics=True,
            ):
                raise SubtitleQualityError(
                    f"Published manifest failed strict post-publication verification: {manifest}"
                )
            if tm_outbox is not None:
                self._replay_translation_memory_outbox_best_effort(tm_outbox.path)
            self.logger.info("Published complete AI output manifest: %s", manifest)

        if self.config.export_ai_ass and not self.config.keep_intermediate_files:
            self._set_stage(video, "cleanup", "running", "Keeping Japanese transcript; removing translated SRT intermediates")
            self._cleanup_intermediate_files(paths.zh_cn_srt, paths.zh_tw_srt)
            remove_translation_memory_origin(self.config.work_path, paths.zh_cn_srt)
        elif not self.config.keep_intermediate_files:
            self._set_stage(video, "cleanup", "running", "Keeping Japanese transcript; removing simplified SRT intermediate")
            self._cleanup_intermediate_files(paths.zh_cn_srt)
            remove_translation_memory_origin(self.config.work_path, paths.zh_cn_srt)

        self.logger.info("Finished video: %s", video)
        self._close_stage_state()
        return ProcessOutcome()

    def _has_strict_chinese_publication(self, video: Path, *, force_ai: bool) -> bool:
        if force_ai:
            return has_ai_finished_subtitle(video, self.config)
        try:
            identity = delivery_identity(video, self.config)
        except OSError:
            return False
        if not validate_output_manifest(
            video,
            self.config,
            verify_hashes=True,
            require_delivery_evidence=True,
            expected_obligation_id=str(identity["obligation_id"]),
            expected_policy_revision=str(identity["policy_revision"]),
            require_publication_semantics=True,
        ):
            return False
        try:
            payload = json.loads(
                output_manifest_path(video, self.config).read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return publication_is_traditional_chinese_delivery(
            manifest_publication_semantics(payload)
        )

    def _recover_interrupted_output_publication(
        self,
        video: Path,
        paths: SubtitlePaths,
    ) -> bool:
        """Finish an exact valid publication left between manifest and marker commit.

        The marker is deliberately ignored only for the first verification.  No
        output is rewritten: current media/policy identity, publication
        semantics, exact output paths, sizes, mtimes, and hashes must all match
        the existing v2 manifest.  This preserves an already-recorded outbox
        intent instead of creating a new manifest hash and colliding with it.
        """

        marker = output_publication_marker_path(video, self.config)
        if not marker.exists() or not self.config.export_ai_ass:
            return False
        try:
            identity = delivery_identity(video, self.config)
            valid_without_marker = validate_output_manifest(
                video,
                self.config,
                verify_hashes=True,
                required_outputs=(
                    paths.ai_ja_ass,
                    paths.ai_zh_cn_ass,
                    paths.ai_zh_tw_ass,
                ),
                expected_obligation_id=str(identity["obligation_id"]),
                expected_policy_revision=str(identity["policy_revision"]),
                expected_publication_kind="translated_trilingual",
                expected_output_languages=("ja", "zh-CN", "zh-TW"),
                require_publication_semantics=True,
            )
        except Exception as exc:  # noqa: BLE001 - incomplete publication proceeds through normal rebuild.
            self.logger.warning(
                "Interrupted publication could not be verified for recovery: video=%s error=%s",
                video,
                exc,
            )
            return False
        if not valid_without_marker:
            self.logger.warning(
                "Interrupted publication is incomplete and will be rebuilt without trusting its marker: %s",
                video,
            )
            return False

        origin = self._read_translation_memory_origin_for_video(video, paths)
        manifest = output_manifest_path(video, self.config)
        # Keep the marker and intermediates when a required lineage-bearing
        # outbox intent cannot be persisted.  A later retry may safely repeat
        # the idempotent record operation; finishing here would make cleanup
        # destroy the only replayable learning material.
        outbox = self._record_translation_memory_outbox(
            video,
            paths,
            manifest,
            origin,
        )

        finish_output_publication(video, self.config)
        if not validate_output_manifest(
            video,
            self.config,
            verify_hashes=True,
            required_outputs=(
                paths.ai_ja_ass,
                paths.ai_zh_cn_ass,
                paths.ai_zh_tw_ass,
            ),
            require_delivery_evidence=True,
            expected_obligation_id=str(identity["obligation_id"]),
            expected_policy_revision=str(identity["policy_revision"]),
            expected_publication_kind="translated_trilingual",
            expected_output_languages=("ja", "zh-CN", "zh-TW"),
            require_publication_semantics=True,
        ):
            # A concurrent artifact change after pre-verification must restore
            # the fail-closed marker before stopping the job.
            begin_output_publication(video, self.config)
            raise SubtitleQualityError(
                f"Interrupted publication changed during recovery: {manifest}"
            )
        if outbox is not None:
            self._replay_translation_memory_outbox_best_effort(outbox.path)
        self.logger.info(
            "Recovered exact interrupted publication without regenerating manifest: %s",
            video,
        )
        return True

    def _record_subtitle_source_decision(
        self,
        decision: SubtitleSourceDecision,
    ) -> None:
        self._subtitle_source_decision = decision
        payload = decision.provenance()
        self.logger.info(
            "Selected subtitle source before audio: strategy=%s language=%s kind=%s stream=%s path=%s",
            decision.strategy,
            decision.source_language,
            decision.source_kind,
            decision.stream_index,
            decision.source_path,
        )
        if self._provenance is not None:
            self._provenance.update("subtitle_source", payload)

    def _adopt_traditional_chinese_source(
        self,
        video: Path,
        decision: SubtitleSourceDecision,
    ) -> ProcessOutcome:
        self._set_stage(
            video,
            "subtitle_source_qc",
            "running",
            "Validating existing Traditional-Chinese subtitle",
        )
        report = analyze_subtitle_file(decision.source_path, self.config, role="unknown")
        if report.has_failures or report.dialogues <= 0:
            raise SubtitleQualityError(
                "Traditional-Chinese source failed prepublication QC: "
                f"{summarize_quality_report(report)}"
            )
        self._set_stage(
            video,
            "subtitle_source_qc",
            "ok",
            summarize_quality_report(report),
        )
        begin_output_publication(video, self.config)
        manifest = write_output_manifest(
            video,
            self.config,
            [decision.source_path],
            provenance=self._source_publication_provenance(
                decision,
                output_quality=report.to_dict(),
            ),
            publication_kind=ADOPTED_ZH_TW_PUBLICATION_KIND,
            output_languages=("zh-TW",),
        )
        finish_output_publication(video, self.config)
        self.logger.info(
            "Adopted validated Traditional-Chinese subtitle without audio or AI: video=%s source=%s manifest=%s",
            video,
            decision.source_path,
            manifest,
        )
        self._close_stage_state()
        return ProcessOutcome(
            "complete",
            "ok",
            "Validated Traditional-Chinese subtitle adopted without audio or AI",
        )

    def _convert_simplified_chinese_source(
        self,
        video: Path,
        decision: SubtitleSourceDecision,
    ) -> ProcessOutcome:
        destination = video.with_name(f"{video.stem}.zh-TW.ass")
        self._set_stage(
            video,
            "opencc_source",
            "running",
            "Converting existing zh-CN subtitle to zh-TW without ASR",
        )
        begin_output_publication(video, self.config)
        convert_ass_to_zh_tw(
            decision.source_path,
            destination,
            self.config.opencc_config,
        )
        classification = classify_subtitle_content_file(destination)
        if classification.language != "zh-tw":
            raise SubtitleQualityError(
                "OpenCC output did not classify as Traditional Chinese: "
                f"path={destination} language={classification.language or 'unknown'}"
            )
        report = analyze_subtitle_file(destination, self.config, role="unknown")
        if report.has_failures or report.dialogues <= 0:
            raise SubtitleQualityError(
                "OpenCC Traditional-Chinese output failed prepublication QC: "
                f"{summarize_quality_report(report)}"
            )
        manifest = write_output_manifest(
            video,
            self.config,
            [destination],
            provenance=self._source_publication_provenance(
                decision,
                output_quality=report.to_dict(),
            ),
            publication_kind=CONVERTED_ZH_CN_PUBLICATION_KIND,
            output_languages=("zh-TW",),
        )
        finish_output_publication(video, self.config)
        self._set_stage(
            video,
            "opencc_source",
            "ok",
            "Existing zh-CN subtitle converted to validated zh-TW without ASR",
        )
        self.logger.info(
            "Converted validated zh-CN subtitle to zh-TW without audio, ASR, or LLM: video=%s source=%s output=%s manifest=%s",
            video,
            decision.source_path,
            destination,
            manifest,
        )
        self._close_stage_state()
        return ProcessOutcome(
            "complete",
            "ok",
            "Existing zh-CN subtitle converted to Traditional Chinese without audio or AI",
        )

    def _seed_japanese_subtitle_source(
        self,
        video: Path,
        paths: SubtitlePaths,
        decision: SubtitleSourceDecision,
    ) -> None:
        self._set_stage(
            video,
            "subtitle_source",
            "running",
            "Importing Japanese subtitle timeline; ASR is not required",
        )
        if paths.zh_cn_srt.exists() or paths.zh_tw_srt.exists():
            self._invalidate_translation_intermediates(paths)
        paths.ja_srt.parent.mkdir(parents=True, exist_ok=True)
        asr_diagnostics_path(paths.ja_srt, self.config).unlink(missing_ok=True)
        asr_transcription_hold_path(paths.ja_srt, self.config).unlink(missing_ok=True)
        convert_ass_file_to_srt(decision.source_path, paths.ja_srt)
        self._assert_ass_srt_timing_preserved(decision.source_path, paths.ja_srt)
        self._validate_srt_output(paths.ja_srt, "Japanese subtitle source")
        self._set_stage(
            video,
            "subtitle_source",
            "ok",
            "Japanese subtitle timeline imported; audio and ASR skipped",
        )

    def _source_publication_provenance(
        self,
        decision: SubtitleSourceDecision,
        *,
        output_quality: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload = (
            dict(self._provenance.payload)
            if self._provenance is not None
            else {}
        )
        payload["subtitle_source"] = decision.provenance(
            output_quality=output_quality,
        )
        return payload

    def _current_publication_provenance(self) -> dict[str, object]:
        payload = (
            dict(self._provenance.payload)
            if self._provenance is not None
            else {}
        )
        if self._subtitle_source_decision is not None:
            payload["subtitle_source"] = self._subtitle_source_decision.provenance()
        return payload

    def _read_translation_memory_origin_for_video(
        self,
        video: Path,
        paths: SubtitlePaths,
    ) -> TranslationMemoryOrigin | None:
        if not bool(getattr(self.config, "translation_memory_enabled", True)):
            return None
        try:
            origin = read_translation_memory_origin_strict(
                self.config.work_path,
                paths.zh_cn_srt,
            )
        except TranslationMemoryBridgeError as exc:
            self.logger.warning(
                "Translation-memory lineage is invalid; output remains publishable but cannot be learned: "
                "video=%s error=%s",
                video,
                exc,
            )
            return None
        if origin is None:
            return None
        scope = self._translation_memory_scope(video)
        if origin.series_key != scope.series_key or origin.policy_version != scope.policy_version:
            self.logger.warning(
                "Translation-memory lineage scope mismatch; output cannot be learned: video=%s",
                video,
            )
            return None
        return origin

    def _rebind_translation_memory_origin_after_qc(
        self,
        video: Path,
        paths: SubtitlePaths,
        origin: TranslationMemoryOrigin | None,
    ) -> TranslationMemoryOrigin | None:
        if origin is None:
            return None
        current_source_hash = sha256_file(Path(origin.source_srt_path))
        current_hash = sha256_file(paths.zh_cn_srt)
        if current_source_hash != origin.source_srt_sha256:
            self.logger.warning(
                "Dropping translation-memory lineage after validated prepublication source repair: "
                "video=%s",
                video,
            )
            remove_translation_memory_origin(self.config.work_path, paths.zh_cn_srt)
            return None
        if current_hash != origin.target_srt_sha256:
            write_translation_memory_origin(
                self.config.work_path,
                paths.zh_cn_srt,
                source_srt_path=origin.source_srt_path,
                source_srt_sha256=origin.source_srt_sha256,
                target_srt_sha256=current_hash,
                split_decision_digest=origin.split_decision_digest,
                cached_indexes=origin.cached_indexes,
                translation_lineage_mode=origin.translation_lineage_mode,
                scope=self._translation_memory_scope(video),
            )
        return self._read_translation_memory_origin_for_video(video, paths)

    @staticmethod
    def _translation_memory_lineage_payload(
        origin: TranslationMemoryOrigin,
    ) -> dict[str, object]:
        return {
            "contract": TRANSLATION_MEMORY_LINEAGE_CONTRACT,
            "mode": origin.translation_lineage_mode,
            "split_decision_digest": origin.split_decision_digest,
            "tm_origin_indexes": list(origin.cached_indexes),
            "source_srt_sha256": origin.source_srt_sha256,
            "target_srt_sha256": origin.target_srt_sha256,
        }

    def _translation_memory_episode_id(self, video: Path) -> str | None:
        episode = extract_episode_number(video.name)
        if episode is None:
            return None
        scope = self._translation_memory_scope(video)
        season = season_number_for_video(video)
        return f"{scope.series_key}:season:{int(season or 0)}:episode:{int(episode)}"

    def _record_translation_memory_outbox(
        self,
        video: Path,
        paths: SubtitlePaths,
        manifest: Path,
        origin: TranslationMemoryOrigin | None,
    ) -> RecordedTranslationMemoryOutboxIntent | None:
        if origin is None or not bool(getattr(self.config, "translation_memory_enabled", True)):
            return None
        if self._translation_memory_manual_run:
            self.logger.info(
                "Skipping translation-memory learning intent for manual/force run: %s",
                video,
            )
            return None
        episode_id = self._translation_memory_episode_id(video)
        if episode_id is None:
            self.logger.warning(
                "Skipping translation-memory learning because episode identity is ambiguous: %s",
                video,
            )
            return None
        if read_translation_quality_hold_strict(paths.zh_cn_srt) is not None:
            raise TranslationMemoryOutboxError(
                "translation_commit_pending",
                f"translation hold remains for {paths.zh_cn_srt}",
            )
        if read_translation_quality_events_strict(paths.zh_cn_srt):
            raise TranslationMemoryOutboxError(
                "translation_quality_events_present",
                f"quality events remain for {paths.zh_cn_srt}",
            )
        identity = delivery_identity(video, self.config)
        return record_translation_memory_outbox_intent(
            self._translation_memory_outbox_root(),
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            video_identity=str(identity["obligation_id"]),
            scope=self._translation_memory_scope(video),
            episode_id=episode_id,
            source_srt_path=paths.ja_srt,
            source_srt_sha256=origin.source_srt_sha256,
            target_srt_path=paths.zh_cn_srt,
            target_srt_sha256=origin.target_srt_sha256,
            tm_origin_indexes=origin.cached_indexes,
            translation_lineage_mode=origin.translation_lineage_mode,
            split_decision_digest=origin.split_decision_digest,
            allow_publication_in_progress=True,
        )

    def _replay_translation_memory_outbox_best_effort(self, intent_path: Path) -> None:
        """Learn after publication without rolling back a verified delivery."""

        try:
            from translation_memory_replay import replay_translation_memory_outbox_intent

            result = replay_translation_memory_outbox_intent(
                intent_path,
                database_path=self._translation_memory_database_path(),
            )
            self.logger.info(
                "Translation-memory outbox replay completed: path=%s status=%s",
                intent_path,
                getattr(result, "status", result),
            )
        except Exception as exc:  # noqa: BLE001 - outbox remains durable for retry.
            self.logger.warning(
                "Translation-memory learning deferred; durable outbox retained: path=%s error=%s",
                intent_path,
                exc,
            )

    def replay_pending_translation_memory_outbox(self, *, limit: int = 32) -> dict[str, int]:
        """Replay a bounded startup batch of durable post-publication intents."""

        summary = {"attempted": 0, "completed": 0, "retained": 0}
        if not bool(getattr(self.config, "translation_memory_enabled", True)):
            return summary
        root = self._translation_memory_outbox_root()
        if not root.is_dir():
            return summary
        try:
            candidates = sorted(
                (path for path in root.glob("*.json") if path.is_file()),
                key=lambda path: path.name,
            )[: max(0, int(limit))]
        except (OSError, TypeError, ValueError) as exc:
            self.logger.warning("Could not enumerate translation-memory outbox %s: %s", root, exc)
            return summary
        try:
            from translation_memory_replay import replay_translation_memory_outbox_intent
        except Exception as exc:  # noqa: BLE001 - optional replay must never block Worker startup.
            summary["attempted"] = len(candidates)
            summary["retained"] = len(candidates)
            self.logger.warning(
                "Translation-memory replay consumer is unavailable; retaining bounded startup batch: "
                "candidates=%s error=%s",
                len(candidates),
                exc,
            )
            return summary

        for candidate in candidates:
            summary["attempted"] += 1
            try:
                replay_translation_memory_outbox_intent(
                    candidate,
                    database_path=self._translation_memory_database_path(),
                )
            except Exception as exc:  # noqa: BLE001 - retain exact durable intent for a later retry.
                summary["retained"] += 1
                self.logger.warning(
                    "Pending translation-memory outbox replay deferred: path=%s error=%s",
                    candidate,
                    exc,
                )
            else:
                summary["completed"] += 1
        return summary

    @staticmethod
    def _assert_ass_srt_timing_preserved(source_ass: Path, output_srt: Path) -> None:
        source_timings: list[tuple[int, int]] = []
        for raw_line in source_ass.read_text(encoding="utf-8-sig").splitlines():
            if not raw_line.startswith("Dialogue:"):
                continue
            fields = raw_line.split(":", 1)[1].lstrip().split(",", 9)
            if len(fields) != 10 or not fields[9].strip():
                continue
            source_timings.append(
                (
                    _ass_timestamp_milliseconds(fields[1].strip()),
                    _ass_timestamp_milliseconds(fields[2].strip()),
                )
            )
        output_timings = [
            _srt_block_timing_milliseconds(block)
            for block in read_srt(output_srt)
        ]
        if not source_timings or source_timings != output_timings:
            raise SubtitleQualityError(
                "Japanese subtitle import changed or dropped event timings: "
                f"source_events={len(source_timings)} output_events={len(output_timings)}"
            )

    def _process_source_transcription(self, video: Path, audio_path: Path, language: str) -> ProcessOutcome:
        source_paths = source_transcript_paths_for_video(video, self.config, language)
        model_name = (
            getattr(self.config, "non_japanese_transcription_model", None)
            or getattr(self.config, "language_detect_model", None)
            or self.config.whisper_model
        )
        transcribe_config = _config_with_overrides(
            self.config,
            whisper_model=model_name,
            whisper_language=source_paths.language,
            transcription_backend=_non_japanese_transcription_backend(self.config),
        )

        self._recover_pending_asr_output(
            video,
            source_paths.srt,
            label=f"source-language:{source_paths.language}",
        )
        if (
            source_paths.srt.is_file()
            and not self._asr_cache_diagnostics_are_trusted(source_paths.srt)
        ):
            self.logger.warning(
                "Invalidating untrusted source-language ASR cache before reuse: "
                "video=%s language=%s srt=%s diagnostic=%s",
                video,
                source_paths.language,
                source_paths.srt,
                asr_diagnostics_path(source_paths.srt, self.config),
            )
            self._fail_closed_asr_output(
                source_paths.srt,
                reason="source-language ASR diagnostic is rejected or untrusted",
            )
            if source_paths.srt.exists():
                raise TranscriptionError(
                    "Untrusted source-language ASR cache could not be removed: "
                    f"{source_paths.srt}"
                )

        if not source_paths.srt.exists():
            self._set_stage(
                video,
                "source_transcription",
                "running",
                f"Transcribing source language={source_paths.language} without LLM",
            )
            self.logger.info(
                "Running ASR route=source-language without LLM backend=%s model=%s language=%s video=%s",
                transcribe_config.transcription_backend,
                model_name,
                source_paths.language,
                video,
            )
            hold = self._begin_asr_commit(
                source_paths.srt,
                reason=(
                    "source-language ASR in progress "
                    f"language={source_paths.language}"
                ),
                active_config=transcribe_config,
            )
            try:
                asr_diagnostics_path(
                    source_paths.srt,
                    self.config,
                ).unlink(missing_ok=True)
                self._transcribe_source_with_fallback(
                    video,
                    audio_path,
                    source_paths.srt,
                    source_paths.language,
                    transcribe_config,
                )
                self._validate_srt_output(
                    source_paths.srt,
                    f"Source-language transcription ({source_paths.language})",
                )
                fresh_diagnostic = asr_diagnostics_path(
                    source_paths.srt,
                    transcribe_config,
                )
                if (
                    bool(
                        getattr(
                            transcribe_config,
                            "asr_diagnostics_enabled",
                            True,
                        )
                    )
                    and not fresh_diagnostic.is_file()
                ):
                    raise TranscriptionError(
                        "Fresh source-language ASR diagnostic is missing: "
                        f"{fresh_diagnostic}"
                    )
                if not self._asr_cache_diagnostics_are_trusted(source_paths.srt):
                    raise TranscriptionError(
                        "Fresh source-language ASR diagnostic is rejected or "
                        f"untrusted: {source_paths.srt}"
                    )
                normalized = self._normalize_source_language_srt_for_readability(
                    source_paths.srt
                )
                if normalized:
                    # The final validated SRT no longer matches the backend's
                    # pre-normalization diagnostic hash.  The durable hold is
                    # still active, so removing that stale evidence is safe.
                    asr_diagnostics_path(
                        source_paths.srt,
                        self.config,
                    ).unlink(missing_ok=True)
                self._finish_asr_commit(
                    source_paths.srt,
                    hold,
                    label=(
                        "Source-language transcription "
                        f"({source_paths.language})"
                    ),
                )
            except Exception as exc:
                self._attach_asr_failure_context(
                    exc,
                    audio_path,
                    source_paths.srt,
                    video=video,
                )
                self._capture_asr_review_checkpoint(
                    exc,
                    source_paths.srt,
                    language=source_paths.language,
                )
                self._fail_closed_asr_output(
                    source_paths.srt,
                    reason=(
                        "Source-language ASR attempt failed: "
                        f"{_compact_error_message(exc)}"
                    ),
                )
                if not source_paths.srt.exists():
                    hold.unlink(missing_ok=True)
                raise
            self._set_stage(
                video,
                "source_transcription",
                "ok",
                f"Source-language SRT created language={source_paths.language}",
            )
        else:
            self._set_stage(
                video,
                "source_transcription",
                "ok",
                f"Source-language SRT cache hit language={source_paths.language}",
            )
            self.logger.info("Source-language SRT exists, skip ASR: %s", source_paths.srt)

        if bool(getattr(self.config, "translate_non_japanese_sources", True)):
            return self._translate_source_transcription(video, source_paths)

        if self.config.export_ai_ass:
            self._set_stage(video, "source_ass_export", "running", "Exporting source-language AI ASS")
            begin_output_publication(video, self.config)
            self._publish_source_ass(video, source_paths.srt, source_paths.ass)
            manifest = write_output_manifest(
                video,
                self.config,
                [source_paths.ass],
                provenance=dict(self._provenance.payload) if self._provenance is not None else {},
                publication_kind="source_language",
                output_languages=(source_paths.language,),
            )
            finish_output_publication(video, self.config)
            self._set_stage(
                video,
                "source_ass_export",
                "ok",
                f"Source-language AI ASS exported language={source_paths.language}",
            )
            self.logger.info("Exported source-language AI ASS: %s", source_paths.ass)
            self.logger.info("Published complete source-language output manifest: %s", manifest)
            if not self.config.keep_intermediate_files:
                self._set_stage(video, "cleanup", "running", "Removing source-language SRT cache")
                self._cleanup_intermediate_files(source_paths.srt)

        self.logger.info("Finished source-language transcription without LLM: video=%s language=%s", video, source_paths.language)
        self._close_stage_state()
        return ProcessOutcome(
            "source_transcription",
            "ok",
            f"Source-language transcript completed without LLM language={source_paths.language}",
        )

    def _translate_source_transcription(
        self,
        video: Path,
        source_paths: SourceTranscriptPaths,
        *,
        _allow_source_asr_recovery: bool = True,
    ) -> ProcessOutcome:
        """Translate a verified non-Japanese ASR transcript into strict zh-TW delivery."""

        canonical_paths = paths_for_video(video, self.config)
        translated_paths = SubtitlePaths(
            ja_srt=source_paths.srt,
            zh_cn_srt=canonical_paths.zh_cn_srt,
            zh_tw_srt=canonical_paths.zh_tw_srt,
            ai_ja_ass=source_paths.ass,
            ai_zh_cn_ass=canonical_paths.ai_zh_cn_ass,
            ai_zh_tw_ass=canonical_paths.ai_zh_tw_ass,
        )
        source_blocks = read_srt(source_paths.srt)
        validate_srt_structure(source_blocks)

        self._set_stage(
            video,
            "translation",
            "running",
            f"Translating source language={source_paths.language} to Chinese",
        )
        series_context = self._build_series_metadata_context(video)
        translator = self._get_translator()
        self._translator_progress_video = video
        try:
            translator.translate_blocks(
                source_blocks,
                source_paths.srt,
                translated_paths.zh_cn_srt,
                series_context=(series_context.text if series_context is not None else ""),
                series_glossary=(series_context.glossary if series_context is not None else {}),
                source_language=source_paths.language,
            )
        finally:
            self._translator_progress_video = None

        self._validate_srt_output(
            translated_paths.zh_cn_srt,
            f"Simplified Chinese translation from {source_paths.language}",
        )
        translation_hold = read_translation_quality_hold_strict(
            translated_paths.zh_cn_srt
        )
        translation_events = (
            []
            if translation_hold is not None
            else read_translation_quality_events_strict(translated_paths.zh_cn_srt)
        )
        hard_events = [
            event
            for event in translation_events
            if str(event.get("severity") or "").lower() == "fail"
        ]
        if hard_events and _allow_source_asr_recovery:
            recovered = self._repair_cached_translation_safe_omissions(
                video,
                translated_paths,
                allow_asr_escalation=True,
                source_language=source_paths.language,
                asr_fallback_config=self._prompt_free_source_fallback_asr_config(
                    source_paths.language
                ),
            )
            if recovered and not translated_paths.zh_cn_srt.exists():
                self.logger.warning(
                    "Source-language translation omissions escalated to same-language "
                    "prompt-free ASR; retranslating once in the same job: "
                    "video=%s language=%s",
                    video,
                    source_paths.language,
                )
                return self._translate_source_transcription(
                    video,
                    source_paths,
                    _allow_source_asr_recovery=False,
                )
            translation_hold = read_translation_quality_hold_strict(
                translated_paths.zh_cn_srt
            )
            translation_events = (
                []
                if translation_hold is not None
                else read_translation_quality_events_strict(
                    translated_paths.zh_cn_srt
                )
            )
            hard_events = [
                event
                for event in translation_events
                if str(event.get("severity") or "").lower() == "fail"
            ]
        if hard_events:
            for repair_attempt in range(1, 3):
                self.logger.warning(
                    "Automatically repairing source-language translation omissions: "
                    "video=%s language=%s attempt=%s indexes=%s",
                    video,
                    source_paths.language,
                    repair_attempt,
                    sorted(
                        int(event.get("index") or 0)
                        for event in hard_events
                        if int(event.get("index") or 0) > 0
                    ),
                )
                repaired = self._repair_cached_translation_safe_omissions(
                    video,
                    translated_paths,
                    allow_asr_escalation=False,
                    source_language=source_paths.language,
                )
                if not repaired:
                    break
                translation_hold = read_translation_quality_hold_strict(
                    translated_paths.zh_cn_srt
                )
                translation_events = (
                    []
                    if translation_hold is not None
                    else read_translation_quality_events_strict(
                        translated_paths.zh_cn_srt
                    )
                )
                hard_events = [
                    event
                    for event in translation_events
                    if str(event.get("severity") or "").lower() == "fail"
                ]
                if not hard_events:
                    break
        if hard_events:
            failed_indexes = sorted(
                {
                    int(event.get("index") or 0)
                    for event in hard_events
                    if int(event.get("index") or 0) > 0
                }
            )
            raise SubtitleQualityError(
                "Source-language translation quality event blocks publication: "
                f"language={source_paths.language} indexes={failed_indexes}"
            )
        self._repair_translation_cps_violations(
            video,
            translated_paths,
            source_language=source_paths.language,
        )
        self._set_stage(
            video,
            "translation",
            "ok",
            f"zh-CN SRT created from language={source_paths.language}",
        )

        self._set_stage(video, "opencc", "running", "Converting zh-CN to zh-TW")
        self._convert_to_zh_tw(
            translated_paths.zh_cn_srt,
            translated_paths.zh_tw_srt,
        )
        self._validate_srt_output(
            translated_paths.zh_tw_srt,
            "Traditional Chinese conversion",
        )
        validate_translation(source_blocks, read_srt(translated_paths.zh_cn_srt))
        validate_translation(source_blocks, read_srt(translated_paths.zh_tw_srt))
        validate_translation(
            read_srt(translated_paths.zh_cn_srt),
            read_srt(translated_paths.zh_tw_srt),
        )
        self._finalize_translation_cache_commit(translated_paths)
        self._set_stage(video, "opencc", "ok", "zh-TW SRT created")

        if self.config.export_ai_ass:
            self._set_stage(
                video,
                "ass_export",
                "running",
                f"Exporting {source_paths.language}/zh-CN/zh-TW AI ASS",
            )
            begin_output_publication(video, self.config)
            self._publish_ai_ass(
                video,
                translated_paths,
                source_language=source_paths.language,
            )
            source_stat = source_paths.srt.stat()
            publication_provenance = (
                dict(self._provenance.payload)
                if self._provenance is not None
                else {}
            )
            publication_provenance["source_transcription"] = {
                "contract": SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT,
                "language": source_paths.language,
                "asr_used": True,
                "path": str(source_paths.srt),
                "size": int(source_stat.st_size),
                "mtime_ns": int(source_stat.st_mtime_ns),
                "sha256": sha256_file(source_paths.srt),
            }
            outputs = (
                source_paths.ass,
                translated_paths.ai_zh_cn_ass,
                translated_paths.ai_zh_tw_ass,
            )
            output_languages = (
                source_paths.language,
                "zh-CN",
                "zh-TW",
            )
            manifest = write_output_manifest(
                video,
                self.config,
                list(outputs),
                provenance=publication_provenance,
                publication_kind=TRANSLATED_PUBLICATION_KIND,
                output_languages=output_languages,
            )
            finish_output_publication(video, self.config)
            identity = delivery_identity(video, self.config)
            if not validate_output_manifest(
                video,
                self.config,
                verify_hashes=True,
                required_outputs=outputs,
                require_delivery_evidence=True,
                expected_obligation_id=str(identity["obligation_id"]),
                expected_policy_revision=str(identity["policy_revision"]),
                expected_publication_kind=TRANSLATED_PUBLICATION_KIND,
                expected_output_languages=output_languages,
                require_publication_semantics=True,
            ):
                raise SubtitleQualityError(
                    f"Published source-language translation manifest failed strict verification: {manifest}"
                )
            self._set_stage(
                video,
                "ass_export",
                "ok",
                f"Traditional-Chinese AI ASS exported from language={source_paths.language}",
            )
            self.logger.info(
                "Published strict source-language translation manifest: %s",
                manifest,
            )

        self.logger.info(
            "Finished source-language translation delivery: video=%s language=%s",
            video,
            source_paths.language,
        )
        self._close_stage_state()
        return ProcessOutcome(
            "source_translation",
            "ok",
            f"Traditional-Chinese delivery completed from language={source_paths.language}",
        )

    def _transcribe_source_with_fallback(
        self,
        video: Path,
        audio_path: Path,
        source_srt: Path,
        language: str,
        primary_config: AppConfig,
    ) -> None:
        """Run bounded, same-language recovery after source ASR rejects.

        Source-language output must never enter the Japanese-specific translation prompt.
        ASR recovery remains a prompt-free pass in the detected language;
        when a low-confidence error provides exact ranges, the existing
        selective repair gate gets the first chance to preserve good blocks.
        """

        primary_backend = str(
            getattr(primary_config, "transcription_backend", "") or ""
        )
        primary_model = str(getattr(primary_config, "whisper_model", "") or "")
        try:
            self._transcribe_with_config(audio_path, source_srt, primary_config)
            return
        except Exception as raw_primary_error:  # noqa: BLE001 - normalize backend-specific failures at this route boundary.
            if isinstance(raw_primary_error, TranscriptionError):
                primary_error = raw_primary_error
            else:
                primary_error = TranscriptionError(
                    "Source-language ASR primary backend failed: "
                    f"{_compact_error_message(raw_primary_error)}"
                )
                primary_error.__cause__ = raw_primary_error

            error_summary = _compact_error_message(primary_error)
            fallback_backend = str(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_backend",
                    None,
                )
                or getattr(self.config, "japanese_transcription_fallback_backend", None)
                or primary_backend
            )
            fallback_model = str(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_model",
                    None,
                )
                or getattr(self.config, "japanese_transcription_fallback_model", None)
                or primary_model
            )
            primary_compute_type = str(
                getattr(primary_config, "whisper_compute_type", "") or ""
            )
            fallback_compute_type = str(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_compute_type",
                    None,
                )
                or getattr(
                    self.config,
                    "japanese_transcription_fallback_compute_type",
                    None,
                )
                or primary_compute_type
            )
            fallback_config = _config_with_overrides(
                primary_config,
                transcription_backend=fallback_backend,
                whisper_model=fallback_model,
                whisper_compute_type=fallback_compute_type,
                whisper_language=language,
                whisper_initial_prompt=None,
                op_ed_initial_prompt=None,
                whisper_condition_on_previous_text=False,
                asr_prompt_free_allow_recovered_primary_artifacts=True,
            )
            review_ranges = (
                _normalize_review_ranges(list(primary_error.review_ranges))
                if isinstance(primary_error, LowConfidenceTranscriptionError)
                else []
            )

            if primary_backend == "faster-whisper" and (
                fallback_backend != primary_backend
                or fallback_model != primary_model
                or fallback_compute_type != primary_compute_type
            ):
                # Do not keep the rejected primary model's frame alive while
                # loading the configured recovery model on a bounded GPU.
                raw_primary_error.__traceback__ = None
                primary_error.__traceback__ = None
                clear_whisper_model_cache(logger=self.logger)

            self.logger.warning(
                "Source-language ASR primary pass failed; running one prompt-free "
                "same-language recovery. primary_backend=%s primary_model=%s "
                "fallback_backend=%s fallback_model=%s fallback_compute_type=%s "
                "language=%s video=%s error=%s",
                primary_backend,
                primary_model,
                fallback_backend,
                fallback_model,
                fallback_compute_type,
                language,
                video,
                primary_error,
            )

            if (
                review_ranges
                and bool(getattr(self.config, "asr_selective_retry_enabled", True))
                and fallback_backend == "faster-whisper"
                and source_srt.is_file()
            ):
                selective_config = _config_with_overrides(
                    fallback_config,
                    transcription_quality_check_enabled=False,
                    enable_gap_rescue=False,
                    enable_leading_gap_rescue=False,
                    op_ed_transcription_enabled=False,
                    write_gap_report=False,
                    asr_diagnostics_enabled=False,
                )
                self._set_stage(
                    video,
                    "source_transcription",
                    "running",
                    f"Retrying {len(review_ranges)} source ASR range(s) "
                    f"without prompts language={language}",
                )
                try:
                    repair_result = repair_low_confidence_ranges(
                        audio_path,
                        source_srt,
                        review_ranges,
                        selective_config,
                        self.logger,
                    )
                    finalize_repaired_transcription(
                        audio_path,
                        source_srt,
                        review_ranges,
                        fallback_config,
                        self.logger,
                        segment_confidences=getattr(
                            repair_result,
                            "segment_confidences",
                            (),
                        ),
                        require_confidence=(
                            str(getattr(primary_error, "reason_code", ""))
                            in {"low_confidence", "rescue_low_confidence"}
                        ),
                    )
                    self.logger.warning(
                        "Recovered source-language ASR with prompt-free selective "
                        "repair: video=%s language=%s ranges=%s model=%s",
                        video,
                        language,
                        review_ranges,
                        fallback_model,
                    )
                    return
                except Exception as selective_error:  # noqa: BLE001 - discard the rejected cache before the sole full retry.
                    self.logger.warning(
                        "Source-language selective ASR repair failed; running the "
                        "single full prompt-free fallback: video=%s language=%s "
                        "ranges=%s error=%s",
                        video,
                        language,
                        review_ranges,
                        selective_error,
                    )

            source_srt.unlink(missing_ok=True)
            asr_diagnostics_path(source_srt, fallback_config).unlink(missing_ok=True)
            self._set_stage(
                video,
                "source_transcription",
                "running",
                f"Primary source ASR failed: {error_summary}; running prompt-free "
                f"fallback {fallback_backend} model={fallback_model} language={language}",
            )
            try:
                self._transcribe_with_config(
                    audio_path,
                    source_srt,
                    fallback_config,
                )
            except Exception as raw_fallback_error:  # noqa: BLE001 - OOM is the only extra retry allowed.
                current_compute_type = str(
                    getattr(fallback_config, "whisper_compute_type", "") or ""
                )
                can_retry_lower_memory = (
                    fallback_backend == "faster-whisper"
                    and str(
                        getattr(fallback_config, "whisper_device", "") or ""
                    ).casefold()
                    == "cuda"
                    and "int8" not in current_compute_type.casefold()
                    and is_cuda_oom(raw_fallback_error)
                )
                if not can_retry_lower_memory:
                    if self._run_source_final_fallback(
                        video,
                        audio_path,
                        source_srt,
                        language,
                        primary_config,
                        fallback_config,
                        raw_fallback_error,
                    ):
                        return
                    if isinstance(raw_fallback_error, TranscriptionError):
                        raise
                    raise TranscriptionError(
                        "Source-language prompt-free ASR fallback failed: "
                        f"{_compact_error_message(raw_fallback_error)}"
                    ) from raw_fallback_error

                raw_fallback_error.__traceback__ = None
                clear_whisper_model_cache(logger=self.logger)
                source_srt.unlink(missing_ok=True)
                asr_diagnostics_path(source_srt, fallback_config).unlink(
                    missing_ok=True
                )
                retry_compute_type = "int8_float16"
                retry_config = _config_with_overrides(
                    fallback_config,
                    whisper_compute_type=retry_compute_type,
                )
                self.logger.warning(
                    "Source-language prompt-free ASR fallback hit CUDA OOM; "
                    "retrying once with lower-memory compute_type=%s model=%s "
                    "language=%s video=%s",
                    retry_compute_type,
                    fallback_model,
                    language,
                    video,
                )
                try:
                    self._transcribe_with_config(
                        audio_path,
                        source_srt,
                        retry_config,
                    )
                except Exception as raw_retry_error:  # noqa: BLE001 - present one stable worker error type.
                    if self._run_source_final_fallback(
                        video,
                        audio_path,
                        source_srt,
                        language,
                        primary_config,
                        retry_config,
                        raw_retry_error,
                    ):
                        return
                    if isinstance(raw_retry_error, TranscriptionError):
                        raise
                    raise TranscriptionError(
                        "Source-language lower-memory ASR fallback failed: "
                        f"{_compact_error_message(raw_retry_error)}"
                    ) from raw_retry_error

    def _run_source_final_fallback(
        self,
        video: Path,
        audio_path: Path,
        source_srt: Path,
        language: str,
        primary_config: AppConfig,
        previous_config: AppConfig,
        previous_error: BaseException,
    ) -> bool:
        backend = str(
            getattr(
                self.config,
                "non_japanese_transcription_final_fallback_backend",
                None,
            )
            or ""
        ).strip()
        model = str(
            getattr(
                self.config,
                "non_japanese_transcription_final_fallback_model",
                None,
            )
            or ""
        ).strip()
        if not backend or not model:
            return False

        compute_type = str(
            getattr(
                self.config,
                "non_japanese_transcription_final_fallback_compute_type",
                None,
            )
            or getattr(previous_config, "whisper_compute_type", "")
            or ""
        )
        previous_route = (
            str(getattr(previous_config, "transcription_backend", "") or ""),
            str(getattr(previous_config, "whisper_model", "") or ""),
            str(getattr(previous_config, "whisper_compute_type", "") or ""),
        )
        if (backend, model, compute_type) == previous_route:
            return False

        final_config = _config_with_overrides(
            primary_config,
            transcription_backend=backend,
            whisper_model=model,
            whisper_compute_type=compute_type,
            whisper_language=language,
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
            asr_prompt_free_allow_recovered_primary_artifacts=True,
        )
        previous_error.__traceback__ = None
        if previous_route[0] == "faster-whisper":
            clear_whisper_model_cache(logger=self.logger)
        source_srt.unlink(missing_ok=True)
        asr_diagnostics_path(source_srt, previous_config).unlink(missing_ok=True)
        asr_diagnostics_path(source_srt, final_config).unlink(missing_ok=True)
        self._set_stage(
            video,
            "source_transcription",
            "running",
            "Prompt-free source ASR fallback failed; running independent "
            f"final fallback {backend} model={model} language={language}",
        )
        self.logger.warning(
            "Source-language prompt-free ASR fallback failed; running independent "
            "final backend. previous_backend=%s previous_model=%s "
            "final_backend=%s final_model=%s language=%s video=%s error=%s",
            previous_route[0],
            previous_route[1],
            backend,
            model,
            language,
            video,
            previous_error,
        )
        try:
            self._transcribe_with_config(audio_path, source_srt, final_config)
            diagnostic = asr_diagnostics_path(source_srt, final_config)
            if (
                bool(getattr(final_config, "asr_diagnostics_enabled", True))
                and not diagnostic.is_file()
                and write_asr_acceptance_diagnostics(
                    source_srt,
                    audio_path,
                    final_config,
                    status="accepted",
                )
                is None
            ):
                raise TranscriptionError(
                    "Source-language final ASR fallback could not write its "
                    f"hash-bound acceptance diagnostic: {diagnostic}"
                )
        except Exception as final_error:  # noqa: BLE001 - expose one stable route failure.
            if isinstance(final_error, TranscriptionError):
                raise
            raise TranscriptionError(
                "Source-language final ASR fallback failed: "
                f"{_compact_error_message(final_error)}"
            ) from final_error

        self.logger.warning(
            "Recovered source-language ASR with independent final backend: "
            "video=%s language=%s backend=%s model=%s",
            video,
            language,
            backend,
            model,
        )
        return True

    def refresh_ass(self, video_path: str | Path) -> bool:
        video = Path(video_path)
        lock = VideoLock(video)
        if not lock.acquire():
            self.logger.info("Skip locked video during ASS refresh: %s", video)
            return False

        try:
            paths = paths_for_video(video, self.config)
            self._validate_translation_cache_chain(video, paths)
            if not self.config.export_ai_ass:
                self.logger.info("Skip ASS refresh because export_ai_ass is disabled: %s", video)
                return False
            if all(path.is_file() for path in (paths.ja_srt, paths.zh_cn_srt, paths.zh_tw_srt)):
                begin_output_publication(video, self.config)
                exported = self._publish_ai_ass(video, paths)
                normalized = self._normalize_existing_ai_ass_names(paths)
                write_output_manifest(
                    video,
                    self.config,
                    [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                    provenance=dict(self._provenance.payload) if self._provenance is not None else {},
                    publication_kind="translated_trilingual",
                    output_languages=("ja", "zh-CN", "zh-TW"),
                )
                finish_output_publication(video, self.config)
                if not self.config.keep_intermediate_files:
                    # Keep the verified Japanese transcript as the durable ASR
                    # source of truth.  ASS refreshes must follow the same
                    # cache contract as a normal AI run so later line repair,
                    # leading-gap recovery, or republishing never has to rerun
                    # Whisper just because styles were refreshed.
                    self._cleanup_intermediate_files(paths.zh_cn_srt, paths.zh_tw_srt)
                self.logger.info(
                    "Refreshed complete ASS exports safely: %s exported=%s normalized=%s",
                    video,
                    exported,
                    normalized,
                )
                return True

            partial_srt = [path for path in (paths.ja_srt, paths.zh_cn_srt, paths.zh_tw_srt) if path.is_file()]
            if partial_srt:
                self.logger.warning(
                    "Refused partial ASS regeneration; all three SRT inputs are required before publication: "
                    "video=%s present=%s",
                    video,
                    [str(path) for path in partial_srt],
                )

            normalized = self._normalize_existing_ai_ass_names(paths)
            restyled = self._restyle_existing_ai_ass_safely(video, paths)
            if normalized == 0 and restyled == 0:
                self.logger.info("Skip ASS refresh because no complete SRT set or restylable AI ASS exists: %s", video)
                return False

            outputs = [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass]
            if normalized > 0 and restyled == 0:
                self._quality_check_ai_outputs(
                    video,
                    [
                        (paths.ai_ja_ass, "japanese"),
                        (paths.ai_zh_cn_ass, "translated"),
                        (paths.ai_zh_tw_ass, "translated"),
                    ],
                )
            if all(path.is_file() for path in outputs):
                write_output_manifest(
                    video,
                    self.config,
                    outputs,
                    provenance=dict(self._provenance.payload) if self._provenance is not None else {},
                    publication_kind="translated_trilingual",
                    output_languages=("ja", "zh-CN", "zh-TW"),
                )
                finish_output_publication(video, self.config)
            self.logger.info(
                "Refreshed existing ASS files safely: %s normalized=%s restyled=%s",
                video,
                normalized,
                restyled,
            )
            return True
        except Exception as exc:
            log_failure(self.config.log_path, video, self._stage_for_exception(exc), exc)
            return False
        finally:
            self._close_stage_state()
            lock.release()

    def _convert_to_zh_tw(self, zh_cn_srt: Path, zh_tw_srt: Path) -> None:
        self.logger.info("Converting zh-CN to zh-TW: %s", zh_cn_srt)
        convert_srt_to_zh_tw(zh_cn_srt, zh_tw_srt, self.config.opencc_config)

    def _has_required_finished_subtitle(self, video: Path, *, force_ai: bool = False) -> bool:
        # A publication marker means the generated artifact set is not a
        # committed delivery.  Do not let legacy filename detection skip the
        # recovery path after a crash between manifest write and finish.
        if output_publication_marker_path(video, self.config).exists():
            return False
        if force_ai or getattr(self.config, "require_ai_subtitles", False):
            finished = has_ai_finished_subtitle(video, self.config)
        else:
            finished = has_finished_subtitle(video, self.config)
        if finished and bool(getattr(self.config, "completed_delivery_enabled", False)):
            # Strict publications are handled by the earlier fast path and the
            # outer delivery hook. A legacy filename-only success cannot be
            # promoted to a completed MKV without first rebuilding auditable
            # subtitle publication evidence.
            return False
        return finished

    def _force_ai_requested(self, video: Path) -> bool:
        if not getattr(self.config, "scanner_cache_enabled", True):
            return False
        if not getattr(self.config, "scanner_queue_enabled", True):
            return False
        try:
            from scan_state import ScanStateStore

            state = ScanStateStore.from_config(self.config)
            try:
                return state.is_force_ai_queue_candidate(video)
            finally:
                state.close()
        except Exception as exc:  # noqa: BLE001 - force flag lookup must not break normal processing.
            self.logger.debug("Failed to read force AI queue flag for %s: %s", video, exc)
            return False

    def _manual_ai_requested(self, video: Path) -> bool:
        """Return whether this run was explicitly prioritized/retried by a user."""

        if not getattr(self.config, "scanner_cache_enabled", True):
            return True
        if not getattr(self.config, "scanner_queue_enabled", True):
            return True
        try:
            from scan_state import ScanStateStore

            state = ScanStateStore.from_config(self.config)
            try:
                snapshot = state.ai_queue_candidate_snapshot(video)
            finally:
                state.close()
            if snapshot is None:
                return True
            source = str((snapshot or {}).get("source") or "")
            return source.startswith("manual_")
        except Exception as exc:  # noqa: BLE001 - TM learning policy cannot break processing.
            self.logger.debug("Failed to read manual queue provenance for %s: %s", video, exc)
            # Unknown provenance is fail-closed for learning but must not send
            # an otherwise healthy subtitle job to review.
            return True

    def _migrate_legacy_ai_srt_paths(self, video: Path, paths: SubtitlePaths) -> None:
        try:
            candidates = list(video.parent.iterdir())
        except OSError:
            return

        for legacy_path in sorted(candidates, key=lambda path: path.name.casefold()):
            if not legacy_path.is_file() or legacy_path.suffix.casefold() != ".srt":
                continue
            if not legacy_path.name.startswith(video.stem):
                continue
            target_path = _canonical_ai_srt_path(paths, legacy_path)
            if target_path is None or legacy_path == target_path or target_path.exists():
                continue
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_path.replace(target_path)
                self.logger.info("Renamed legacy AI SRT: %s -> %s", legacy_path, target_path)
            except OSError as exc:
                self.logger.warning("Failed to rename legacy AI SRT %s -> %s: %s", legacy_path, target_path, exc)

    def _restore_japanese_srt_cache_from_ass(self, paths: SubtitlePaths) -> bool:
        if paths.ja_srt.exists() or not paths.ai_ja_ass.is_file():
            return False
        asr_diagnostics_path(paths.ja_srt, self.config).unlink(missing_ok=True)
        asr_transcription_hold_path(paths.ja_srt, self.config).unlink(
            missing_ok=True
        )
        try:
            report = analyze_subtitle_file(paths.ai_ja_ass, self.config, role="japanese")
            if report.has_failures:
                self.logger.warning(
                    "Japanese ASS is not safe to reuse as SRT cache: path=%s summary=%s",
                    paths.ai_ja_ass,
                    summarize_quality_report(report),
                )
                return False
            convert_ass_file_to_srt(paths.ai_ja_ass, paths.ja_srt)
            self._validate_srt_output(paths.ja_srt, "Restored Japanese transcription")
            self.logger.info(
                "Restored Japanese SRT cache from existing AI ASS; Whisper can be skipped: ass=%s srt=%s",
                paths.ai_ja_ass,
                paths.ja_srt,
            )
            return True
        except Exception as exc:
            paths.ja_srt.unlink(missing_ok=True)
            asr_diagnostics_path(paths.ja_srt, self.config).unlink(missing_ok=True)
            asr_transcription_hold_path(paths.ja_srt, self.config).unlink(
                missing_ok=True
            )
            self.logger.warning(
                "Failed to restore Japanese SRT cache from AI ASS; normal ASR fallback will run: ass=%s error=%s",
                paths.ai_ja_ass,
                exc,
            )
            return False

    def _repair_cached_asr_rejection(
        self,
        video: Path,
        audio_path: Path,
        paths: SubtitlePaths,
        *,
        audio_ready: bool,
    ) -> bool:
        """Recover a rejected cache selectively, then rebuild it from source."""

        if not paths.ja_srt.is_file():
            return audio_ready
        diagnostics_path = asr_diagnostics_path(paths.ja_srt, self.config)
        if not diagnostics_path.is_file():
            return audio_ready
        diagnostics = read_asr_diagnostics(paths.ja_srt, self.config)
        diagnostic_status = str(diagnostics.get("status") or "")
        diagnosed_sha256 = str(diagnostics.get("srt_sha256") or "").strip()
        evidence_matches = bool(diagnosed_sha256) and (
            diagnosed_sha256 == sha256_file(paths.ja_srt)
        )
        if diagnostic_status in {"accepted", "accepted_after_selective_retry"}:
            if evidence_matches:
                return audio_ready
            self._raise_cached_asr_fail_closed(
                diagnostics,
                reason=(
                    "accepted ASR diagnostic transcript hash missing"
                    if not diagnosed_sha256
                    else "accepted ASR diagnostic transcript hash mismatch"
                ),
                reason_code="accepted_cache_hash_mismatch",
            )
        if not diagnostics or diagnostic_status not in {
            "selective_retry_required",
            "selective_repair_rejected",
        }:
            self._raise_cached_asr_fail_closed(
                diagnostics,
                reason="ASR diagnostics are corrupt or have an untrusted status",
                reason_code="untrusted_cache",
            )
        raw_ranges = diagnostics.get("review_ranges")
        parsed_ranges: list[tuple[float, float]] = []
        if isinstance(raw_ranges, list):
            for raw in raw_ranges[:64]:
                if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                    continue
                try:
                    parsed_ranges.append((float(raw[0]), float(raw[1])))
                except (TypeError, ValueError):
                    continue
        review_ranges = _normalize_review_ranges(parsed_ranges)

        reason_code = str(diagnostics.get("reason_code") or "").strip()
        try:
            low_confidence_segments = int(
                diagnostics.get("low_confidence_segments") or 0
            )
        except (TypeError, ValueError):
            low_confidence_segments = 0
        confidence_triggered = (
            reason_code in {"low_confidence", "rescue_low_confidence"}
            or (
                not reason_code
                and low_confidence_segments > 0
            )
        )
        fallback_config = self._prompt_free_fallback_asr_config()
        reasons: list[str] = []
        if not bool(getattr(self.config, "asr_selective_retry_enabled", True)):
            reasons.append("selective retry disabled")
        if not review_ranges:
            reasons.append("review ranges missing or invalid")
        if not diagnosed_sha256:
            reasons.append("diagnostic transcript hash missing")
        elif not evidence_matches:
            reasons.append("diagnostic transcript hash mismatch")
        if fallback_config.transcription_backend != "faster-whisper":
            reasons.append(
                f"selective backend unsupported: {fallback_config.transcription_backend}"
            )
        for key, label in (
            ("media_fingerprint", "source media"),
            ("audio_fingerprint", "extracted audio"),
            ("audio_stream_fingerprint", "audio stream"),
            ("cache_fingerprint", "Japanese SRT cache"),
        ):
            value = diagnostics.get(key)
            if not isinstance(value, dict) or not str(value.get("fingerprint") or ""):
                reasons.append(f"{label} fingerprint missing")
        repair_fingerprint = str(diagnostics.get("repair_fingerprint") or "").strip()
        if not repair_fingerprint:
            reasons.append("repair fingerprint missing")
        if reasons:
            self._raise_cached_asr_fail_closed(
                diagnostics,
                review_ranges=review_ranges,
                reason="; ".join(reasons),
                reason_code=reason_code or "untrusted_cache",
            )

        if not audio_ready:
            self._set_stage(video, "audio", "running", "Extracting audio for rejected ASR recovery")
            self._extract_preferred_audio(video, audio_path)
            audio_ready = True
        context_matches, context_reasons, current_context = verify_asr_diagnostics_context(
            paths.ja_srt,
            self.config,
            media_path=video,
            audio_path=audio_path,
            audio_stream=self._selected_audio_stream,
        )
        if not context_matches:
            return self._run_full_prompt_free_asr_fallback(
                video,
                audio_path,
                paths,
                audio_ready=audio_ready,
                reason=(
                    "cached ASR context no longer matches source: "
                    + "; ".join(context_reasons)
                ),
                fallback_config=fallback_config,
            )
        if not claim_asr_repair_attempt(
            paths.ja_srt,
            self.config,
            repair_fingerprint,
        ):
            return self._run_full_prompt_free_asr_fallback(
                video,
                audio_path,
                paths,
                audio_ready=audio_ready,
                reason="selective ASR repair was already attempted for this fingerprint",
                fallback_config=fallback_config,
            )
        if self._try_prompt_free_selective_asr_repair(
            video,
            audio_path,
            paths,
            review_ranges,
            fallback_config,
            require_confidence=confidence_triggered or not reason_code,
        ):
            return audio_ready

        return self._run_full_prompt_free_asr_fallback(
            video,
            audio_path,
            paths,
            audio_ready=audio_ready,
            reason="selective ASR repair or final validation failed",
            fallback_config=fallback_config,
        )

    @staticmethod
    def _raise_cached_asr_fail_closed(
        diagnostics: dict[str, object],
        *,
        reason: str,
        reason_code: str,
        review_ranges: list[tuple[float, float]] | None = None,
        context: dict[str, object] | None = None,
        repair_attempted: bool | None = None,
    ) -> None:
        ranges = list(review_ranges or [])
        if not ranges:
            raw_ranges = diagnostics.get("review_ranges")
            parsed: list[tuple[float, float]] = []
            if isinstance(raw_ranges, list):
                for raw in raw_ranges[:64]:
                    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                        continue
                    try:
                        parsed.append((float(raw[0]), float(raw[1])))
                    except (TypeError, ValueError):
                        continue
            ranges = _normalize_review_ranges(parsed)
        asr_context: dict[str, object] = {
            "failure_code": str(reason_code or "untrusted_cache"),
            "reason_code": str(reason_code or "untrusted_cache"),
            "review_ranges": [[start, end] for start, end in ranges],
            "media_fingerprint": diagnostics.get("media_fingerprint"),
            "audio_fingerprint": diagnostics.get("audio_fingerprint"),
            "audio_stream_fingerprint": diagnostics.get(
                "audio_stream_fingerprint"
            ),
            "cache_fingerprint": diagnostics.get("cache_fingerprint"),
            "repair_fingerprint": str(
                diagnostics.get("repair_fingerprint") or ""
            ),
            "repair_attempted": (
                bool(diagnostics.get("repair_attempted"))
                if repair_attempted is None
                else bool(repair_attempted)
            ),
            "cache_trusted": False,
        }
        if context:
            asr_context.update(context)
            asr_context["cache_trusted"] = True
        raise AsrSelectiveRepairUnavailableError(
            f"Cached ASR selective repair refused fail-closed: {reason}",
            ranges,
            reason_code=str(reason_code or "untrusted_cache"),
            asr_context=asr_context,
        )

    def _prompt_free_fallback_asr_config(self) -> AppConfig:
        return _config_with_overrides(
            self.config,
            transcription_backend=(
                getattr(self.config, "japanese_transcription_fallback_backend", None)
                or self.config.transcription_backend
            ),
            whisper_model=(
                getattr(self.config, "japanese_transcription_fallback_model", None)
                or self.config.whisper_model
            ),
            whisper_compute_type=(
                getattr(self.config, "japanese_transcription_fallback_compute_type", None)
                or getattr(self.config, "whisper_compute_type", "float16")
            ),
            whisper_language="ja",
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
            asr_optional_rescue_rejection_is_fatal=False,
            asr_prompt_free_allow_recovered_primary_artifacts=True,
        )

    def _prompt_free_source_fallback_asr_config(self, language: str) -> AppConfig:
        """Build the bounded prompt-free ASR repair route for a non-Japanese source."""

        normalized_language = str(language or "").strip().replace("_", "-").casefold()
        primary_backend = _non_japanese_transcription_backend(self.config)
        primary_model = str(
            getattr(self.config, "non_japanese_transcription_model", None)
            or getattr(self.config, "language_detect_model", None)
            or self.config.whisper_model
        )
        primary_compute_type = str(
            getattr(self.config, "whisper_compute_type", "") or ""
        )
        return _config_with_overrides(
            self.config,
            transcription_backend=(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_backend",
                    None,
                )
                or getattr(
                    self.config,
                    "japanese_transcription_fallback_backend",
                    None,
                )
                or primary_backend
            ),
            whisper_model=(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_model",
                    None,
                )
                or getattr(
                    self.config,
                    "japanese_transcription_fallback_model",
                    None,
                )
                or primary_model
            ),
            whisper_compute_type=(
                getattr(
                    self.config,
                    "non_japanese_transcription_fallback_compute_type",
                    None,
                )
                or getattr(
                    self.config,
                    "japanese_transcription_fallback_compute_type",
                    None,
                )
                or primary_compute_type
            ),
            whisper_language=normalized_language,
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
            asr_optional_rescue_rejection_is_fatal=False,
            asr_prompt_free_allow_recovered_primary_artifacts=True,
        )

    def _run_japanese_final_asr_fallback(
        self,
        audio_path: Path,
        output_srt: Path,
        previous_config: AppConfig,
        previous_error: BaseException,
    ) -> AppConfig | None:
        """Run the independent, smaller Japanese ASR route once.

        The large-v2 recovery model can still exhaust VRAM after its
        lower-memory compute retry.  Releasing it before loading a genuinely
        smaller model prevents a recoverable GPU-pressure event from becoming
        a permanent failed_retry item.  The normal ASR quality and diagnostic
        gates remain in force for this final route.
        """

        backend = str(
            getattr(
                self.config,
                "japanese_transcription_final_fallback_backend",
                None,
            )
            or ""
        ).strip()
        model = str(
            getattr(
                self.config,
                "japanese_transcription_final_fallback_model",
                None,
            )
            or ""
        ).strip()
        if not backend or not model:
            return None

        compute_type = str(
            getattr(
                self.config,
                "japanese_transcription_final_fallback_compute_type",
                None,
            )
            or getattr(previous_config, "whisper_compute_type", "")
            or ""
        )
        previous_route = (
            str(getattr(previous_config, "transcription_backend", "") or ""),
            str(getattr(previous_config, "whisper_model", "") or ""),
            str(getattr(previous_config, "whisper_compute_type", "") or ""),
        )
        if (backend, model, compute_type) == previous_route:
            return None

        final_config = _config_with_overrides(
            self.config,
            transcription_backend=backend,
            whisper_model=model,
            whisper_compute_type=compute_type,
            whisper_language="ja",
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
            asr_optional_rescue_rejection_is_fatal=False,
            asr_prompt_free_allow_recovered_primary_artifacts=True,
        )
        previous_error.__traceback__ = None
        if previous_route[0] == "faster-whisper":
            clear_whisper_model_cache(logger=self.logger)
        output_srt.unlink(missing_ok=True)
        asr_diagnostics_path(output_srt, previous_config).unlink(missing_ok=True)
        asr_diagnostics_path(output_srt, final_config).unlink(missing_ok=True)
        self._set_active_transcription_stage(
            "running",
            "Japanese ASR recovery failed; running independent smaller "
            f"fallback {backend} model={model}",
        )
        self.logger.warning(
            "Japanese ASR recovery failed; running independent smaller model. "
            "previous_backend=%s previous_model=%s final_backend=%s "
            "final_model=%s final_compute_type=%s audio=%s error=%s",
            previous_route[0],
            previous_route[1],
            backend,
            model,
            compute_type,
            audio_path,
            previous_error,
        )
        try:
            self._transcribe_with_config(audio_path, output_srt, final_config)
        except Exception as final_error:  # noqa: BLE001 - expose one stable ASR failure type.
            if isinstance(final_error, TranscriptionError):
                raise
            raise TranscriptionError(
                "Japanese final ASR fallback failed: "
                f"{_compact_error_message(final_error)}"
            ) from final_error

        self.logger.warning(
            "Recovered Japanese ASR with independent smaller model: "
            "backend=%s model=%s compute_type=%s audio=%s",
            backend,
            model,
            compute_type,
            audio_path,
        )
        return final_config

    def _try_prompt_free_selective_asr_repair(
        self,
        video: Path,
        audio_path: Path,
        paths: SubtitlePaths,
        review_ranges: list[tuple[float, float]],
        fallback_config: AppConfig,
        *,
        require_confidence: bool,
        require_changed_transcript: bool = False,
    ) -> bool:
        repair_root = Path(self.config.work_path) / "asr_recovery"
        repair_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        temporary = repair_root / f"selective-{digest}-{time.time_ns()}.srt"
        original_snapshot = repair_root / f"original-{digest}-{time.time_ns()}.srt"
        temporary_diagnostics = asr_diagnostics_path(temporary, self.config)
        original_diagnostics_snapshot = original_snapshot.with_suffix(".diagnostics.json")
        before_sha256 = sha256_file(paths.ja_srt)
        live_hold: Path | None = None
        live_published = False
        try:
            verified_copy_replace(paths.ja_srt, original_snapshot)
            live_diagnostics = asr_diagnostics_path(paths.ja_srt, self.config)
            if live_diagnostics.is_file():
                verified_copy_replace(
                    live_diagnostics,
                    original_diagnostics_snapshot,
                )
            verified_copy_replace(paths.ja_srt, temporary)
            repair_config = _config_with_overrides(
                fallback_config,
                transcription_quality_check_enabled=False,
                enable_gap_rescue=False,
                enable_leading_gap_rescue=False,
                op_ed_transcription_enabled=False,
                write_gap_report=False,
                asr_diagnostics_enabled=False,
            )
            self._set_stage(
                video,
                "transcription",
                "running",
                f"Retrying {len(review_ranges)} rejected ASR range(s) without prompts",
            )
            repair_result = repair_low_confidence_ranges(
                audio_path,
                temporary,
                review_ranges,
                repair_config,
                self.logger,
            )
            finalize_repaired_transcription(
                audio_path,
                temporary,
                review_ranges,
                fallback_config,
                self.logger,
                segment_confidences=getattr(
                    repair_result,
                    "segment_confidences",
                    (),
                ),
                require_confidence=require_confidence,
            )
            if (
                require_changed_transcript
                and sha256_file(temporary) == before_sha256
            ):
                raise TranscriptionError(
                    "Prompt-free selective ASR retry did not change the rejected transcript"
                )
            live_hold = self._begin_asr_commit(
                paths.ja_srt,
                reason="prompt-free selective ASR repair commit",
                active_config=fallback_config,
            )
            verified_copy_replace(temporary, paths.ja_srt)
            live_published = True
            promoted_diagnostics = promote_asr_diagnostics(
                temporary,
                paths.ja_srt,
                fallback_config,
            )
            if promoted_diagnostics is None:
                promoted_diagnostics = write_asr_acceptance_diagnostics(
                    paths.ja_srt,
                    audio_path,
                    fallback_config,
                    status="accepted_after_selective_retry",
                    repaired_ranges=review_ranges,
                    segment_confidences=getattr(
                        repair_result,
                        "segment_confidences",
                        (),
                    ),
                )
            if (
                bool(getattr(fallback_config, "asr_diagnostics_enabled", True))
                and promoted_diagnostics is None
            ):
                raise TranscriptionError(
                    "Prompt-free selective ASR diagnostics could not be "
                    "promoted to the live cache"
                )
            bound_context = attach_asr_diagnostics_context(
                paths.ja_srt,
                fallback_config,
                media_path=video,
                audio_path=audio_path,
                audio_stream=self._selected_audio_stream,
            )
            if (
                bool(getattr(fallback_config, "asr_diagnostics_enabled", True))
                and not bound_context
            ):
                raise TranscriptionError(
                    "Prompt-free selective ASR diagnostics could not be "
                    "bound to the repair inputs"
                )
            self._validate_srt_output(
                paths.ja_srt,
                "Prompt-free selective ASR repair",
            )
            self._invalidate_translation_intermediates(paths)
            self._finish_asr_commit(
                paths.ja_srt,
                live_hold,
                label="Prompt-free selective ASR repair",
            )
            self._set_stage(
                video,
                "transcription",
                "ok",
                f"Recovered {len(review_ranges)} rejected ASR range(s) without prompts",
            )
            self.logger.warning(
                "Recovered cached ASR rejection with prompt-free selective retry: "
                "video=%s ranges=%s model=%s",
                video,
                review_ranges,
                fallback_config.whisper_model,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - caller leaves the cache fail-closed for review.
            if live_published:
                try:
                    if (
                        not original_snapshot.is_file()
                        or sha256_file(original_snapshot) != before_sha256
                    ):
                        raise TranscriptionError(
                            "original selective ASR cache snapshot checksum mismatch"
                        )
                    verified_copy_replace(original_snapshot, paths.ja_srt)
                    live_diagnostics = asr_diagnostics_path(
                        paths.ja_srt,
                        self.config,
                    )
                    if original_diagnostics_snapshot.is_file():
                        verified_copy_replace(
                            original_diagnostics_snapshot,
                            live_diagnostics,
                        )
                    else:
                        live_diagnostics.unlink(missing_ok=True)
                except Exception as rollback_error:
                    self._fail_closed_asr_output(
                        paths.ja_srt,
                        reason=(
                            "Prompt-free selective ASR live rollback failed: "
                            f"{_compact_error_message(rollback_error)}"
                        ),
                    )
                    self.logger.error(
                        "Prompt-free selective ASR rollback failed: video=%s error=%s",
                        video,
                        rollback_error,
                    )
            if live_hold is not None:
                live_hold.unlink(missing_ok=True)
            self.logger.warning(
                "Prompt-free selective ASR repair rejected; preparing the full prompt-free fallback: "
                "video=%s ranges=%s error=%s",
                video,
                review_ranges,
                exc,
            )
            return False
        finally:
            temporary.unlink(missing_ok=True)
            temporary_diagnostics.unlink(missing_ok=True)
            original_snapshot.unlink(missing_ok=True)
            original_diagnostics_snapshot.unlink(missing_ok=True)

    def _run_full_prompt_free_asr_fallback(
        self,
        video: Path,
        audio_path: Path,
        paths: SubtitlePaths,
        *,
        audio_ready: bool,
        reason: str,
        fallback_config: AppConfig | None = None,
    ) -> bool:
        if not audio_ready:
            self._set_stage(video, "audio", "running", "Extracting audio for full prompt-free ASR fallback")
            self._extract_preferred_audio(video, audio_path)
            audio_ready = True
        active_config = fallback_config or self._prompt_free_fallback_asr_config()
        archive = self._archive_rejected_asr_cache(video, paths, reason=reason)
        repair_root = Path(self.config.work_path) / "asr_recovery"
        repair_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        temporary = repair_root / f"full-{digest}-{time.time_ns()}.srt"
        temporary_diagnostics = asr_diagnostics_path(temporary, active_config)
        live_hold: Path | None = None
        self._set_stage(
            video,
            "transcription",
            "running",
            f"Running full prompt-free ASR fallback: {reason}",
        )
        try:
            try:
                self._transcribe_with_config(audio_path, temporary, active_config)
            except TranscriptionError as fallback_error:
                current_compute_type = str(
                    getattr(active_config, "whisper_compute_type", "") or ""
                )
                can_retry_lower_memory = (
                    str(
                        getattr(active_config, "transcription_backend", "") or ""
                    ).casefold()
                    == "faster-whisper"
                    and str(
                        getattr(active_config, "whisper_device", "") or ""
                    ).casefold()
                    == "cuda"
                    and "int8" not in current_compute_type.casefold()
                    and is_cuda_oom(fallback_error)
                )
                recovery_error: TranscriptionError | None = fallback_error
                recovery_config = active_config
                if can_retry_lower_memory:
                    fallback_error.__traceback__ = None
                    clear_whisper_model_cache(logger=self.logger)
                    temporary.unlink(missing_ok=True)
                    temporary_diagnostics.unlink(missing_ok=True)
                    retry_config = _config_with_overrides(
                        active_config,
                        whisper_compute_type="int8_float16",
                    )
                    self.logger.warning(
                        "Full prompt-free ASR fallback hit CUDA OOM; retrying once "
                        "with lower-memory compute_type=%s model=%s audio=%s",
                        retry_config.whisper_compute_type,
                        retry_config.whisper_model,
                        audio_path,
                    )
                    try:
                        self._transcribe_with_config(
                            audio_path,
                            temporary,
                            retry_config,
                        )
                    except TranscriptionError as retry_error:
                        recovery_error = retry_error
                        recovery_config = retry_config
                    else:
                        recovery_error = None
                        active_config = retry_config

                if recovery_error is not None:
                    final_config = self._run_japanese_final_asr_fallback(
                        audio_path,
                        temporary,
                        recovery_config,
                        recovery_error,
                    )
                    if final_config is None:
                        raise recovery_error
                    active_config = final_config
            self._validate_srt_output(temporary, "Full prompt-free ASR fallback")
            live_hold = self._begin_asr_commit(
                paths.ja_srt,
                reason="full prompt-free ASR fallback commit",
                active_config=active_config,
            )
            verified_copy_replace(temporary, paths.ja_srt)
            promoted_diagnostics = promote_asr_diagnostics(
                temporary,
                paths.ja_srt,
                active_config,
            )
            if promoted_diagnostics is None:
                promoted_diagnostics = write_asr_acceptance_diagnostics(
                    paths.ja_srt,
                    audio_path,
                    active_config,
                    status="accepted",
                )
            if (
                bool(getattr(active_config, "asr_diagnostics_enabled", True))
                and promoted_diagnostics is None
            ):
                raise TranscriptionError(
                    "Full prompt-free ASR diagnostics could not be promoted "
                    "to the live cache"
                )
            self._validate_srt_output(
                paths.ja_srt,
                "Full prompt-free ASR fallback",
            )
            self._invalidate_translation_intermediates(paths)
            self._finish_asr_commit(
                paths.ja_srt,
                live_hold,
                label="Full prompt-free ASR fallback",
            )
        except Exception as exc:
            self._fail_closed_asr_output(
                paths.ja_srt,
                reason=(
                    "Full prompt-free ASR fallback failed: "
                    f"{_compact_error_message(exc)}"
                ),
            )
            if live_hold is not None and not paths.ja_srt.exists():
                live_hold.unlink(missing_ok=True)
            raise TranscriptionError(
                "Full prompt-free ASR fallback failed after rejected cache was archived: "
                f"archive={archive} error={exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
            temporary_diagnostics.unlink(missing_ok=True)
            asr_diagnostics_path(temporary, active_config).unlink(missing_ok=True)
        self._last_asr_route = AsrRouteOutcome(
            backend=str(active_config.transcription_backend),
            model=str(active_config.whisper_model),
            fallback_used=True,
            failed_model=_japanese_transcription_model(self.config),
            failed_reason=reason,
        )
        if self._provenance is not None:
            self._provenance.update("asr", self._last_asr_route)
        self._set_stage(
            video,
            "transcription",
            "ok",
            f"Full prompt-free ASR fallback succeeded after cache rejection: {reason}",
        )
        self.logger.warning(
            "Rebuilt rejected ASR cache with full prompt-free fallback: "
            "video=%s backend=%s model=%s archive=%s reason=%s",
            video,
            active_config.transcription_backend,
            active_config.whisper_model,
            archive,
            reason,
        )
        return audio_ready

    def _archive_rejected_asr_cache(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        reason: str,
    ) -> Path:
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        archive = (
            Path(self.config.work_path)
            / "asr_rejected_cache"
            / f"{time.time_ns()}-{digest}"
        )
        archive.mkdir(parents=True, exist_ok=False)
        sources = [
            path
            for path in (
                paths.ja_srt,
                paths.zh_cn_srt,
                paths.zh_tw_srt,
                translation_quality_events_path(paths.zh_cn_srt),
                asr_diagnostics_path(paths.ja_srt, self.config),
                asr_transcription_hold_path(paths.ja_srt, self.config),
            )
            if path.is_file()
        ]
        copied: list[dict[str, str]] = []
        for index, source in enumerate(sources):
            name_digest = hashlib.sha1(
                str(source).encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            destination = archive / f"{index:02d}-{name_digest}-{source.name}"
            verified_copy_replace(source, destination)
            copied.append({"source": str(source), "archive": str(destination)})
        atomic_write_text(
            archive / "manifest.json",
            json.dumps(
                {
                    "video": str(video),
                    "reason": str(reason),
                    "created_at": time.time(),
                    "copied": copied,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        for source in sources:
            source.unlink()
        return archive

    def _repair_cached_translation_safe_omissions(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        allow_asr_escalation: bool = True,
        source_language: str = "ja",
        asr_fallback_config: AppConfig | None = None,
    ) -> bool:
        """Retranslate only lines rejected by the previous translation attempt."""

        event_path = translation_quality_events_path(paths.zh_cn_srt)
        tm_origin = self._read_translation_memory_origin_for_video(video, paths)
        try:
            events = read_translation_quality_events_strict(paths.zh_cn_srt)
        except (OSError, RuntimeError) as exc:
            self.logger.warning(
                "Invalidated translation cache with unreadable quality-event sidecar: "
                "video=%s sidecar=%s error=%s",
                video,
                event_path,
                exc,
            )
            self._invalidate_translation_intermediates(paths)
            return False
        if not paths.zh_cn_srt.is_file():
            # A translated SRT event without its SRT is a stale work-cache
            # sidecar. The normal translation path will rebuild both.
            event_path.unlink(missing_ok=True)
            return False
        omission_indexes = {
            int(event.get("index") or 0)
            for event in events
            if str(event.get("code") or "") == TRANSLATION_SAFE_OMISSION
            and int(event.get("index") or 0) > 0
        }
        if not omission_indexes:
            if event_path.is_file() and not events:
                event_path.unlink(missing_ok=True)
            return False
        if not paths.ja_srt.is_file():
            self._invalidate_translation_intermediates(paths)
            return False

        try:
            source_blocks = read_srt(paths.ja_srt)
            translated_blocks = read_srt(paths.zh_cn_srt)
            validate_translation(source_blocks, translated_blocks)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warning(
                "Invalidated unreadable translation cache before safe-omission retry: "
                "video=%s error=%s",
                video,
                exc,
            )
            self._invalidate_translation_intermediates(paths)
            return False
        source_by_index = {block.index: block for block in source_blocks}
        translated_by_index = {block.index: block for block in translated_blocks}
        source_indexes = set(source_by_index)
        if (
            omission_indexes - source_indexes
            or set(translated_by_index) != source_indexes
            or len(source_blocks) != len(translated_blocks)
        ):
            self.logger.warning(
                "Invalidated stale/incomplete translation cache before safe-omission retry: "
                "video=%s omissions=%s source_lines=%s translated_lines=%s",
                video,
                sorted(omission_indexes),
                len(source_blocks),
                len(translated_blocks),
            )
            self._invalidate_translation_intermediates(paths)
            return False

        selected_source = [source_by_index[index] for index in sorted(omission_indexes)]
        repair_root = Path(self.config.work_path) / "ai_translation_repair"
        repair_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        temporary = repair_root / f"{digest}-{time.time_ns()}.srt"
        temporary_events = translation_quality_events_path(temporary)
        temporary_hold = translation_quality_hold_path(temporary)
        committed_events: list[dict[str, object]] = []
        self._set_stage(
            video,
            "translation",
            "running",
            f"Retranslating {len(selected_source)} previously rejected line(s)",
        )
        try:
            series_context = self._build_series_metadata_context(video)
            translator = self._get_translator()
            translator.set_targeted_repair_context(
                source_blocks,
                translated_blocks,
                omission_indexes,
                series_context=(series_context.text if series_context else ""),
            )
            try:
                repair_kwargs: dict[str, object] = {
                    "series_glossary": (
                        (series_context.glossary or {})
                        if series_context
                        else {}
                    )
                }
                if str(source_language or "ja").strip().casefold() not in {
                    "ja",
                    "jpn",
                }:
                    repair_kwargs["source_language"] = source_language
                translator.retranslate_problem_blocks(
                    selected_source,
                    paths.ja_srt,
                    temporary,
                    **repair_kwargs,
                )
            finally:
                translator.clear_targeted_repair_context()
            replacement_blocks = read_srt(temporary)
            validate_translation(selected_source, replacement_blocks)
            replacements = {block.index: block for block in replacement_blocks}
            if set(replacements) != omission_indexes:
                raise RuntimeError(
                    "Targeted translation retry returned unexpected indexes: "
                    f"expected={sorted(omission_indexes)} actual={sorted(replacements)}"
                )
            replacement_events = read_translation_quality_events_strict(temporary)
            merged = [
                replacements.get(block.index, translated_by_index[block.index])
                for block in source_blocks
            ]
            # Remove the derived traditional cache before publishing a new
            # simplified cache. If this fails, the original rejected zh-CN
            # SRT and its omission sidecar remain intact for the next retry.
            paths.zh_tw_srt.unlink(missing_ok=True)
            write_srt(temporary, merged)
            retained_events = [
                event
                for event in events
                if int(event.get("index") or 0) not in omission_indexes
            ]
            merged_events = [*retained_events, *replacement_events]
            committed_events = list(merged_events)
            planned_sha256 = sha256_file(temporary)
            if merged_events:
                write_translation_quality_events(
                    paths.zh_cn_srt,
                    merged_events,
                    srt_sha256=planned_sha256,
                )
            else:
                write_translation_quality_hold(
                    paths.zh_cn_srt,
                    srt_sha256=planned_sha256,
                    reason="targeted merge pending derived zh-TW regeneration",
                )
            temporary.replace(paths.zh_cn_srt)
            if tm_origin is not None:
                if omission_indexes.intersection(tm_origin.cached_indexes):
                    # A repaired line is no longer TM-origin.  The old split
                    # digest cannot truthfully describe the new origin set, so
                    # conservatively disable learning for this publication.
                    remove_translation_memory_origin(
                        self.config.work_path,
                        paths.zh_cn_srt,
                    )
                    self.logger.warning(
                        "Translation repair changed a TM-origin block; learning lineage was discarded: "
                        "video=%s indexes=%s",
                        video,
                        sorted(omission_indexes.intersection(tm_origin.cached_indexes)),
                    )
                else:
                    try:
                        self._rebind_translation_memory_origin_after_qc(
                            video,
                            paths,
                            tm_origin,
                        )
                    except (OSError, TranslationMemoryBridgeError) as exc:
                        remove_translation_memory_origin(
                            self.config.work_path,
                            paths.zh_cn_srt,
                        )
                        self.logger.warning(
                            "Translation repair succeeded but TM lineage could not be rebound; learning disabled: "
                            "video=%s error=%s",
                            video,
                            exc,
                        )
        finally:
            temporary.unlink(missing_ok=True)
            temporary_events.unlink(missing_ok=True)
            temporary_hold.unlink(missing_ok=True)

        unresolved = [
            int(event.get("index") or 0)
            for event in committed_events
            if str(event.get("code") or "") == TRANSLATION_SAFE_OMISSION
        ]
        self.logger.warning(
            "Targeted safe-omission translation retry completed: "
            "video=%s requested=%s unresolved=%s",
            video,
            sorted(omission_indexes),
            sorted(index for index in unresolved if index > 0),
        )
        unresolved_indexes = sorted(index for index in unresolved if index > 0)
        if unresolved_indexes and allow_asr_escalation:
            padding = max(
                0.0,
                float(getattr(self.config, "asr_selective_retry_padding_seconds", 1.5) or 0),
            )
            asr_ranges = _normalize_review_ranges(
                [
                    (
                        max(0.0, _srt_timing_seconds(source_by_index[index].timing)[0] - padding),
                        _srt_timing_seconds(source_by_index[index].timing)[1] + padding,
                    )
                    for index in unresolved_indexes
                ]
            )
            audio_path = self._audio_path(video)
            preferred_audio = (
                self._selected_audio_stream
                or preferred_audio_stream_info(video)
            )
            audio_ready = validate_cached_audio(
                audio_path,
                video,
                stream_index=preferred_audio.index if preferred_audio else None,
            )
            if not audio_ready:
                self._set_stage(video, "audio", "running", "Extracting audio for translation-omission ASR escalation")
                self._extract_preferred_audio(video, audio_path)
                audio_ready = True
            fallback_config = (
                asr_fallback_config or self._prompt_free_fallback_asr_config()
            )
            selective_ok = (
                bool(getattr(self.config, "asr_selective_retry_enabled", True))
                and fallback_config.transcription_backend == "faster-whisper"
                and bool(asr_ranges)
                and self._try_prompt_free_selective_asr_repair(
                    video,
                    audio_path,
                    paths,
                    asr_ranges,
                    fallback_config,
                    require_confidence=False,
                    require_changed_transcript=True,
                )
            )
            if not selective_ok and asr_fallback_config is None:
                self._run_full_prompt_free_asr_fallback(
                    video,
                    audio_path,
                    paths,
                    audio_ready=audio_ready,
                    reason=(
                        "targeted translation remained unresolved; "
                        f"escalated subtitle indexes={unresolved_indexes}"
                    ),
                    fallback_config=fallback_config,
                )
            elif not selective_ok:
                self.logger.warning(
                    "Same-language selective ASR repair was unavailable; preserving "
                    "the source translation failure for bounded review: "
                    "video=%s language=%s indexes=%s",
                    video,
                    source_language,
                    unresolved_indexes,
                )
            if selective_ok or asr_fallback_config is None:
                self.logger.warning(
                    "Escalated unresolved translation lines to prompt-free ASR recovery: "
                    "video=%s indexes=%s ranges=%s",
                    video,
                    unresolved_indexes,
                    asr_ranges,
                )
        elif unresolved_indexes:
            self.logger.warning(
                "Final targeted translation retry remains unresolved; "
                "additional ASR escalation is disabled: video=%s indexes=%s",
                video,
                unresolved_indexes,
            )
        return True

    def _repair_translation_cps_violations(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        source_language: str = "ja",
    ) -> bool:
        """Retranslate only over-speed Chinese cues without changing the timeline."""

        if not bool(getattr(self.config, "subtitle_quality_check_enabled", True)):
            return False
        if not paths.ja_srt.is_file() or not paths.zh_cn_srt.is_file():
            return False

        source_blocks = read_srt(paths.ja_srt)
        translated_blocks = read_srt(paths.zh_cn_srt)
        validate_translation(source_blocks, translated_blocks)
        source_by_index = {block.index: block for block in source_blocks}
        fail_cps = max(
            1.0,
            float(getattr(self.config, "subtitle_quality_fail_cps", 25.0) or 25.0),
        )
        diagnostics: list[dict[str, object]] = []
        tm_origin = self._read_translation_memory_origin_for_video(video, paths)

        for repair_attempt in range(1, 3):
            report = analyze_subtitle_file(
                paths.zh_cn_srt,
                self.config,
                role="translated_zh_cn",
            )
            target_indexes = sorted(
                {
                    int(index)
                    for issue in report.issues
                    if str(issue.code) == "cps_too_high"
                    for index in issue.indexes
                    if int(index) > 0
                }
            )
            if not target_indexes:
                if diagnostics and self._provenance is not None:
                    self._provenance.update(
                        "translation_readability_repair",
                        diagnostics,
                    )
                return bool(diagnostics)

            translated_by_index = {
                block.index: block for block in read_srt(paths.zh_cn_srt)
            }
            if set(translated_by_index) != set(source_by_index):
                raise SubtitleQualityError(
                    "Translation readability repair found a stale zh-CN cache"
                )
            display_limits: dict[int, int] = {}
            for index in target_indexes:
                source = source_by_index.get(index)
                if source is None:
                    raise SubtitleQualityError(
                        f"Translation readability repair index is missing: {index}"
                    )
                start, end = _srt_timing_seconds(source.timing)
                display_limits[index] = max(1, int(max(0.0, end - start) * fail_cps))

            selected_source = [source_by_index[index] for index in target_indexes]
            repair_root = Path(self.config.work_path) / "ai_translation_repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
            temporary = repair_root / f"{digest}-cps-{time.time_ns()}.srt"
            temporary_events = translation_quality_events_path(temporary)
            temporary_hold = translation_quality_hold_path(temporary)
            self._set_stage(
                video,
                "translation",
                "running",
                f"Compressing {len(target_indexes)} over-speed subtitle line(s)",
            )
            self.logger.warning(
                "Automatically repairing translation reading speed: "
                "video=%s attempt=%s indexes=%s limits=%s",
                video,
                repair_attempt,
                target_indexes,
                display_limits,
            )
            try:
                series_context = self._build_series_metadata_context(video)
                translator = self._get_translator()
                translator.set_targeted_repair_context(
                    source_blocks,
                    list(translated_by_index.values()),
                    set(target_indexes),
                    series_context=(series_context.text if series_context else ""),
                )
                try:
                    repair_kwargs: dict[str, object] = {
                        "series_glossary": (
                            (series_context.glossary or {})
                            if series_context
                            else {}
                        ),
                        "max_display_chars_by_index": display_limits,
                    }
                    if str(source_language or "ja").strip().casefold() not in {
                        "ja",
                        "jpn",
                    }:
                        repair_kwargs["source_language"] = source_language
                    translator.retranslate_problem_blocks(
                        selected_source,
                        paths.ja_srt,
                        temporary,
                        **repair_kwargs,
                    )
                finally:
                    translator.clear_targeted_repair_context()

                replacement_hold = read_translation_quality_hold_strict(temporary)
                replacement_events = (
                    []
                    if replacement_hold is not None
                    else read_translation_quality_events_strict(temporary)
                )
                if any(
                    str(event.get("severity") or "").casefold() == "fail"
                    for event in replacement_events
                ):
                    raise SubtitleQualityError(
                        "Translation readability repair returned a blocking quality event"
                    )
                replacement_blocks = read_srt(temporary)
                validate_translation(selected_source, replacement_blocks)
                replacements = {block.index: block for block in replacement_blocks}
                if set(replacements) != set(target_indexes):
                    raise SubtitleQualityError(
                        "Translation readability repair returned unexpected indexes: "
                        f"expected={target_indexes} actual={sorted(replacements)}"
                    )

                merged = [
                    replacements.get(block.index, translated_by_index[block.index])
                    for block in source_blocks
                ]
                validate_translation(source_blocks, merged)
                paths.zh_tw_srt.unlink(missing_ok=True)
                write_srt(temporary, merged)
                planned_sha256 = sha256_file(temporary)
                write_translation_quality_hold(
                    paths.zh_cn_srt,
                    srt_sha256=planned_sha256,
                    reason="targeted CPS repair pending derived zh-TW regeneration",
                )
                temporary.replace(paths.zh_cn_srt)
                if tm_origin is not None:
                    remove_translation_memory_origin(
                        self.config.work_path,
                        paths.zh_cn_srt,
                    )
                    tm_origin = None
                    self.logger.warning(
                        "Translation readability repair changed output text; "
                        "TM learning lineage was conservatively discarded: video=%s",
                        video,
                    )
            finally:
                temporary.unlink(missing_ok=True)
                temporary_events.unlink(missing_ok=True)
                temporary_hold.unlink(missing_ok=True)

            rechecked = analyze_subtitle_file(
                paths.zh_cn_srt,
                self.config,
                role="translated_zh_cn",
            )
            diagnostics.append(
                {
                    "attempt": repair_attempt,
                    "indexes": target_indexes,
                    "display_limits": display_limits,
                    "recheck": rechecked.to_dict(),
                }
            )
            self.logger.info(
                "Re-ran full subtitle QC after targeted reading-speed repair: "
                "video=%s attempt=%s result=%s",
                video,
                repair_attempt,
                summarize_quality_report(rechecked),
            )

        final_report = analyze_subtitle_file(
            paths.zh_cn_srt,
            self.config,
            role="translated_zh_cn",
        )
        if diagnostics and self._provenance is not None:
            self._provenance.update("translation_readability_repair", diagnostics)
        if any(str(issue.code) == "cps_too_high" for issue in final_report.issues):
            raise SubtitleQualityError(summarize_quality_report(final_report))
        return bool(diagnostics)

    def _validate_translation_cache_chain(
        self,
        video: Path,
        paths: SubtitlePaths,
    ) -> None:
        hold = translation_quality_hold_path(paths.zh_cn_srt)
        if hold.is_file():
            try:
                hold_payload = read_translation_quality_hold_strict(paths.zh_cn_srt)
                if hold_payload is None:
                    raise SubtitleQualityError("translation hold disappeared during validation")
                if not paths.ja_srt.is_file() or not paths.zh_cn_srt.is_file():
                    raise SubtitleQualityError("translation hold has no complete JA/zh-CN pair")
                validate_translation(read_srt(paths.ja_srt), read_srt(paths.zh_cn_srt))
                if bool(getattr(self.config, "translation_memory_enabled", True)):
                    origin = self._read_translation_memory_origin_for_video(video, paths)
                    if origin is None:
                        raise SubtitleQualityError(
                            "translation hold has no valid hash-bound TM lineage"
                        )
            except (OSError, ValueError, RuntimeError) as exc:
                self.logger.warning(
                    "Invalidating interrupted translation cache commit: video=%s hold=%s error=%s",
                    video,
                    hold,
                    exc,
                )
                self._invalidate_translation_intermediates(paths)
                return
            # The complete zh-CN commit is durable.  Preserve it and restart at
            # the derived OpenCC stage; stale zh-TW cannot be trusted across the
            # crash boundary.
            paths.zh_tw_srt.unlink(missing_ok=True)
            self.logger.info(
                "Resuming interrupted translation after durable zh-CN commit: video=%s hold=%s",
                video,
                hold,
            )
            return

        if not paths.ja_srt.is_file():
            self._quarantine_unverifiable_translation_cache(video, paths)
            return

        if not paths.zh_cn_srt.is_file():
            # Derived caches must be gone before a new zh-CN is created. A
            # deletion failure aborts while zh-CN is still absent.
            paths.zh_tw_srt.unlink(missing_ok=True)
            translation_quality_events_path(paths.zh_cn_srt).unlink(missing_ok=True)
            hold.unlink(missing_ok=True)
            return

        try:
            ja_blocks = read_srt(paths.ja_srt)
            zh_cn_blocks = read_srt(paths.zh_cn_srt)
            validate_translation(ja_blocks, zh_cn_blocks)
            read_translation_quality_events_strict(paths.zh_cn_srt)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warning(
                "Invalidating mismatched Japanese/zh-CN cache chain: "
                "video=%s error=%s",
                video,
                exc,
            )
            self._invalidate_translation_intermediates(paths)
            return

        if not paths.zh_tw_srt.is_file():
            return
        try:
            validate_translation(zh_cn_blocks, read_srt(paths.zh_tw_srt))
        except (OSError, ValueError) as exc:
            self.logger.warning(
                "Removing stale zh-TW cache that does not match zh-CN: "
                "video=%s error=%s",
                video,
                exc,
            )
            paths.zh_tw_srt.unlink(missing_ok=True)

    def _finalize_translation_cache_commit(self, paths: SubtitlePaths) -> None:
        hold = read_translation_quality_hold_strict(paths.zh_cn_srt)
        if hold is None:
            return
        validate_translation(
            read_srt(paths.zh_cn_srt),
            read_srt(paths.zh_tw_srt),
        )
        # Old failure events are cleared only after both the new zh-CN and its
        # regenerated zh-TW are durable and structurally paired.
        write_translation_quality_events(paths.zh_cn_srt, [])
        translation_quality_hold_path(paths.zh_cn_srt).unlink()

    @staticmethod
    def _invalidate_translation_intermediates(paths: SubtitlePaths) -> None:
        fail_closed_translation_output(
            paths.zh_cn_srt,
            reason="translation cache invalidated by worker validation",
        )
        paths.zh_tw_srt.unlink(missing_ok=True)
        if paths.zh_tw_srt.exists():
            raise OSError(
                f"Unable to remove stale traditional translation cache: {paths.zh_tw_srt}"
            )

    def _refresh_cached_japanese_leading_gap(
        self,
        video: Path,
        audio_path: Path,
        paths: SubtitlePaths,
        *,
        audio_ready: bool,
    ) -> bool:
        """Probe the uncovered opening before trusting an older Japanese SRT.

        Older cache files predate the short overlapping opening pass.  Reusing
        them blindly also reuses missing first lines forever.  A bounded,
        prompt-free ASR probe either adds real opening speech or records that
        the range contained no speech, so translation retries do not repeat
        the probe.
        """

        review_range = self._cached_japanese_leading_gap_range(paths.ja_srt)
        if review_range is None:
            return audio_ready
        signature = self._leading_gap_cache_signature(video, paths.ja_srt, review_range)
        if self._leading_gap_cache_probe_is_current(paths.ja_srt, signature):
            self.logger.info(
                "Japanese SRT opening cache already verified: path=%s range=%.1f-%.1fs",
                paths.ja_srt,
                review_range[0],
                review_range[1],
            )
            return audio_ready

        backend = _japanese_transcription_backend(self.config)
        if backend != "faster-whisper":
            self.logger.warning(
                "Cannot selectively verify cached Japanese SRT opening with backend=%s; "
                "the cache will not be marked verified: %s",
                backend,
                paths.ja_srt,
            )
            return audio_ready
        if not audio_ready:
            self._set_stage(video, "audio", "running", "Extracting audio for cached opening verification")
            self.logger.info("Extracting audio to verify cached Japanese SRT opening: %s", video)
            self._extract_preferred_audio(video, audio_path)
            audio_ready = True

        repair_config = _config_with_overrides(
            self.config,
            transcription_backend="faster-whisper",
            whisper_model=_japanese_transcription_model(self.config),
            whisper_language="ja",
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
            whisper_no_speech_threshold=float(
                getattr(self.config, "gap_rescue_no_speech_threshold", 0.95)
            ),
            whisper_log_prob_threshold=float(
                getattr(self.config, "gap_rescue_log_prob_threshold", -1.5)
            ),
            whisper_compression_ratio_threshold=float(
                getattr(self.config, "gap_rescue_compression_ratio_threshold", 2.4)
            ),
            transcription_quality_check_enabled=False,
            enable_gap_rescue=False,
            enable_leading_gap_rescue=False,
            op_ed_transcription_enabled=False,
            write_gap_report=False,
            asr_diagnostics_enabled=False,
        )
        before_sha256 = str(signature["srt_sha256"])
        self._set_active_transcription_stage(
            "running",
            f"Verifying cached opening {review_range[0]:.1f}-{review_range[1]:.1f}s "
            f"with model={repair_config.whisper_model}",
        )
        hold = self._begin_asr_commit(
            paths.ja_srt,
            reason="cached Japanese leading-gap ASR repair in progress",
            active_config=repair_config,
        )
        try:
            try:
                repair_result = repair_low_confidence_ranges(
                    audio_path,
                    paths.ja_srt,
                    [review_range],
                    repair_config,
                    self.logger,
                )
                finalize_repaired_transcription(
                    audio_path,
                    paths.ja_srt,
                    [review_range],
                    _config_with_overrides(
                        self.config,
                        whisper_initial_prompt=None,
                        op_ed_initial_prompt=None,
                    ),
                    self.logger,
                    segment_confidences=getattr(
                        repair_result,
                        "segment_confidences",
                        (),
                    ),
                )
            except TranscriptionError as exc:
                message = str(exc)
                no_speech = (
                    "Whisper returned no subtitle segments" in message
                    or "Selective ASR fallback returned no subtitle blocks" in message
                )
                if not no_speech:
                    reason = (
                        "cached Japanese opening verification rejected: "
                        f"{_compact_error_message(exc)}"
                    )
                    self.logger.warning(
                        "Cached Japanese opening verification was rejected; "
                        "switching to full prompt-free ASR fallback: "
                        "video=%s range=%.1f-%.1fs error=%s",
                        video,
                        review_range[0],
                        review_range[1],
                        exc,
                    )
                    rebuilt = self._run_full_prompt_free_asr_fallback(
                        video,
                        audio_path,
                        paths,
                        audio_ready=audio_ready,
                        reason=reason,
                    )
                    if rebuilt and paths.ja_srt.is_file():
                        rebuilt_range = self._cached_japanese_leading_gap_range(
                            paths.ja_srt
                        )
                        if rebuilt_range is not None:
                            self._write_leading_gap_cache_probe(
                                paths.ja_srt,
                                self._leading_gap_cache_signature(
                                    video,
                                    paths.ja_srt,
                                    rebuilt_range,
                                ),
                                status="full_prompt_free_rebuilt",
                            )
                    return rebuilt
                if (
                    not paths.ja_srt.is_file()
                    or sha256_file(paths.ja_srt) != before_sha256
                ):
                    raise TranscriptionError(
                        "Cached opening ASR reported no speech after mutating "
                        "the live Japanese transcript"
                    ) from exc
                self._write_leading_gap_cache_probe(
                    paths.ja_srt,
                    signature,
                    status="no_speech_detected",
                )
                self._finish_asr_commit(
                    paths.ja_srt,
                    hold,
                    label="Cached Japanese opening verification",
                )
                self.logger.info(
                    "Cached Japanese SRT opening probe found no additional speech: "
                    "path=%s range=%.1f-%.1fs",
                    paths.ja_srt,
                    review_range[0],
                    review_range[1],
                )
                return audio_ready

            self._validate_srt_output(
                paths.ja_srt,
                "Cached Japanese opening ASR repair",
            )
            after_sha256 = sha256_file(paths.ja_srt)
            updated_signature = self._leading_gap_cache_signature(
                video,
                paths.ja_srt,
                review_range,
            )
            changed = after_sha256 != before_sha256
            self._write_leading_gap_cache_probe(
                paths.ja_srt,
                updated_signature,
                status="repaired" if changed else "verified_unchanged",
            )
            if changed:
                asr_diagnostics_path(
                    paths.ja_srt,
                    self.config,
                ).unlink(missing_ok=True)
                # The translated intermediates no longer have the same source
                # bytes.  Keep the ASR hold until invalidation is durable.
                self._invalidate_translation_intermediates(paths)
            self._finish_asr_commit(
                paths.ja_srt,
                hold,
                label="Cached Japanese opening ASR repair",
            )
            if changed:
                self.logger.warning(
                    "Recovered missing opening lines from cached Japanese SRT; "
                    "stale translated intermediates were invalidated: "
                    "video=%s path=%s",
                    video,
                    paths.ja_srt,
                )
            return audio_ready
        except Exception as exc:
            self._fail_closed_asr_output(
                paths.ja_srt,
                reason=(
                    "Cached Japanese opening ASR repair failed: "
                    f"{_compact_error_message(exc)}"
                ),
            )
            if not paths.ja_srt.exists():
                try:
                    self._invalidate_translation_intermediates(paths)
                except Exception as cleanup_error:  # noqa: BLE001 - preserve pending hold.
                    self.logger.warning(
                        "Failed to invalidate translations after leading-gap "
                        "ASR failure; pending marker retained: video=%s error=%s",
                        video,
                        cleanup_error,
                    )
                else:
                    hold.unlink(missing_ok=True)
            raise

    def _cached_japanese_leading_gap_range(self, ja_srt: Path) -> tuple[float, float] | None:
        if not bool(getattr(self.config, "enable_leading_gap_rescue", True)):
            return None
        blocks = read_srt(ja_srt)
        if not blocks:
            return None
        first_start = min(_srt_timing_seconds(block.timing)[0] for block in blocks)
        threshold = max(
            0.1,
            float(getattr(self.config, "gap_rescue_leading_threshold_seconds", 1.5)),
        )
        if first_start < threshold:
            return None
        maximum = max(
            threshold,
            float(getattr(self.config, "gap_rescue_leading_max_seconds", 120.0)),
        )
        return (0.0, min(first_start, maximum))

    def _quarantine_unverifiable_translation_cache(
        self,
        video: Path,
        paths: SubtitlePaths,
    ) -> list[Path]:
        """Preserve but stop reusing translations that have no source SRT.

        Without the Japanese timing/index source, a translated cache cannot be
        checked for a missing opening and cannot be safely paired with a new
        transcript.  These are intermediate files under ``/work``; move them
        into a reversible quarantine and rebuild them from audio.
        """

        if paths.ja_srt.exists():
            return []
        candidates = [
            path
            for path in (
                paths.zh_cn_srt,
                paths.zh_tw_srt,
                translation_quality_events_path(paths.zh_cn_srt),
                translation_quality_hold_path(paths.zh_cn_srt),
                asr_diagnostics_path(paths.ja_srt, self.config),
                asr_transcription_hold_path(paths.ja_srt, self.config),
            )
            if path.is_file()
        ]
        if not candidates:
            return []
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        quarantine = (
            Path(self.config.work_path)
            / "ai_cache_quarantine"
            / "missing_japanese_source"
            / f"{digest}-{time.time_ns()}"
        )
        quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        for source in candidates:
            destination = quarantine / source.name
            verified_move(source, destination)
            moved.append(destination)
        self.logger.warning(
            "Quarantined translated SRT cache without Japanese source; "
            "ASR and translation will be rebuilt: video=%s files=%s quarantine=%s",
            video,
            len(moved),
            quarantine,
        )
        return moved

    def _leading_gap_cache_probe_path(self, ja_srt: Path) -> Path:
        configured = Path(
            str(getattr(self.config, "asr_diagnostics_path", "asr_diagnostics") or "asr_diagnostics")
        )
        root = configured if configured.is_absolute() else Path(self.config.work_path) / configured
        digest = hashlib.sha1(str(ja_srt.resolve()).encode("utf-8")).hexdigest()[:20]
        return root / "leading_gap_cache" / f"{digest}.json"

    def _leading_gap_cache_signature(
        self,
        video: Path,
        ja_srt: Path,
        review_range: tuple[float, float],
    ) -> dict[str, object]:
        video_stat = video.stat()
        return {
            "policy_version": LEADING_GAP_CACHE_POLICY_VERSION,
            "video_path": str(video),
            "video_size": int(video_stat.st_size),
            "video_mtime_ns": int(video_stat.st_mtime_ns),
            "srt_path": str(ja_srt),
            "srt_sha256": sha256_file(ja_srt),
            "review_range": [round(review_range[0], 3), round(review_range[1], 3)],
            "model": _japanese_transcription_model(self.config),
        }

    def _leading_gap_cache_probe_is_current(
        self,
        ja_srt: Path,
        signature: dict[str, object],
    ) -> bool:
        marker = self._leading_gap_cache_probe_path(ja_srt)
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return all(payload.get(key) == value for key, value in signature.items()) and str(
            payload.get("status", "")
        ) in {
            "no_speech_detected",
            "repaired",
            "verified_unchanged",
            "full_prompt_free_rebuilt",
        }

    def _write_leading_gap_cache_probe(
        self,
        ja_srt: Path,
        signature: dict[str, object],
        *,
        status: str,
    ) -> None:
        marker = self._leading_gap_cache_probe_path(ja_srt)
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **signature,
            "status": status,
            "verified_at": time.time(),
        }
        temporary = marker.with_name(f".{marker.name}.{time.time_ns()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _export_ai_ass(self, paths: SubtitlePaths) -> int:
        exported = 0
        if paths.ja_srt.exists():
            convert_srt_file_to_ass(paths.ja_srt, paths.ai_ja_ass, self._ass_style)
            self.logger.info("Exported AI Japanese ASS: %s", paths.ai_ja_ass)
            exported += 1
        if paths.zh_cn_srt.exists():
            if paths.ja_srt.exists():
                convert_bilingual_srt_files_to_ass(paths.zh_cn_srt, paths.ja_srt, paths.ai_zh_cn_ass, self._ass_style)
                self.logger.info("Exported bilingual AI simplified Chinese ASS: %s", paths.ai_zh_cn_ass)
            else:
                convert_srt_file_to_ass(paths.zh_cn_srt, paths.ai_zh_cn_ass, self._ass_style)
                self.logger.info("Exported AI simplified Chinese ASS: %s", paths.ai_zh_cn_ass)
            exported += 1
        if paths.zh_tw_srt.exists():
            if paths.ja_srt.exists():
                convert_bilingual_srt_files_to_ass(paths.zh_tw_srt, paths.ja_srt, paths.ai_zh_tw_ass, self._ass_style)
                self.logger.info("Exported bilingual AI traditional Chinese ASS: %s", paths.ai_zh_tw_ass)
            else:
                convert_srt_file_to_ass(paths.zh_tw_srt, paths.ai_zh_tw_ass, self._ass_style)
                self.logger.info("Exported AI traditional Chinese ASS: %s", paths.ai_zh_tw_ass)
            exported += 1
        return exported

    def _publish_ai_ass(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        source_language: str = "ja",
    ) -> int:
        """Validate a complete staged ASS set before replacing media sidecars."""

        normalized_source_language = str(source_language or "ja").split("-", 1)[0].casefold()
        source_is_japanese = normalized_source_language in {"ja", "jpn"}
        self._enforce_asr_publication_gate(
            paths.ja_srt,
            label=(
                "Japanese"
                if source_is_japanese
                else f"Source-language:{source_language}"
            ),
        )
        srt_snapshots: dict[Path, bytes] = {}
        for srt_path in (paths.ja_srt, paths.zh_cn_srt, paths.zh_tw_srt):
            if srt_path.is_file():
                srt_snapshots[srt_path] = srt_path.read_bytes()
        asr_diagnostic = asr_diagnostics_path(paths.ja_srt, self.config)
        asr_diagnostic_existed = asr_diagnostic.is_file()
        asr_diagnostic_snapshot = (
            asr_diagnostic.read_bytes() if asr_diagnostic_existed else None
        )
        staging_root: Path | None = None
        staged_outputs: list[Path] = []
        try:
            self._remediate_prepublication_srts(
                video,
                paths,
                source_language=source_language,
            )
            self._enforce_asr_publication_gate(
                paths.ja_srt,
                label=(
                    "Japanese"
                    if source_is_japanese
                    else f"Source-language:{source_language}"
                ),
            )
            run_token = f"{time.time_ns()}-{hashlib.sha1(str(video).encode('utf-8')).hexdigest()[:8]}"
            video_digest = hashlib.sha1(str(video.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
            staging_root = Path(self.config.work_path) / "ai_publish_staging" / video_digest / run_token
            staging_root.mkdir(parents=True, exist_ok=False)
            staged = SubtitlePaths(
                ja_srt=paths.ja_srt,
                zh_cn_srt=paths.zh_cn_srt,
                zh_tw_srt=paths.zh_tw_srt,
                ai_ja_ass=staging_root / "ja.ass",
                ai_zh_cn_ass=staging_root / "zh-CN.ass",
                ai_zh_tw_ass=staging_root / "zh-TW.ass",
            )
            staged_outputs = [staged.ai_ja_ass, staged.ai_zh_cn_ass, staged.ai_zh_tw_ass]
            destinations = [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass]
            exported = self._export_ai_ass(staged)
            if exported != 3 or not all(path.is_file() for path in staged_outputs):
                raise SubtitleQualityError(
                    f"Incomplete ASS staging set for {video}: exported={exported} expected=3"
                )
            reports = self._quality_check_ai_outputs(
                video,
                [
                    (
                        staged.ai_ja_ass,
                        "japanese" if source_is_japanese else "unknown",
                    ),
                    (staged.ai_zh_cn_ass, "translated_zh_cn"),
                    (staged.ai_zh_tw_ass, "translated_zh_tw"),
                ],
                discard_on_failure=False,
                persist_reports=False,
            )
            self._replace_ai_outputs_with_rollback(video, staged_outputs, destinations)
            self._persist_validated_quality_reports(reports, destinations)
            return exported
        except Exception as exc:
            restore_errors: list[str] = []
            for srt_path, original in srt_snapshots.items():
                try:
                    atomic_write_bytes(srt_path, original)
                except OSError as restore_exc:
                    restore_errors.append(f"{srt_path}: {restore_exc}")
            try:
                if asr_diagnostic_existed and asr_diagnostic_snapshot is not None:
                    atomic_write_bytes(asr_diagnostic, asr_diagnostic_snapshot)
                else:
                    asr_diagnostic.unlink(missing_ok=True)
            except OSError as restore_exc:
                restore_errors.append(f"{asr_diagnostic}: {restore_exc}")
            if restore_errors:
                raise SubtitleQualityError(
                    "AI publication failed and repaired SRT rollback was incomplete: "
                    + "; ".join(restore_errors)
                ) from exc
            raise
        finally:
            for path in staged_outputs:
                path.unlink(missing_ok=True)
                quality_report_path(path).unlink(missing_ok=True)
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)

    def _remediate_prepublication_srts(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        source_language: str = "ja",
    ) -> list[dict[str, object]]:
        """Run bounded deterministic SRT repair and always re-run full QC.

        Only explicitly safe issue codes are eligible.  ASR hallucinations,
        residual Japanese, prompt leakage, omissions and semantic failures are
        deliberately left to the existing targeted recovery/review paths.
        Every changed candidate is revalidated structurally and with the
        ordinary subtitle-quality gate before ASS staging can begin.
        """

        if not bool(getattr(self.config, "subtitle_quality_check_enabled", True)):
            return []
        diagnostics = self._remediate_aligned_timing_bundle(
            video,
            paths,
            source_language=source_language,
        )
        targets = (
            (paths.zh_cn_srt, "translated_zh_cn", "zh-CN"),
            (paths.zh_tw_srt, "translated_zh_tw", "zh-TW"),
        )
        safe_codes = {
            "simplified_chinese_remnant",
            "glossary_term_inconsistent",
            "repeated_punctuation",
            "long_line",
            "very_long_line",
        }
        active_glossary = dict(getattr(self.config, "translation_glossary", {}) or {})
        for srt_path, qc_role, remediation_role in targets:
            if not srt_path.is_file():
                continue
            round_number = 0
            while round_number < 2:
                report = analyze_subtitle_file(srt_path, self.config, role=qc_role)
                issue_codes = sorted(
                    {
                        str(issue.code)
                        for issue in report.issues
                        if str(issue.code) in safe_codes
                    }
                )
                if not issue_codes:
                    if report.has_failures:
                        raise SubtitleQualityError(summarize_quality_report(report))
                    break
                if round_number == 0:
                    try:
                        round_number = next_remediation_round(
                            srt_path,
                            work_path=self.config.work_path,
                        )
                    except (SubtitleRemediationError, RemediationRoundLimitError) as exc:
                        self.logger.warning(
                            "Subtitle remediation lineage cannot resume: video=%s path=%s error=%s",
                            video,
                            srt_path,
                            exc,
                        )
                        break
                before_blocks = read_srt(srt_path)
                try:
                    result = remediate_srt_in_place(
                        srt_path,
                        role=remediation_role,
                        issue_codes=issue_codes,
                        config=self.config,
                        glossary=active_glossary,
                        work_path=self.config.work_path,
                        round_number=round_number,
                    )
                except (SubtitleRemediationError, RemediationRoundLimitError) as exc:
                    self.logger.warning(
                        "Bounded subtitle remediation refused candidate: video=%s path=%s round=%s error=%s",
                        video,
                        srt_path,
                        round_number,
                        exc,
                    )
                    break
                if not result.changed or not result.requires_qc:
                    break
                after_blocks = read_srt(srt_path)
                # Remediation may make a narrowly bounded timing adjustment,
                # so the ordinary translation invariant (identical timings)
                # is intentionally too strict here.  The remediation engine
                # has already validated its hard timing budget; the Worker
                # independently reasserts the structural invariants and then
                # runs the complete subtitle-quality gate below.
                validate_srt_structure(after_blocks)
                if len(before_blocks) != len(after_blocks):
                    raise SubtitleQualityError(
                        f"Subtitle remediation changed cue count for {srt_path}"
                    )
                validate_same_numbering(before_blocks, after_blocks)
                rechecked = analyze_subtitle_file(srt_path, self.config, role=qc_role)
                diagnostics.append(
                    {
                        "path": str(srt_path),
                        "round": round_number,
                        "diagnostic_path": result.diagnostic_path,
                        "input_sha256": result.input_sha256,
                        "output_sha256": result.output_sha256,
                        "applied_rules": list(result.applied_rules),
                        "recheck": rechecked.to_dict(),
                    }
                )
                self.logger.info(
                    "Re-ran full subtitle QC after bounded remediation: video=%s path=%s round=%s result=%s",
                    video,
                    srt_path,
                    round_number,
                    summarize_quality_report(rechecked),
                )
                if not rechecked.has_failures:
                    break
                round_number += 1
            final_report = analyze_subtitle_file(srt_path, self.config, role=qc_role)
            if final_report.has_failures:
                raise SubtitleQualityError(summarize_quality_report(final_report))
        if diagnostics and self._provenance is not None:
            self._provenance.update("subtitle_remediation", diagnostics)
        return diagnostics

    def _remediate_aligned_timing_bundle(
        self,
        video: Path,
        paths: SubtitlePaths,
        *,
        source_language: str,
    ) -> list[dict[str, object]]:
        """Repair timing-only failures across an exactly aligned SRT bundle."""

        tracks = (
            (paths.ja_srt, "japanese", "source"),
            (paths.zh_cn_srt, "translated_zh_cn", "zh-CN"),
            (paths.zh_tw_srt, "translated_zh_tw", "zh-TW"),
        )
        if not all(path.is_file() for path, _qc_role, _repair_role in tracks):
            return []

        normalized_source = str(source_language or "ja").split("-", 1)[0].casefold()
        if normalized_source not in {"ja", "jpn"}:
            tracks = (
                (paths.ja_srt, "unknown", "source"),
                tracks[1],
                tracks[2],
            )
        original_blocks = {path: read_srt(path) for path, _qc, _repair in tracks}
        validate_translation(original_blocks[paths.ja_srt], original_blocks[paths.zh_cn_srt])
        validate_translation(original_blocks[paths.ja_srt], original_blocks[paths.zh_tw_srt])

        reports = [
            analyze_subtitle_file(path, self.config, role=qc_role)
            for path, qc_role, _repair_role in tracks
        ]
        failure_issues = [
            issue
            for report in reports
            for issue in report.issues
            if str(issue.severity) == "fail"
        ]
        failure_codes = {str(issue.code) for issue in failure_issues}
        # Timing may be changed only when every hard failure is mechanical and
        # the three tracks are still exactly aligned.  A hard CPS failure is
        # eligible only when the same cue also carries duration evidence; this
        # prevents semantic/verbose translations from stretching the source
        # timeline when retranslation is the correct remedy.
        allowed_failure_codes = {"timing_overlap", "too_short", "cps_too_high"}
        if not failure_codes or failure_codes - allowed_failure_codes:
            return []
        repair_codes: set[str] = set()
        duration_indexes: set[int] = set()
        aligned_target_min_duration = float(
            getattr(self.config, "subtitle_quality_min_duration_seconds", 0.35)
        )
        if "timing_overlap" in failure_codes:
            repair_codes.add("timing_overlap")
            if not self._timing_overlap_ranges(original_blocks[paths.ja_srt]):
                return []
        if "too_short" in failure_codes:
            repair_codes.add("too_short")
            duration_indexes.update(
                int(index)
                for issue in failure_issues
                if str(issue.code) == "too_short"
                for index in issue.indexes
            )
        if "cps_too_high" in failure_codes:
            cps_indexes = {
                int(index)
                for issue in failure_issues
                if str(issue.code) == "cps_too_high"
                for index in issue.indexes
            }
            duration_issues = [
                issue
                for report in reports
                for issue in report.issues
                if str(issue.code) in {"too_short", "short_duration"}
            ]
            duration_evidence_indexes = {
                int(index)
                for issue in duration_issues
                for index in issue.indexes
            }
            if not cps_indexes or not cps_indexes.issubset(duration_evidence_indexes):
                return []
            duration_indexes.update(cps_indexes)
            fail_cps_limit = float(
                getattr(self.config, "subtitle_quality_fail_cps", 25.0)
            )
            if not 1.0 <= fail_cps_limit <= 100.0:
                return []
            block_maps = [
                {int(block.index): block for block in blocks}
                for blocks in original_blocks.values()
            ]
            for index in cps_indexes:
                display_length = max(
                    len("".join(" ".join(block_map[index].text).split()))
                    for block_map in block_maps
                )
                aligned_target_min_duration = max(
                    aligned_target_min_duration,
                    (display_length / fail_cps_limit) + 0.001,
                )
            # The deterministic remediation engine has a hard one-second
            # duration target ceiling.  Anything larger belongs to semantic
            # line shortening/retranslation rather than timeline stretching.
            if aligned_target_min_duration > 1.0:
                return []
            repair_codes.discard("too_short")
            repair_codes.add("short_duration")
        if not repair_codes:
            return []
        remediation_config = _config_with_overrides(
            self.config,
            subtitle_remediation_max_timing_shift_seconds=float(
                getattr(
                    self.config,
                    "subtitle_remediation_aligned_max_timing_shift_seconds",
                    2.0,
                )
            ),
            subtitle_remediation_max_total_timing_shift_seconds=float(
                getattr(
                    self.config,
                    "subtitle_remediation_aligned_max_total_timing_shift_seconds",
                    3.0,
                )
            ),
            subtitle_remediation_max_overlap_repair_seconds=float(
                getattr(
                    self.config,
                    "subtitle_remediation_aligned_max_overlap_repair_seconds",
                    2.0,
                )
            ),
            subtitle_quality_min_duration_seconds=aligned_target_min_duration,
        )
        video_digest = hashlib.sha1(
            str(video.resolve()).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        staging_root = (
            Path(self.config.work_path)
            / "subtitle_remediation_bundle"
            / video_digest
            / str(time.time_ns())
        )
        staging_root.mkdir(parents=True, exist_ok=False)
        candidates: dict[Path, Path] = {}
        remediation_results = []
        for position, (source, _qc_role, repair_role) in enumerate(tracks):
            candidate = staging_root / f"{position:02d}-{source.name}"
            result = remediate_srt(
                source,
                candidate,
                role=repair_role,
                issue_codes=tuple(sorted(repair_codes)),
                config=remediation_config,
                work_path=staging_root,
                round_number=1,
                timing_duration_indexes=(
                    sorted(duration_indexes) if duration_indexes else None
                ),
            )
            if not result.changed or not candidate.is_file():
                raise SubtitleQualityError(
                    f"Aligned timing remediation refused track: {source}"
                )
            candidates[source] = candidate
            remediation_results.append(result)

        candidate_blocks = {source: read_srt(path) for source, path in candidates.items()}
        validate_translation(candidate_blocks[paths.ja_srt], candidate_blocks[paths.zh_cn_srt])
        validate_translation(candidate_blocks[paths.ja_srt], candidate_blocks[paths.zh_tw_srt])
        rechecked = [
            analyze_subtitle_file(candidates[path], self.config, role=qc_role)
            for path, qc_role, _repair_role in tracks
        ]
        failed = [report for report in rechecked if report.has_failures]
        if failed:
            raise SubtitleQualityError(
                "Aligned timing remediation failed full QC: "
                + "; ".join(summarize_quality_report(report) for report in failed)
            )

        changed_indexes = {
            int(index)
            for result in remediation_results
            for index in result.changed_indexes
        }
        repaired_ranges = [
            _srt_timing_seconds(block.timing)
            for block in original_blocks[paths.ja_srt]
            if int(block.index) in changed_indexes
        ]
        if not repaired_ranges:
            raise SubtitleQualityError(
                "Aligned timing remediation produced no evidence-bound timing ranges"
            )

        originals = {path: path.read_bytes() for path, _qc, _repair in tracks}
        diagnostic_path = asr_diagnostics_path(paths.ja_srt, self.config)
        diagnostic_existed = diagnostic_path.is_file()
        diagnostic_original = diagnostic_path.read_bytes() if diagnostic_existed else None
        hold: Path | None = None
        try:
            hold = self._begin_asr_commit(
                paths.ja_srt,
                reason="aligned trilingual timing remediation",
            )
            for source, candidate in candidates.items():
                atomic_write_bytes(source, candidate.read_bytes())

            if bool(getattr(self.config, "asr_diagnostics_enabled", True)):
                audio_path = self._audio_path(video)
                if not audio_path.is_file():
                    self._extract_preferred_audio(video, audio_path)
                diagnostic = write_asr_acceptance_diagnostics(
                    paths.ja_srt,
                    audio_path,
                    self.config,
                    status="accepted",
                    repaired_ranges=repaired_ranges,
                )
                if diagnostic is None:
                    raise SubtitleQualityError(
                        "Aligned timing remediation could not write ASR diagnostics"
                    )
                bound = attach_asr_diagnostics_context(
                    paths.ja_srt,
                    self.config,
                    media_path=video,
                    audio_path=audio_path,
                    audio_stream=self._selected_audio_stream,
                )
                if not bound:
                    raise SubtitleQualityError(
                        "Aligned timing remediation could not bind ASR diagnostics"
                    )
            self._finish_asr_commit(
                paths.ja_srt,
                hold,
                label="Aligned trilingual timing remediation",
            )
            hold = None
        except Exception:
            for path, original in originals.items():
                atomic_write_bytes(path, original)
            if diagnostic_existed and diagnostic_original is not None:
                atomic_write_bytes(diagnostic_path, diagnostic_original)
            else:
                diagnostic_path.unlink(missing_ok=True)
            if hold is not None:
                hold.unlink(missing_ok=True)
            raise

        diagnostics = [
            {
                "path": str(source),
                "round": result.round_number,
                "diagnostic_path": result.diagnostic_path,
                "input_sha256": result.input_sha256,
                "output_sha256": result.output_sha256,
                "applied_rules": list(result.applied_rules),
                "recheck": report.to_dict(),
                "bundle": "aligned_trilingual_timing",
            }
            for (source, _qc, _repair), result, report in zip(
                tracks,
                remediation_results,
                rechecked,
                strict=True,
            )
        ]
        self.logger.warning(
            "Safely repaired aligned trilingual timing overlap: video=%s ranges=%s",
            video,
            repaired_ranges,
        )
        return diagnostics

    def _timing_overlap_ranges(
        self,
        blocks: list[SrtBlock],
    ) -> list[tuple[float, float]]:
        tolerance = float(getattr(self.config, "subtitle_quality_max_overlap_seconds", 0.10))
        ordered = sorted(
            (_srt_timing_seconds(block.timing) for block in blocks),
            key=lambda value: (value[0], value[1]),
        )
        if not ordered:
            return []
        active_start, active_end = ordered[0]
        ranges: list[tuple[float, float]] = []
        for current_start, current_end in ordered[1:]:
            if active_end - current_start > tolerance:
                ranges.append((current_start, active_end))
            if current_end > active_end:
                active_start, active_end = current_start, current_end
        return _normalize_review_ranges(ranges)

    def _publish_source_ass(self, video: Path, source_srt: Path, destination: Path) -> None:
        """Quality-gate one non-Japanese source transcript before publication."""

        self._enforce_asr_publication_gate(
            source_srt,
            label="source-language",
        )
        run_token = f"{time.time_ns()}-{hashlib.sha1(str(video).encode('utf-8')).hexdigest()[:8]}"
        video_digest = hashlib.sha1(str(video.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
        staging_root = Path(self.config.work_path) / "ai_publish_staging" / video_digest / run_token
        staging_root.mkdir(parents=True, exist_ok=False)
        staged = staging_root / "source.ass"
        try:
            convert_srt_file_to_ass(source_srt, staged, self._ass_style)
            if not staged.is_file():
                raise SubtitleQualityError(f"Source-language ASS staging output is missing for {video}")
            reports = self._quality_check_ai_outputs(
                video,
                [(staged, "source")],
                discard_on_failure=False,
                persist_reports=False,
            )
            self._replace_ai_outputs_with_rollback(video, [staged], [destination])
            self._persist_validated_quality_reports(reports, [destination])
        finally:
            staged.unlink(missing_ok=True)
            quality_report_path(staged).unlink(missing_ok=True)
            shutil.rmtree(staging_root, ignore_errors=True)

    def _enforce_asr_publication_gate(
        self,
        srt_path: Path,
        *,
        label: str,
    ) -> None:
        hold = asr_transcription_hold_path(srt_path, self.config)
        if hold.is_file():
            raise SubtitleQualityError(
                f"{label} ASR commit is still pending: {hold}"
            )
        diagnostic = asr_diagnostics_path(srt_path, self.config)
        if not diagnostic.is_file():
            return
        try:
            trusted = self._asr_cache_diagnostics_are_trusted(srt_path)
        except OSError:
            trusted = False
        if not trusted:
            raise SubtitleQualityError(
                f"{label} ASR diagnostic is rejected, corrupt, or does not "
                f"match the SRT; retranscription is required: {diagnostic}"
            )

    def _persist_validated_quality_reports(
        self,
        reports: list[SubtitleQualityReport],
        destinations: list[Path],
    ) -> None:
        """Persist reports produced in staging under their published identities."""

        if not reports:
            return
        published_reports: list[SubtitleQualityReport] = []
        for report, destination in zip(reports, destinations, strict=True):
            published = replace(report, path=str(destination))
            write_quality_report(
                published,
                managed_quality_report_path(destination, self.config.work_path),
            )
            try:
                quality_report_path(destination).unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning("Failed to remove legacy media quality sidecar %s: %s", destination, exc)
            published_reports.append(published)
        if self._provenance is not None and published_reports:
            self._provenance.update(
                "subtitle_quality",
                [report.to_dict() for report in published_reports],
            )

    def _replace_ai_outputs_with_rollback(
        self,
        video: Path,
        staged_outputs: list[Path],
        destinations: list[Path],
    ) -> None:
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
        version_root = Path(self.config.work_path) / "ai_output_versions" / digest / str(time.time_ns())
        version_root.mkdir(parents=True, exist_ok=True)
        backups: dict[Path, Path] = {}
        published: list[Path] = []
        try:
            for index, destination in enumerate(destinations):
                if not destination.is_file():
                    continue
                backup = version_root / f"previous-{index}-{hashlib.sha256(destination.name.encode('utf-8', errors='replace')).hexdigest()[:12]}.ass"
                verified_copy_replace(destination, backup)
                backups[destination] = backup
            manifest_path = version_root / "manifest.json"
            prepared_manifest = {
                "video": str(video),
                "created_at": time.time(),
                "status": "prepared",
                "destinations": [str(destination) for destination in destinations],
                "backups": [
                    {"path": str(destination), "backup": str(backup), "sha256": sha256_file(backup)}
                    for destination, backup in backups.items()
                ],
            }
            atomic_write_text(
                manifest_path,
                json.dumps(prepared_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            for staged, destination in zip(staged_outputs, destinations, strict=True):
                # Publish the verified copy before removing the staging source.
                # Tracking the destination first guarantees that even a source
                # cleanup failure participates in the all-or-nothing rollback.
                published.append(destination)
                verified_copy_replace(staged, destination)
                staged.unlink(missing_ok=True)
            completed_manifest = {
                **prepared_manifest,
                "status": "completed",
                "completed_at": time.time(),
                "published": [
                    {"path": str(destination), "sha256": sha256_file(destination)}
                    for destination in destinations
                ],
            }
            atomic_write_text(
                manifest_path,
                json.dumps(completed_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            self._prune_completed_ai_output_versions(digest)
        except Exception as publish_error:
            rollback_errors: list[dict[str, str]] = []
            for destination in reversed(published):
                try:
                    backup = backups.get(destination)
                    if backup is None:
                        destination.unlink(missing_ok=True)
                        continue
                    verified_copy_replace(backup, destination)
                except Exception as rollback_error:
                    rollback_errors.append(
                        {
                            "path": str(destination),
                            "error": f"{type(rollback_error).__name__}: {rollback_error}",
                        }
                    )
                    self.logger.exception(
                        "Failed to restore previous AI output after publication failure: %s",
                        destination,
                    )
            if "manifest_path" in locals() and manifest_path.is_file():
                try:
                    rolled_back_manifest = {
                        **prepared_manifest,
                        "status": "rollback_failed" if rollback_errors else "rolled_back",
                        "rolled_back_at": time.time(),
                        "publication_error": f"{type(publish_error).__name__}: {publish_error}",
                        "rollback_errors": rollback_errors,
                    }
                    atomic_write_text(
                        manifest_path,
                        json.dumps(rolled_back_manifest, ensure_ascii=False, indent=2) + "\n",
                    )
                except Exception as manifest_error:
                    self.logger.warning(
                        "Failed to record AI output rollback manifest; prepared manifest remains. path=%s error=%s",
                        manifest_path,
                        manifest_error,
                    )
            if rollback_errors:
                failed_paths = ", ".join(item["path"] for item in rollback_errors)
                raise RuntimeError(
                    "AI output publication failed and rollback was incomplete; "
                    f"manual restore required for: {failed_paths}"
                ) from publish_error
            raise

    def _prune_completed_ai_output_versions(self, video_digest: str) -> None:
        """Keep bounded completed restore points while preserving incomplete recovery evidence."""

        keep = max(1, int(getattr(self.config, "ai_output_versions_keep", 3) or 3))
        root = Path(self.config.work_path) / "ai_output_versions" / video_digest
        try:
            resolved_root = root.resolve()
            completed: list[tuple[int, Path]] = []
            for candidate in root.iterdir():
                if not candidate.is_dir() or not candidate.name.isdigit():
                    continue
                manifest_path = candidate / "manifest.json"
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                status = payload.get("status")
                legacy_completed = (
                    status is None
                    and isinstance(payload.get("backups"), list)
                    and payload.get("created_at") is not None
                    and payload.get("video") is not None
                )
                if status != "completed" and not legacy_completed:
                    continue
                completed.append((int(candidate.name), candidate))
            completed.sort(key=lambda item: item[0], reverse=True)
            for _stamp, candidate in completed[keep:]:
                resolved = candidate.resolve()
                if resolved.parent != resolved_root or not resolved.name.isdigit():
                    self.logger.warning("Refused unsafe AI output version cleanup path: %s", candidate)
                    continue
                shutil.rmtree(resolved)
                self.logger.info("Pruned old completed AI output restore point: %s", resolved)
        except OSError as exc:
            self.logger.warning("Failed to prune completed AI output restore points root=%s error=%s", root, exc)

    def _restyle_existing_ai_ass_safely(self, video: Path, paths: SubtitlePaths) -> int:
        """Restyle and validate existing AI sidecars before replacing them."""

        candidates = [
            (paths.ai_ja_ass, "japanese"),
            (paths.ai_zh_cn_ass, "translated"),
            (paths.ai_zh_tw_ass, "translated"),
        ]
        existing = [(path, role) for path, role in candidates if path.is_file()]
        if not existing:
            return 0

        video_digest = hashlib.sha1(str(video.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
        staging_root = (
            Path(self.config.work_path)
            / "ai_publish_staging"
            / video_digest
            / f"refresh-{time.time_ns()}"
        )
        staging_root.mkdir(parents=True, exist_ok=False)
        staged_outputs: list[Path] = []
        destinations: list[Path] = []
        roles: list[str] = []
        try:
            changed = 0
            for index, (destination, role) in enumerate(existing):
                staged = staging_root / f"{index}.ass"
                shutil.copy2(destination, staged)
                if restyle_ass_file(staged, self._ass_style):
                    changed += 1
                staged_outputs.append(staged)
                destinations.append(destination)
                roles.append(role)

            if changed == 0:
                return 0

            reports = self._quality_check_ai_outputs(
                video,
                list(zip(staged_outputs, roles, strict=True)),
                discard_on_failure=False,
                persist_reports=False,
            )
            complete_set = len(destinations) == 3
            if complete_set:
                begin_output_publication(video, self.config)
            self._replace_ai_outputs_with_rollback(video, staged_outputs, destinations)
            self._persist_validated_quality_reports(reports, destinations)
            return changed
        finally:
            for staged in staged_outputs:
                staged.unlink(missing_ok=True)
                quality_report_path(staged).unlink(missing_ok=True)
            shutil.rmtree(staging_root, ignore_errors=True)

    def _normalize_existing_ai_ass_names(self, paths: SubtitlePaths) -> int:
        normalized = 0
        for legacy_path, target_path in _iter_legacy_ai_ass_renames(paths):
            if legacy_path == target_path:
                continue
            try:
                if target_path.exists():
                    legacy_sha256 = sha256_file(legacy_path)
                    target_sha256 = sha256_file(target_path)
                    if legacy_sha256 == target_sha256:
                        legacy_path.unlink(missing_ok=True)
                        self.logger.info("Removed byte-identical legacy AI ASS duplicate: %s", legacy_path)
                    else:
                        archive_digest = hashlib.sha1(
                            str(legacy_path.resolve()).encode("utf-8", errors="replace")
                        ).hexdigest()[:16]
                        archive_root = Path(self.config.work_path) / "ai_legacy_duplicates" / archive_digest[:2]
                        archive_root.mkdir(parents=True, exist_ok=True)
                        archive_path = archive_root / f"{time.time_ns()}-{archive_digest}.ass"
                        verified_move(legacy_path, archive_path)
                        try:
                            atomic_write_text(
                                archive_path.with_suffix(".json"),
                                json.dumps(
                                    {
                                        "created_at": time.time(),
                                        "source": str(legacy_path),
                                        "canonical": str(target_path),
                                        "archive": str(archive_path),
                                        "legacy_sha256": legacy_sha256,
                                        "canonical_sha256": target_sha256,
                                        "reason": "conflicting legacy AI ASS duplicate",
                                    },
                                    ensure_ascii=False,
                                    indent=2,
                                )
                                + "\n",
                            )
                        except OSError as manifest_error:
                            self.logger.warning(
                                "Archived conflicting legacy AI ASS but could not write metadata: archive=%s error=%s",
                                archive_path,
                                manifest_error,
                            )
                        self.logger.warning(
                            "Archived conflicting legacy AI ASS instead of deleting it: %s -> %s",
                            legacy_path,
                            archive_path,
                        )
                    normalized += 1
                    continue
                legacy_path.replace(target_path)
                self.logger.info("Renamed legacy AI ASS: %s -> %s", legacy_path, target_path)
                normalized += 1
            except OSError as exc:
                self.logger.warning("Failed to normalize legacy AI ASS %s -> %s: %s", legacy_path, target_path, exc)
        return normalized

    def _audio_path(self, video: Path) -> Path:
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:12]
        return self.config.work_path / f"{video.stem}.{digest}.wav"

    def _extract_preferred_audio(
        self,
        video: Path,
        audio_path: Path,
        *,
        stream: AudioStreamInfo | None = None,
    ) -> Path:
        selected = stream if stream is not None else preferred_audio_stream_info(video)
        self._selected_audio_stream = selected
        if selected is None:
            message = "Audio stream metadata unavailable; using ffmpeg default"
            self.logger.warning("%s video=%s", message, video)
            self._set_stage(video, "audio_selection", "ok", message)
            output = extract_audio(video, audio_path)
        else:
            message = (
                "Selected audio stream: "
                f"index={selected.index} "
                f"language={selected.language or 'unknown'} "
                f"default={int(selected.default)} "
                f"commentary={int(selected.commentary)} "
                f"title={json.dumps(selected.title, ensure_ascii=False)}"
            )
            self.logger.info("%s video=%s", message, video)
            self._set_stage(video, "audio_selection", "ok", message)
            output = extract_audio(video, audio_path, stream_index=selected.index)
        self._audio_selection_payload.update(
            {
                "video_path": str(video),
                "selection_reason": "metadata_preference" if selected is not None else "ffmpeg_default",
                "selected": selected.to_dict() if selected is not None else None,
                "streams": [item.to_dict() for item in probe_audio_streams(video)],
                "updated_at": time.time(),
            }
        )
        self._write_audio_selection_manifest(video)
        return output

    def _recover_japanese_audio_stream(
        self,
        video: Path,
        audio_path: Path,
        result: LanguageDetectionResult | None,
        *,
        force_ai: bool,
    ) -> LanguageDetectionResult | None:
        if result is None or result.allowed:
            return result
        if _should_retry_language_gate_with_japanese_audio(result, self.config, video):
            self.logger.warning(
                "Non-Japanese language detected despite Japanese stream metadata; re-extracting preferred stream. "
                "video=%s language=%s probability=%.2f",
                video,
                result.language,
                result.probability,
            )
            self._set_stage(
                video,
                "audio_selection",
                "running",
                "Re-extracting metadata-selected Japanese audio stream",
            )
            audio_path.unlink(missing_ok=True)
            self._extract_preferred_audio(video, audio_path)
            result = self._detect_source_language(
                video,
                audio_path,
                force_ai=force_ai,
                force_refresh=True,
            )
            if result is None or result.allowed:
                return result
            self.logger.warning(
                "Metadata-selected Japanese stream still failed language gate; continuing with content probes. "
                "video=%s language=%s probability=%.2f",
                video,
                result.language,
                result.probability,
            )
        if not bool(getattr(self.config, "audio_content_probe_enabled", True)):
            return result
        streams = probe_audio_streams(video)
        if len(streams) <= 1:
            return result

        self._set_stage(video, "audio_selection", "running", "Sampling audio streams to locate Japanese dialogue")
        try:
            detector = LanguageDetector(
                self._resource_adjusted_asr_config(self.config),
                self.logger,
            )
            selection = detector.select_japanese_audio_stream(video)
        except Exception as exc:  # noqa: BLE001 - retain the already extracted stream on diagnostics failure.
            self.logger.warning("Content-based audio stream selection failed video=%s error=%s", video, exc)
            self._audio_selection_payload.update({"content_probe_error": str(exc), "updated_at": time.time()})
            self._write_audio_selection_manifest(video)
            return result

        self._audio_selection_payload.update(
            {
                "content_selection": selection.to_dict(),
                "selection_reason": selection.reason,
                "updated_at": time.time(),
            }
        )
        self._write_audio_selection_manifest(video)
        selected = selection.selected
        if selected is None:
            self._set_stage(
                video,
                "audio_selection",
                "ok",
                f"No Japanese audio stream detected after sampling streams={len(streams)}",
            )
            return result

        changed = self._selected_audio_stream is None or self._selected_audio_stream.index != selected.index
        if changed:
            self.logger.warning(
                "Switching to content-detected Japanese audio stream video=%s old_stream=%s new_stream=%s",
                video,
                self._selected_audio_stream.index if self._selected_audio_stream is not None else None,
                selected.index,
            )
            self._set_stage(
                video,
                "audio_selection",
                "running",
                f"Switching to Japanese audio stream index={selected.index}",
            )
            audio_path.unlink(missing_ok=True)
            self._extract_preferred_audio(video, audio_path, stream=selected)
        else:
            self._selected_audio_stream = selected

        refreshed = self._detect_source_language(
            video,
            audio_path,
            force_ai=force_ai,
            force_refresh=True,
        )
        self._set_stage(
            video,
            "audio_selection",
            "ok",
            f"Content-selected Japanese stream index={selected.index} language={getattr(refreshed, 'language', 'unknown')}",
        )
        return refreshed

    def _write_audio_selection_manifest(self, video: Path) -> None:
        configured = Path(str(getattr(self.config, "audio_selection_manifest_path", "audio_selection")))
        root = configured if configured.is_absolute() else self.config.work_path / configured
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:20]
        path = root / f"{digest}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{path.name}.tmp")
            temp.write_text(json.dumps(self._audio_selection_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            self.logger.debug("Unable to write audio selection manifest path=%s error=%s", path, exc)
        if self._provenance is not None:
            try:
                self._provenance.update("audio_selection", self._audio_selection_payload)
            except OSError as exc:
                self.logger.debug("Unable to update audio provenance video=%s error=%s", video, exc)

    def _get_translator(self) -> SubtitleTranslator:
        if self._translator is None:
            translator_config = self.config
            effective = self._resource_effective_limits()
            overrides: dict[str, object] = {}
            if isinstance(effective.get("batch_size"), int):
                overrides["batch_size"] = max(1, min(self.config.batch_size, int(effective["batch_size"])))
            if isinstance(effective.get("translation_context_max_blocks"), int):
                overrides["translation_context_max_blocks"] = max(
                    1,
                    min(
                        self.config.translation_context_max_blocks,
                        int(effective["translation_context_max_blocks"]),
                    ),
                )
            if isinstance(effective.get("translation_context_max_chars"), int):
                overrides["translation_context_max_chars"] = max(
                    1000,
                    min(
                        self.config.translation_context_max_chars,
                        int(effective["translation_context_max_chars"]),
                    ),
                )
            if overrides:
                translator_config = _config_with_overrides(self.config, **overrides)
            self._translator = SubtitleTranslator(
                translator_config,
                self.logger,
                progress_callback=self._translation_progress,
            )
        return self._translator

    def _load_resource_launch_plan(self, video: Path) -> dict[str, object] | None:
        if not bool(getattr(self.config, "resource_admission_enabled", False)):
            return None
        encoded = os.environ.get("ANIME_RESOURCE_LAUNCH_PLAN", "").strip()
        if not encoded:
            raise RuntimeError(
                "Resource admission is enabled but ANIME_RESOURCE_LAUNCH_PLAN is missing"
            )
        try:
            from resource_runtime import parse_authorized_resource_launch_plan

            return parse_authorized_resource_launch_plan(
                self.config,
                encoded,
                expected_video=video,
            )
        except Exception as exc:
            raise RuntimeError(f"Invalid resource launch plan for {video}: {exc}") from exc

    def _resource_effective_limits(self) -> dict[str, object]:
        plan = self._resource_launch_plan or {}
        effective = plan.get("effective")
        return dict(effective) if isinstance(effective, dict) else {}

    def _resource_adjusted_asr_config(self, config: AppConfig) -> AppConfig:
        plan = self._resource_launch_plan or {}
        route = plan.get("selected_route")
        effective = self._resource_effective_limits()
        overrides: dict[str, object] = {}
        if isinstance(effective.get("whisperx_batch_size"), int):
            overrides["whisperx_batch_size"] = max(
                1, min(int(config.whisperx_batch_size), int(effective["whisperx_batch_size"]))
            )
        if isinstance(effective.get("transformers_whisper_batch_size"), int):
            overrides["transformers_whisper_batch_size"] = max(
                1,
                min(
                    int(config.transformers_whisper_batch_size),
                    int(effective["transformers_whisper_batch_size"]),
                ),
            )
        if isinstance(route, dict):
            selected_model = str(route.get("model") or "").strip()
            selected_compute = str(route.get("compute_type") or "").strip()
            configured_primary = str(
                getattr(self.config, "japanese_transcription_model", None)
                or self.config.whisper_model
            )
            requested_model = str(getattr(config, "whisper_model", "") or "")
            # The admission compute route is an upper memory bound for every
            # Whisper-backed GPU phase, including language/audio probes and a
            # configured ASR fallback. Model replacement is narrower: only the
            # admitted primary model may be substituted.
            if selected_compute:
                requested_compute = str(
                    getattr(config, "whisper_compute_type", "") or ""
                ).strip()

                def memory_rank(value: str) -> int | None:
                    normalized = value.lower().replace("-", "_")
                    if "int8" in normalized:
                        return 0
                    if "int16" in normalized:
                        return 1
                    if normalized in {"float16", "fp16", "bfloat16", "bf16"}:
                        return 2
                    if normalized in {"float32", "fp32"}:
                        return 3
                    return None

                requested_rank = memory_rank(requested_compute)
                selected_rank = memory_rank(selected_compute)
                # Never let admission upgrade an explicit low-memory recovery
                # route (for example int8_float16) back to float16.  Admission
                # may still downshift a more expensive request.
                if (
                    not requested_compute
                    or requested_compute.lower() in {"auto", "default"}
                    or (
                        requested_rank is not None
                        and selected_rank is not None
                        and selected_rank < requested_rank
                    )
                ):
                    overrides["whisper_compute_type"] = selected_compute
            if requested_model == configured_primary:
                if selected_model:
                    overrides["whisper_model"] = selected_model
        return _config_with_overrides(config, **overrides) if overrides else config

    def _translation_progress(self, stage: str, status: str, message: str) -> None:
        video = self._translator_progress_video
        if video is None:
            return
        self._set_stage(video, stage, status, message)

    def _detect_source_language(
        self,
        video: Path,
        audio_path: Path,
        *,
        force_ai: bool,
        force_refresh: bool = False,
    ) -> LanguageDetectionResult | None:
        if not bool(getattr(self.config, "language_gate_enabled", False)):
            return None
        if force_ai and bool(getattr(self.config, "force_ai_bypass_language_gate", False)):
            self._set_stage(video, "language_detect", "ok", "Language gate bypassed by Force AI")
            return None

        self._set_stage(video, "language_detect", "running", "Detecting source language")
        cache_variant = (
            f"stream:{self._selected_audio_stream.index}"
            if self._selected_audio_stream is not None
            else "stream:ffmpeg-default"
        )
        detector_config = self._resource_adjusted_asr_config(self.config)
        result = LanguageDetector(detector_config, self.logger).detect(
            audio_path,
            video,
            cache_variant=cache_variant,
            force_refresh=force_refresh,
        )
        message = format_language_result(result, self.config)
        if self._provenance is not None:
            self._provenance.update("language_detection", result)
        self._set_stage(video, "language_detect", "ok", message)
        self.logger.info(
            "Language detection result: video=%s model=%s %s",
            video,
            getattr(self.config, "language_detect_model", None) or self.config.whisper_model,
            message,
        )
        if not result.confident:
            if str(getattr(self.config, "language_uncertain_policy", "skip")).lower() == "continue":
                self.logger.warning("Language detection confidence is low; continuing AI pipeline: video=%s %s", video, message)
            else:
                self.logger.info("Language detection confidence is low; policy will stop AI pipeline: video=%s %s", video, message)
        return result

    def _translation_memory_scope(self, video: Path) -> MemoryScope:
        series_root = series_root_for_video(video)
        series_key = stable_series_id(canonical_local_path(series_root))
        return MemoryScope(
            series_key=series_key,
            policy_version=processing_config_signature(self.config),
        )

    def _translation_memory_database_path(self) -> Path:
        configured = Path(
            str(
                getattr(
                    self.config,
                    "translation_memory_path",
                    "translation_memory.sqlite3",
                )
                or "translation_memory.sqlite3"
            )
        )
        return configured if configured.is_absolute() else Path(self.config.work_path) / configured

    def _translation_memory_outbox_root(self) -> Path:
        configured = Path(
            str(
                getattr(
                    self.config,
                    "translation_memory_outbox_path",
                    "translation_memory_outbox",
                )
                or "translation_memory_outbox"
            )
        )
        return configured if configured.is_absolute() else Path(self.config.work_path) / configured

    def _configure_translation_memory_plan(
        self,
        video: Path,
        translator: SubtitleTranslator,
        source_blocks: list[SrtBlock],
        *,
        series_glossary: dict[str, str],
    ) -> None:
        """Prepare one fail-open lookup plan and fail-closed output lineage.

        A broken, missing, locked, or conflicting TM database is an optional
        cache failure: every hit is discarded and the ordinary full translator
        runs.  The resulting output still receives explicit lookup-fallback
        lineage so it can be learned later without ambiguity.
        """

        if not bool(getattr(self.config, "translation_memory_enabled", True)):
            return
        scope = self._translation_memory_scope(video)
        combined_glossary = {
            **dict(getattr(self.config, "translation_glossary", {}) or {}),
            **{
                str(key): str(value)
                for key, value in (series_glossary or {}).items()
                if str(key).strip() and str(value).strip()
            },
        }
        cached_blocks: tuple[SrtBlock, ...] = ()
        mode = "tm_disabled"
        digest = translation_memory_full_plan_digest(
            scope,
            source_blocks,
            translation_lineage_mode=mode,
        )
        if bool(getattr(self.config, "translation_memory_auto_apply_enabled", True)):
            try:
                split = split_blocks_by_readonly_translation_memory(
                    self._translation_memory_database_path(),
                    scope,
                    source_blocks,
                    explicit_series_glossary=combined_glossary,
                )
                cached_blocks = split.cached_blocks
                if cached_blocks:
                    mode = "tm_split"
                else:
                    mode = "no_hits"
                digest = translation_memory_split_digest(scope, split)
                self.logger.info(
                    "Translation-memory lookup completed: video=%s mode=%s cached=%s unresolved=%s",
                    video,
                    mode,
                    len(split.cached_blocks),
                    len(split.unresolved_blocks),
                )
            except Exception as exc:  # noqa: BLE001 - optional cache must fail open to the normal translator.
                cached_blocks = ()
                mode = "lookup_fallback"
                digest = translation_memory_full_plan_digest(
                    scope,
                    source_blocks,
                    translation_lineage_mode=mode,
                )
                self.logger.warning(
                    "Translation-memory lookup failed; discarding all hits and using full translation: "
                    "video=%s error=%s",
                    video,
                    exc,
                )
        translator.set_translation_memory_plan(
            pretranslated_blocks=cached_blocks,
            scope=scope,
            decision_digest=digest,
            lineage_mode=mode,
        )
        if self._provenance is not None:
            self._provenance.update(
                "translation_memory_lookup",
                {
                    "mode": mode,
                    "cached_blocks": len(cached_blocks),
                    "source_blocks": len(source_blocks),
                    "split_decision_digest": digest,
                    "scope": {
                        "series_key": scope.series_key,
                        "policy_version": scope.policy_version,
                        "source_language": scope.source_language,
                        "target_language": scope.target_language,
                    },
                },
            )

    def _build_series_metadata_context(self, video: Path) -> MetadataContext | None:
        if not bool(getattr(self.config, "translation_metadata_context_enabled", False)):
            return None
        self._set_stage(video, "metadata_context", "running", "Fetching series metadata context")
        context = build_series_metadata_context(video, self.config, self.logger)
        if context is None:
            self._set_stage(video, "metadata_context", "ok", "No series metadata context found")
            return None
        message = (
            "Series metadata context ready: "
            f"provider={context.provider} "
            f"cached={int(context.cached)} "
            f"chars={len(context.text)}"
        )
        self._set_stage(video, "metadata_context", "ok", message)
        if self._provenance is not None:
            self._provenance.update("series_metadata", context)
        return context

    def _begin_asr_commit(
        self,
        ja_srt: Path,
        *,
        reason: str,
        active_config: AppConfig | None = None,
    ) -> Path:
        commit_config = active_config or self.config
        hold = asr_transcription_hold_path(ja_srt, commit_config)
        atomic_write_text(
            hold,
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "transcription_commit_pending",
                    "srt_path": str(ja_srt),
                    "backend": str(
                        getattr(commit_config, "transcription_backend", "")
                    ),
                    "model": str(getattr(commit_config, "whisper_model", "")),
                    "reason": str(reason),
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return hold

    def _finish_asr_commit(self, ja_srt: Path, hold: Path, *, label: str) -> None:
        self._validate_srt_output(ja_srt, label)
        hold.unlink()
        if hold.exists():
            raise OSError(f"Unable to clear ASR pending marker: {hold}")

    def _recover_pending_asr_commit(
        self,
        video: Path,
        paths: SubtitlePaths,
    ) -> None:
        recovered = self._recover_pending_asr_output(
            video,
            paths.ja_srt,
            label="Japanese",
        )
        if not recovered:
            return
        self._invalidate_translation_intermediates(paths)

    def _recover_pending_asr_output(
        self,
        video: Path,
        srt_path: Path,
        *,
        label: str,
    ) -> bool:
        hold = asr_transcription_hold_path(srt_path, self.config)
        if not hold.is_file():
            return False
        self.logger.warning(
            "Invalidating interrupted ASR commit before cache reuse: "
            "video=%s role=%s srt=%s hold=%s",
            video,
            label,
            srt_path,
            hold,
        )
        self._fail_closed_asr_output(
            srt_path,
            reason="interrupted ASR commit detected during worker restart",
        )
        if srt_path.exists():
            # Keep the marker when the live output cannot be removed.  No
            # future run may trust that SRT until fail-closed cleanup succeeds.
            raise TranscriptionError(
                "Interrupted ASR output could not be invalidated safely: "
                f"{srt_path}"
            )
        asr_diagnostics_path(srt_path, self.config).unlink(missing_ok=True)
        hold.unlink(missing_ok=True)
        if hold.exists():
            raise TranscriptionError(
                f"Unable to clear recovered ASR pending marker: {hold}"
            )
        return True

    def _asr_cache_diagnostics_are_trusted(self, srt_path: Path) -> bool:
        diagnostic_path = asr_diagnostics_path(srt_path, self.config)
        if not diagnostic_path.is_file():
            # Legacy caches and backends with diagnostics disabled are accepted
            # only when there is no pending marker.  Every new worker-managed
            # attempt has the durable hold as its crash barrier.
            return True
        diagnostics = read_asr_diagnostics(srt_path, self.config)
        status = str(diagnostics.get("status") or "")
        expected_sha256 = str(diagnostics.get("srt_sha256") or "").strip()
        if status not in {"accepted", "accepted_after_selective_retry"}:
            return False
        return bool(expected_sha256) and expected_sha256 == sha256_file(srt_path)

    def _bind_asr_diagnostics_context(
        self,
        audio_path: Path,
        srt_path: Path,
    ) -> dict[str, object]:
        video = self._active_transcription_video
        diagnostics = asr_diagnostics_path(srt_path, self.config)
        if video is None or not diagnostics.is_file():
            return {}
        context = attach_asr_diagnostics_context(
            srt_path,
            self.config,
            media_path=video,
            audio_path=audio_path,
            audio_stream=self._selected_audio_stream,
        )
        if not context:
            raise TranscriptionError(
                "ASR diagnostics could not be bound to source media, audio stream, "
                "audio, and transcript fingerprints"
            )
        return context

    def _attach_asr_failure_context(
        self,
        exc: BaseException,
        audio_path: Path,
        srt_path: Path,
        *,
        video: Path | None = None,
    ) -> dict[str, object]:
        review_ranges, reason_code = self._asr_exception_review_evidence(exc)
        context: dict[str, object] = {
            "failure_code": reason_code or "asr_failure",
            "reason_code": reason_code,
            "review_ranges": [[start, end] for start, end in review_ranges],
            "media_fingerprint": None,
            "audio_fingerprint": None,
            "audio_stream_fingerprint": asr_audio_stream_fingerprint(
                self._selected_audio_stream
            ),
            "cache_fingerprint": None,
            "repair_fingerprint": "",
            "repair_attempted": False,
            "cache_trusted": False,
            "asr_review_checkpoint": {"status": "unavailable"},
        }
        active_video = video or self._active_transcription_video
        if active_video is not None:
            try:
                context["media_fingerprint"] = asr_file_fingerprint(active_video)
            except OSError:
                pass
        if audio_path.is_file():
            try:
                context["audio_fingerprint"] = asr_file_fingerprint(audio_path)
            except OSError:
                pass
        if srt_path.is_file():
            try:
                context["cache_fingerprint"] = asr_file_fingerprint(
                    srt_path,
                    full_hash=True,
                )
            except OSError:
                pass
        if active_video is not None and audio_path.is_file() and srt_path.is_file():
            try:
                bound = attach_asr_diagnostics_context(
                    srt_path,
                    self.config,
                    media_path=active_video,
                    audio_path=audio_path,
                    audio_stream=self._selected_audio_stream,
                )
            except OSError:
                bound = {}
            if bound:
                context.update(bound)
                payload = read_asr_diagnostics(srt_path, self.config)
                context["repair_attempted"] = bool(
                    payload.get("repair_attempted")
                )
                context["cache_trusted"] = True
        try:
            setattr(exc, "asr_context", context)
        except Exception:
            pass
        return context

    def _capture_asr_review_checkpoint(
        self,
        exc: BaseException,
        srt_path: Path,
        *,
        language: str,
    ) -> dict[str, object]:
        """Preserve trusted rejected ASR evidence before fail-closed cleanup."""

        raw_context = getattr(exc, "asr_context", None)
        if not isinstance(raw_context, dict):
            return {}
        diagnostics_path = asr_diagnostics_path(srt_path, self.config)
        diagnostics = read_asr_diagnostics(srt_path, self.config)
        if str(diagnostics.get("status") or "") not in {
            "selective_retry_required",
            "selective_repair_rejected",
        }:
            return {}
        fingerprints = {
            key: raw_context.get(key)
            for key in (
                "media_fingerprint",
                "audio_fingerprint",
                "audio_stream_fingerprint",
                "cache_fingerprint",
            )
        }
        try:
            checkpoint = create_asr_review_checkpoint(
                self.config.work_path,
                target_path=srt_path,
                language=language,
                rejected_srt_path=srt_path,
                diagnostics_path=diagnostics_path,
                review_ranges=raw_context.get("review_ranges"),
                repair_fingerprint=str(
                    raw_context.get("repair_fingerprint") or ""
                ),
                fingerprints=fingerprints,
            )
        except Exception as checkpoint_error:  # noqa: BLE001 - checkpoint capture must not mask the ASR failure.
            self.logger.warning(
                "Could not preserve trusted ASR review checkpoint; full re-transcription remains available. "
                "srt=%s language=%s error=%s",
                srt_path,
                language,
                checkpoint_error,
            )
            return {}

        metadata: dict[str, object] = {
            "schema_version": checkpoint.schema_version,
            "checkpoint_id": checkpoint.checkpoint_id,
            "manifest_path": str(checkpoint.manifest_path),
            "manifest_sha256": checkpoint.manifest_sha256,
            "target_path": str(checkpoint.target_path),
            "language": checkpoint.language,
            "review_ranges": [list(item) for item in checkpoint.review_ranges],
            "repair_fingerprint": checkpoint.repair_fingerprint,
            "fingerprints": checkpoint.fingerprints,
            "rejected_srt_sha256": checkpoint.rejected_srt_sha256,
            "diagnostics_sha256": checkpoint.diagnostics_sha256,
            "repair_attempted": bool(raw_context.get("repair_attempted")),
            # The current cached selective engine is Japanese-target aware.
            # Source-language checkpoints use the same durable contract but
            # keep the existing full fallback until their runtime route is
            # wired to this executor.
            "selective_retry_supported": checkpoint.language == "ja",
        }
        raw_context["asr_review_checkpoint"] = metadata
        try:
            setattr(exc, "asr_context", raw_context)
        except Exception:
            pass
        self.logger.warning(
            "Preserved immutable rejected ASR checkpoint before fail-closed cleanup. "
            "checkpoint=%s language=%s srt=%s",
            checkpoint.checkpoint_id,
            checkpoint.language,
            srt_path,
        )
        return metadata

    def _transcribe(self, audio_path: Path, ja_srt: Path) -> None:
        hold = self._begin_asr_commit(
            ja_srt,
            reason="primary/fallback Japanese ASR in progress",
        )
        try:
            asr_diagnostics_path(ja_srt, self.config).unlink(missing_ok=True)
            self._transcribe_with_fallback(audio_path, ja_srt)
            self._bind_asr_diagnostics_context(audio_path, ja_srt)
            self._finish_asr_commit(
                ja_srt,
                hold,
                label="Japanese transcription",
            )
        except Exception as exc:
            self._attach_asr_failure_context(exc, audio_path, ja_srt)
            if not self._suppress_asr_review_checkpoint_capture:
                self._capture_asr_review_checkpoint(
                    exc,
                    ja_srt,
                    language="ja",
                )
            # Neither a primary rejection nor a failed full fallback may
            # survive as an apparently accepted cache merely because ASR
            # diagnostics were disabled or could not be persisted.
            self._fail_closed_asr_output(
                ja_srt,
                reason=f"ASR attempt failed: {_compact_error_message(exc)}",
            )
            if not ja_srt.exists():
                hold.unlink(missing_ok=True)
            raise

    def _transcribe_with_fallback(self, audio_path: Path, ja_srt: Path) -> None:
        model_name = _japanese_transcription_model(self.config)
        backend_name = _japanese_transcription_backend(self.config)
        transcribe_config = _config_with_overrides(
            self.config,
            whisper_model=model_name,
            whisper_language="ja",
            transcription_backend=backend_name,
        )
        try:
            self._transcribe_with_config(audio_path, ja_srt, transcribe_config)
            self._last_asr_route = AsrRouteOutcome(
                backend=backend_name,
                model=model_name,
                fallback_used=False,
            )
        except TranscriptionError as exc:
            fallback_model = (
                getattr(self.config, "japanese_transcription_fallback_model", None)
                or self.config.whisper_model
            )
            fallback_backend = (
                getattr(self.config, "japanese_transcription_fallback_backend", None)
                or self.config.transcription_backend
            )
            primary_compute_type = str(getattr(self.config, "whisper_compute_type", "") or "")
            fallback_compute_type = (
                getattr(self.config, "japanese_transcription_fallback_compute_type", None)
                or primary_compute_type
            )
            prompt_free_fallback_changes_request = (
                isinstance(exc, LowConfidenceTranscriptionError)
                and bool(
                    getattr(self.config, "whisper_initial_prompt", None)
                    or getattr(self.config, "op_ed_initial_prompt", None)
                )
            )
            if (
                fallback_model == model_name
                and fallback_backend == backend_name
                and fallback_compute_type == str(getattr(transcribe_config, "whisper_compute_type", "") or "")
                and not prompt_free_fallback_changes_request
            ):
                raise
            error_summary = _compact_error_message(exc)
            review_ranges = list(exc.review_ranges) if isinstance(exc, LowConfidenceTranscriptionError) else []
            primary_context = self._attach_asr_failure_context(
                exc,
                audio_path,
                ja_srt,
            )
            repair_fingerprint = str(
                primary_context.get("repair_fingerprint") or ""
            ).strip()
            selective_attempt_claimed = False
            if repair_fingerprint:
                try:
                    selective_attempt_claimed = claim_asr_repair_attempt(
                        ja_srt,
                        self.config,
                        repair_fingerprint,
                    )
                except OSError as claim_error:
                    self.logger.warning(
                        "Unable to persist selective ASR attempt guard; "
                        "skipping selective repair audio=%s error=%s",
                        audio_path,
                        claim_error,
                    )
            if backend_name == "faster-whisper" and (
                fallback_model != model_name or fallback_compute_type != primary_compute_type
            ):
                # The exception traceback otherwise keeps the primary model's
                # transcribe frame alive while the fallback model is loaded,
                # temporarily requiring both models in VRAM on 12 GB GPUs.
                exc.__traceback__ = None
                clear_whisper_model_cache(logger=self.logger)
            self.logger.warning(
                "Japanese ASR primary model failed; retrying fallback model. primary_backend=%s primary_model=%s fallback_backend=%s fallback_model=%s fallback_compute_type=%s language=ja audio=%s error=%s",
                backend_name,
                model_name,
                fallback_backend,
                fallback_model,
                fallback_compute_type,
                audio_path,
                exc,
            )
            fallback_overrides: dict[str, object] = {
                "whisper_model": fallback_model,
                "whisper_compute_type": fallback_compute_type,
                "whisper_language": "ja",
                "transcription_backend": fallback_backend,
            }
            if isinstance(exc, LowConfidenceTranscriptionError):
                # A rejected range may be an explicit prompt echo, a
                # hallucination, or low-confidence speech shaped by the same
                # prompt. Never feed that prompt into either selective repair
                # or the full fallback used when selective repair cannot
                # produce a complete transcript. The full retry is also a
                # recovery pass: optional gap/OP-ED guesses may be discarded,
                # while artifacts in the primary full-pass transcript remain
                # fatal.
                fallback_overrides.update(
                    whisper_initial_prompt=None,
                    op_ed_initial_prompt=None,
                    whisper_condition_on_previous_text=False,
                    asr_optional_rescue_rejection_is_fatal=False,
                    asr_prompt_free_allow_recovered_primary_artifacts=True,
                )
            fallback_config = _config_with_overrides(self.config, **fallback_overrides)
            if (
                isinstance(exc, LowConfidenceTranscriptionError)
                and bool(getattr(self.config, "asr_selective_retry_enabled", True))
                and fallback_backend == "faster-whisper"
                and ja_srt.exists()
                and (
                    self._active_transcription_video is None
                    or selective_attempt_claimed
                )
            ):
                selective_config = _config_with_overrides(
                    fallback_config,
                    transcription_quality_check_enabled=False,
                    enable_gap_rescue=False,
                    enable_leading_gap_rescue=False,
                    op_ed_transcription_enabled=False,
                    write_gap_report=False,
                    asr_diagnostics_enabled=False,
                )
                self._set_active_transcription_stage(
                    "running",
                    f"Repairing {len(review_ranges)} low-confidence range(s) with "
                    f"{fallback_backend} model={fallback_model}",
                )
                try:
                    repair_result = repair_low_confidence_ranges(
                        audio_path,
                        ja_srt,
                        review_ranges,
                        selective_config,
                        self.logger,
                    )
                    finalize_repaired_transcription(
                        audio_path,
                        ja_srt,
                        review_ranges,
                        fallback_config,
                        self.logger,
                        segment_confidences=getattr(
                            repair_result,
                            "segment_confidences",
                            (),
                        ),
                        require_confidence=(
                            str(getattr(exc, "reason_code", ""))
                            in {"low_confidence", "rescue_low_confidence"}
                        ),
                    )
                    self._last_asr_route = AsrRouteOutcome(
                        backend=fallback_backend,
                        model=fallback_model,
                        fallback_used=True,
                        failed_model=model_name,
                        failed_reason=f"selective repair: {error_summary}",
                    )
                    if self._provenance is not None:
                        self._provenance.update(
                            "asr",
                            {
                                "primary_model": model_name,
                                "fallback_model": fallback_model,
                                "selective_retry": True,
                                "review_ranges": review_ranges,
                            },
                        )
                    return
                except Exception as selective_error:  # noqa: BLE001 - fall back to the proven full retry path.
                    self.logger.warning(
                        "Selective ASR repair failed; running full fallback audio=%s error=%s",
                        audio_path,
                        selective_error,
                    )
            ja_srt.unlink(missing_ok=True)
            asr_diagnostics_path(ja_srt, self.config).unlink(missing_ok=True)
            self._set_active_transcription_stage(
                "running",
                f"Primary ASR {backend_name} model={model_name} failed: {error_summary}; "
                f"running fallback {fallback_backend} model={fallback_model} language=ja",
            )
            self.logger.info(
                "Running ASR route=japanese-fallback backend=%s model=%s language=ja audio=%s",
                fallback_backend,
                fallback_model,
                audio_path,
            )
            actual_fallback_config = fallback_config
            try:
                self._transcribe_with_config(audio_path, ja_srt, fallback_config)
            except TranscriptionError as fallback_error:
                current_compute_type = str(
                    getattr(fallback_config, "whisper_compute_type", "") or ""
                )
                can_retry_lower_memory = (
                    fallback_backend == "faster-whisper"
                    and str(
                        getattr(fallback_config, "whisper_device", "") or ""
                    ).casefold()
                    == "cuda"
                    and "int8" not in current_compute_type.casefold()
                    and is_cuda_oom(fallback_error)
                )
                recovery_error: TranscriptionError | None = fallback_error
                recovery_config = fallback_config
                if can_retry_lower_memory:
                    # Do not retain the failed constructor/transcribe frames while
                    # loading the one permitted lower-memory retry.
                    fallback_error.__traceback__ = None
                    clear_whisper_model_cache(logger=self.logger)
                    ja_srt.unlink(missing_ok=True)
                    asr_diagnostics_path(ja_srt, fallback_config).unlink(missing_ok=True)
                    retry_compute_type = "int8_float16"
                    retry_config = _config_with_overrides(
                        fallback_config,
                        whisper_compute_type=retry_compute_type,
                    )
                    self.logger.warning(
                        "Japanese ASR fallback hit CUDA OOM; retrying once with "
                        "lower-memory compute_type=%s model=%s audio=%s",
                        retry_compute_type,
                        fallback_model,
                        audio_path,
                    )
                    try:
                        self._transcribe_with_config(
                            audio_path,
                            ja_srt,
                            retry_config,
                        )
                    except TranscriptionError as retry_error:
                        recovery_error = retry_error
                        recovery_config = retry_config
                    else:
                        recovery_error = None
                        actual_fallback_config = retry_config

                if recovery_error is not None:
                    final_config = self._run_japanese_final_asr_fallback(
                        audio_path,
                        ja_srt,
                        recovery_config,
                        recovery_error,
                    )
                    if final_config is None:
                        raise recovery_error
                    actual_fallback_config = final_config
            self._last_asr_route = AsrRouteOutcome(
                backend=str(actual_fallback_config.transcription_backend),
                model=str(actual_fallback_config.whisper_model),
                fallback_used=True,
                failed_model=model_name,
                failed_reason=error_summary,
            )
        if self._provenance is not None and self._last_asr_route is not None:
            self._provenance.update("asr", self._last_asr_route)

    def _fail_closed_asr_output(self, ja_srt: Path, *, reason: str) -> None:
        diagnostics = asr_diagnostics_path(ja_srt, self.config)
        try:
            ja_srt.unlink(missing_ok=True)
        except OSError:
            pass
        if not ja_srt.exists():
            try:
                diagnostics.unlink(missing_ok=True)
            except OSError:
                pass
            return

        try:
            atomic_write_text(
                diagnostics,
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "transcription_failed",
                        "srt_path": str(ja_srt),
                        "srt_sha256": sha256_file(ja_srt),
                        "reason": str(reason),
                        "updated_at": time.time(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            return
        except OSError:
            pass

        quarantine_root = Path(self.config.work_path) / "asr_rejected_cache"
        digest = hashlib.sha1(str(ja_srt.resolve()).encode("utf-8")).hexdigest()[:16]
        quarantine = quarantine_root / f"failed-{time.time_ns()}-{digest}.srt"
        try:
            quarantine_root.mkdir(parents=True, exist_ok=True)
            verified_move(ja_srt, quarantine)
        except OSError:
            pass
        if not ja_srt.exists():
            try:
                diagnostics.unlink(missing_ok=True)
            except OSError:
                pass
            return

        try:
            ja_srt.write_text("", encoding="utf-8")
        except OSError:
            pass

    def _set_active_transcription_stage(self, status: str, message: str) -> None:
        video = self._active_transcription_video
        if video is None:
            return
        self._set_stage(video, "transcription", status, message)

    def _japanese_srt_created_message(self) -> str:
        route = self._last_asr_route
        if route is None:
            return "Japanese SRT created"
        if route.fallback_used:
            reason = f": {route.failed_reason}" if route.failed_reason else ""
            return (
                f"Japanese SRT created by fallback {route.backend} model={route.model} "
                f"after {route.failed_model} failed{reason}"
            )
        return f"Japanese SRT created by {route.backend} model={route.model}"

    def _transcribe_with_config(self, audio_path: Path, ja_srt: Path, config: AppConfig) -> None:
        config = self._resource_adjusted_asr_config(config)
        backend = str(getattr(config, "transcription_backend", "") or "faster-whisper")
        try:
            if backend == "vibevoice":
                from transcriber_vibevoice import transcribe_to_srt_with_vibevoice

                transcribe_to_srt_with_vibevoice(audio_path, ja_srt, config, self.logger)
            elif backend == "whisperx":
                from transcriber_whisperx import transcribe_to_srt_with_whisperx

                transcribe_to_srt_with_whisperx(audio_path, ja_srt, config, self.logger)
            elif backend == "transformers-whisper":
                from transcriber_transformers_whisper import transcribe_to_srt_with_transformers_whisper

                transcribe_to_srt_with_transformers_whisper(audio_path, ja_srt, config, self.logger)
            else:
                transcribe_to_srt(audio_path, ja_srt, config, self.logger)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"ASR backend {backend} failed for {audio_path}: "
                f"{_compact_error_message(exc)}"
            ) from exc

        validate_transcription_srt_quality(
            audio_path,
            ja_srt,
            config,
            self.logger,
        )

    def _postprocess_ja_srt(self, ja_srt: Path) -> list[SrtBlock]:
        blocks = read_srt(ja_srt)
        cleaned_blocks: list[SrtBlock] = []
        removed = 0
        changed = False

        for block in blocks:
            original_text = " ".join(line.strip() for line in block.text if line.strip())
            cleaned_text = _clean_transcribed_text(original_text, self.config)
            if not cleaned_text or _is_hallucination_text(cleaned_text, self.config):
                removed += 1
                changed = True
                continue
            if cleaned_text != original_text:
                changed = True
            cleaned_blocks.append(
                SrtBlock(
                    index=len(cleaned_blocks) + 1,
                    timing=block.timing,
                    text=[cleaned_text],
                )
            )

        cleaned_blocks, merged_fragments = _merge_short_kana_fragments(cleaned_blocks)
        if merged_fragments:
            changed = True

        if not cleaned_blocks:
            raise TranscriptionError(f"Japanese SRT became empty after post-processing: {ja_srt}")

        if changed:
            cleaned_blocks = [
                SrtBlock(index=index, timing=block.timing, text=block.text)
                for index, block in enumerate(cleaned_blocks, start=1)
            ]
            write_srt(ja_srt, cleaned_blocks)
            asr_diagnostics_path(ja_srt, self.config).unlink(missing_ok=True)
            asr_transcription_hold_path(ja_srt, self.config).unlink(
                missing_ok=True
            )
            self.logger.info(
                "Post-processed Japanese SRT: %s changed=%s removed=%s merged_fragments=%s remaining=%s",
                ja_srt,
                changed,
                removed,
                merged_fragments,
                len(cleaned_blocks),
            )
        return cleaned_blocks

    def _separated_audio_path(self, video: Path) -> Path:
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:12]
        return self.config.work_path / f"{video.stem}.{digest}.{self.config.vocal_separation_output}.wav"

    def _cleanup_audio_files(self, *paths: Path) -> None:
        for position, path in enumerate(paths):
            try:
                path.unlink(missing_ok=True)
                if position == 0:
                    audio_cache_metadata_path(path).unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning("Failed to remove temp audio %s: %s", path, exc)

    def _cleanup_intermediate_files(self, *paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning("Failed to remove intermediate file %s: %s", path, exc)

    def _validate_srt_output(self, path: Path, label: str) -> None:
        blocks = read_srt(path)
        if not blocks:
            raise TranscriptionError(f"{label} SRT is empty: {path}")
        empty_blocks = [
            block.index
            for block in blocks
            if not " ".join(line.strip() for line in block.text if line.strip())
        ]
        if empty_blocks:
            raise TranscriptionError(f"{label} SRT has empty subtitle blocks at indexes {empty_blocks[:10]}: {path}")

    def _normalize_source_language_srt_for_readability(self, path: Path) -> bool:
        max_chars = int(
            getattr(
                self.config,
                "subtitle_quality_max_primary_chars",
                getattr(self.config, "subtitle_max_chars", 42),
            )
            or 42
        )
        max_chars = max(24, max_chars)
        blocks = read_srt(path)
        changed = False
        normalized: list[SrtBlock] = []
        for block in blocks:
            original_lines = [line.strip() for line in block.text if line.strip()]
            if not original_lines:
                normalized.append(block)
                continue
            joined = " ".join(original_lines)
            wrapped = _wrap_subtitle_text_for_readability(joined, max_chars)
            if wrapped != block.text:
                changed = True
            normalized.append(SrtBlock(index=block.index, timing=block.timing, text=wrapped))
        if changed:
            write_srt(path, normalized)
            self.logger.info("Normalized source-language SRT readability: %s max_chars=%s", path, max_chars)
        return changed

    def _quality_check_ai_outputs(
        self,
        video: Path,
        outputs: list[tuple[Path, str]],
        *,
        discard_on_failure: bool = True,
        persist_reports: bool = True,
    ) -> list[SubtitleQualityReport]:
        subtitle_paths = paths_for_video(video, self.config)
        pending_hold = translation_quality_hold_path(subtitle_paths.zh_cn_srt)
        hold_payload = read_translation_quality_hold_strict(
            subtitle_paths.zh_cn_srt
        )
        if hold_payload is not None:
            raise SubtitleQualityError(
                f"Translation cache commit is still pending: {pending_hold}"
            )
        translation_events = read_translation_quality_events_strict(subtitle_paths.zh_cn_srt)
        hard_translation_events = [
            event
            for event in translation_events
            if str(event.get("severity") or "").lower() == "fail"
        ]
        if hard_translation_events:
            indexes = sorted(
                {
                    int(event.get("index") or 0)
                    for event in hard_translation_events
                    if int(event.get("index") or 0) > 0
                }
            )
            raise SubtitleQualityError(
                "Translation quality event blocks publication: "
                f"indexes={indexes}"
            )
        if not bool(getattr(self.config, "subtitle_quality_check_enabled", True)):
            return []

        reports: list[SubtitleQualityReport] = []
        failures: list[SubtitleQualityReport] = []
        self._set_stage(video, "quality_check", "running", "Checking subtitle viewing quality")

        for path, role in outputs:
            if not path.exists():
                continue
            report = analyze_subtitle_file(path, self.config, role=role)
            if role in {"translated", "translated_zh_cn"} and translation_events:
                report = add_translation_quality_events(report, translation_events)
            if self._provenance is not None:
                report = replace(
                    report,
                    provenance={
                        "config_signature": self._provenance.payload.get("config_signature"),
                        "translation": self._provenance.payload.get("translation"),
                        "series_metadata": self._provenance.payload.get("series_metadata"),
                        "asr": self._provenance.payload.get("asr"),
                    },
                )
            if persist_reports:
                report_path = managed_quality_report_path(path, self.config.work_path)
                write_quality_report(report, report_path)
                try:
                    quality_report_path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning("Failed to remove legacy media quality sidecar %s: %s", path, exc)
            reports.append(report)
            if report.has_failures:
                failures.append(report)
            self.logger.info("Subtitle quality report: %s path=%s", summarize_quality_report(report), path)

        if not reports:
            self._set_stage(video, "quality_check", "skipped", "No AI ASS output found for quality check")
            return reports

        if self._provenance is not None:
            self._provenance.update("subtitle_quality", [report.to_dict() for report in reports])
            if translation_events:
                self._provenance.update("translation_quality_events", translation_events)

        if failures:
            message = summarize_quality_report(failures[0])
            omission_indexes = sorted(
                {
                    int(event.get("index") or 0)
                    for event in translation_events
                    if str(event.get("code") or "") == TRANSLATION_SAFE_OMISSION
                    and int(event.get("index") or 0) > 0
                }
            )
            remediation_candidates: list[dict[str, object]] = []
            if omission_indexes:
                remediation_candidates.append(
                    {
                        "action": "ai.retranslate_lines",
                        "label": "Re-translate only failed lines",
                        "lines": ",".join(str(index) for index in omission_indexes),
                    }
                )
            remediation_candidates.extend(
                [
                    {"action": "ai.retranslate", "label": "Re-translate using verified Japanese cache"},
                    {"action": "ai.retranscribe", "label": "Re-transcribe from source audio"},
                ]
            )
            self._set_stage(video, "quality_check", "failed", message)
            self._create_review_item(
                video,
                kind="subtitle_quality",
                summary=message,
                diagnosis={
                    "stage": "quality_check",
                    "reports": [report.to_dict() for report in failures],
                    "line_previews": self._quality_line_previews(subtitle_paths, failures),
                },
                candidates=remediation_candidates,
            )
            if bool(getattr(self.config, "subtitle_quality_fail_job", True)) and discard_on_failure:
                self._discard_failed_ai_outputs(video, failures)
                raise SubtitleQualityError(message)
            if bool(getattr(self.config, "subtitle_quality_fail_job", True)):
                raise SubtitleQualityError(message)
            return reports

        warning = next((report for report in reports if report.has_warnings), None)
        if warning is not None:
            self._set_stage(video, "quality_check", "ok", summarize_quality_report(warning))
            return reports

        self._set_stage(video, "quality_check", "ok", "subtitle quality watchable")
        return reports

    @staticmethod
    def _quality_line_previews(
        paths: SubtitlePaths,
        reports: list[SubtitleQualityReport],
    ) -> list[dict[str, object]]:
        """Capture a bounded source/output comparison before failed files are archived."""

        issue_codes: dict[int, set[str]] = {}
        for report in reports:
            for issue in report.issues:
                for index in issue.indexes:
                    if int(index) > 0:
                        issue_codes.setdefault(int(index), set()).add(str(issue.code))
        if not issue_codes:
            return []

        def blocks_by_index(path: Path) -> dict[int, SrtBlock]:
            try:
                return {block.index: block for block in read_srt(path)} if path.is_file() else {}
            except (OSError, ValueError):
                return {}

        source = blocks_by_index(paths.ja_srt)
        translated = blocks_by_index(paths.zh_cn_srt)
        previews: list[dict[str, object]] = []
        for index in sorted(issue_codes)[:50]:
            source_block = source.get(index)
            translated_block = translated.get(index)
            previews.append(
                {
                    "index": index,
                    "timing": str((source_block or translated_block).timing if (source_block or translated_block) else "")[:80],
                    "source_ja": " ".join(source_block.text if source_block else [])[:500],
                    "output_zh": " ".join(translated_block.text if translated_block else [])[:500],
                    "issue_codes": sorted(issue_codes[index])[:8],
                }
            )
        return previews

    def _create_review_item(
        self,
        video: Path,
        *,
        kind: str,
        summary: str,
        diagnosis: dict[str, object],
        candidates: list[dict[str, object]],
        replace_candidates: bool = False,
    ) -> None:
        try:
            upsert_review_item(
                self.config,
                kind=kind,
                target_key=str(video.resolve()),
                summary=summary,
                diagnosis={"video": str(video), **diagnosis},
                candidates=candidates,
                severity="error",
                replace_candidates=replace_candidates,
            )
        except Exception as exc:  # noqa: BLE001 - review persistence cannot mask the processing failure.
            self.logger.warning(
                "Could not persist AI review item; original processing failure remains authoritative. video=%s kind=%s error=%s",
                video,
                kind,
                exc,
            )

    def _discard_failed_ai_outputs(
        self,
        video: Path,
        failures: list[SubtitleQualityReport],
    ) -> None:
        remove_output_manifest(video, self.config)
        archive_root = Path(self.config.work_path) / "failed_ai_quality"
        archive_root.mkdir(parents=True, exist_ok=True)
        paths = paths_for_video(video, self.config)
        failed_roles = {str(report.role or "unknown") for report in failures}
        outputs_to_remove = {Path(report.path) for report in failures}
        caches_to_remove: set[Path] = set()

        if "translated" in failed_roles:
            outputs_to_remove.update({paths.ai_zh_cn_ass, paths.ai_zh_tw_ass})
            caches_to_remove.update(
                {
                    paths.zh_cn_srt,
                    paths.zh_tw_srt,
                    translation_quality_events_path(paths.zh_cn_srt),
                    translation_quality_hold_path(paths.zh_cn_srt),
                }
            )
        if "japanese" in failed_roles:
            outputs_to_remove.update({paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass})
            caches_to_remove.update(
                {
                    paths.ja_srt,
                    paths.zh_cn_srt,
                    paths.zh_tw_srt,
                    asr_diagnostics_path(paths.ja_srt, self.config),
                    asr_transcription_hold_path(paths.ja_srt, self.config),
                    translation_quality_events_path(paths.zh_cn_srt),
                    translation_quality_hold_path(paths.zh_cn_srt),
                }
            )
        if "source" in failed_roles:
            cache_prefix = paths.ja_srt.name.split(".AI", 1)[0] + ".AI"
            try:
                caches_to_remove.update(
                    path
                    for path in paths.ja_srt.parent.iterdir()
                    if path.is_file() and path.name.startswith(cache_prefix) and path.suffix.casefold() == ".srt"
                )
            except OSError:
                pass

        for report in failures:
            digest = hashlib.sha1(str(report.path).encode("utf-8", errors="replace")).hexdigest()[:16]
            archived_report = archive_root / f"{digest}.{report.role}.quality.json"
            try:
                write_quality_report(report, archived_report)
            except OSError as exc:
                self.logger.warning("Failed to archive rejected AI quality report %s: %s", report.path, exc)

        for output in sorted(outputs_to_remove, key=lambda path: str(path).casefold()):
            if output.exists():
                digest = hashlib.sha1(str(output).encode("utf-8", errors="replace")).hexdigest()[:16]
                archive_suffix = output.suffix if output.suffix else ".bin"
                archive_path = archive_root / f"{digest}{archive_suffix}"
                try:
                    verified_move(output, archive_path)
                except OSError as exc:
                    self.logger.warning(
                        "Failed to archive rejected AI subtitle safely; source retained. source=%s archive=%s error=%s",
                        output,
                        archive_path,
                        exc,
                    )
            for report_path in quality_report_candidates(output, self.config.work_path):
                try:
                    report_path.unlink(missing_ok=True)
                except OSError:
                    pass

        for cache in sorted(caches_to_remove, key=lambda path: str(path).casefold()):
            try:
                cache.unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning("Failed to remove rejected AI cache %s: %s", cache, exc)

        self.logger.warning(
            "Rejected AI subtitle output removed for clean retry. video=%s roles=%s outputs=%s caches=%s archive=%s",
            video,
            ",".join(sorted(failed_roles)),
            len(outputs_to_remove),
            len(caches_to_remove),
            archive_root,
        )

    def _archive_asr_review_outputs(
        self,
        video: Path,
        *,
        reason: str = "translator_requested_fresh_asr",
    ) -> None:
        paths = paths_for_video(video, self.config)
        digest = hashlib.sha1(str(video.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
        archive_dir = (
            Path(self.config.work_path)
            / "asr_review_archive"
            / f"{int(time.time() * 1000)}-{digest}"
        )
        archive_dir.mkdir(parents=True, exist_ok=True)
        candidates = {
            paths.ja_srt,
            paths.zh_cn_srt,
            paths.zh_tw_srt,
            paths.ai_ja_ass,
            paths.ai_zh_cn_ass,
            paths.ai_zh_tw_ass,
            translation_quality_events_path(paths.zh_cn_srt),
            translation_quality_hold_path(paths.zh_cn_srt),
            asr_diagnostics_path(paths.ja_srt, self.config),
            asr_transcription_hold_path(paths.ja_srt, self.config),
        }
        report_candidates: set[Path] = set()
        for path in list(candidates):
            report_candidates.update(quality_report_candidates(path, self.config.work_path))
        candidates.update(report_candidates)
        moved: list[dict[str, str]] = []
        archive_failures: list[dict[str, str]] = []
        for source in sorted(candidates, key=lambda path: str(path).casefold()):
            if not source.is_file():
                continue
            suffix_digest = hashlib.sha1(str(source).encode("utf-8", errors="replace")).hexdigest()[:10]
            destination = archive_dir / f"{suffix_digest}-{source.name}"
            try:
                verified_move(source, destination)
                moved.append({"source": str(source), "archive": str(destination)})
            except OSError as archive_error:
                # Never turn an archive failure into an irreversible delete.
                # Keep the source and leave this job in review so the archive
                # can be retried before any fresh ASR replaces its inputs.
                archive_failures.append(
                    {
                        "source": str(source),
                        "archive": str(destination),
                        "error": str(archive_error),
                    }
                )
                self.logger.warning(
                    "Failed to archive ASR review output safely; source retained. source=%s archive=%s error=%s",
                    source,
                    destination,
                    archive_error,
                )

        manifest = {
            "video": str(video),
            "reason": str(reason or "asr_quality_review"),
            "created_at": time.time(),
            "status": "partial" if archive_failures else "complete",
            "moved": moved,
            "failures": archive_failures,
        }
        try:
            (archive_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.logger.warning("Failed to write ASR review archive manifest %s: %s", archive_dir, exc)
        self.logger.warning(
            "ASR review requested; archived dependent outputs for fresh transcription. video=%s moved=%s archive=%s",
            video,
            len(moved),
            archive_dir,
        )
        if archive_failures:
            raise OSError(
                "ASR review archive is incomplete; retained "
                f"{len(archive_failures)} source file(s) for a safe retry"
            )

    def _set_stage(self, video: Path, stage: str, status: str, message: str = "") -> None:
        if self._provenance is not None:
            try:
                self._provenance.record_stage(stage, status, message)
            except OSError as exc:
                self.logger.debug("Failed to update processing provenance for %s: %s", video, exc)
        if not getattr(self.config, "scanner_cache_enabled", True):
            return
        if not getattr(self.config, "scanner_queue_enabled", True):
            return
        try:
            if self._stage_state is None:
                from scan_state import ScanStateStore

                self._stage_state = ScanStateStore.from_config(self.config)
            self._stage_state.update_ai_job_stage(video, stage, status, message)
            self._stage_state.commit()
        except Exception as exc:  # noqa: BLE001 - stage tracking must not break subtitle generation.
            self._discard_stage_state()
            self.logger.debug("Failed to update AI job stage for %s: %s", video, exc)

    def _close_stage_state(self) -> None:
        if self._stage_state is None:
            return
        state = self._stage_state
        self._stage_state = None
        try:
            state.commit()
        except Exception:
            try:
                state.rollback()
            except Exception:
                pass
            raise
        finally:
            state.close()

    def _discard_stage_state(self) -> None:
        state = self._stage_state
        self._stage_state = None
        if state is None:
            return
        try:
            state.rollback()
        except Exception:
            pass
        finally:
            try:
                state.close()
            except Exception:
                pass

    def _asr_review_context(
        self,
        video: Path,
        exc: BaseException,
    ) -> dict[str, object]:
        review_ranges, reason_code = self._asr_exception_review_evidence(exc)
        result: dict[str, object] = {
            "failure_code": reason_code or "asr_review",
            "reason_code": reason_code,
            "review_ranges": [[start, end] for start, end in review_ranges],
            "media_fingerprint": None,
            "audio_fingerprint": None,
            "audio_stream_fingerprint": asr_audio_stream_fingerprint(
                self._selected_audio_stream
            ),
            "cache_fingerprint": None,
            "repair_fingerprint": "",
            "repair_attempted": False,
            "cache_trusted": False,
        }
        for current in self._exception_chain(exc):
            raw_context = getattr(current, "asr_context", None)
            if not isinstance(raw_context, dict) or not raw_context:
                continue
            for key in (
                "failure_code",
                "reason_code",
                "review_ranges",
                "media_fingerprint",
                "audio_fingerprint",
                "audio_stream_fingerprint",
                "cache_fingerprint",
                "repair_fingerprint",
                "repair_attempted",
                "cache_trusted",
                "asr_review_checkpoint",
            ):
                value = raw_context.get(key)
                if value not in (None, "", [], {}):
                    result[key] = value
            break
        if result["media_fingerprint"] is None and video.is_file():
            try:
                result["media_fingerprint"] = asr_file_fingerprint(video)
            except OSError:
                pass
        return result

    def _asr_review_candidates(
        self,
        context: dict[str, object],
    ) -> list[dict[str, object]]:
        full_retry = {
            "action": "ai.retranscribe",
            "label": "Run a full re-transcription (archives current outputs)",
            "strategy": "full_transcription_rerun",
            "selective": False,
        }
        checkpoint = context.get("asr_review_checkpoint")
        if (
            not bool(getattr(self.config, "asr_selective_retry_enabled", True))
            or not isinstance(checkpoint, dict)
            or not bool(checkpoint.get("selective_retry_supported"))
            or bool(checkpoint.get("repair_attempted"))
            or str(context.get("reason_code") or "")
            not in {"low_confidence", "rescue_low_confidence", "asr_artifact"}
        ):
            return [full_retry]
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "").strip()
        manifest_sha256 = str(checkpoint.get("manifest_sha256") or "").strip()
        repair_fingerprint = str(
            checkpoint.get("repair_fingerprint") or ""
        ).strip()
        if not (
            checkpoint_id.startswith("asrchk_")
            and len(manifest_sha256) == 64
            and len(repair_fingerprint) == 64
        ):
            return [full_retry]
        return [
            {
                "action": "ai.retry_selective_asr",
                "label": "Repair only the rejected ASR ranges from a verified checkpoint",
                "strategy": "selective_asr_repair",
                "selective": True,
                "checkpoint_id": checkpoint_id,
                "manifest_sha256": manifest_sha256,
                "repair_fingerprint": repair_fingerprint,
                "requires_runtime_fingerprint_verification": True,
            },
            full_retry,
        ]

    @staticmethod
    def _asr_exception_review_evidence(
        exc: BaseException,
    ) -> tuple[list[tuple[float, float]], str]:
        ranges: list[tuple[float, float]] = []
        reason_code = ""
        for current in VideoWorker._exception_chain(exc):
            raw_ranges = getattr(current, "review_ranges", None)
            if isinstance(raw_ranges, list):
                for raw in raw_ranges:
                    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                        continue
                    try:
                        ranges.append((float(raw[0]), float(raw[1])))
                    except (TypeError, ValueError):
                        continue
            current_reason = str(getattr(current, "reason_code", "") or "").strip()
            if current_reason and not reason_code:
                reason_code = current_reason
        return _normalize_review_ranges(ranges), reason_code

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        pending: list[BaseException] = [exc]
        result: list[BaseException] = []
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(current)
            if isinstance(current.__cause__, BaseException):
                pending.append(current.__cause__)
            if isinstance(current.__context__, BaseException):
                pending.append(current.__context__)
        return result

    @staticmethod
    def _requires_asr_review(exc: BaseException) -> bool:
        """Return whether exhausted ASR recovery requires a human decision."""

        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            if isinstance(
                current,
                (
                    AsrReviewError,
                    AsrSelectiveRepairUnavailableError,
                    LowConfidenceTranscriptionError,
                ),
            ):
                return True
            if VideoWorker._is_transient_asr_failure(current):
                # A primary low-confidence exception remains as __context__
                # when its fallback fails for a transient reason. The current
                # failure is authoritative and must retain automatic retry.
                continue
            if isinstance(current.__cause__, BaseException):
                pending.append(current.__cause__)
            if isinstance(current.__context__, BaseException):
                pending.append(current.__context__)
        return False

    @staticmethod
    def _translation_safe_omission_review_indexes(
        exc: BaseException,
    ) -> list[int]:
        """Accept only the exact bounded omission failure emitted by this Worker."""

        if not isinstance(exc, SubtitleQualityError):
            return []
        message = " ".join(str(exc).strip().split())
        match = re.fullmatch(
            r"Translation safe-omission remained after bounded same-job recovery: "
            r"indexes=\[([1-9]\d*(?:,\s*[1-9]\d*)*)\]",
            message,
        )
        if match is None:
            return []
        indexes = [int(value.strip()) for value in match.group(1).split(",")]
        if (
            not indexes
            or len(indexes) > 32
            or any(index > 1_000_000 for index in indexes)
            or indexes != sorted(set(indexes))
        ):
            return []
        return indexes

    @staticmethod
    def _asr_review_archive_reason(exc: BaseException) -> str:
        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            if isinstance(current, AsrReviewError):
                return "translator_requested_fresh_asr"
            if isinstance(current, AsrSelectiveRepairUnavailableError):
                return str(
                    getattr(current, "reason_code", "")
                    or "selective_repair_unavailable"
                )
            if isinstance(current.__cause__, BaseException):
                pending.append(current.__cause__)
            if isinstance(current.__context__, BaseException):
                pending.append(current.__context__)
        return "deterministic_asr_quality_review"

    @staticmethod
    def _is_transient_asr_failure(exc: BaseException) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError)) or is_cuda_oom(exc):
            return True
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "connection reset",
                "connection refused",
                "network is unreachable",
                "out of memory",
                "resource temporarily unavailable",
                "temporarily unavailable",
                "temporary failure",
                "timed out",
                "timeout",
            )
        )

    @staticmethod
    def _stage_for_exception(exc: Exception) -> str:
        if VideoWorker._requires_asr_review(exc):
            return "transcription_review"
        module = exc.__class__.__module__
        name = exc.__class__.__name__
        if (
            module == "gpu_lease"
            or "GpuLease" in name
            or "resource launch plan" in str(exc).casefold()
        ):
            return "resource_runtime"
        if module == "completed_delivery" or "CompletedDelivery" in name:
            return "completed_delivery"
        if module == "audio" or "Audio" in name:
            return "audio"
        if module == "transcriber" or "Transcription" in name:
            return "transcription"
        if module == "translator" or "Translation" in name:
            return "translation"
        if module == "subtitle_quality" or "Quality" in name:
            return "quality_check"
        if module == "opencc_convert" or "OpenCC" in name:
            return "opencc"
        if module == "ass_utils" or "Ass" in name:
            return "ass_export"
        return "worker"


def _iter_legacy_ai_ass_renames(paths: SubtitlePaths) -> list[tuple[Path, Path]]:
    video_stem = _video_stem_from_ai_ass_path(paths.ai_zh_tw_ass)
    renames: list[tuple[Path, Path]] = []
    try:
        candidates = list(paths.ai_zh_tw_ass.parent.iterdir())
    except OSError:
        return []
    for ass_path in sorted(candidates, key=lambda path: path.name.casefold()):
        if not ass_path.is_file() or not ass_path.name.startswith(video_stem) or ass_path.suffix.casefold() != ".ass":
            continue
        target_path = _canonical_ai_ass_path(paths, ass_path)
        if target_path is not None:
            renames.append((ass_path, target_path))
    return renames


def _canonical_ai_ass_path(paths: SubtitlePaths, ass_path: Path) -> Path | None:
    lowered = ass_path.name.casefold()
    if ".ai" not in lowered or not lowered.endswith(".ass"):
        return None
    role = _canonical_ai_role_from_name(lowered)
    if role == "zh_tw":
        return paths.ai_zh_tw_ass
    if role == "zh_cn":
        return paths.ai_zh_cn_ass
    if role == "ja":
        return paths.ai_ja_ass
    return None


def _canonical_ai_srt_path(paths: SubtitlePaths, srt_path: Path) -> Path | None:
    lowered = srt_path.name.casefold()
    if ".ai" not in lowered or not lowered.endswith(".srt"):
        return None
    role = _canonical_ai_role_from_name(lowered)
    if role == "zh_tw":
        return paths.zh_tw_srt
    if role == "zh_cn":
        return paths.zh_cn_srt
    if role == "ja":
        return paths.ja_srt
    return None


def _canonical_ai_role_from_name(lowered_name: str) -> str | None:
    compact = "".join(lowered_name.split())
    if (
        ".ai繁日雙語" in compact
        or ".ai繁體中文" in compact
        or ".繁日雙語" in compact
        or ".繁體中文" in compact
        or ".zh-tw." in compact
        or ".zh_tw." in compact
        or ".zhtw." in compact
    ):
        return "zh_tw"
    if (
        ".ai简日双语" in compact
        or ".ai簡日雙語" in compact
        or ".ai简体中文" in compact
        or ".ai簡體中文" in compact
        or ".简日双语" in compact
        or ".簡日雙語" in compact
        or ".简体中文" in compact
        or ".簡體中文" in compact
        or ".zh-cn." in compact
        or ".zh_cn." in compact
        or ".zh." in compact
    ):
        return "zh_cn"
    if (
        ".ai日本語" in compact
        or ".ai日文" in compact
        or ".ai日語" in compact
        or ".japanese." in compact
        or ".ja." in compact
    ):
        return "ja"
    return None


def _language_gate_stage(result: object) -> str:
    reason = str(getattr(result, "reason", "") or "")
    if reason == "language_uncertain":
        return "language_uncertain"
    return "language_skip"


def _source_transcription_language(result: LanguageDetectionResult | None, config: AppConfig) -> str | None:
    if not _should_transcribe_non_allowed_language(result, config):
        return None
    language = _normalized_language(getattr(result, "language", ""))
    if language in {"", "unknown", "und"}:
        return None
    return language


def _uncertain_language_has_japanese_evidence(
    result: LanguageDetectionResult,
    config: AppConfig,
    video: Path,
    selected_audio_stream: AudioStreamInfo | None,
) -> bool:
    """Allow forced Japanese ASR only when an uncertain result has Japanese evidence."""

    detected = _canonical_language(getattr(result, "language", ""))
    allowed = {
        _canonical_language(language)
        for language in (getattr(config, "allowed_source_languages", ()) or ())
    }
    if detected and (detected in allowed or detected == "ja"):
        return True
    if _audio_stream_is_explicitly_japanese(selected_audio_stream):
        return True
    try:
        return has_japanese_audio_stream(video)
    except Exception:
        return False


def _audio_stream_is_explicitly_japanese(stream: AudioStreamInfo | None) -> bool:
    if stream is None:
        return False
    if _canonical_language(getattr(stream, "language", "")) == "ja":
        return True
    title = str(getattr(stream, "title", "") or "").strip().casefold()
    return any(marker in title for marker in ("japanese", "jpn", "日本語", "日語", "日语"))


def _canonical_language(language: object) -> str:
    normalized = _normalized_language(language)
    primary = normalized.split("-", 1)[0]
    if primary in {"ja", "jp", "jpn", "jap", "japanese"}:
        return "ja"
    return primary


def _should_retry_language_gate_with_japanese_audio(
    result: LanguageDetectionResult | None,
    config: AppConfig,
    video: Path,
) -> bool:
    if result is None:
        return False
    if not bool(getattr(config, "language_gate_enabled", False)):
        return False
    if not _should_transcribe_non_allowed_language(result, config):
        return False
    if _normalized_language(getattr(result, "language", "")) in {"ja", "jpn", "japanese"}:
        return False
    try:
        return has_japanese_audio_stream(video)
    except Exception:
        return False


def _japanese_transcription_model(config: AppConfig) -> str:
    return str(getattr(config, "japanese_transcription_model", None) or config.whisper_model)


def _japanese_transcription_backend(config: AppConfig) -> str:
    return str(getattr(config, "japanese_transcription_backend", None) or config.transcription_backend)


def _non_japanese_transcription_backend(config: AppConfig) -> str:
    return str(getattr(config, "non_japanese_transcription_backend", None) or config.transcription_backend)


def _compact_error_message(exc: BaseException, *, limit: int = 240) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = exc.__class__.__name__
    if len(message) > limit:
        return message[: limit - 3].rstrip() + "..."
    return message


def _should_transcribe_non_allowed_language(result: LanguageDetectionResult | None, config: AppConfig) -> bool:
    if result is None:
        return False
    if not bool(getattr(config, "transcribe_non_allowed_languages", False)):
        return False
    if not bool(getattr(result, "confident", False)):
        return False
    if bool(getattr(result, "allowed", False)):
        return False
    return _normalized_language(getattr(result, "language", "")) not in {"", "unknown", "und"}


def _normalized_language(language: object) -> str:
    return str(language or "").strip().lower().replace("_", "-")


def _config_with_overrides(config: AppConfig, **overrides: object) -> AppConfig:
    if is_dataclass(config) and not isinstance(config, type):
        return replace(config, **overrides)
    values = dict(vars(config))
    values.update(overrides)
    return SimpleNamespace(**values)


def _video_stem_from_ai_ass_path(path: Path) -> str:
    marker = ".AI"
    index = path.name.find(marker)
    if index >= 0:
        return path.name[:index]
    return path.stem


def _wrap_subtitle_text_for_readability(text: str, max_chars: int) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    max_chars = max(1, int(max_chars or 42))
    if _display_width(normalized) <= max_chars:
        return [normalized]
    words = normalized.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and _display_width(candidate) > max_chars:
            lines.extend(_split_long_token_for_readability(current, max_chars))
            current = word
        else:
            current = candidate
    if current:
        lines.extend(_split_long_token_for_readability(current, max_chars))
    return [line for line in lines if line.strip()]


def _split_long_token_for_readability(text: str, max_chars: int) -> list[str]:
    if _display_width(text) <= max_chars:
        return [text]
    result: list[str] = []
    current = ""
    for char in text:
        candidate = f"{current}{char}"
        if current and _display_width(candidate) > max_chars:
            result.append(current)
            current = char
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _display_width(text: str) -> int:
    width = 0
    for char in str(text or ""):
        width += 2 if ord(char) >= 0x2E80 else 1
    return width


KANA_FRAGMENT_CHARS = set("ぁぃぅぇぉゃゅょっァィゥェォャュョッー")


def _merge_short_kana_fragments(blocks: list[SrtBlock]) -> tuple[list[SrtBlock], int]:
    merged: list[SrtBlock] = []
    merged_count = 0
    for block in blocks:
        text = "".join("".join(block.text).split())
        if merged and 0 < len(text) <= 2 and all(char in KANA_FRAGMENT_CHARS for char in text):
            previous = merged[-1]
            merged[-1] = SrtBlock(
                index=previous.index,
                timing=_merge_timing(previous.timing, block.timing),
                text=[f"{''.join(previous.text)}{text}"],
            )
            merged_count += 1
            continue
        merged.append(block)
    return merged, merged_count


def _merge_timing(previous_timing: str, current_timing: str) -> str:
    previous_start = previous_timing.split("-->")[0].strip()
    current_end = current_timing.split("-->")[-1].strip()
    return f"{previous_start} --> {current_end}"


def _ass_timestamp_milliseconds(value: str) -> int:
    try:
        hours, minutes, remainder = value.split(":", 2)
        seconds, centiseconds = remainder.split(".", 1)
        return (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(centiseconds.ljust(2, "0")[:2]) * 10
        )
    except (TypeError, ValueError) as exc:
        raise SubtitleQualityError(f"Invalid ASS timestamp: {value!r}") from exc


def _srt_block_timing_milliseconds(block: SrtBlock) -> tuple[int, int]:
    try:
        start, end = [part.strip() for part in block.timing.split("-->", 1)]
        return _srt_timestamp_milliseconds(start), _srt_timestamp_milliseconds(end)
    except (AttributeError, ValueError) as exc:
        raise SubtitleQualityError(f"Invalid SRT timing: {getattr(block, 'timing', '')!r}") from exc


def _srt_timestamp_milliseconds(value: str) -> int:
    try:
        hours, minutes, remainder = value.split(":", 2)
        seconds, milliseconds = remainder.split(",", 1)
        return (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(milliseconds.ljust(3, "0")[:3])
        )
    except (TypeError, ValueError) as exc:
        raise SubtitleQualityError(f"Invalid SRT timestamp: {value!r}") from exc
