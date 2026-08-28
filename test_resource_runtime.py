from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from resource_runtime import (
    build_resource_launch_plan,
    parse_authorized_resource_launch_plan,
    parse_resource_launch_plan,
    read_resource_admission_state,
    record_resource_oom,
    serialize_launch_plan,
)
from resource_telemetry_worker import WorkerResourceTelemetrySample


def config(root: Path):
    return SimpleNamespace(
        work_path=root,
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
        translation_context_max_blocks=120,
        translation_context_max_chars=6000,
        whisperx_batch_size=8,
        transformers_whisper_batch_size=16,
    )


def sample(*, now=1000.0, cpu=20.0, free=11500.0):
    return WorkerResourceTelemetrySample(
        cpu_percent=cpu,
        ram_available_mib=16000.0,
        ram_total_mib=32000.0,
        gpu_util_percent=10.0,
        vram_free_mib=free,
        vram_total_mib=12288.0,
        sampled_at_epoch_seconds=now,
        available=True,
    )


class ResourceRuntimeTest(unittest.TestCase):
    def test_admitted_plan_round_trips_and_state_is_fresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            plan = build_resource_launch_plan(
                config(root), video, stage="transcription", now=1000.0,
                telemetry_sampler=lambda _config: sample(),
            )
            self.assertTrue(plan["admitted"])
            parsed = parse_resource_launch_plan(
                serialize_launch_plan(plan), video, now=1001.0
            )
            self.assertEqual(parsed["decision_id"], plan["decision_id"])
            authorized = parse_authorized_resource_launch_plan(
                config(root), serialize_launch_plan(plan), video, now=1001.0
            )
            self.assertEqual(authorized["decision_id"], plan["decision_id"])
            self.assertIsNotNone(read_resource_admission_state(config(root), now=1001.0))

    def test_production_clock_is_captured_after_telemetry_sampling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            clock = iter((1000.0, 1002.0))

            with patch("resource_runtime.time.time", side_effect=lambda: next(clock)):
                plan = build_resource_launch_plan(
                    config(root),
                    video,
                    stage="transcription",
                    telemetry_sampler=lambda _config: sample(now=next(clock)),
                )

            self.assertTrue(plan["admitted"])
            self.assertEqual(plan["sampled_at"], 1000.0)

    def test_child_plan_must_match_atomically_persisted_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            cfg = config(root)
            plan = build_resource_launch_plan(
                cfg,
                video,
                stage="transcription",
                now=1000.0,
                telemetry_sampler=lambda _config: sample(),
            )
            altered = dict(plan)
            altered["decision_id"] = "f" * 32

            with self.assertRaisesRegex(ValueError, "decision identity mismatch"):
                parse_authorized_resource_launch_plan(
                    cfg,
                    serialize_launch_plan(altered),
                    video,
                    now=1001.0,
                )

    def test_pressure_defers_without_fabricating_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            plan = build_resource_launch_plan(
                config(root), video, stage="transcription", now=1000.0,
                telemetry_sampler=lambda _config: sample(cpu=99.0),
            )
            self.assertFalse(plan["admitted"])
            self.assertIn("cpu_critical", plan["reason_codes"])
            self.assertGreater(plan["retry_at"], 1000.0)

    def test_video_drift_or_expiry_rejects_child_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            plan = build_resource_launch_plan(
                config(root), video, stage="transcription", now=1000.0,
                telemetry_sampler=lambda _config: sample(),
            )
            encoded = serialize_launch_plan(plan)
            with self.assertRaises(ValueError):
                parse_resource_launch_plan(encoded, video, now=1061.0)
            video.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                parse_resource_launch_plan(encoded, video, now=1001.0)

    def test_oom_cooldown_selects_lower_memory_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            cfg = config(root)
            record_resource_oom(cfg, video, "CUDA OOM", now=999.0)
            plan = build_resource_launch_plan(
                cfg, video, stage="transcription", now=1000.0,
                telemetry_sampler=lambda _config: sample(),
            )
            self.assertTrue(plan["admitted"])
            self.assertEqual(plan["selected_route"]["compute_type"], "int8_float16")
            self.assertLess(plan["effective"]["batch_size"], cfg.batch_size)
            state = read_resource_admission_state(cfg, now=1000.0)
            self.assertEqual(state["last_oom"]["reason_code"], "transient_oom")
            self.assertEqual(
                state["last_oom"]["retry_strategy"],
                "lower_memory_same_pipeline",
            )


if __name__ == "__main__":
    unittest.main()
