import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ConfigError, load_config


class ConfigEnvironmentExpansionTests(unittest.TestCase):
    def test_shipped_config_omits_optional_op_ed_prompt_for_rollback_compatibility(self):
        import yaml

        raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))

        self.assertNotIn("op_ed_initial_prompt", raw)

    def test_load_config_expands_environment_values_and_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text(
                f"""
input_path: "${{TEST_INPUT_PATH:-{root.as_posix()}/input}}"
work_path: "${{TEST_WORK_PATH:-{root.as_posix()}/work}}"
log_path: "${{TEST_LOG_PATH:-{root.as_posix()}/logs}}"
video_extensions: [".mkv", ".mp4"]
whisper_model: "large-v3"
whisper_device: "cuda"
whisper_compute_type: "float16"
whisper_language: "ja"
whisper_task: "transcribe"
japanese_transcription_backend: "transformers-whisper"
japanese_transcription_model: "kotoba-tech/kotoba-whisper-v2.1"
japanese_transcription_final_fallback_backend: "faster-whisper"
japanese_transcription_final_fallback_model: "Systran/faster-whisper-medium"
japanese_transcription_final_fallback_compute_type: "int8_float16"
non_japanese_transcription_backend: "faster-whisper"
non_japanese_transcription_model: "large-v3"
transformers_whisper_chunk_length_s: 15
transformers_whisper_batch_size: 16
transformers_whisper_torch_dtype: "float16"
transformers_whisper_trust_remote_code: true
transformers_whisper_punctuator: true
transformers_whisper_attn_implementation: "sdpa"
transformers_whisper_task: "auto"
transformers_whisper_stable_ts: false
whisper_vad_filter: false
whisper_condition_on_previous_text: false
whisper_temperature: 0
enable_vocal_separation: false
vocal_separation_engine: "none"
vocal_separation_output: "vocals"
translator_base_url: "${{TEST_TRANSLATOR_URL:-http://default.invalid/v1}}"
translator_api_key: "EMPTY"
translator_model: "SakuraLLM"
translator_fallback_models: ["SakuraSecondary", "SakuraSmall"]
translator_timeout_seconds: 120
batch_size: 10
max_retries: 3
watch_interval_seconds: 300
mikan_completed_poll_interval_seconds: 12
mikan_operation_lock_wait_seconds: 42
opencc_config: "s2twp.json"
keep_intermediate_files: true
qbit_password: "${{TEST_QBIT_PASSWORD:-CHANGE_ME}}"
""".lstrip(),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "TEST_TRANSLATOR_URL": "http://sakura.local/v1",
                    "TEST_QBIT_PASSWORD": "secret",
                },
                clear=False,
            ):
                config = load_config(config_path)

            self.assertEqual(config.translator_base_url, "http://sakura.local/v1")
            self.assertEqual(
                config.translator_fallback_models,
                ["SakuraSecondary", "SakuraSmall"],
            )
            self.assertEqual(config.qbit_password, "secret")
            self.assertEqual(config.config_path, config_path)
            self.assertTrue(config.ai_process_isolation_enabled)
            self.assertEqual(config.ai_subprocess_timeout_seconds, 14400)
            self.assertFalse(config.resource_admission_enabled)
            self.assertEqual(config.resource_admission_state_path, "resource_admission_state.json")
            self.assertFalse(config.completed_delivery_enabled)
            self.assertEqual(config.completed_delivery_path, "")
            self.assertEqual(config.completed_delivery_source_policy, "retain")
            self.assertEqual(config.mikan_completed_poll_interval_seconds, 12)
            self.assertEqual(config.mikan_operation_lock_wait_seconds, 42)
            self.assertEqual(config.mikan_extract_workers, 2)
            self.assertEqual(config.mikan_extract_workers_during_ai, 1)
            self.assertEqual(config.mikan_download_start_timeout_seconds, 180)
            self.assertEqual(config.mikan_download_metadata_timeout_seconds, 300)
            self.assertEqual(config.mikan_download_unhealthy_timeout_seconds, 300)
            self.assertEqual(config.mikan_download_stall_timeout_seconds, 600)
            self.assertEqual(config.mikan_download_max_eta_seconds, 86400)
            self.assertEqual(config.mikan_completed_reconcile_max_age_seconds, 21600)
            self.assertEqual(config.mikan_no_candidate_retry_seconds, 600)
            self.assertEqual(config.mikan_no_candidate_retry_max_seconds, 86400)
            self.assertEqual(config.mikan_episode_index_ttl_seconds, 21600)
            self.assertFalse(config.mikan_fallback_sources_enabled)
            self.assertEqual(
                config.mikan_fallback_sources,
                ["animegarden", "dmhy", "acgrip", "bangumimoe", "kisssub", "nyaa"],
            )
            self.assertEqual(config.mikan_fallback_source_failure_threshold, 2)
            self.assertEqual(config.mikan_fallback_source_cooldown_seconds, 1800)
            self.assertEqual(config.mikan_fallback_min_nyaa_seeders, 1)
            self.assertEqual(config.mikan_completed_tags, ["mikansub-completed"])
            self.assertTrue(config.translation_split_batch_on_timeout)
            self.assertTrue(config.translation_split_batch_on_format_error)
            self.assertFalse(config.language_gate_enabled)
            self.assertEqual(config.allowed_source_languages, ["ja"])
            self.assertEqual(config.japanese_transcription_backend, "transformers-whisper")
            self.assertEqual(config.japanese_transcription_model, "kotoba-tech/kotoba-whisper-v2.1")
            self.assertEqual(
                config.japanese_transcription_final_fallback_backend,
                "faster-whisper",
            )
            self.assertEqual(
                config.japanese_transcription_final_fallback_model,
                "Systran/faster-whisper-medium",
            )
            self.assertEqual(
                config.japanese_transcription_final_fallback_compute_type,
                "int8_float16",
            )
            self.assertEqual(config.non_japanese_transcription_backend, "faster-whisper")
            self.assertEqual(config.non_japanese_transcription_model, "large-v3")
            self.assertEqual(config.transformers_whisper_chunk_length_s, 15)
            self.assertEqual(config.transformers_whisper_batch_size, 16)
            self.assertEqual(config.transformers_whisper_torch_dtype, "float16")
            self.assertTrue(config.transformers_whisper_trust_remote_code)
            self.assertTrue(config.transformers_whisper_punctuator)
            self.assertEqual(config.transformers_whisper_attn_implementation, "sdpa")
            self.assertEqual(config.transformers_whisper_task, "auto")
            self.assertFalse(config.transformers_whisper_stable_ts)
            self.assertEqual(config.language_uncertain_policy, "skip")
            self.assertEqual(config.language_detect_sample_count, 3)
            self.assertEqual(config.language_detect_sample_seconds, 30)
            self.assertEqual(config.scanner_background_scan_interval_seconds, 21600)
            self.assertEqual(config.scanner_background_scan_startup_delay_seconds, 600)
            self.assertTrue(config.scanner_active_queue_ledger_backfill_enabled)
            self.assertEqual(config.scanner_active_queue_ledger_backfill_interval_seconds, 10)
            self.assertEqual(config.scanner_active_queue_ledger_backfill_batch_size, 250)
            self.assertEqual(config.scanner_active_queue_ledger_backfill_no_progress_seconds, 300)
            self.assertEqual(config.scanner_walk_yield_every_entries, 256)
            self.assertEqual(config.scanner_walk_yield_seconds, 0.025)
            self.assertEqual(config.scanner_queue_oldest_every_n_cycles, 12)
            self.assertEqual(config.auto_ai_max_attempts, 3)
            self.assertTrue(config.op_ed_transcription_enabled)
            self.assertTrue(config.scanner_skip_standalone_op_ed)
            self.assertEqual(config.op_ed_min_audio_seconds, 600.0)
            self.assertEqual(config.op_ed_opening_window_seconds, 360.0)
            self.assertEqual(config.op_ed_ending_window_seconds, 300.0)
            self.assertEqual(config.op_ed_gap_threshold_seconds, 6.0)
            self.assertEqual(config.op_ed_max_rescue_ranges, 6)
            self.assertIsNone(config.op_ed_initial_prompt)
            self.assertTrue(config.enable_leading_gap_rescue)
            self.assertEqual(config.gap_rescue_leading_threshold_seconds, 1.5)
            self.assertEqual(config.gap_rescue_leading_max_seconds, 120.0)
            self.assertEqual(config.gap_rescue_max_gap_seconds, 45.0)
            self.assertEqual(config.gap_rescue_clip_seconds, 30.0)
            self.assertEqual(config.gap_rescue_clip_overlap_seconds, 2.0)
            self.assertEqual(config.gap_rescue_no_speech_threshold, 0.95)
            self.assertEqual(config.gap_rescue_log_prob_threshold, -1.5)
            self.assertEqual(config.gap_rescue_compression_ratio_threshold, 2.4)
            self.assertEqual(config.gap_rescue_accept_min_avg_logprob, -1.15)
            self.assertEqual(config.gap_rescue_accept_max_no_speech_prob, 0.90)
            self.assertEqual(config.gap_rescue_accept_max_compression_ratio, 2.4)
            self.assertEqual(config.transcription_quality_max_leading_gap_seconds, 30.0)
            self.assertEqual(config.subtitle_quality_max_leading_gap_seconds, 30.0)
            self.assertEqual(config.subtitle_remediation_punctuation_repeat_limit, 3)
            self.assertEqual(config.subtitle_remediation_wrap_max_chars, 42)
            self.assertEqual(config.subtitle_remediation_max_visual_lines, 2)
            self.assertEqual(config.subtitle_remediation_max_timing_shift_seconds, 0.20)
            self.assertEqual(config.subtitle_remediation_max_total_timing_shift_seconds, 0.50)
            self.assertEqual(config.subtitle_remediation_max_overlap_repair_seconds, 0.20)
            self.assertEqual(config.subtitle_remediation_aligned_max_timing_shift_seconds, 2.0)
            self.assertEqual(config.subtitle_remediation_aligned_max_total_timing_shift_seconds, 3.0)
            self.assertEqual(config.subtitle_remediation_aligned_max_overlap_repair_seconds, 2.0)
            self.assertEqual(config.op_ed_accept_min_avg_logprob, -1.15)
            self.assertTrue(config.database_maintenance_enabled)
            self.assertEqual(config.database_maintenance_interval_hours, 168)
            self.assertEqual(config.database_maintenance_min_reclaim_mib, 64.0)
            self.assertEqual(config.database_maintenance_min_freelist_ratio, 0.25)
            self.assertFalse(config.translation_metadata_context_enabled)
            self.assertTrue(config.translation_memory_enabled)
            self.assertTrue(config.translation_memory_auto_apply_enabled)
            self.assertEqual(config.translation_memory_path, "translation_memory.sqlite3")
            self.assertEqual(config.translation_memory_outbox_path, "translation_memory_outbox")
            self.assertEqual(config.metadata_context_providers, ["anilist"])
            self.assertTrue(config.series_metadata_sync_enabled)
            self.assertEqual(config.series_metadata_sync_interval_seconds, 21600)
            self.assertEqual(config.series_metadata_sync_startup_delay_seconds, 30)
            self.assertTrue(config.work_path.exists())
            self.assertTrue(config.log_path.exists())

    def test_resource_admission_rejects_unsafe_multi_lane_or_nonisolated_config(self):
        base = Path("config.yaml").read_text(encoding="utf-8")
        for old, new in (
            ("max_concurrent_videos: 1", "max_concurrent_videos: 2"),
            ("ai_process_isolation_enabled: true", "ai_process_isolation_enabled: false"),
        ):
            with self.subTest(new=new), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "config.yaml"
                path.write_text(base.replace(old, new, 1), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_rejects_invalid_japanese_final_fallback_backend(self):
        base = Path("config.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.yaml"
            path.write_text(
                base.replace(
                    "japanese_transcription_final_fallback_backend: faster-whisper",
                    "japanese_transcription_final_fallback_backend: invalid-backend",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_completed_delivery_rejects_input_subtree_or_destructive_source_policy(self):
        base = Path("config.yaml").read_text(encoding="utf-8").replace(
            "completed_delivery_enabled: ${COMPLETED_DELIVERY_ENABLED:-false}",
            "completed_delivery_enabled: true",
            1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            input_root.mkdir()
            inside = input_root / "completed"
            inside.mkdir()
            outside = root / "completed"
            outside.mkdir()
            cases = (
                (
                    base.replace(
                        "completed_delivery_path: ${ANIME_COMPLETED_PATH:-/completed}",
                        f'completed_delivery_path: "{inside.as_posix()}"',
                        1,
                    ),
                    "input subtree",
                ),
                (
                    base.replace(
                        "completed_delivery_path: ${ANIME_COMPLETED_PATH:-/completed}",
                        f'completed_delivery_path: "{outside.as_posix()}"',
                        1,
                    ).replace(
                        "completed_delivery_source_policy: retain",
                        "completed_delivery_source_policy: remove",
                        1,
                    ),
                    "destructive policy",
                ),
            )
            for content, label in cases:
                with self.subTest(label=label):
                    path = root / f"{label.replace(' ', '-')}.yaml"
                    path.write_text(content, encoding="utf-8")
                    with patch.dict("os.environ", {"ANIME_INPUT_PATH": str(input_root)}):
                        with self.assertRaises(ConfigError):
                            load_config(path)

    def test_completed_delivery_can_be_enabled_by_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            work_root = root / "work"
            log_root = root / "logs"
            completed_root = root / "completed"
            for path in (input_root, work_root, log_root, completed_root):
                path.mkdir()
            with patch.dict(
                "os.environ",
                {
                    "ANIME_INPUT_PATH": str(input_root),
                    "ANIME_WORK_PATH": str(work_root),
                    "ANIME_LOG_PATH": str(log_root),
                    "ANIME_COMPLETED_PATH": str(completed_root),
                    "COMPLETED_DELIVERY_ENABLED": "true",
                },
                clear=False,
            ):
                config = load_config(Path("config.yaml"))

            self.assertTrue(config.completed_delivery_enabled)
            self.assertEqual(config.completed_delivery_path, str(completed_root))


if __name__ == "__main__":
    unittest.main()
