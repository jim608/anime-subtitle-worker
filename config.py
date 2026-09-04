from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
from typing import Any

from source_analyzer import (
    ANALYZER_VERSION as DEFAULT_SOURCE_ANALYZER_VERSION,
    DECISION_SCHEMA_VERSION as DEFAULT_SOURCE_DECISION_SCHEMA_VERSION,
    DECISION_VERSION as DEFAULT_SOURCE_DECISION_VERSION,
    AnalyzerThresholds,
)


REQUIRED_FIELDS = {
    "input_path",
    "work_path",
    "log_path",
    "video_extensions",
    "whisper_model",
    "whisper_device",
    "whisper_compute_type",
    "whisper_language",
    "whisper_task",
    "whisper_vad_filter",
    "whisper_condition_on_previous_text",
    "whisper_temperature",
    "enable_vocal_separation",
    "vocal_separation_engine",
    "vocal_separation_output",
    "translator_base_url",
    "translator_api_key",
    "translator_model",
    "translator_timeout_seconds",
    "batch_size",
    "max_retries",
    "watch_interval_seconds",
    "opencc_config",
    "keep_intermediate_files",
}


@dataclass(frozen=True)
class AppConfig:
    input_path: Path
    work_path: Path
    log_path: Path
    video_extensions: list[str]
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str
    whisper_task: str
    whisper_vad_filter: bool
    whisper_condition_on_previous_text: bool
    whisper_temperature: float
    enable_vocal_separation: bool
    vocal_separation_engine: str
    vocal_separation_output: str
    translator_base_url: str
    translator_api_key: str
    translator_model: str
    translator_timeout_seconds: int
    batch_size: int
    max_retries: int
    watch_interval_seconds: int
    opencc_config: str
    keep_intermediate_files: bool
    config_path: Path | None = None
    translator_fallback_models: list[str] = field(default_factory=list)
    translator_ollama_auto_unload_enabled: bool = False
    translator_ollama_unload_timeout_seconds: float = 15.0
    translation_request_hard_timeout_seconds: int = 180
    translation_request_max_tokens: int = 512
    translation_repair_max_tokens: int = 96
    translation_max_line_chars: int = 320
    translation_max_line_expansion_ratio: float = 8.0
    max_concurrent_videos: int = 1
    m2_server_canary_observer_enabled: bool = False
    m2_server_canary_observation_gate_size: int = 20
    m2_server_canary_observation_state_path: str = "m2_server_canary_observation.json"
    m2_server_canary_observation_output_dir: str = "m2_server_canary_observations"
    m2_server_canary_circuit_breaker_enabled: bool = False
    m2_server_canary_circuit_breaker_state_path: str = "m2_server_canary_circuit_breaker.json"
    m2_server_canary_repeated_oom_threshold: int = 3
    m2_server_canary_identical_failure_threshold: int = 3
    auto_enable_ai_fallback: bool = True
    auto_mikan_parallel_with_ai: bool = False
    auto_ai_run_before_mikan: bool = False
    auto_ai_max_videos_per_cycle: int = 1
    auto_ai_drain_queue_between_cycles: bool = True
    auto_ai_failure_cooldown_seconds: int = 86400
    auto_ai_max_attempts: int = 3
    auto_ai_asr_review_cooldown_seconds: int = 900
    auto_ai_failed_retry_sweep_enabled: bool = False
    auto_ai_failed_retry_sweep_interval_seconds: int = 300
    auto_ai_failed_retry_sweep_max_items: int = 1
    auto_ai_quality_review_autopilot_enabled: bool = False
    auto_ai_quality_review_autopilot_interval_seconds: int = 60
    auto_target_review_autopilot_enabled: bool = False
    auto_target_review_autopilot_interval_seconds: int = 300
    ai_queue_running_stale_seconds: int = 21600
    ai_queue_stage_stale_seconds: int = 900
    ai_process_isolation_enabled: bool = True
    ai_subprocess_timeout_seconds: int = 14400
    resource_admission_enabled: bool = False
    resource_admission_state_path: str = "resource_admission_state.json"
    resource_admission_telemetry_stale_seconds: float = 15.0
    resource_admission_cpu_yellow_percent: float = 80.0
    resource_admission_cpu_red_percent: float = 95.0
    resource_admission_ram_yellow_available_ratio: float = 0.20
    resource_admission_ram_red_available_ratio: float = 0.08
    resource_admission_gpu_yellow_percent: float = 85.0
    resource_admission_gpu_red_percent: float = 98.0
    resource_admission_vram_reserve_mib: float = 2048.0
    resource_admission_primary_vram_mib: float = 8500.0
    resource_admission_lower_memory_vram_mib: float = 6200.0
    resource_admission_recovery_samples: int = 3
    resource_admission_yellow_retry_seconds: float = 30.0
    resource_admission_red_retry_seconds: float = 120.0
    resource_admission_unavailable_retry_seconds: float = 60.0
    resource_admission_recent_oom_cooldown_seconds: int = 21600
    resource_telemetry_gpu_timeout_seconds: float = 2.0
    resource_telemetry_host_timeout_seconds: float = 1.0
    resource_telemetry_cpu_sample_interval_seconds: float = 0.10
    resource_gpu_lease_path: str = "gpu_leases/gpu-0.lock"
    resource_gpu_lease_wait_seconds: float = 7200.0
    completed_delivery_enabled: bool = False
    completed_delivery_path: str = ""
    completed_delivery_manifest_path: str = "completed_delivery_manifests"
    completed_delivery_source_policy: str = "retain"
    completed_delivery_timeout_seconds: int = 7200
    require_ai_subtitles: bool = False
    scanner_cache_enabled: bool = True
    scanner_state_path: str = "scanner_state.sqlite3"
    scanner_recent_first: bool = True
    scanner_queue_enabled: bool = True
    pipeline_job_store_required: bool = True
    source_analyzer_enabled: bool = False
    source_analyzer_high_confidence: float = 0.90
    source_analyzer_low_confidence: float = 0.60
    source_analyzer_min_dialogue_completeness_score: float = 0.68
    source_analyzer_min_subtitle_coverage_ratio: float = 0.60
    source_analyzer_tie_margin: float = 0.025
    source_analyzer_version: str = DEFAULT_SOURCE_ANALYZER_VERSION
    source_decision_schema_version: int = DEFAULT_SOURCE_DECISION_SCHEMA_VERSION
    source_decision_version: str = DEFAULT_SOURCE_DECISION_VERSION
    source_integrity_sha256_enabled: bool = False
    ai_canary_once_enabled: bool = False
    acceptance_queue_lane_enabled: bool = False
    acceptance_queue_lane_plan_path: str = "acceptance/plan.json"
    acceptance_fault_execution_enabled: bool = False
    acceptance_fault_execution_run_id: str = ""
    acceptance_fault_execution_plan_sha256: str = ""
    scanner_event_watch_enabled: bool = False
    scanner_event_stability_interval_seconds: float = 2.0
    scanner_event_quiet_window_seconds: float = 5.0
    scanner_event_stable_observations_required: int = 2
    scanner_event_watch_health_interval_seconds: float = 30.0
    scanner_event_ffprobe_path: str = "ffprobe"
    scanner_event_media_probe_timeout_seconds: float = 30.0
    scanner_event_media_probe_min_throughput_mib_per_second: float = 8.0
    scanner_event_media_probe_max_timeout_seconds: float = 1800.0
    scanner_event_media_probe_max_attempts: int = 4
    scanner_event_media_probe_max_retry_seconds: float = 900.0
    scanner_skip_standalone_op_ed: bool = True
    scanner_candidate_min_age_seconds: int = 0
    scanner_incremental_scan_enabled: bool = False
    scanner_incremental_overlap_seconds: int = 300
    scanner_quick_scan_recent_days: int = 0
    scanner_full_scan_interval_seconds: int = 0
    scanner_background_scan_interval_seconds: int = 21600
    scanner_fallback_scan_interval_seconds: int = 21600
    scanner_background_scan_startup_delay_seconds: int = 600
    scanner_reconcile_batch_size: int = 1000
    scanner_reconcile_budget_seconds: int = 60
    scanner_reconcile_batch_interval_seconds: int = 60
    scanner_active_queue_ledger_backfill_enabled: bool = True
    scanner_active_queue_ledger_backfill_interval_seconds: int = 10
    scanner_active_queue_ledger_backfill_batch_size: int = 250
    scanner_active_queue_ledger_backfill_no_progress_seconds: int = 300
    scanner_inventory_file_timeout_seconds: int = 30
    scanner_walk_yield_every_entries: int = 256
    scanner_walk_yield_seconds: float = 0.025
    scanner_queue_oldest_every_n_cycles: int = 12
    control_state_path: str = "control_state.sqlite3"
    control_inbox_path: str = "control_inbox"
    ai_output_manifest_path: str = "ai_output_manifests"
    ai_output_versions_keep: int = 3
    official_subtitle_versions_keep: int = 3
    ai_stage_event_retention_days: int = 30
    ai_stage_event_max_rows: int = 25000
    translation_allow_source_fallback: bool = False
    translation_context_retry_without_context: bool = True
    translation_context_auto_disable: bool = True
    translation_context_fast_retry_without_context_on_format_error: bool = True
    translation_context_retry_without_context_on_timeout: bool = True
    translation_split_batch_on_timeout: bool = True
    translation_split_batch_on_format_error: bool = True
    translation_reject_residual_kana: bool = True
    cleanup_backup_files: bool = False
    backup_retention_count: int = 0
    state_backup_path: str = "state_backups"
    state_backup_retention_count: int = 14
    state_backup_enabled: bool = True
    state_backup_interval_hours: int = 24
    state_backup_startup_delay_seconds: int = 120
    database_maintenance_enabled: bool = True
    database_maintenance_interval_hours: int = 168
    database_maintenance_startup_delay_seconds: int = 1800
    database_maintenance_min_reclaim_mib: float = 64.0
    database_maintenance_min_freelist_ratio: float = 0.25
    transcription_backend: str = "faster-whisper"
    japanese_transcription_backend: str | None = None
    japanese_transcription_fallback_backend: str | None = None
    japanese_transcription_final_fallback_backend: str | None = None
    non_japanese_transcription_backend: str | None = None
    non_japanese_transcription_fallback_backend: str | None = None
    non_japanese_transcription_final_fallback_backend: str | None = None
    max_subtitle_duration_seconds: float = 7.0
    whisper_initial_prompt: str | None = None
    whisper_beam_size: int = 5
    whisper_best_of: int = 5
    whisper_patience: float = 1.0
    whisper_length_penalty: float = 1.0
    whisper_repetition_penalty: float = 1.0
    whisper_no_repeat_ngram_size: int = 0
    whisper_word_timestamps: bool = True
    whisper_no_speech_threshold: float | None = 0.6
    whisper_log_prob_threshold: float | None = -1.0
    whisper_compression_ratio_threshold: float | None = 2.4
    whisper_hallucination_silence_threshold: float | None = None
    whisper_model_cache_enabled: bool = True
    whisper_vad_threshold: float = 0.5
    whisper_vad_min_silence_duration_ms: int = 2000
    whisper_vad_speech_pad_ms: int = 400
    whisper_hallucination_phrases: list[str] = field(default_factory=list)
    filter_repeated_vocalizations: bool = True
    repeated_vocalization_min_chars: int = 6
    transcription_quality_check_enabled: bool = True
    transcription_quality_min_audio_seconds: float = 600.0
    transcription_quality_min_coverage_percent: float = 8.0
    transcription_quality_min_blocks_per_minute: float = 1.5
    transcription_quality_min_avg_logprob: float = -1.0
    transcription_quality_max_low_confidence_percent: float = 25.0
    transcription_quality_min_confidence_segments: int = 8
    transcription_quality_max_leading_gap_seconds: float = 30.0
    asr_diagnostics_enabled: bool = True
    asr_diagnostics_path: str = "asr_diagnostics"
    asr_selective_retry_enabled: bool = True
    asr_selective_retry_padding_seconds: float = 1.5
    asr_selective_retry_merge_gap_seconds: float = 3.0
    asr_optional_rescue_rejection_is_fatal: bool = True
    asr_prompt_free_allow_recovered_primary_artifacts: bool = False
    subtitle_quality_check_enabled: bool = True
    subtitle_quality_fail_job: bool = True
    subtitle_quality_max_duration_seconds: float = 5.5
    subtitle_quality_max_primary_chars: int = 42
    subtitle_quality_hard_max_primary_chars: int = 64
    subtitle_quality_max_gap_seconds: float = 45.0
    subtitle_quality_max_leading_gap_seconds: float = 30.0
    subtitle_quality_warn_cps: float = 17.0
    subtitle_quality_fail_cps: float = 25.0
    subtitle_quality_min_duration_seconds: float = 0.35
    subtitle_quality_hard_min_duration_seconds: float = 0.12
    subtitle_quality_max_overlap_seconds: float = 0.10
    subtitle_remediation_punctuation_repeat_limit: int = 3
    subtitle_remediation_wrap_max_chars: int = 42
    subtitle_remediation_max_visual_lines: int = 2
    subtitle_remediation_max_timing_shift_seconds: float = 0.20
    subtitle_remediation_max_total_timing_shift_seconds: float = 0.50
    subtitle_remediation_max_overlap_repair_seconds: float = 0.20
    subtitle_remediation_aligned_max_timing_shift_seconds: float = 2.0
    subtitle_remediation_aligned_max_total_timing_shift_seconds: float = 3.0
    subtitle_remediation_aligned_max_overlap_repair_seconds: float = 2.0
    translation_glossary: dict[str, str] = field(default_factory=dict)
    translation_memory_enabled: bool = True
    translation_memory_auto_apply_enabled: bool = True
    translation_memory_path: str = "translation_memory.sqlite3"
    translation_memory_outbox_path: str = "translation_memory_outbox"
    translation_context_enabled: bool = False
    translation_context_max_blocks: int = 120
    translation_context_max_chars: int = 6000
    translation_context_max_output_chars: int = 2000
    language_gate_enabled: bool = False
    allowed_source_languages: list[str] = field(default_factory=lambda: ["ja"])
    skip_non_allowed_language: bool = True
    transcribe_non_allowed_languages: bool = False
    translate_non_japanese_sources: bool = True
    language_detect_model: str | None = None
    japanese_transcription_model: str | None = None
    japanese_transcription_fallback_model: str | None = None
    japanese_transcription_fallback_compute_type: str | None = None
    japanese_transcription_final_fallback_model: str | None = None
    japanese_transcription_final_fallback_compute_type: str | None = None
    non_japanese_transcription_model: str | None = None
    non_japanese_transcription_fallback_model: str | None = None
    non_japanese_transcription_fallback_compute_type: str | None = None
    non_japanese_transcription_final_fallback_model: str | None = None
    non_japanese_transcription_final_fallback_compute_type: str | None = None
    ai_source_transcript_ass_suffix_template: str = ".AI{label}.{language}.ass"
    language_detect_min_probability: float = 0.70
    language_uncertain_policy: str = "skip"
    language_detect_sample_count: int = 3
    language_detect_sample_seconds: int = 30
    language_detect_cache_enabled: bool = True
    language_detect_cache_path: str = "language_detection_cache.json"
    audio_content_probe_enabled: bool = True
    audio_content_probe_max_streams: int = 8
    audio_content_probe_sample_count: int = 3
    audio_content_probe_sample_seconds: int = 12
    audio_selection_manifest_path: str = "audio_selection"
    processing_provenance_path: str = "provenance"
    force_ai_bypass_language_gate: bool = False
    translation_metadata_context_enabled: bool = False
    metadata_context_providers: list[str] = field(default_factory=lambda: ["anilist"])
    metadata_context_cache_path: str = "metadata_context_cache.json"
    metadata_context_ttl_days: int = 30
    metadata_context_max_chars: int = 2000
    metadata_context_timeout_seconds: int = 15
    metadata_context_include_spoilers: bool = False
    series_metadata_db_path: str = "series_metadata.sqlite3"
    series_metadata_match_min_confidence: float = 0.65
    series_metadata_auto_seed_terms: bool = True
    series_metadata_sync_enabled: bool = True
    series_metadata_sync_interval_seconds: int = 21600
    series_metadata_sync_startup_delay_seconds: int = 30
    series_metadata_enrich_enabled: bool = True
    series_metadata_enrich_per_cycle: int = 5
    series_metadata_enrich_delay_seconds: float = 1.0
    series_artwork_cache_enabled: bool = True
    series_artwork_cache_path: str = "series_artwork"
    series_artwork_cache_max_mib: int = 512
    series_artwork_max_bytes: int = 3 * 1024 * 1024
    series_artwork_ttl_days: int = 30
    notification_webhook_url: str = ""
    notification_events: list[str] = field(
        default_factory=lambda: ["asr_review", "ai_failure", "extract_terminal_failure"]
    )
    notification_min_interval_seconds: int = 300
    notification_state_path: str = "notification_state.json"
    storage_io_pressure_enabled: bool = True
    storage_io_pressure_some_avg10_threshold: float = 35.0
    storage_io_pressure_full_avg10_threshold: float = 10.0
    storage_io_pressure_backoff_seconds: float = 2.0
    subtitle_timing_mode: str = "word"
    subtitle_max_duration_seconds: float = 4.8
    subtitle_min_duration_seconds: float = 0.8
    subtitle_max_chars: int = 24
    subtitle_end_padding_seconds: float = 0.12
    subtitle_min_gap_seconds: float = 0.06
    write_gap_report: bool = True
    gap_report_threshold_seconds: float = 4.0
    enable_gap_rescue: bool = True
    enable_leading_gap_rescue: bool = True
    gap_rescue_threshold_seconds: float = 4.0
    gap_rescue_leading_threshold_seconds: float = 1.5
    gap_rescue_max_gap_seconds: float = 45.0
    gap_rescue_leading_max_seconds: float = 120.0
    gap_rescue_padding_seconds: float = 0.8
    gap_rescue_clip_seconds: float = 30.0
    gap_rescue_clip_overlap_seconds: float = 2.0
    gap_rescue_max_gaps: int = 12
    gap_rescue_min_chars: int = 2
    gap_rescue_no_speech_threshold: float = 0.95
    gap_rescue_log_prob_threshold: float = -1.5
    gap_rescue_compression_ratio_threshold: float = 2.4
    gap_rescue_accept_min_avg_logprob: float = -1.15
    gap_rescue_accept_max_no_speech_prob: float = 0.90
    gap_rescue_accept_max_compression_ratio: float = 2.4
    op_ed_transcription_enabled: bool = True
    op_ed_min_audio_seconds: float = 600.0
    op_ed_opening_window_seconds: float = 360.0
    op_ed_ending_window_seconds: float = 300.0
    op_ed_gap_threshold_seconds: float = 6.0
    op_ed_max_gap_seconds: float = 210.0
    op_ed_padding_seconds: float = 1.0
    op_ed_max_rescue_ranges: int = 6
    op_ed_no_speech_threshold: float = 0.95
    op_ed_log_prob_threshold: float = -1.5
    op_ed_compression_ratio_threshold: float = 3.0
    op_ed_accept_min_avg_logprob: float = -1.15
    op_ed_accept_max_no_speech_prob: float = 0.90
    op_ed_accept_max_compression_ratio: float = 2.4
    op_ed_initial_prompt: str | None = None
    export_ai_ass: bool = True
    ai_japanese_ass_suffix: str = ".AI日本語.ja.ass"
    ai_simplified_chinese_ass_suffix: str = ".AI简日双语.zh-CN.ass"
    ai_traditional_chinese_ass_suffix: str = ".AI繁日雙語.zh-TW.ass"
    finished_subtitle_suffixes: list[str] = field(
        default_factory=lambda: [
            ".AI繁日雙語.zh-TW.ass",
            ".AI简日双语.zh-CN.ass",
            ".AI繁體中文.zh-TW.ass",
            ".AI简体中文.zh-CN.ass",
            ".繁體中文.zh-TW.ass",
            ".简体中文.zh-CN.ass",
            ".zh-TW.ass",
            ".zh-CN.ass",
        ]
    )
    ass_play_res_x: int = 1920
    ass_play_res_y: int = 1080
    ass_font_name: str = "Noto Sans CJK TC"
    ass_primary_font_size: int = 58
    ass_secondary_font_size: int = 32
    ass_primary_color: str = "&H00FFFFFF"
    ass_secondary_color: str = "&HE6E6E6&"
    ass_outline_color: str = "&H00000000"
    ass_back_color: str = "&H80000000"
    ass_secondary_alpha: str = "&H18&"
    ass_primary_outline: float = 2.2
    ass_secondary_outline: float = 1.4
    ass_shadow: float = 0.0
    ass_margin_l: int = 40
    ass_margin_r: int = 40
    ass_margin_v: int = 70
    ass_style_versioning_enabled: bool = True
    safety_check_enabled: bool = True
    disk_min_free_gb: float = 2.0
    mikan_enabled: bool = False
    mikan_base_url: str = "https://mikanani.me"
    mikan_bangumi_ids: list[int] = field(default_factory=list)
    mikan_auto_match_enabled: bool = True
    mikan_auto_match_threshold: float = 0.86
    mikan_auto_match_cache_path: str = "mikan_auto_matches.json"
    mikan_auto_match_max_candidates: int = 6
    mikan_auto_match_max_lookups_per_cycle: int = 25
    mikan_library_scan_recent_first: bool = True
    mikan_library_scan_recent_series_per_cycle: int = 20
    mikan_library_scan_max_series_per_cycle: int = 80
    mikan_episode_index_ttl_seconds: int = 21600
    mikan_library_fallback_scan_interval_seconds: int = 3600
    mikan_library_fallback_scan_max_series_per_cycle: int = 8
    mikan_seen_path: str = "mikan_seen.json"
    mikan_pending_path: str = "mikan_pending.json"
    mikan_sqlite_authoritative_state: bool = True
    mikan_download_start_timeout_seconds: int = 180
    mikan_download_metadata_timeout_seconds: int = 300
    mikan_download_unhealthy_timeout_seconds: int = 300
    mikan_download_stall_timeout_seconds: int = 600
    mikan_download_max_eta_seconds: int = 86400
    mikan_completed_reconcile_max_age_seconds: int = 21600
    mikan_no_candidate_retry_seconds: int = 600
    mikan_no_candidate_retry_max_seconds: int = 86400
    mikan_delete_stalled_torrents: bool = True
    mikan_request_timeout_seconds: int = 30
    mikan_max_items_per_bangumi: int = 1
    mikan_watch_interval_seconds: int = 300
    mikan_completed_poll_interval_seconds: int = 30
    mikan_active_poll_interval_seconds: int = 5
    mikan_operation_lock_wait_seconds: int = 300
    mikan_extract_failed_retry_seconds: int = 900
    mikan_extract_workers: int = 2
    mikan_extract_workers_during_ai: int = 1
    mikan_extract_job_timeout_seconds: int = 900
    mikan_extract_job_timeout_per_video_seconds: int = 300
    mikan_extract_job_timeout_max_seconds: int = 14400
    mikan_extract_cancel_grace_seconds: int = 15
    mikan_extract_timeout_retry_seconds: int = 60
    mikan_extract_lease_seconds: int = 900
    mikan_extract_completed: bool = True
    mikan_remove_ai_after_extract: bool = True
    mikan_require_extractable_subtitle: bool = True
    subtitle_extract_timeout_seconds: int = 300
    mikan_fallback_sources_enabled: bool = False
    mikan_fallback_sources: list[str] = field(
        default_factory=lambda: ["animegarden", "dmhy", "acgrip", "bangumimoe", "kisssub", "nyaa"]
    )
    mikan_fallback_cache_path: str = "mikan_fallback_sources.json"
    mikan_fallback_cache_ttl_seconds: int = 21600
    mikan_fallback_max_lookups_per_cycle: int = 6
    mikan_fallback_source_timeout_seconds: int = 20
    mikan_fallback_source_failure_threshold: int = 2
    mikan_fallback_source_cooldown_seconds: int = 1800
    mikan_fallback_min_nyaa_seeders: int = 1
    mikan_prefer_keywords: list[str] = field(
        default_factory=lambda: [
            "简繁日内封",
            "簡繁日內封",
            "简繁内封",
            "簡繁內封",
            "繁日双语",
            "繁日雙語",
            "繁日内封",
            "繁日內封",
            "繁体",
            "繁體",
            "CHT",
        ]
    )
    mikan_reject_keywords: list[str] = field(default_factory=list)
    qbit_base_url: str = "http://localhost:8080"
    qbit_username: str = "admin"
    qbit_password: str = "adminadmin"
    qbit_timeout_seconds: int = 30
    qbit_save_path: str | None = None
    qbit_category: str | None = None
    qbit_tags: list[str] = field(default_factory=lambda: ["llm-sub"])
    qbit_paused: bool = False
    qbit_path_mappings: list[dict[str, str]] = field(default_factory=list)
    mikan_series_path_mappings: list[dict[str, Any]] = field(default_factory=list)
    mikan_processed_tags: list[str] = field(default_factory=lambda: ["llm-sub-extracted"])
    mikan_completed_tags: list[str] = field(default_factory=lambda: ["mikansub-completed"])
    whisperx_batch_size: int = 8
    whisper_dynamic_batch_enabled: bool = True
    whisper_gpu_memory_reserve_mib: int = 2048
    whisperx_align_model: str | None = None
    whisperx_vad_method: str = "pyannote"
    whisperx_vad_onset: float = 0.5
    whisperx_vad_offset: float = 0.363
    whisperx_chunk_size: int = 30
    vibevoice_model: str = "microsoft/VibeVoice-ASR"
    vibevoice_device_map: str = "auto"
    vibevoice_torch_dtype: str = "auto"
    vibevoice_trust_remote_code: bool = True
    vibevoice_max_new_tokens: int = 0
    vibevoice_tokenizer_chunk_size: int = 0
    vibevoice_prompt: str | None = "Japanese anime dialogue. Transcribe Japanese speech with timestamps."
    transformers_whisper_chunk_length_s: float = 30.0
    transformers_whisper_batch_size: int = 8
    transformers_whisper_torch_dtype: str = "float16"
    transformers_whisper_trust_remote_code: bool = False
    transformers_whisper_punctuator: bool = False
    transformers_whisper_attn_implementation: str | None = None
    transformers_whisper_task: str | None = None
    transformers_whisper_stable_ts: bool = False

    def source_analyzer_thresholds(self) -> AnalyzerThresholds:
        """Build the analyzer policy from the validated application config."""

        defaults = AnalyzerThresholds()
        return AnalyzerThresholds(
            auto_accept_confidence=self.source_analyzer_high_confidence,
            review_confidence=self.source_analyzer_low_confidence,
            min_subtitle_coverage_ratio=self.source_analyzer_min_subtitle_coverage_ratio,
            min_dialogue_completeness_score=(
                self.source_analyzer_min_dialogue_completeness_score
            ),
            close_candidate_score_margin=self.source_analyzer_tie_margin,
            exact_tie_score_epsilon=min(
                defaults.exact_tie_score_epsilon,
                self.source_analyzer_tie_margin,
            ),
        )


