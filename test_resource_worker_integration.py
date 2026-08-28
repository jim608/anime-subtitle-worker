from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from resource_runtime import (
    build_resource_launch_plan,
    record_resource_oom,
    serialize_launch_plan,
)
from resource_telemetry_worker import WorkerResourceTelemetrySample
from worker import VideoWorker


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_path=root,
        resource_admission_enabled=True,
        resource_admission_state_path="resource.json",
        resource_admission_telemetry_stale_seconds=15.0,
        resource_admission_cpu_yellow_percent=80.0,
        resource_admission_cpu_red_percent=95.0,
        resource_admission_ram_yellow_available_ratio=0.20,
        resource_admission_ram_red_available_ratio=0.08,
        resource_admission_gpu_yellow_percent=85.0,
        resource_admission_gpu_red_percent=98.0,
        resource_admission_vram_reserve_mib=2048.0,
        resource_admission_primary_vram_mib=8500.0,
        resource_admission_lower_memory_vram_mib=6200.0,
        resource_admission_recovery_samples=1,
        resource_admission_yellow_retry_seconds=30.0,
        resource_admission_red_retry_seconds=120.0,
        resource_admission_unavailable_retry_seconds=60.0,
        resource_admission_recent_oom_cooldown_seconds=21600,
        resource_telemetry_gpu_timeout_seconds=2.0,
        resource_telemetry_host_timeout_seconds=1.0,
        resource_telemetry_cpu_sample_interval_seconds=0.1,
        japanese_transcription_model="large-v3",
        whisper_model="large-v3",
        whisper_compute_type="float16",
        batch_size=6,
        translation_context_max_blocks=40,
        translation_context_max_chars=3000,
        whisperx_batch_size=8,
        transformers_whisper_batch_size=16,
    )


def _sample(now: float = 1000.0) -> WorkerResourceTelemetrySample:
    return WorkerResourceTelemetrySample(
        cpu_percent=20.0,
        ram_available_mib=16000.0,
        ram_total_mib=32000.0,
        gpu_util_percent=10.0,
        vram_free_mib=11500.0,
        vram_total_mib=12288.0,
        sampled_at_epoch_seconds=now,
        available=True,
    )


def _worker(config: SimpleNamespace) -> VideoWorker:
    worker = VideoWorker.__new__(VideoWorker)
    worker.config = config
    worker.logger = Mock()
    worker._resource_launch_plan = None
    worker._translator = None
    worker._translator_progress_video = None
    return worker


class ResourceWorkerIntegrationTest(unittest.TestCase):
    def test_resource_enabled_child_rejects_missing_environment_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            worker = _worker(_config(root))

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "ANIME_RESOURCE_LAUNCH_PLAN is missing"):
                    worker._load_resource_launch_plan(video)

    def test_authorized_child_applies_route_batch_and_context_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            config = _config(root)
            record_resource_oom(config, video, "CUDA OOM", now=999.0)
            plan = build_resource_launch_plan(
                config,
                video,
                stage="transcription",
                now=1000.0,
                telemetry_sampler=lambda _config: _sample(),
            )
            worker = _worker(config)

            with patch.dict(
                os.environ,
                {"ANIME_RESOURCE_LAUNCH_PLAN": serialize_launch_plan(plan)},
                clear=False,
            ), patch("resource_runtime.time.time", return_value=1001.0):
                worker._resource_launch_plan = worker._load_resource_launch_plan(video)

            asr = worker._resource_adjusted_asr_config(config)
            self.assertEqual(asr.whisper_model, "large-v3")
            self.assertEqual(asr.whisper_compute_type, "int8_float16")
            self.assertEqual(asr.whisperx_batch_size, 4)
            self.assertEqual(asr.transformers_whisper_batch_size, 8)
            detector_input = SimpleNamespace(**vars(config))
            detector_input.whisper_model = "language-probe-small"
            detector = worker._resource_adjusted_asr_config(detector_input)
            self.assertEqual(detector.whisper_model, "language-probe-small")
            self.assertEqual(detector.whisper_compute_type, "int8_float16")

            translator = object()
            with patch("worker.SubtitleTranslator", return_value=translator) as constructor:
                self.assertIs(worker._get_translator(), translator)
            translator_config = constructor.call_args.args[0]
            self.assertEqual(translator_config.batch_size, 3)
            self.assertEqual(translator_config.translation_context_max_blocks, 20)
            self.assertEqual(translator_config.translation_context_max_chars, 1500)
            self.assertEqual(worker._resource_effective_limits()["concurrency"], 1)

    def test_cache_cleanup_failure_still_releases_kernel_lease(self) -> None:
        worker = _worker(SimpleNamespace())
        lease = Mock()

        with patch("worker.clear_whisper_model_cache", side_effect=RuntimeError("cleanup failed")):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                worker._release_resource_lease(lease)

        lease.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