class ConfigError(ValueError):
    pass


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _expand_env_values(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env_value, value)
    if isinstance(value, list):
        return [_expand_env_values(item) for item in value]
    if isinstance(value, dict):
        return {
            _expand_env_values(key): _expand_env_values(item)
            for key, item in value.items()
        }
    return value


def _replace_env_value(match: re.Match[str]) -> str:
    name = match.group(1)
    default = match.group(2)
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value
    if default is not None:
        return default
    return match.group(0)


def load_config(config_path: str | Path) -> AppConfig:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("pyyaml is not installed. Run: pip install -r requirements.txt") from exc

    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a YAML mapping.")

    raw = _expand_env_values(raw)

    missing = sorted(REQUIRED_FIELDS - set(raw))
    if missing:
        raise ConfigError(f"Missing required config fields: {', '.join(missing)}")

    config = AppConfig(
        config_path=path,
        input_path=Path(_as_str(raw, "input_path")),
        work_path=Path(_as_str(raw, "work_path")),
        log_path=Path(_as_str(raw, "log_path")),
        video_extensions=_normalize_extensions(raw["video_extensions"]),
        whisper_model=_as_str(raw, "whisper_model"),
        whisper_device=_as_str(raw, "whisper_device"),
        whisper_compute_type=_as_str(raw, "whisper_compute_type"),
        whisper_language=_as_str(raw, "whisper_language"),
        whisper_task=_as_str(raw, "whisper_task"),
        whisper_vad_filter=_as_bool(raw, "whisper_vad_filter"),
        whisper_condition_on_previous_text=_as_bool(raw, "whisper_condition_on_previous_text"),
        whisper_temperature=float(raw["whisper_temperature"]),
        enable_vocal_separation=_as_bool(raw, "enable_vocal_separation"),
        vocal_separation_engine=_as_str(raw, "vocal_separation_engine"),
        vocal_separation_output=_as_str(raw, "vocal_separation_output"),
        translator_base_url=_as_str(raw, "translator_base_url"),
        translator_api_key=_as_str(raw, "translator_api_key"),
        translator_model=_as_str(raw, "translator_model"),
        translator_fallback_models=_optional_str_list(raw, "translator_fallback_models"),
        translator_timeout_seconds=_as_positive_int(raw, "translator_timeout_seconds"),
        translator_ollama_auto_unload_enabled=_optional_bool(
            raw,
            "translator_ollama_auto_unload_enabled",
            False,
        ),
        translator_ollama_unload_timeout_seconds=_optional_positive_float(
            raw,
            "translator_ollama_unload_timeout_seconds",
            15.0,
        ),
        translation_request_hard_timeout_seconds=_optional_positive_int(
            raw,
            "translation_request_hard_timeout_seconds",
            int(raw.get("translator_timeout_seconds", 120) or 120),
        ),
        translation_request_max_tokens=_optional_positive_int(raw, "translation_request_max_tokens", 512),
        translation_repair_max_tokens=_optional_positive_int(raw, "translation_repair_max_tokens", 96),
        translation_max_line_chars=_optional_positive_int(raw, "translation_max_line_chars", 320),
        translation_max_line_expansion_ratio=_optional_positive_float(
            raw,
            "translation_max_line_expansion_ratio",
            8.0,
        ),
        translation_allow_source_fallback=_optional_bool(raw, "translation_allow_source_fallback", False),
        translation_context_retry_without_context=_optional_bool(
            raw,
            "translation_context_retry_without_context",
            True,
        ),
        translation_context_auto_disable=_optional_bool(raw, "translation_context_auto_disable", True),
        translation_context_fast_retry_without_context_on_format_error=_optional_bool(
            raw,
            "translation_context_fast_retry_without_context_on_format_error",
            True,
        ),
        translation_context_retry_without_context_on_timeout=_optional_bool(
            raw,
            "translation_context_retry_without_context_on_timeout",
            True,
        ),
        translation_split_batch_on_timeout=_optional_bool(raw, "translation_split_batch_on_timeout", True),
        translation_split_batch_on_format_error=_optional_bool(
            raw,
            "translation_split_batch_on_format_error",
            True,
        ),
        translation_reject_residual_kana=_optional_bool(raw, "translation_reject_residual_kana", True),
        batch_size=_as_positive_int(raw, "batch_size"),
        max_retries=_as_positive_int(raw, "max_retries"),
        watch_interval_seconds=_as_positive_int(raw, "watch_interval_seconds"),
        max_concurrent_videos=_optional_positive_int(raw, "max_concurrent_videos", 1),
        m2_server_canary_observer_enabled=_optional_bool(
            raw,
            "m2_server_canary_observer_enabled",
            False,
        ),
        m2_server_canary_observation_gate_size=_optional_positive_int(
            raw,
            "m2_server_canary_observation_gate_size",
            20,
        ),
        m2_server_canary_observation_state_path=_as_optional_str(
            raw,
            "m2_server_canary_observation_state_path",
            "m2_server_canary_observation.json",
        ),
        m2_server_canary_observation_output_dir=_as_optional_str(
            raw,
            "m2_server_canary_observation_output_dir",
            "m2_server_canary_observations",
        ),
        m2_server_canary_circuit_breaker_enabled=_optional_bool(
            raw,
            "m2_server_canary_circuit_breaker_enabled",
            False,
        ),
        m2_server_canary_circuit_breaker_state_path=_as_optional_str(
            raw,
            "m2_server_canary_circuit_breaker_state_path",
            "m2_server_canary_circuit_breaker.json",
        ),
        m2_server_canary_repeated_oom_threshold=_optional_positive_int(
            raw,
            "m2_server_canary_repeated_oom_threshold",
            3,
        ),
        m2_server_canary_identical_failure_threshold=_optional_positive_int(
            raw,
            "m2_server_canary_identical_failure_threshold",
            3,
        ),
        auto_enable_ai_fallback=_optional_bool(raw, "auto_enable_ai_fallback", True),
        auto_mikan_parallel_with_ai=_optional_bool(raw, "auto_mikan_parallel_with_ai", False),
        auto_ai_run_before_mikan=_optional_bool(raw, "auto_ai_run_before_mikan", False),
        auto_ai_max_videos_per_cycle=_optional_non_negative_int(raw, "auto_ai_max_videos_per_cycle", 1),
        auto_ai_drain_queue_between_cycles=_optional_bool(raw, "auto_ai_drain_queue_between_cycles", True),
        auto_ai_failure_cooldown_seconds=_optional_non_negative_int(
            raw,
            "auto_ai_failure_cooldown_seconds",
            86400,
        ),
        auto_ai_max_attempts=_optional_non_negative_int(raw, "auto_ai_max_attempts", 3),
        auto_ai_asr_review_cooldown_seconds=_optional_non_negative_int(
            raw,
            "auto_ai_asr_review_cooldown_seconds",
            900,
        ),
        auto_ai_failed_retry_sweep_enabled=_optional_bool(
            raw,
            "auto_ai_failed_retry_sweep_enabled",
            False,
        ),
        auto_ai_failed_retry_sweep_interval_seconds=_optional_positive_int(
            raw,
            "auto_ai_failed_retry_sweep_interval_seconds",
            300,
        ),
        auto_ai_failed_retry_sweep_max_items=_optional_positive_int(
            raw,
            "auto_ai_failed_retry_sweep_max_items",
            1,
        ),
        auto_ai_quality_review_autopilot_enabled=_optional_bool(
            raw,
            "auto_ai_quality_review_autopilot_enabled",
            False,
        ),
        auto_ai_quality_review_autopilot_interval_seconds=_optional_positive_int(
            raw,
            "auto_ai_quality_review_autopilot_interval_seconds",
            900,
        ),
        auto_target_review_autopilot_enabled=_optional_bool(
            raw,
            "auto_target_review_autopilot_enabled",
            False,
        ),
        auto_target_review_autopilot_interval_seconds=_optional_positive_int(
            raw,
            "auto_target_review_autopilot_interval_seconds",
            300,
        ),
        ai_queue_running_stale_seconds=_optional_non_negative_int(
            raw,
            "ai_queue_running_stale_seconds",
            21600,
        ),
        ai_queue_stage_stale_seconds=_optional_non_negative_int(
            raw,
            "ai_queue_stage_stale_seconds",
            900,
        ),
        ai_process_isolation_enabled=_optional_bool(raw, "ai_process_isolation_enabled", True),
        ai_subprocess_timeout_seconds=_optional_non_negative_int(
            raw,
            "ai_subprocess_timeout_seconds",
            14400,
        ),
        resource_admission_enabled=_optional_bool(raw, "resource_admission_enabled", False),
        resource_admission_state_path=_as_optional_str(
            raw,
            "resource_admission_state_path",
            "resource_admission_state.json",
        ),
        resource_admission_telemetry_stale_seconds=_optional_positive_float(
            raw, "resource_admission_telemetry_stale_seconds", 15.0
        ),
        resource_admission_cpu_yellow_percent=_optional_positive_float(
            raw, "resource_admission_cpu_yellow_percent", 80.0
        ),
        resource_admission_cpu_red_percent=_optional_positive_float(
            raw, "resource_admission_cpu_red_percent", 95.0
        ),
        resource_admission_ram_yellow_available_ratio=_optional_positive_float(
            raw, "resource_admission_ram_yellow_available_ratio", 0.20
        ),
        resource_admission_ram_red_available_ratio=_optional_positive_float(
            raw, "resource_admission_ram_red_available_ratio", 0.08
        ),
        resource_admission_gpu_yellow_percent=_optional_positive_float(
            raw, "resource_admission_gpu_yellow_percent", 85.0
        ),
        resource_admission_gpu_red_percent=_optional_positive_float(
            raw, "resource_admission_gpu_red_percent", 98.0
        ),
        resource_admission_vram_reserve_mib=_optional_non_negative_float(
            raw, "resource_admission_vram_reserve_mib", 2048.0
        ),
        resource_admission_primary_vram_mib=_optional_positive_float(
            raw, "resource_admission_primary_vram_mib", 8500.0
        ),
        resource_admission_lower_memory_vram_mib=_optional_positive_float(
            raw, "resource_admission_lower_memory_vram_mib", 6200.0
        ),
        resource_admission_recovery_samples=_optional_positive_int(
            raw, "resource_admission_recovery_samples", 3
        ),
        resource_admission_yellow_retry_seconds=_optional_non_negative_float(
            raw, "resource_admission_yellow_retry_seconds", 30.0
        ),
        resource_admission_red_retry_seconds=_optional_non_negative_float(
            raw, "resource_admission_red_retry_seconds", 120.0
        ),
        resource_admission_unavailable_retry_seconds=_optional_non_negative_float(
            raw, "resource_admission_unavailable_retry_seconds", 60.0
        ),
        resource_admission_recent_oom_cooldown_seconds=_optional_non_negative_int(
            raw, "resource_admission_recent_oom_cooldown_seconds", 21600
        ),
        resource_telemetry_gpu_timeout_seconds=_optional_positive_float(
            raw, "resource_telemetry_gpu_timeout_seconds", 2.0
        ),
        resource_telemetry_host_timeout_seconds=_optional_positive_float(
            raw, "resource_telemetry_host_timeout_seconds", 1.0
        ),
        resource_telemetry_cpu_sample_interval_seconds=_optional_positive_float(
            raw, "resource_telemetry_cpu_sample_interval_seconds", 0.10
        ),
        resource_gpu_lease_path=_as_optional_str(
            raw, "resource_gpu_lease_path", "gpu_leases/gpu-0.lock"
        ),
        resource_gpu_lease_wait_seconds=_optional_positive_float(
            raw, "resource_gpu_lease_wait_seconds", 7200.0
        ),
        completed_delivery_enabled=_optional_bool(raw, "completed_delivery_enabled", False),
        completed_delivery_path=_optional_allow_empty_str(raw, "completed_delivery_path", ""),
        completed_delivery_manifest_path=_as_optional_str(
            raw, "completed_delivery_manifest_path", "completed_delivery_manifests"
        ),
        completed_delivery_source_policy=_as_optional_str(
            raw, "completed_delivery_source_policy", "retain"
        ),
        completed_delivery_timeout_seconds=_optional_positive_int(
            raw, "completed_delivery_timeout_seconds", 7200
        ),
        require_ai_subtitles=_optional_bool(raw, "require_ai_subtitles", False),
        scanner_cache_enabled=_optional_bool(raw, "scanner_cache_enabled", True),
        scanner_state_path=_as_optional_str(raw, "scanner_state_path", "scanner_state.sqlite3"),
        scanner_recent_first=_optional_bool(raw, "scanner_recent_first", True),
        scanner_queue_enabled=_optional_bool(raw, "scanner_queue_enabled", True),
        pipeline_job_store_required=_optional_bool(
            raw,
            "pipeline_job_store_required",
            True,
        ),
        source_analyzer_enabled=_optional_bool(raw, "source_analyzer_enabled", False),
        source_analyzer_high_confidence=_optional_probability(
            raw,
            "source_analyzer_high_confidence",
            0.90,
        ),
        source_analyzer_low_confidence=_optional_probability(
            raw,
            "source_analyzer_low_confidence",
            0.60,
        ),
        source_analyzer_min_dialogue_completeness_score=_optional_probability(
            raw,
            "source_analyzer_min_dialogue_completeness_score",
            0.68,
        ),
        source_analyzer_min_subtitle_coverage_ratio=_optional_probability(
            raw,
            "source_analyzer_min_subtitle_coverage_ratio",
            0.60,
        ),
        source_analyzer_tie_margin=_optional_probability(
            raw,
            "source_analyzer_tie_margin",
            0.025,
        ),
        source_analyzer_version=_as_optional_str(
            raw,
            "source_analyzer_version",
            DEFAULT_SOURCE_ANALYZER_VERSION,
        ),
        source_decision_schema_version=_optional_positive_int(
            raw,
            "source_decision_schema_version",
            DEFAULT_SOURCE_DECISION_SCHEMA_VERSION,
        ),
        source_decision_version=_as_optional_str(
            raw,
            "source_decision_version",
            DEFAULT_SOURCE_DECISION_VERSION,
        ),
        source_integrity_sha256_enabled=_optional_bool(
            raw,
            "source_integrity_sha256_enabled",
            False,
        ),
        ai_canary_once_enabled=_optional_bool(raw, "ai_canary_once_enabled", False),
        acceptance_queue_lane_enabled=_optional_bool(
            raw,
            "acceptance_queue_lane_enabled",
            False,
        ),
        acceptance_queue_lane_plan_path=_as_optional_str(
            raw,
            "acceptance_queue_lane_plan_path",
            "acceptance/plan.json",
        ),
        acceptance_fault_execution_enabled=_optional_bool(
            raw,
            "acceptance_fault_execution_enabled",
            False,
        ),
        acceptance_fault_execution_run_id=_optional_allow_empty_str(
            raw,
            "acceptance_fault_execution_run_id",
            "",
        ),
        acceptance_fault_execution_plan_sha256=_optional_allow_empty_str(
            raw,
            "acceptance_fault_execution_plan_sha256",
            "",
        ),
        scanner_event_watch_enabled=_optional_bool(raw, "scanner_event_watch_enabled", False),
        scanner_event_stability_interval_seconds=_optional_positive_float(
            raw,
            "scanner_event_stability_interval_seconds",
            2.0,
        ),
        scanner_event_quiet_window_seconds=_optional_positive_float(
            raw,
            "scanner_event_quiet_window_seconds",
            5.0,
        ),
        scanner_event_stable_observations_required=_optional_positive_int(
            raw,
            "scanner_event_stable_observations_required",
            2,
        ),
        scanner_event_watch_health_interval_seconds=_optional_positive_float(
            raw,
            "scanner_event_watch_health_interval_seconds",
            30.0,
        ),
        scanner_event_ffprobe_path=_as_optional_str(
            raw,
            "scanner_event_ffprobe_path",
            "ffprobe",
        ),
        scanner_event_media_probe_timeout_seconds=_optional_positive_float(
            raw,
            "scanner_event_media_probe_timeout_seconds",
            30.0,
        ),
        scanner_event_media_probe_min_throughput_mib_per_second=_optional_positive_float(
            raw, "scanner_event_media_probe_min_throughput_mib_per_second", 8.0
        ),
        scanner_event_media_probe_max_timeout_seconds=_optional_positive_float(
            raw, "scanner_event_media_probe_max_timeout_seconds", 1800.0
        ),
        scanner_event_media_probe_max_attempts=_optional_positive_int(
            raw, "scanner_event_media_probe_max_attempts", 4
        ),
        scanner_event_media_probe_max_retry_seconds=_optional_positive_float(
            raw, "scanner_event_media_probe_max_retry_seconds", 900.0
        ),
        scanner_skip_standalone_op_ed=_optional_bool(raw, "scanner_skip_standalone_op_ed", True),
        scanner_candidate_min_age_seconds=_optional_non_negative_int(
            raw,
            "scanner_candidate_min_age_seconds",
            0,
        ),
        scanner_incremental_scan_enabled=_optional_bool(raw, "scanner_incremental_scan_enabled", False),
        scanner_incremental_overlap_seconds=_optional_non_negative_int(
            raw,
            "scanner_incremental_overlap_seconds",
            300,
        ),
        scanner_quick_scan_recent_days=_optional_non_negative_int(raw, "scanner_quick_scan_recent_days", 0),
        scanner_full_scan_interval_seconds=_optional_non_negative_int(
            raw,
            "scanner_full_scan_interval_seconds",
            0,
        ),
        scanner_background_scan_interval_seconds=_optional_positive_int(
            raw,
            "scanner_background_scan_interval_seconds",
            21600,
        ),
        scanner_fallback_scan_interval_seconds=_optional_positive_int(
            raw,
            "scanner_fallback_scan_interval_seconds",
            21600,
        ),
        scanner_background_scan_startup_delay_seconds=_optional_non_negative_int(
            raw,
            "scanner_background_scan_startup_delay_seconds",
            600,
        ),
        scanner_reconcile_batch_size=_optional_positive_int(raw, "scanner_reconcile_batch_size", 1000),
        scanner_reconcile_budget_seconds=_optional_positive_int(raw, "scanner_reconcile_budget_seconds", 60),
        scanner_reconcile_batch_interval_seconds=_optional_positive_int(
            raw,
            "scanner_reconcile_batch_interval_seconds",
            60,
        ),
        scanner_active_queue_ledger_backfill_enabled=_optional_bool(
            raw,
            "scanner_active_queue_ledger_backfill_enabled",
            True,
        ),
        scanner_active_queue_ledger_backfill_interval_seconds=_optional_positive_int(
            raw,
            "scanner_active_queue_ledger_backfill_interval_seconds",
            10,
        ),
        scanner_active_queue_ledger_backfill_batch_size=_optional_positive_int(
            raw,
            "scanner_active_queue_ledger_backfill_batch_size",
            250,
        ),
        scanner_active_queue_ledger_backfill_no_progress_seconds=_optional_positive_int(
            raw,
            "scanner_active_queue_ledger_backfill_no_progress_seconds",
            300,
        ),
        scanner_inventory_file_timeout_seconds=_optional_positive_int(
            raw,
            "scanner_inventory_file_timeout_seconds",
            30,
        ),
        scanner_walk_yield_every_entries=_optional_positive_int(
            raw,
            "scanner_walk_yield_every_entries",
            256,
        ),
        scanner_walk_yield_seconds=_optional_non_negative_float(
            raw,
            "scanner_walk_yield_seconds",
            0.025,
        ),
        scanner_queue_oldest_every_n_cycles=_optional_non_negative_int(
            raw,
            "scanner_queue_oldest_every_n_cycles",
            12,
        ),
        control_state_path=_as_optional_str(raw, "control_state_path", "control_state.sqlite3"),
        control_inbox_path=_as_optional_str(raw, "control_inbox_path", "control_inbox"),
        ai_output_manifest_path=_as_optional_str(raw, "ai_output_manifest_path", "ai_output_manifests"),
        ai_output_versions_keep=_optional_positive_int(raw, "ai_output_versions_keep", 3),
        official_subtitle_versions_keep=_optional_positive_int(raw, "official_subtitle_versions_keep", 3),
        ai_stage_event_retention_days=_optional_positive_int(raw, "ai_stage_event_retention_days", 30),
        ai_stage_event_max_rows=_optional_positive_int(raw, "ai_stage_event_max_rows", 25000),
        opencc_config=_as_str(raw, "opencc_config"),
        keep_intermediate_files=_as_bool(raw, "keep_intermediate_files"),
        cleanup_backup_files=_optional_bool(raw, "cleanup_backup_files", False),
        backup_retention_count=int(raw.get("backup_retention_count", 0)),
        state_backup_path=_as_optional_str(raw, "state_backup_path", "state_backups"),
        state_backup_retention_count=_optional_positive_int(raw, "state_backup_retention_count", 14),
        state_backup_enabled=_optional_bool(raw, "state_backup_enabled", True),
        state_backup_interval_hours=_optional_positive_int(raw, "state_backup_interval_hours", 24),
        state_backup_startup_delay_seconds=_optional_non_negative_int(
            raw,
            "state_backup_startup_delay_seconds",
            120,
        ),
        database_maintenance_enabled=_optional_bool(raw, "database_maintenance_enabled", True),
        database_maintenance_interval_hours=_optional_positive_int(
            raw,
            "database_maintenance_interval_hours",
            168,
        ),
        database_maintenance_startup_delay_seconds=_optional_non_negative_int(
            raw,
            "database_maintenance_startup_delay_seconds",
            1800,
        ),
        database_maintenance_min_reclaim_mib=_optional_non_negative_float(
            raw,
            "database_maintenance_min_reclaim_mib",
            64.0,
        ),
        database_maintenance_min_freelist_ratio=_optional_non_negative_float(
            raw,
            "database_maintenance_min_freelist_ratio",
            0.25,
        ),
        transcription_backend=_as_optional_str(raw, "transcription_backend", "faster-whisper"),
        japanese_transcription_backend=_optional_str(raw, "japanese_transcription_backend"),
        japanese_transcription_fallback_backend=_optional_str(raw, "japanese_transcription_fallback_backend"),
        japanese_transcription_final_fallback_backend=_optional_str(
            raw,
            "japanese_transcription_final_fallback_backend",
        ),
        non_japanese_transcription_backend=_optional_str(raw, "non_japanese_transcription_backend"),
        non_japanese_transcription_fallback_backend=_optional_str(
            raw,
            "non_japanese_transcription_fallback_backend",
        ),
        non_japanese_transcription_final_fallback_backend=_optional_str(
            raw,
            "non_japanese_transcription_final_fallback_backend",
        ),
        max_subtitle_duration_seconds=float(raw.get("max_subtitle_duration_seconds", 7.0)),
        whisper_initial_prompt=_optional_str(raw, "whisper_initial_prompt"),
        whisper_beam_size=int(raw.get("whisper_beam_size", 5)),
        whisper_best_of=int(raw.get("whisper_best_of", 5)),
        whisper_patience=float(raw.get("whisper_patience", 1.0)),
        whisper_length_penalty=float(raw.get("whisper_length_penalty", 1.0)),
        whisper_repetition_penalty=float(raw.get("whisper_repetition_penalty", 1.0)),
        whisper_no_repeat_ngram_size=int(raw.get("whisper_no_repeat_ngram_size", 0)),
        whisper_word_timestamps=_optional_bool(raw, "whisper_word_timestamps", True),
        whisper_no_speech_threshold=_optional_float(raw, "whisper_no_speech_threshold", 0.6),
        whisper_log_prob_threshold=_optional_float(raw, "whisper_log_prob_threshold", -1.0),
        whisper_compression_ratio_threshold=_optional_float(raw, "whisper_compression_ratio_threshold", 2.4),
        whisper_hallucination_silence_threshold=_optional_float(
            raw,
            "whisper_hallucination_silence_threshold",
            None,
        ),
        whisper_model_cache_enabled=_optional_bool(raw, "whisper_model_cache_enabled", True),
        whisper_vad_threshold=float(raw.get("whisper_vad_threshold", 0.5)),
        whisper_vad_min_silence_duration_ms=int(raw.get("whisper_vad_min_silence_duration_ms", 2000)),
        whisper_vad_speech_pad_ms=int(raw.get("whisper_vad_speech_pad_ms", 400)),
        whisper_hallucination_phrases=_optional_str_list(raw, "whisper_hallucination_phrases"),
        filter_repeated_vocalizations=_optional_bool(raw, "filter_repeated_vocalizations", True),
        repeated_vocalization_min_chars=int(raw.get("repeated_vocalization_min_chars", 6)),
        transcription_quality_check_enabled=_optional_bool(raw, "transcription_quality_check_enabled", True),
        transcription_quality_min_audio_seconds=float(raw.get("transcription_quality_min_audio_seconds", 600.0)),
        transcription_quality_min_coverage_percent=float(raw.get("transcription_quality_min_coverage_percent", 8.0)),
        transcription_quality_min_blocks_per_minute=float(raw.get("transcription_quality_min_blocks_per_minute", 1.5)),
        transcription_quality_min_avg_logprob=float(raw.get("transcription_quality_min_avg_logprob", -1.0)),
        transcription_quality_max_low_confidence_percent=float(
            raw.get("transcription_quality_max_low_confidence_percent", 25.0)
        ),
        transcription_quality_min_confidence_segments=_optional_positive_int(
            raw,
            "transcription_quality_min_confidence_segments",
            8,
        ),
        transcription_quality_max_leading_gap_seconds=float(
            raw.get("transcription_quality_max_leading_gap_seconds", 30.0)
        ),
        asr_diagnostics_enabled=_optional_bool(raw, "asr_diagnostics_enabled", True),
        asr_diagnostics_path=_as_optional_str(raw, "asr_diagnostics_path", "asr_diagnostics"),
        asr_selective_retry_enabled=_optional_bool(raw, "asr_selective_retry_enabled", True),
        asr_selective_retry_padding_seconds=_optional_non_negative_float(
            raw,
            "asr_selective_retry_padding_seconds",
            1.5,
        ),
        asr_selective_retry_merge_gap_seconds=_optional_non_negative_float(
            raw,
            "asr_selective_retry_merge_gap_seconds",
            3.0,
        ),
        asr_optional_rescue_rejection_is_fatal=_optional_bool(
            raw,
            "asr_optional_rescue_rejection_is_fatal",
            True,
        ),
        asr_prompt_free_allow_recovered_primary_artifacts=_optional_bool(
            raw,
            "asr_prompt_free_allow_recovered_primary_artifacts",
            False,
        ),
        subtitle_quality_check_enabled=_optional_bool(raw, "subtitle_quality_check_enabled", True),
        subtitle_quality_fail_job=_optional_bool(raw, "subtitle_quality_fail_job", True),
        subtitle_quality_max_duration_seconds=float(raw.get("subtitle_quality_max_duration_seconds", 5.5)),
        subtitle_quality_max_primary_chars=int(raw.get("subtitle_quality_max_primary_chars", 42)),
        subtitle_quality_hard_max_primary_chars=int(raw.get("subtitle_quality_hard_max_primary_chars", 64)),
        subtitle_quality_max_gap_seconds=float(raw.get("subtitle_quality_max_gap_seconds", 45.0)),
        subtitle_quality_max_leading_gap_seconds=float(
            raw.get("subtitle_quality_max_leading_gap_seconds", 30.0)
        ),
        subtitle_quality_warn_cps=float(raw.get("subtitle_quality_warn_cps", 17.0)),
        subtitle_quality_fail_cps=float(raw.get("subtitle_quality_fail_cps", 25.0)),
        subtitle_quality_min_duration_seconds=float(
            raw.get("subtitle_quality_min_duration_seconds", 0.35)
        ),
        subtitle_quality_hard_min_duration_seconds=float(
            raw.get("subtitle_quality_hard_min_duration_seconds", 0.12)
        ),
        subtitle_quality_max_overlap_seconds=float(
            raw.get("subtitle_quality_max_overlap_seconds", 0.10)
        ),
        subtitle_remediation_punctuation_repeat_limit=_optional_positive_int(
            raw,
            "subtitle_remediation_punctuation_repeat_limit",
            3,
        ),
        subtitle_remediation_wrap_max_chars=_optional_positive_int(
            raw,
            "subtitle_remediation_wrap_max_chars",
            int(raw.get("subtitle_quality_max_primary_chars", 42)),
        ),
        subtitle_remediation_max_visual_lines=_optional_positive_int(
            raw,
            "subtitle_remediation_max_visual_lines",
            2,
        ),
        subtitle_remediation_max_timing_shift_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_max_timing_shift_seconds",
            0.20,
        ),
        subtitle_remediation_max_total_timing_shift_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_max_total_timing_shift_seconds",
            0.50,
        ),
        subtitle_remediation_max_overlap_repair_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_max_overlap_repair_seconds",
            0.20,
        ),
        subtitle_remediation_aligned_max_timing_shift_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_aligned_max_timing_shift_seconds",
            2.0,
        ),
        subtitle_remediation_aligned_max_total_timing_shift_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_aligned_max_total_timing_shift_seconds",
            3.0,
        ),
        subtitle_remediation_aligned_max_overlap_repair_seconds=_optional_positive_float(
            raw,
            "subtitle_remediation_aligned_max_overlap_repair_seconds",
            2.0,
        ),
        translation_glossary=_optional_str_mapping(raw, "translation_glossary"),
        translation_memory_enabled=_optional_bool(
            raw,
            "translation_memory_enabled",
            True,
        ),
        translation_memory_auto_apply_enabled=_optional_bool(
            raw,
            "translation_memory_auto_apply_enabled",
            True,
        ),
        translation_memory_path=_as_optional_str(
            raw,
            "translation_memory_path",
            "translation_memory.sqlite3",
        ),
        translation_memory_outbox_path=_as_optional_str(
            raw,
            "translation_memory_outbox_path",
            "translation_memory_outbox",
        ),
        translation_context_enabled=_optional_bool(raw, "translation_context_enabled", False),
        translation_context_max_blocks=_optional_positive_int(raw, "translation_context_max_blocks", 120),
        translation_context_max_chars=_optional_positive_int(raw, "translation_context_max_chars", 6000),
        translation_context_max_output_chars=_optional_positive_int(raw, "translation_context_max_output_chars", 2000),
        language_gate_enabled=_optional_bool(raw, "language_gate_enabled", False),
        allowed_source_languages=_optional_str_list_with_default(raw, "allowed_source_languages", ["ja"]),
        skip_non_allowed_language=_optional_bool(raw, "skip_non_allowed_language", True),
        transcribe_non_allowed_languages=_optional_bool(raw, "transcribe_non_allowed_languages", False),
        translate_non_japanese_sources=_optional_bool(
            raw,
            "translate_non_japanese_sources",
            True,
        ),
        language_detect_model=_optional_str(raw, "language_detect_model"),
        japanese_transcription_model=_optional_str(raw, "japanese_transcription_model"),
        japanese_transcription_fallback_model=_optional_str(raw, "japanese_transcription_fallback_model"),
        japanese_transcription_fallback_compute_type=_optional_str(
            raw,
            "japanese_transcription_fallback_compute_type",
        ),
        japanese_transcription_final_fallback_model=_optional_str(
            raw,
            "japanese_transcription_final_fallback_model",
        ),
        japanese_transcription_final_fallback_compute_type=_optional_str(
            raw,
            "japanese_transcription_final_fallback_compute_type",
        ),
        non_japanese_transcription_model=_optional_str(raw, "non_japanese_transcription_model"),
        non_japanese_transcription_fallback_model=_optional_str(
            raw,
            "non_japanese_transcription_fallback_model",
        ),
        non_japanese_transcription_fallback_compute_type=_optional_str(
            raw,
            "non_japanese_transcription_fallback_compute_type",
        ),
        non_japanese_transcription_final_fallback_model=_optional_str(
            raw,
            "non_japanese_transcription_final_fallback_model",
        ),
        non_japanese_transcription_final_fallback_compute_type=_optional_str(
            raw,
            "non_japanese_transcription_final_fallback_compute_type",
        ),
        ai_source_transcript_ass_suffix_template=_as_optional_str(
            raw,
            "ai_source_transcript_ass_suffix_template",
            ".AI{label}.{language}.ass",
        ),
        language_detect_min_probability=float(raw.get("language_detect_min_probability", 0.70)),
        language_uncertain_policy=_as_optional_str(raw, "language_uncertain_policy", "skip").strip().lower(),
        language_detect_sample_count=_optional_positive_int(raw, "language_detect_sample_count", 3),
        language_detect_sample_seconds=_optional_positive_int(raw, "language_detect_sample_seconds", 30),
        language_detect_cache_enabled=_optional_bool(raw, "language_detect_cache_enabled", True),
        language_detect_cache_path=_as_optional_str(
            raw,
            "language_detect_cache_path",
            "language_detection_cache.json",
        ),
        audio_content_probe_enabled=_optional_bool(raw, "audio_content_probe_enabled", True),
        audio_content_probe_max_streams=_optional_positive_int(raw, "audio_content_probe_max_streams", 8),
        audio_content_probe_sample_count=_optional_positive_int(raw, "audio_content_probe_sample_count", 3),
        audio_content_probe_sample_seconds=_optional_positive_int(raw, "audio_content_probe_sample_seconds", 12),
        audio_selection_manifest_path=_as_optional_str(raw, "audio_selection_manifest_path", "audio_selection"),
        processing_provenance_path=_as_optional_str(raw, "processing_provenance_path", "provenance"),
        force_ai_bypass_language_gate=_optional_bool(raw, "force_ai_bypass_language_gate", False),
        translation_metadata_context_enabled=_optional_bool(raw, "translation_metadata_context_enabled", False),
        metadata_context_providers=_optional_str_list_with_default(raw, "metadata_context_providers", ["anilist"]),
        metadata_context_cache_path=_as_optional_str(raw, "metadata_context_cache_path", "metadata_context_cache.json"),
        metadata_context_ttl_days=_optional_positive_int(raw, "metadata_context_ttl_days", 30),
        metadata_context_max_chars=_optional_positive_int(raw, "metadata_context_max_chars", 2000),
        metadata_context_timeout_seconds=_optional_positive_int(raw, "metadata_context_timeout_seconds", 15),
        metadata_context_include_spoilers=_optional_bool(raw, "metadata_context_include_spoilers", False),
        series_metadata_db_path=_as_optional_str(raw, "series_metadata_db_path", "series_metadata.sqlite3"),
        series_metadata_match_min_confidence=float(raw.get("series_metadata_match_min_confidence", 0.65)),
        series_metadata_auto_seed_terms=_optional_bool(raw, "series_metadata_auto_seed_terms", True),
        series_metadata_sync_enabled=_optional_bool(raw, "series_metadata_sync_enabled", True),
        series_metadata_sync_interval_seconds=_optional_positive_int(
            raw,
            "series_metadata_sync_interval_seconds",
            21600,
        ),
        series_metadata_sync_startup_delay_seconds=_optional_non_negative_int(
            raw,
            "series_metadata_sync_startup_delay_seconds",
            30,
        ),
        series_metadata_enrich_enabled=_optional_bool(raw, "series_metadata_enrich_enabled", True),
        series_metadata_enrich_per_cycle=_optional_non_negative_int(
            raw,
            "series_metadata_enrich_per_cycle",
            5,
        ),
        series_metadata_enrich_delay_seconds=_optional_non_negative_float(
            raw,
            "series_metadata_enrich_delay_seconds",
            1.0,
        ),
        series_artwork_cache_enabled=_optional_bool(raw, "series_artwork_cache_enabled", True),
        series_artwork_cache_path=_as_optional_str(raw, "series_artwork_cache_path", "series_artwork"),
        series_artwork_cache_max_mib=_optional_positive_int(raw, "series_artwork_cache_max_mib", 512),
        series_artwork_max_bytes=_optional_positive_int(raw, "series_artwork_max_bytes", 3 * 1024 * 1024),
        series_artwork_ttl_days=_optional_positive_int(raw, "series_artwork_ttl_days", 30),
        notification_webhook_url=_optional_allow_empty_str(raw, "notification_webhook_url", ""),
        notification_events=_optional_str_list_with_default(
            raw,
            "notification_events",
            ["asr_review", "ai_failure", "extract_terminal_failure"],
        ),
        notification_min_interval_seconds=_optional_non_negative_int(
            raw,
            "notification_min_interval_seconds",
            300,
        ),
        notification_state_path=_as_optional_str(raw, "notification_state_path", "notification_state.json"),
        storage_io_pressure_enabled=_optional_bool(raw, "storage_io_pressure_enabled", True),
        storage_io_pressure_some_avg10_threshold=_optional_non_negative_float(
            raw,
            "storage_io_pressure_some_avg10_threshold",
            35.0,
        ),
        storage_io_pressure_full_avg10_threshold=_optional_non_negative_float(
            raw,
            "storage_io_pressure_full_avg10_threshold",
            10.0,
        ),
        storage_io_pressure_backoff_seconds=_optional_non_negative_float(
            raw,
            "storage_io_pressure_backoff_seconds",
            2.0,
        ),
        subtitle_timing_mode=_as_optional_str(raw, "subtitle_timing_mode", "word"),
        subtitle_max_duration_seconds=float(raw.get("subtitle_max_duration_seconds", 4.8)),
        subtitle_min_duration_seconds=float(raw.get("subtitle_min_duration_seconds", 0.8)),
        subtitle_max_chars=int(raw.get("subtitle_max_chars", 24)),
        subtitle_end_padding_seconds=float(raw.get("subtitle_end_padding_seconds", 0.12)),
        subtitle_min_gap_seconds=float(raw.get("subtitle_min_gap_seconds", 0.06)),
        write_gap_report=_optional_bool(raw, "write_gap_report", True),
        gap_report_threshold_seconds=float(raw.get("gap_report_threshold_seconds", 4.0)),
        enable_gap_rescue=_optional_bool(raw, "enable_gap_rescue", True),
        enable_leading_gap_rescue=_optional_bool(raw, "enable_leading_gap_rescue", True),
        gap_rescue_threshold_seconds=float(raw.get("gap_rescue_threshold_seconds", 4.0)),
        gap_rescue_leading_threshold_seconds=float(
            raw.get("gap_rescue_leading_threshold_seconds", 1.5)
        ),
        gap_rescue_max_gap_seconds=float(raw.get("gap_rescue_max_gap_seconds", 45.0)),
        gap_rescue_leading_max_seconds=float(raw.get("gap_rescue_leading_max_seconds", 120.0)),
        gap_rescue_padding_seconds=float(raw.get("gap_rescue_padding_seconds", 0.8)),
        gap_rescue_clip_seconds=float(raw.get("gap_rescue_clip_seconds", 30.0)),
        gap_rescue_clip_overlap_seconds=float(raw.get("gap_rescue_clip_overlap_seconds", 2.0)),
        gap_rescue_max_gaps=int(raw.get("gap_rescue_max_gaps", 12)),
        gap_rescue_min_chars=int(raw.get("gap_rescue_min_chars", 2)),
        gap_rescue_no_speech_threshold=float(raw.get("gap_rescue_no_speech_threshold", 0.95)),
        gap_rescue_log_prob_threshold=float(raw.get("gap_rescue_log_prob_threshold", -1.5)),
        gap_rescue_compression_ratio_threshold=float(raw.get("gap_rescue_compression_ratio_threshold", 2.4)),
        gap_rescue_accept_min_avg_logprob=float(
            raw.get("gap_rescue_accept_min_avg_logprob", -1.15)
        ),
        gap_rescue_accept_max_no_speech_prob=float(
            raw.get("gap_rescue_accept_max_no_speech_prob", 0.90)
        ),
        gap_rescue_accept_max_compression_ratio=float(
            raw.get("gap_rescue_accept_max_compression_ratio", 2.4)
        ),
        op_ed_transcription_enabled=_optional_bool(raw, "op_ed_transcription_enabled", True),
        op_ed_min_audio_seconds=float(raw.get("op_ed_min_audio_seconds", 600.0)),
        op_ed_opening_window_seconds=float(raw.get("op_ed_opening_window_seconds", 360.0)),
        op_ed_ending_window_seconds=float(raw.get("op_ed_ending_window_seconds", 300.0)),
        op_ed_gap_threshold_seconds=float(raw.get("op_ed_gap_threshold_seconds", 6.0)),
        op_ed_max_gap_seconds=float(raw.get("op_ed_max_gap_seconds", 210.0)),
        op_ed_padding_seconds=float(raw.get("op_ed_padding_seconds", 1.0)),
        op_ed_max_rescue_ranges=int(raw.get("op_ed_max_rescue_ranges", 6)),
        op_ed_no_speech_threshold=float(raw.get("op_ed_no_speech_threshold", 0.95)),
        op_ed_log_prob_threshold=float(raw.get("op_ed_log_prob_threshold", -1.5)),
        op_ed_compression_ratio_threshold=float(raw.get("op_ed_compression_ratio_threshold", 3.0)),
        op_ed_accept_min_avg_logprob=float(raw.get("op_ed_accept_min_avg_logprob", -1.15)),
        op_ed_accept_max_no_speech_prob=float(
            raw.get("op_ed_accept_max_no_speech_prob", 0.90)
        ),
        op_ed_accept_max_compression_ratio=float(
            raw.get("op_ed_accept_max_compression_ratio", 2.4)
        ),
        op_ed_initial_prompt=_optional_str(raw, "op_ed_initial_prompt"),
        export_ai_ass=_optional_bool(raw, "export_ai_ass", True),
        ai_japanese_ass_suffix=_as_optional_str(raw, "ai_japanese_ass_suffix", ".AI日本語.ja.ass"),
        ai_simplified_chinese_ass_suffix=_as_optional_str(
            raw,
            "ai_simplified_chinese_ass_suffix",
            ".AI简日双语.zh-CN.ass",
        ),
        ai_traditional_chinese_ass_suffix=_as_optional_str(
            raw,
            "ai_traditional_chinese_ass_suffix",
            ".AI繁日雙語.zh-TW.ass",
        ),
        finished_subtitle_suffixes=_optional_suffix_list(
            raw,
            "finished_subtitle_suffixes",
            [
                ".AI繁日雙語.zh-TW.ass",
                ".AI简日双语.zh-CN.ass",
                ".AI繁體中文.zh-TW.ass",
                ".AI简体中文.zh-CN.ass",
                ".繁體中文.zh-TW.ass",
                ".简体中文.zh-CN.ass",
                ".zh-TW.ass",
                ".zh-CN.ass",
            ],
        ),
        ass_play_res_x=_optional_positive_int(raw, "ass_play_res_x", 1920),
        ass_play_res_y=_optional_positive_int(raw, "ass_play_res_y", 1080),
        ass_font_name=_as_optional_str(raw, "ass_font_name", "Noto Sans CJK TC"),
        ass_primary_font_size=_optional_positive_int(raw, "ass_primary_font_size", 58),
        ass_secondary_font_size=_optional_positive_int(raw, "ass_secondary_font_size", 32),
        ass_primary_color=_as_optional_str(raw, "ass_primary_color", "&H00FFFFFF"),
        ass_secondary_color=_as_optional_str(raw, "ass_secondary_color", "&HE6E6E6&"),
        ass_outline_color=_as_optional_str(raw, "ass_outline_color", "&H00000000"),
        ass_back_color=_as_optional_str(raw, "ass_back_color", "&H80000000"),
        ass_secondary_alpha=_as_optional_str(raw, "ass_secondary_alpha", "&H18&"),
        ass_primary_outline=_optional_non_negative_float(raw, "ass_primary_outline", 2.2),
        ass_secondary_outline=_optional_non_negative_float(raw, "ass_secondary_outline", 1.4),
        ass_shadow=_optional_non_negative_float(raw, "ass_shadow", 0.0),
        ass_margin_l=_optional_non_negative_int(raw, "ass_margin_l", 40),
        ass_margin_r=_optional_non_negative_int(raw, "ass_margin_r", 40),
        ass_margin_v=_optional_non_negative_int(raw, "ass_margin_v", 70),
        ass_style_versioning_enabled=_optional_bool(raw, "ass_style_versioning_enabled", True),
        safety_check_enabled=_optional_bool(raw, "safety_check_enabled", True),
        disk_min_free_gb=_optional_non_negative_float(raw, "disk_min_free_gb", 2.0),
        mikan_enabled=_optional_bool(raw, "mikan_enabled", False),
        mikan_base_url=_as_optional_str(raw, "mikan_base_url", "https://mikanani.me"),
        mikan_bangumi_ids=_optional_int_list(raw, "mikan_bangumi_ids"),
        mikan_auto_match_enabled=_optional_bool(raw, "mikan_auto_match_enabled", True),
        mikan_auto_match_threshold=_optional_float(raw, "mikan_auto_match_threshold", 0.86) or 0.86,
        mikan_auto_match_cache_path=_as_optional_str(
            raw,
            "mikan_auto_match_cache_path",
            "mikan_auto_matches.json",
        ),
        mikan_auto_match_max_candidates=_optional_positive_int(raw, "mikan_auto_match_max_candidates", 6),
        mikan_auto_match_max_lookups_per_cycle=_optional_non_negative_int(
            raw,
            "mikan_auto_match_max_lookups_per_cycle",
            25,
        ),
        mikan_library_scan_recent_first=_optional_bool(raw, "mikan_library_scan_recent_first", True),
        mikan_library_scan_recent_series_per_cycle=_optional_non_negative_int(
            raw,
            "mikan_library_scan_recent_series_per_cycle",
            20,
        ),
        mikan_library_scan_max_series_per_cycle=_optional_non_negative_int(
            raw,
            "mikan_library_scan_max_series_per_cycle",
            80,
        ),
        mikan_episode_index_ttl_seconds=_optional_positive_int(raw, "mikan_episode_index_ttl_seconds", 21600),
        mikan_library_fallback_scan_interval_seconds=_optional_positive_int(
            raw,
            "mikan_library_fallback_scan_interval_seconds",
            3600,
        ),
        mikan_library_fallback_scan_max_series_per_cycle=_optional_positive_int(
            raw,
            "mikan_library_fallback_scan_max_series_per_cycle",
            8,
        ),
        mikan_seen_path=_as_optional_str(raw, "mikan_seen_path", "mikan_seen.json"),
        mikan_pending_path=_as_optional_str(raw, "mikan_pending_path", "mikan_pending.json"),
        mikan_sqlite_authoritative_state=_optional_bool(raw, "mikan_sqlite_authoritative_state", True),
        mikan_download_start_timeout_seconds=_optional_positive_int(
            raw,
            "mikan_download_start_timeout_seconds",
            180,
        ),
        mikan_download_metadata_timeout_seconds=_optional_positive_int(
            raw,
            "mikan_download_metadata_timeout_seconds",
            300,
        ),
        mikan_download_unhealthy_timeout_seconds=_optional_positive_int(
            raw,
            "mikan_download_unhealthy_timeout_seconds",
            300,
        ),
        mikan_download_stall_timeout_seconds=_optional_positive_int(
            raw,
            "mikan_download_stall_timeout_seconds",
            600,
        ),
        mikan_download_max_eta_seconds=_optional_positive_int(
            raw,
            "mikan_download_max_eta_seconds",
            86400,
        ),
        mikan_completed_reconcile_max_age_seconds=_optional_positive_int(
            raw,
            "mikan_completed_reconcile_max_age_seconds",
            21600,
        ),
        mikan_no_candidate_retry_seconds=_optional_positive_int(
            raw,
            "mikan_no_candidate_retry_seconds",
            600,
        ),
        mikan_no_candidate_retry_max_seconds=_optional_positive_int(
            raw,
            "mikan_no_candidate_retry_max_seconds",
            86400,
        ),
        mikan_delete_stalled_torrents=_optional_bool(raw, "mikan_delete_stalled_torrents", True),
        mikan_request_timeout_seconds=_optional_positive_int(raw, "mikan_request_timeout_seconds", 30),
        mikan_max_items_per_bangumi=_optional_positive_int(raw, "mikan_max_items_per_bangumi", 1),
        mikan_watch_interval_seconds=_optional_positive_int(raw, "mikan_watch_interval_seconds", 300),
        mikan_completed_poll_interval_seconds=_optional_positive_int(
            raw,
            "mikan_completed_poll_interval_seconds",
            30,
        ),
        mikan_active_poll_interval_seconds=_optional_positive_int(
            raw,
            "mikan_active_poll_interval_seconds",
            5,
        ),
        mikan_operation_lock_wait_seconds=_optional_non_negative_int(
            raw,
            "mikan_operation_lock_wait_seconds",
            300,
        ),
        mikan_extract_failed_retry_seconds=_optional_non_negative_int(
            raw,
            "mikan_extract_failed_retry_seconds",
            900,
        ),
        mikan_extract_workers=_optional_positive_int(raw, "mikan_extract_workers", 2),
        mikan_extract_workers_during_ai=_optional_positive_int(
            raw,
            "mikan_extract_workers_during_ai",
            1,
        ),
        mikan_extract_job_timeout_seconds=_optional_positive_int(raw, "mikan_extract_job_timeout_seconds", 900),
        mikan_extract_job_timeout_per_video_seconds=_optional_positive_int(
            raw,
            "mikan_extract_job_timeout_per_video_seconds",
            300,
        ),
        mikan_extract_job_timeout_max_seconds=_optional_positive_int(
            raw,
            "mikan_extract_job_timeout_max_seconds",
            14400,
        ),
        mikan_extract_cancel_grace_seconds=_optional_non_negative_int(
            raw,
            "mikan_extract_cancel_grace_seconds",
            15,
        ),
        mikan_extract_timeout_retry_seconds=_optional_non_negative_int(
            raw,
            "mikan_extract_timeout_retry_seconds",
            60,
        ),
        mikan_extract_lease_seconds=_optional_positive_int(raw, "mikan_extract_lease_seconds", 900),
        mikan_extract_completed=_optional_bool(raw, "mikan_extract_completed", True),
        mikan_remove_ai_after_extract=_optional_bool(raw, "mikan_remove_ai_after_extract", True),
        mikan_require_extractable_subtitle=_optional_bool(raw, "mikan_require_extractable_subtitle", True),
        subtitle_extract_timeout_seconds=_optional_positive_int(raw, "subtitle_extract_timeout_seconds", 300),
        mikan_fallback_sources_enabled=_optional_bool(raw, "mikan_fallback_sources_enabled", False),
        mikan_fallback_sources=_optional_str_list_with_default(
            raw,
            "mikan_fallback_sources",
            ["animegarden", "dmhy", "acgrip", "bangumimoe", "kisssub", "nyaa"],
        ),
        mikan_fallback_cache_path=_as_optional_str(
            raw,
            "mikan_fallback_cache_path",
            "mikan_fallback_sources.json",
        ),
        mikan_fallback_cache_ttl_seconds=_optional_positive_int(
            raw,
            "mikan_fallback_cache_ttl_seconds",
            21600,
        ),
        mikan_fallback_max_lookups_per_cycle=_optional_non_negative_int(
            raw,
            "mikan_fallback_max_lookups_per_cycle",
            6,
        ),
        mikan_fallback_source_timeout_seconds=_optional_positive_int(
            raw,
            "mikan_fallback_source_timeout_seconds",
            20,
        ),
        mikan_fallback_source_failure_threshold=_optional_positive_int(
            raw,
            "mikan_fallback_source_failure_threshold",
            2,
        ),
        mikan_fallback_source_cooldown_seconds=_optional_positive_int(
            raw,
            "mikan_fallback_source_cooldown_seconds",
            1800,
        ),
        mikan_fallback_min_nyaa_seeders=_optional_non_negative_int(
            raw,
            "mikan_fallback_min_nyaa_seeders",
            1,
        ),
        mikan_prefer_keywords=_optional_str_list_with_default(
            raw,
            "mikan_prefer_keywords",
            [
                "简繁日内封",
                "簡繁日內封",
                "简繁内封",
                "簡繁內封",
                "繁日双语",
                "繁日雙語",
                "繁日内封",
                "繁日內封",
                "繁体",
                "繁體",
                "CHT",
            ],
        ),
        mikan_reject_keywords=_optional_str_list(raw, "mikan_reject_keywords"),
        qbit_base_url=_as_optional_str(raw, "qbit_base_url", "http://localhost:8080"),
        qbit_username=_as_optional_str(raw, "qbit_username", "admin"),
        qbit_password=_as_optional_str(raw, "qbit_password", "adminadmin"),
        qbit_timeout_seconds=_optional_positive_int(raw, "qbit_timeout_seconds", 30),
        qbit_save_path=_optional_str(raw, "qbit_save_path"),
        qbit_category=_optional_str(raw, "qbit_category"),
        qbit_tags=_optional_str_list_with_default(raw, "qbit_tags", ["llm-sub"]),
        qbit_paused=_optional_bool(raw, "qbit_paused", False),
        qbit_path_mappings=_optional_path_mappings(raw, "qbit_path_mappings"),
        mikan_series_path_mappings=_optional_series_path_mappings(raw, "mikan_series_path_mappings"),
        mikan_processed_tags=_optional_str_list_with_default(
            raw,
            "mikan_processed_tags",
            ["llm-sub-extracted"],
        ),
        mikan_completed_tags=_optional_str_list_with_default(
            raw,
            "mikan_completed_tags",
            ["mikansub-completed"],
        ),
        whisperx_batch_size=int(raw.get("whisperx_batch_size", 8)),
        whisper_dynamic_batch_enabled=_optional_bool(raw, "whisper_dynamic_batch_enabled", True),
        whisper_gpu_memory_reserve_mib=_optional_positive_int(raw, "whisper_gpu_memory_reserve_mib", 2048),
        whisperx_align_model=_optional_str(raw, "whisperx_align_model"),
        whisperx_vad_method=_as_optional_str(raw, "whisperx_vad_method", "pyannote"),
        whisperx_vad_onset=float(raw.get("whisperx_vad_onset", 0.5)),
        whisperx_vad_offset=float(raw.get("whisperx_vad_offset", 0.363)),
        whisperx_chunk_size=int(raw.get("whisperx_chunk_size", 30)),
        vibevoice_model=_as_optional_str(raw, "vibevoice_model", "microsoft/VibeVoice-ASR"),
        vibevoice_device_map=_as_optional_str(raw, "vibevoice_device_map", "auto"),
        vibevoice_torch_dtype=_as_optional_str(raw, "vibevoice_torch_dtype", "auto"),
        vibevoice_trust_remote_code=_optional_bool(raw, "vibevoice_trust_remote_code", True),
        vibevoice_max_new_tokens=int(raw.get("vibevoice_max_new_tokens", 0)),
        vibevoice_tokenizer_chunk_size=int(raw.get("vibevoice_tokenizer_chunk_size", 0)),
        vibevoice_prompt=_optional_str(raw, "vibevoice_prompt"),
        transformers_whisper_chunk_length_s=_optional_positive_float(raw, "transformers_whisper_chunk_length_s", 30.0),
        transformers_whisper_batch_size=_optional_positive_int(raw, "transformers_whisper_batch_size", 8),
        transformers_whisper_torch_dtype=_as_optional_str(raw, "transformers_whisper_torch_dtype", "float16"),
        transformers_whisper_trust_remote_code=_optional_bool(raw, "transformers_whisper_trust_remote_code", False),
        transformers_whisper_punctuator=_optional_bool(raw, "transformers_whisper_punctuator", False),
        transformers_whisper_attn_implementation=_optional_str(raw, "transformers_whisper_attn_implementation"),
        transformers_whisper_task=_optional_str(raw, "transformers_whisper_task"),
        transformers_whisper_stable_ts=_optional_bool(raw, "transformers_whisper_stable_ts", False),
    )

    _validate_config(config)
    config.work_path.mkdir(parents=True, exist_ok=True)
    config.log_path.mkdir(parents=True, exist_ok=True)
    return config


def _as_str(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string.")
    return value


def _as_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false.")
    return value


def _optional_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    # Environment-expanded YAML values arrive as strings because interpolation
    # happens after yaml.safe_load.  Accept only the two unambiguous JSON boolean
    # spellings so deployment flags can be changed without rewriting config.yaml.
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false.")
    return value


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string when set.")
    return value


def _as_optional_str(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string.")
    return value.strip()


def _optional_allow_empty_str(raw: dict[str, Any], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string.")
    return value.strip()


def _optional_probability(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or float(value) < 0.0
        or float(value) > 1.0
    ):
        raise ConfigError(f"{key} must be a finite number between 0 and 1.")
    return float(value)


def _optional_float(raw: dict[str, Any], key: str, default: float | None) -> float | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number or null.")
    return float(value)


def _optional_str_mapping(raw: dict[str, Any], key: str) -> dict[str, str]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping when set.")

    result: dict[str, str] = {}
    for source, target in value.items():
        if not isinstance(source, str) or not source.strip():
            raise ConfigError(f"{key} keys must be non-empty strings.")
        if not isinstance(target, str) or not target.strip():
            raise ConfigError(f"{key} values must be non-empty strings.")
        result[source.strip()] = target.strip()
    return result


def _optional_str_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list when set.")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} items must be non-empty strings.")
        result.append(item.strip())
    return result


def _optional_str_list_with_default(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list when set.")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} items must be non-empty strings.")
        result.append(item.strip())
    return result


def _optional_int_list(raw: dict[str, Any], key: str) -> list[int]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list when set.")

    result: list[int] = []
    for item in value:
        if not isinstance(item, int) or item <= 0:
            raise ConfigError(f"{key} items must be positive integers.")
        result.append(item)
    return result


def _optional_path_mappings(raw: dict[str, Any], key: str) -> list[dict[str, str]]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list when set.")

    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError(f"{key} items must be mappings.")
        remote = item.get("remote")
        local = item.get("local")
        if not isinstance(remote, str) or not remote.strip():
            raise ConfigError(f"{key}.remote must be a non-empty string.")
        if not isinstance(local, str) or not local.strip():
            raise ConfigError(f"{key}.local must be a non-empty string.")
        result.append({"remote": remote.strip(), "local": local.strip()})
    return result


def _optional_series_path_mappings(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list when set.")

    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError(f"{key} items must be mappings.")
        bangumi_id = item.get("bangumi_id")
        path = item.get("path")
        if not isinstance(bangumi_id, int) or bangumi_id <= 0:
            raise ConfigError(f"{key}.bangumi_id must be a positive integer.")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"{key}.path must be a non-empty string.")

        match_value = item.get("match", [])
        if match_value is None:
            match_value = []
        if not isinstance(match_value, list):
            raise ConfigError(f"{key}.match must be a list when set.")
        match: list[str] = []
        for token in match_value:
            if not isinstance(token, str) or not token.strip():
                raise ConfigError(f"{key}.match items must be non-empty strings.")
            match.append(token.strip())

        result.append({"bangumi_id": bangumi_id, "path": path.strip(), "match": match})
    return result


def _optional_suffix_list(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list when set.")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} items must be non-empty strings.")
        suffix = item.strip()
        if not suffix.startswith("."):
            raise ConfigError(f"{key} item must start with '.': {suffix!r}")
        result.append(suffix)
    return result


def _as_positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer.")
    return value


def _optional_positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer.")
    return value


def _optional_non_negative_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{key} must be 0 or greater.")
    return value


def _optional_non_negative_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{key} must be 0 or greater.")
    return float(value)


def _optional_positive_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{key} must be greater than 0.")
    return float(value)


def _normalize_extensions(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError("video_extensions must be a non-empty list.")

    extensions: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("video_extensions items must be non-empty strings.")
        normalized = item.strip().lower()
        if not normalized.startswith("."):
            normalized = f".{normalized}"
        extensions.append(normalized)
    return extensions


def _validate_config(config: AppConfig) -> None:
    valid_backends = {"faster-whisper", "whisperx", "vibevoice", "transformers-whisper"}
    if config.transcription_backend not in valid_backends:
        raise ConfigError(
            "transcription_backend must be 'faster-whisper', 'whisperx', 'vibevoice', or 'transformers-whisper'."
        )
    for field_name, backend in {
        "japanese_transcription_backend": config.japanese_transcription_backend,
        "japanese_transcription_fallback_backend": config.japanese_transcription_fallback_backend,
        "japanese_transcription_final_fallback_backend": config.japanese_transcription_final_fallback_backend,
        "non_japanese_transcription_backend": config.non_japanese_transcription_backend,
        "non_japanese_transcription_fallback_backend": config.non_japanese_transcription_fallback_backend,
        "non_japanese_transcription_final_fallback_backend": config.non_japanese_transcription_final_fallback_backend,
    }.items():
        if backend is not None and backend not in valid_backends:
            raise ConfigError(
                f"{field_name} must be 'faster-whisper', 'whisperx', 'vibevoice', or 'transformers-whisper'."
            )
    valid_transformers_whisper_dtypes = {"auto", "float16", "fp16", "bfloat16", "bf16", "float32", "fp32"}
    if str(config.transformers_whisper_torch_dtype).strip().lower() not in valid_transformers_whisper_dtypes:
        raise ConfigError(
            "transformers_whisper_torch_dtype must be one of: "
            + ", ".join(sorted(valid_transformers_whisper_dtypes))
            + "."
        )
    valid_transformers_whisper_tasks = {"auto", "automatic-speech-recognition", "kotoba-whisper"}
    transformers_whisper_task = str(config.transformers_whisper_task or "").strip().lower()
    if transformers_whisper_task and transformers_whisper_task not in valid_transformers_whisper_tasks:
        raise ConfigError(
            "transformers_whisper_task must be one of: "
            + ", ".join(sorted(valid_transformers_whisper_tasks))
            + "."
        )
    valid_transformers_whisper_attn = {"sdpa", "flash_attention_2", "eager"}
    if (
        config.transformers_whisper_attn_implementation is not None
        and str(config.transformers_whisper_attn_implementation).strip().lower()
        not in valid_transformers_whisper_attn
    ):
        raise ConfigError(
            "transformers_whisper_attn_implementation must be one of: "
            + ", ".join(sorted(valid_transformers_whisper_attn))
            + "."
        )

    if config.backup_retention_count < 0:
        raise ConfigError("backup_retention_count must be 0 or greater.")
    if not str(config.state_backup_path).strip():
        raise ConfigError("state_backup_path must not be empty.")
    if config.database_maintenance_min_freelist_ratio > 1:
        raise ConfigError("database_maintenance_min_freelist_ratio must be between 0 and 1.")

    if config.batch_size < 1 or config.batch_size > 30:
        raise ConfigError("batch_size must be between 1 and 30 for stable subtitle translation.")

    if not (
        0.0
        <= config.source_analyzer_low_confidence
        < config.source_analyzer_high_confidence
        <= 1.0
    ):
        raise ConfigError(
            "source analyzer confidence must satisfy "
            "0 <= source_analyzer_low_confidence < "
            "source_analyzer_high_confidence <= 1."
        )
    if not 0.0 <= config.source_analyzer_min_dialogue_completeness_score <= 1.0:
        raise ConfigError(
            "source_analyzer_min_dialogue_completeness_score must be between 0 and 1."
        )
    if not 0.0 <= config.source_analyzer_min_subtitle_coverage_ratio <= 1.0:
        raise ConfigError(
            "source_analyzer_min_subtitle_coverage_ratio must be between 0 and 1."
        )
    if not 0.0 <= config.source_analyzer_tie_margin <= 1.0:
        raise ConfigError("source_analyzer_tie_margin must be between 0 and 1.")
    if not str(config.source_analyzer_version).strip():
        raise ConfigError("source_analyzer_version must not be empty.")
    if (
        isinstance(config.source_decision_schema_version, bool)
        or config.source_decision_schema_version <= 0
    ):
        raise ConfigError("source_decision_schema_version must be a positive integer.")
    if not str(config.source_decision_version).strip():
        raise ConfigError("source_decision_version must not be empty.")
    if config.source_analyzer_enabled and not config.pipeline_job_store_required:
        raise ConfigError(
            "source_analyzer_enabled requires pipeline_job_store_required so decisions are durable."
        )

    if (
        config.m2_server_canary_circuit_breaker_enabled
        and not config.m2_server_canary_observer_enabled
    ):
        raise ConfigError(
            "m2_server_canary_circuit_breaker_enabled requires "
            "m2_server_canary_observer_enabled."
        )
    if (
        config.m2_server_canary_observer_enabled
        and config.max_concurrent_videos != 1
    ):
        raise ConfigError(
            "max_concurrent_videos must be exactly 1 while the M2 server "
            "canary observer is enabled."
        )

    if config.resource_admission_enabled:
        if config.max_concurrent_videos != 1:
            raise ConfigError(
                "max_concurrent_videos must be exactly 1 while resource_admission_enabled is true."
            )
        if not config.ai_process_isolation_enabled:
            raise ConfigError(
                "ai_process_isolation_enabled must be true while resource_admission_enabled is true."
            )
        if not str(config.resource_admission_state_path).strip():
            raise ConfigError("resource_admission_state_path must not be empty.")
        if not str(config.resource_gpu_lease_path).strip():
            raise ConfigError("resource_gpu_lease_path must not be empty.")
        if config.resource_admission_lower_memory_vram_mib >= config.resource_admission_primary_vram_mib:
            raise ConfigError(
                "resource_admission_lower_memory_vram_mib must be lower than "
                "resource_admission_primary_vram_mib."
            )
        if (
            config.resource_telemetry_cpu_sample_interval_seconds
            >= config.resource_telemetry_host_timeout_seconds
        ):
            raise ConfigError(
                "resource_telemetry_cpu_sample_interval_seconds must be lower than "
                "resource_telemetry_host_timeout_seconds."
            )
        try:
            from resource_admission import ResourceAdmissionConfig, validate_resource_admission_config

            validate_resource_admission_config(
                ResourceAdmissionConfig(
                    telemetry_stale_after_seconds=config.resource_admission_telemetry_stale_seconds,
                    cpu_yellow_percent=config.resource_admission_cpu_yellow_percent,
                    cpu_red_percent=config.resource_admission_cpu_red_percent,
                    ram_yellow_available_ratio=config.resource_admission_ram_yellow_available_ratio,
                    ram_red_available_ratio=config.resource_admission_ram_red_available_ratio,
                    gpu_yellow_percent=config.resource_admission_gpu_yellow_percent,
                    gpu_red_percent=config.resource_admission_gpu_red_percent,
                    vram_reserve_mib=config.resource_admission_vram_reserve_mib,
                    recovery_samples=config.resource_admission_recovery_samples,
                    yellow_retry_after_seconds=config.resource_admission_yellow_retry_seconds,
                    red_retry_after_seconds=config.resource_admission_red_retry_seconds,
                    unavailable_retry_after_seconds=config.resource_admission_unavailable_retry_seconds,
                    gpu_concurrency_limit=1,
                )
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    if config.completed_delivery_enabled:
        if not str(config.completed_delivery_path).strip():
            raise ConfigError(
                "completed_delivery_path must be configured while completed_delivery_enabled is true."
            )
        if str(config.completed_delivery_source_policy).strip().lower() != "retain":
            raise ConfigError(
                "completed_delivery_source_policy must be 'retain'; source removal is not ledger-safe."
            )
        try:
            input_root = config.input_path.resolve()
            completed_root = Path(config.completed_delivery_path)
            if not completed_root.is_absolute():
                completed_root = config.work_path / completed_root
            completed_root = completed_root.resolve()
            if not completed_root.is_dir():
                raise ConfigError(
                    "completed_delivery_path must be an existing mounted directory."
                )
            completed_root.relative_to(input_root)
        except ValueError:
            pass
        else:
            raise ConfigError(
                "completed_delivery_path must stay outside input_path to prevent scanner re-admission."
            )
        try:
            completed_root.relative_to(config.work_path.resolve())
        except ValueError:
            pass
        else:
            raise ConfigError(
                "completed_delivery_path must be a dedicated mount outside work_path."
            )

    if config.acceptance_queue_lane_enabled and (
        not config.scanner_cache_enabled or not config.scanner_queue_enabled
    ):
        raise ConfigError(
            "acceptance_queue_lane_enabled requires scanner_cache_enabled and scanner_queue_enabled."
        )
    if config.ai_canary_once_enabled and (
        not config.scanner_cache_enabled
        or not config.scanner_queue_enabled
        or config.max_concurrent_videos != 1
    ):
        raise ConfigError(
            "ai_canary_once_enabled requires scanner cache/queue and exactly one video lane."
        )
    if config.acceptance_fault_execution_enabled:
        if not config.acceptance_queue_lane_enabled:
            raise ConfigError(
                "acceptance_fault_execution_enabled requires acceptance_queue_lane_enabled."
            )
        if not re.fullmatch(
            r"accrun_[0-9a-f]{48}",
            config.acceptance_fault_execution_run_id,
        ):
            raise ConfigError(
                "acceptance_fault_execution_run_id must be an explicit fresh run id."
            )
        if not re.fullmatch(
            r"[0-9a-f]{64}",
            config.acceptance_fault_execution_plan_sha256,
        ):
            raise ConfigError(
                "acceptance_fault_execution_plan_sha256 must be explicit lowercase SHA-256."
            )

    if not config.allowed_source_languages:
        raise ConfigError("allowed_source_languages must contain at least one language code.")

    if not 0 <= config.language_detect_min_probability <= 1:
        raise ConfigError("language_detect_min_probability must be between 0 and 1.")

    if config.language_uncertain_policy not in {"skip", "continue", "fail"}:
        raise ConfigError("language_uncertain_policy must be 'skip', 'continue', or 'fail'.")

    if "{language}" not in config.ai_source_transcript_ass_suffix_template:
        raise ConfigError("ai_source_transcript_ass_suffix_template must include {language}.")

    if (
        not config.ai_source_transcript_ass_suffix_template.startswith(".")
        or not config.ai_source_transcript_ass_suffix_template.lower().endswith(".ass")
    ):
        raise ConfigError("ai_source_transcript_ass_suffix_template must start with '.' and end with '.ass'.")

    if not config.metadata_context_providers:
        raise ConfigError("metadata_context_providers must contain at least one provider.")

    unsupported_metadata_providers = [
        provider
        for provider in config.metadata_context_providers
        if provider.lower() not in {"anilist"}
    ]
    if unsupported_metadata_providers:
        raise ConfigError(
            "metadata_context_providers currently supports only 'anilist': "
            + ", ".join(unsupported_metadata_providers)
        )

    if config.whisper_task not in {"transcribe", "translate"}:
        raise ConfigError("whisper_task must be either 'transcribe' or 'translate'.")

    if config.whisper_beam_size <= 0:
        raise ConfigError("whisper_beam_size must be a positive integer.")

    if config.whisper_best_of <= 0:
        raise ConfigError("whisper_best_of must be a positive integer.")

    if config.whisper_patience <= 0:
        raise ConfigError("whisper_patience must be greater than 0.")

    if config.whisper_length_penalty <= 0:
        raise ConfigError("whisper_length_penalty must be greater than 0.")

    if config.whisper_repetition_penalty <= 0:
        raise ConfigError("whisper_repetition_penalty must be greater than 0.")

    if config.whisper_no_repeat_ngram_size < 0:
        raise ConfigError("whisper_no_repeat_ngram_size must be 0 or greater.")

    if config.subtitle_timing_mode not in {"segment", "word"}:
        raise ConfigError("subtitle_timing_mode must be either 'segment' or 'word'.")

    if config.subtitle_max_duration_seconds <= 0:
        raise ConfigError("subtitle_max_duration_seconds must be greater than 0.")

    if config.subtitle_min_duration_seconds <= 0:
        raise ConfigError("subtitle_min_duration_seconds must be greater than 0.")

    if config.subtitle_min_duration_seconds > config.subtitle_max_duration_seconds:
        raise ConfigError("subtitle_min_duration_seconds cannot exceed subtitle_max_duration_seconds.")

    if config.subtitle_max_chars <= 0:
        raise ConfigError("subtitle_max_chars must be a positive integer.")

    if config.subtitle_end_padding_seconds < 0:
        raise ConfigError("subtitle_end_padding_seconds must be 0 or greater.")

    if config.subtitle_min_gap_seconds < 0:
        raise ConfigError("subtitle_min_gap_seconds must be 0 or greater.")

    if config.subtitle_quality_max_duration_seconds <= 0:
        raise ConfigError("subtitle_quality_max_duration_seconds must be greater than 0.")

    if config.subtitle_quality_max_primary_chars <= 0:
        raise ConfigError("subtitle_quality_max_primary_chars must be a positive integer.")

    if config.subtitle_quality_hard_max_primary_chars < config.subtitle_quality_max_primary_chars:
        raise ConfigError("subtitle_quality_hard_max_primary_chars cannot be less than subtitle_quality_max_primary_chars.")

    if config.subtitle_quality_max_gap_seconds <= 0:
        raise ConfigError("subtitle_quality_max_gap_seconds must be greater than 0.")

    if config.subtitle_quality_max_leading_gap_seconds <= 0:
        raise ConfigError("subtitle_quality_max_leading_gap_seconds must be greater than 0.")

    if config.subtitle_quality_warn_cps <= 0:
        raise ConfigError("subtitle_quality_warn_cps must be greater than 0.")

    if config.subtitle_quality_fail_cps < config.subtitle_quality_warn_cps:
        raise ConfigError("subtitle_quality_fail_cps cannot be less than subtitle_quality_warn_cps.")

    if config.subtitle_quality_hard_min_duration_seconds <= 0:
        raise ConfigError("subtitle_quality_hard_min_duration_seconds must be greater than 0.")

    if config.subtitle_quality_min_duration_seconds < config.subtitle_quality_hard_min_duration_seconds:
        raise ConfigError(
            "subtitle_quality_min_duration_seconds cannot be less than subtitle_quality_hard_min_duration_seconds."
        )

    if config.subtitle_quality_max_overlap_seconds < 0:
        raise ConfigError("subtitle_quality_max_overlap_seconds must be 0 or greater.")

    if config.gap_report_threshold_seconds <= 0:
        raise ConfigError("gap_report_threshold_seconds must be greater than 0.")

    if config.gap_rescue_threshold_seconds <= 0:
        raise ConfigError("gap_rescue_threshold_seconds must be greater than 0.")

    if config.gap_rescue_leading_threshold_seconds <= 0:
        raise ConfigError("gap_rescue_leading_threshold_seconds must be greater than 0.")

    if config.gap_rescue_leading_threshold_seconds > config.gap_rescue_leading_max_seconds:
        raise ConfigError(
            "gap_rescue_leading_threshold_seconds must not exceed "
            "gap_rescue_leading_max_seconds."
        )

    if config.gap_rescue_max_gap_seconds <= 0:
        raise ConfigError("gap_rescue_max_gap_seconds must be greater than 0.")

    if config.gap_rescue_leading_max_seconds <= 0:
        raise ConfigError("gap_rescue_leading_max_seconds must be greater than 0.")

    if config.gap_rescue_padding_seconds < 0:
        raise ConfigError("gap_rescue_padding_seconds must be 0 or greater.")

    if config.gap_rescue_clip_seconds <= 0:
        raise ConfigError("gap_rescue_clip_seconds must be greater than 0.")

    if config.gap_rescue_clip_overlap_seconds < 0:
        raise ConfigError("gap_rescue_clip_overlap_seconds must be 0 or greater.")

    if config.gap_rescue_clip_overlap_seconds >= config.gap_rescue_clip_seconds:
        raise ConfigError("gap_rescue_clip_overlap_seconds must be less than gap_rescue_clip_seconds.")

    if config.gap_rescue_max_gaps < 0:
        raise ConfigError("gap_rescue_max_gaps must be 0 or greater.")

    if config.gap_rescue_min_chars < 1:
        raise ConfigError("gap_rescue_min_chars must be a positive integer.")

    if not 0 <= config.gap_rescue_no_speech_threshold <= 1:
        raise ConfigError("gap_rescue_no_speech_threshold must be between 0 and 1.")
    if not 0 <= config.gap_rescue_accept_max_no_speech_prob <= 1:
        raise ConfigError("gap_rescue_accept_max_no_speech_prob must be between 0 and 1.")
    if config.gap_rescue_accept_max_compression_ratio <= 0:
        raise ConfigError("gap_rescue_accept_max_compression_ratio must be greater than 0.")
    if config.op_ed_min_audio_seconds < 0:
        raise ConfigError("op_ed_min_audio_seconds must be 0 or greater.")
    if config.op_ed_opening_window_seconds <= 0:
        raise ConfigError("op_ed_opening_window_seconds must be greater than 0.")
    if config.op_ed_ending_window_seconds <= 0:
        raise ConfigError("op_ed_ending_window_seconds must be greater than 0.")
    if config.op_ed_gap_threshold_seconds <= 0:
        raise ConfigError("op_ed_gap_threshold_seconds must be greater than 0.")
    if config.op_ed_max_gap_seconds < config.op_ed_gap_threshold_seconds:
        raise ConfigError("op_ed_max_gap_seconds must be at least op_ed_gap_threshold_seconds.")
    if config.op_ed_padding_seconds < 0:
        raise ConfigError("op_ed_padding_seconds must be 0 or greater.")
    if config.op_ed_max_rescue_ranges < 0:
        raise ConfigError("op_ed_max_rescue_ranges must be 0 or greater.")
    if not 0 <= config.op_ed_no_speech_threshold <= 1:
        raise ConfigError("op_ed_no_speech_threshold must be between 0 and 1.")
    if not 0 <= config.op_ed_accept_max_no_speech_prob <= 1:
        raise ConfigError("op_ed_accept_max_no_speech_prob must be between 0 and 1.")
    if config.op_ed_accept_max_compression_ratio <= 0:
        raise ConfigError("op_ed_accept_max_compression_ratio must be greater than 0.")

    for field_name, suffix in {
        "ai_japanese_ass_suffix": config.ai_japanese_ass_suffix,
        "ai_simplified_chinese_ass_suffix": config.ai_simplified_chinese_ass_suffix,
        "ai_traditional_chinese_ass_suffix": config.ai_traditional_chinese_ass_suffix,
    }.items():
        if not suffix.startswith(".") or not suffix.lower().endswith(".ass"):
            raise ConfigError(f"{field_name} must start with '.' and end with '.ass'.")

    if config.ass_secondary_font_size > config.ass_primary_font_size:
        raise ConfigError("ass_secondary_font_size cannot exceed ass_primary_font_size.")

    if config.mikan_enabled and not config.mikan_bangumi_ids and not config.mikan_auto_match_enabled:
        raise ConfigError("mikan_enabled is true, but mikan_bangumi_ids is empty and auto-match is disabled.")

    if config.mikan_enabled and not config.qbit_tags:
        raise ConfigError("mikan_enabled is true, but qbit_tags is empty.")

    configured_bangumi_ids = set(config.mikan_bangumi_ids)
    for mapping in config.mikan_series_path_mappings:
        bangumi_id = mapping["bangumi_id"]
        if configured_bangumi_ids and bangumi_id not in configured_bangumi_ids:
            raise ConfigError("mikan_series_path_mappings contains a bangumi_id not listed in mikan_bangumi_ids.")

    if not 0 < config.mikan_auto_match_threshold <= 1:
        raise ConfigError("mikan_auto_match_threshold must be greater than 0 and at most 1.")

    if config.whisperx_batch_size <= 0:
        raise ConfigError("whisperx_batch_size must be a positive integer.")

    if config.whisper_gpu_memory_reserve_mib < 256:
        raise ConfigError("whisper_gpu_memory_reserve_mib must be at least 256 MiB.")

    if config.whisperx_vad_method not in {"pyannote", "silero"}:
        raise ConfigError("whisperx_vad_method must be either 'pyannote' or 'silero'.")

    if not 0 <= config.whisperx_vad_onset <= 1:
        raise ConfigError("whisperx_vad_onset must be between 0 and 1.")

    if not 0 <= config.whisperx_vad_offset <= 1:
        raise ConfigError("whisperx_vad_offset must be between 0 and 1.")

    if config.whisperx_vad_offset > config.whisperx_vad_onset:
        raise ConfigError("whisperx_vad_offset cannot exceed whisperx_vad_onset.")

    if config.whisperx_chunk_size <= 0:
        raise ConfigError("whisperx_chunk_size must be a positive integer.")

    if config.vibevoice_torch_dtype not in {"auto", "float16", "fp16", "bfloat16", "bf16", "float32", "fp32"}:
        raise ConfigError("vibevoice_torch_dtype must be auto, float16, bfloat16, or float32.")

    if config.vibevoice_max_new_tokens < 0:
        raise ConfigError("vibevoice_max_new_tokens must be 0 or greater.")

    if config.vibevoice_tokenizer_chunk_size < 0:
        raise ConfigError("vibevoice_tokenizer_chunk_size must be 0 or greater.")

    if config.whisper_no_speech_threshold is not None and not 0 <= config.whisper_no_speech_threshold <= 1:
        raise ConfigError("whisper_no_speech_threshold must be between 0 and 1, or null.")

    if (
        config.whisper_hallucination_silence_threshold is not None
        and config.whisper_hallucination_silence_threshold < 0
    ):
        raise ConfigError("whisper_hallucination_silence_threshold must be 0 or greater, or null.")

    if config.whisper_vad_threshold < 0 or config.whisper_vad_threshold > 1:
        raise ConfigError("whisper_vad_threshold must be between 0 and 1.")

    if config.whisper_vad_min_silence_duration_ms < 0:
        raise ConfigError("whisper_vad_min_silence_duration_ms must be 0 or greater.")

    if config.whisper_vad_speech_pad_ms < 0:
        raise ConfigError("whisper_vad_speech_pad_ms must be 0 or greater.")

    if config.repeated_vocalization_min_chars < 1:
        raise ConfigError("repeated_vocalization_min_chars must be a positive integer.")

    if config.transcription_quality_min_audio_seconds < 0:
        raise ConfigError("transcription_quality_min_audio_seconds must be 0 or greater.")

    if not 0 <= config.transcription_quality_min_coverage_percent <= 100:
        raise ConfigError("transcription_quality_min_coverage_percent must be between 0 and 100.")

    if config.transcription_quality_min_blocks_per_minute < 0:
        raise ConfigError("transcription_quality_min_blocks_per_minute must be 0 or greater.")

    if not 0 <= config.transcription_quality_max_low_confidence_percent <= 100:
        raise ConfigError("transcription_quality_max_low_confidence_percent must be between 0 and 100.")

    if config.transcription_quality_min_confidence_segments <= 0:
        raise ConfigError("transcription_quality_min_confidence_segments must be a positive integer.")

    if config.transcription_quality_max_leading_gap_seconds <= 0:
        raise ConfigError("transcription_quality_max_leading_gap_seconds must be greater than 0.")

    if config.enable_vocal_separation and config.vocal_separation_engine == "none":
        raise ConfigError(
            "enable_vocal_separation is true, but vocal_separation_engine is 'none'. "
            "Set enable_vocal_separation to false for the first version."
        )

    if config.enable_vocal_separation and config.vocal_separation_engine not in {"demucs"}:
        raise ConfigError("vocal_separation_engine must be 'demucs' when enable_vocal_separation is true.")
