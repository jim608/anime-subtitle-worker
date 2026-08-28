from __future__ import annotations

import json
import math
import unittest

from resource_admission import (
    AdmissionHysteresisState,
    ModelMemoryProfile,
    ResourceAdmissionConfig,
    ResourceTelemetry,
    decide_resource_admission,
    resource_admission_config_from_mapping,
    validate_resource_admission_config,
)


PRIMARY = ModelMemoryProfile("large-v3", "float16", 8_500)
FALLBACK_INT8 = ModelMemoryProfile("large-v3", "int8_float16", 6_200)
FALLBACK_SMALL = ModelMemoryProfile("medium", "int8", 3_800)


def healthy_3060(**changes) -> ResourceTelemetry:
    values = {
        "cpu_percent": 35.0,
        "ram_available_mib": 16_000.0,
        "ram_total_mib": 32_000.0,
        "gpu_util_percent": 10.0,
        "vram_free_mib": 11_500.0,
        "vram_total_mib": 12_288.0,
        "age_seconds": 1.0,
        "available": True,
    }
    values.update(changes)
    return ResourceTelemetry(**values)


def decide(snapshot: ResourceTelemetry, **changes):
    values = {
        "job_stage": "transcription",
        "primary_model": PRIMARY,
        "fallback_models": (FALLBACK_INT8, FALLBACK_SMALL),
    }
    values.update(changes)
    return decide_resource_admission(snapshot, **values)


class ResourceAdmissionTableTests(unittest.TestCase):
    def test_rtx3060_12g_selects_primary_and_never_claims_multiple_lanes(self) -> None:
        decision = decide(healthy_3060())

        self.assertEqual(decision.tier, "green")
        self.assertTrue(decision.allow_new_job)
        self.assertEqual(decision.asr_model, "large-v3")
        self.assertEqual(decision.asr_compute_type, "float16")
        self.assertEqual(decision.concurrency_limit, 1)
        self.assertEqual(decision.retry_after_seconds, 0)

    def test_vram_routes_to_lower_memory_profile_before_deferring(self) -> None:
        cases = (
            (7_600.0, "large-v3", "int8_float16", True),
            (5_100.0, "medium", "int8", True),
            (4_000.0, None, None, False),
        )
        for free_mib, model, compute_type, allowed in cases:
            with self.subTest(free_mib=free_mib):
                decision = decide(healthy_3060(vram_free_mib=free_mib))
                self.assertEqual(decision.asr_model, model)
                self.assertEqual(decision.asr_compute_type, compute_type)
                self.assertEqual(decision.allow_new_job, allowed)
                if allowed:
                    self.assertEqual(decision.tier, "yellow")
                    self.assertIn("lower_memory_fallback_selected", decision.reason_codes)
                else:
                    self.assertEqual(decision.tier, "red")
                    self.assertIn("vram_no_model_route_fits", decision.reason_codes)

    def test_recent_oom_bypasses_primary_and_chooses_lower_memory_route(self) -> None:
        decision = decide(healthy_3060(), recent_oom=True)

        self.assertEqual(decision.tier, "yellow")
        self.assertTrue(decision.allow_new_job)
        self.assertEqual(decision.asr_compute_type, "int8_float16")
        self.assertIn("recent_oom", decision.reason_codes)
        self.assertIn("primary_route_bypassed_after_oom", decision.reason_codes)

    def test_recent_oom_without_lower_memory_route_defers(self) -> None:
        decision = decide(
            healthy_3060(),
            recent_oom=True,
            fallback_models=(ModelMemoryProfile("other-large", "float16", 9_000),),
        )

        self.assertEqual(decision.tier, "red")
        self.assertFalse(decision.allow_new_job)
        self.assertIsNone(decision.asr_model)
        self.assertIn("non_lower_memory_fallback_ignored", decision.reason_codes)

    def test_cpu_and_ram_pressure_delay_new_work_without_killing_running(self) -> None:
        cases = (
            (healthy_3060(cpu_percent=82), "yellow", "cpu_pressure"),
            (healthy_3060(cpu_percent=97), "red", "cpu_critical"),
            (healthy_3060(ram_available_mib=5_000), "yellow", "ram_pressure"),
            (healthy_3060(ram_available_mib=2_000), "red", "ram_critical"),
        )
        for snapshot, tier, reason in cases:
            with self.subTest(reason=reason):
                decision = decide(snapshot, running_gpu_jobs=1)
                self.assertEqual(decision.tier, tier)
                self.assertFalse(decision.allow_new_job)
                self.assertTrue(decision.allow_running_job)
                self.assertIn(reason, decision.reason_codes)
                self.assertIn("running_job_drain_only", decision.reason_codes)

    def test_gpu_pressure_delays_new_gpu_work(self) -> None:
        yellow = decide(healthy_3060(gpu_util_percent=90))
        red = decide(healthy_3060(gpu_util_percent=99))

        self.assertEqual(yellow.tier, "yellow")
        self.assertFalse(yellow.allow_new_job)
        self.assertIn("gpu_busy", yellow.reason_codes)
        self.assertEqual(red.tier, "red")
        self.assertFalse(red.allow_new_job)
        self.assertIn("gpu_saturated", red.reason_codes)

    def test_stale_or_unavailable_telemetry_never_means_zero_load(self) -> None:
        stale = decide(healthy_3060(age_seconds=16), running_gpu_jobs=1)
        unavailable = decide(
            ResourceTelemetry(None, None, None, None, None, None, None, available=False),
            running_gpu_jobs=1,
        )

        for decision, reason in (
            (stale, "telemetry_stale"),
            (unavailable, "telemetry_unavailable"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(decision.tier, "unavailable")
                self.assertFalse(decision.allow_new_job)
                self.assertTrue(decision.allow_running_job)
                self.assertEqual(decision.concurrency_limit, 1)
                self.assertEqual(decision.diagnostics["running_jobs_allowed_to_drain"], 1)
                self.assertIn(reason, decision.reason_codes)

    def test_single_lane_busy_blocks_new_claim(self) -> None:
        decision = decide(healthy_3060(), running_gpu_jobs=1)

        self.assertEqual(decision.tier, "green")
        self.assertFalse(decision.allow_new_job)
        self.assertTrue(decision.allow_running_job)
        self.assertIn("gpu_single_lane_busy", decision.reason_codes)

    def test_policy_violation_never_expands_concurrency(self) -> None:
        decision = decide(healthy_3060(), running_gpu_jobs=2)

        self.assertFalse(decision.allow_new_job)
        self.assertFalse(decision.allow_running_job)
        self.assertEqual(decision.concurrency_limit, 1)
        self.assertEqual(decision.diagnostics["running_jobs_allowed_to_drain"], 1)
        self.assertIn("gpu_concurrency_policy_violation", decision.reason_codes)


class InvalidInputTests(unittest.TestCase):
    def test_nan_negative_and_impossible_metrics_fail_closed(self) -> None:
        cases = (
            (healthy_3060(cpu_percent=math.nan), "invalid_cpu_percent"),
            (healthy_3060(cpu_percent=-1), "invalid_cpu_percent"),
            (healthy_3060(gpu_util_percent=101), "invalid_gpu_util_percent"),
            (healthy_3060(ram_available_mib=-1), "invalid_ram_available_mib"),
            (healthy_3060(ram_available_mib=40_000), "invalid_ram_available_mib"),
            (healthy_3060(vram_free_mib=-1), "invalid_vram_free_mib"),
            (healthy_3060(vram_free_mib=13_000), "invalid_vram_free_mib"),
            (healthy_3060(age_seconds=-1), "invalid_telemetry_age"),
        )
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                decision = decide(snapshot)
                self.assertEqual(decision.tier, "unavailable")
                self.assertFalse(decision.allow_new_job)
                self.assertIn(reason, decision.reason_codes)
                json.dumps(decision.to_dict(), allow_nan=False)

    def test_bad_model_profile_fails_closed(self) -> None:
        decision = decide(
            healthy_3060(),
            primary_model=ModelMemoryProfile("large-v3", "float16", math.nan),
        )

        self.assertEqual(decision.tier, "unavailable")
        self.assertFalse(decision.allow_new_job)
        self.assertIn("invalid_primary_required_vram_mib", decision.reason_codes)

    def test_non_telemetry_object_fails_closed_instead_of_raising(self) -> None:
        decision = decide_resource_admission(  # type: ignore[arg-type]
            {"cpu_percent": 0},
            job_stage="transcription",
            primary_model=PRIMARY,
        )

        self.assertEqual(decision.tier, "unavailable")
        self.assertFalse(decision.allow_new_job)
        self.assertIn("invalid_telemetry_object", decision.reason_codes)

    def test_unknown_stage_fails_closed(self) -> None:
        decision = decide(healthy_3060(), job_stage="mystery")

        self.assertEqual(decision.tier, "unavailable")
        self.assertFalse(decision.allow_new_job)
        self.assertIn("invalid_job_stage", decision.reason_codes)

    def test_config_validation_rejects_multi_lane_or_inverted_thresholds(self) -> None:
        bad_configs = (
            ResourceAdmissionConfig(gpu_concurrency_limit=2),
            ResourceAdmissionConfig(cpu_yellow_percent=98, cpu_red_percent=95),
            ResourceAdmissionConfig(ram_yellow_available_ratio=0.05, ram_red_available_ratio=0.08),
            ResourceAdmissionConfig(recovery_samples=0),
        )
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_resource_admission_config(config)

        with self.assertRaises(ValueError):
            resource_admission_config_from_mapping({"unknown_key": 1})
        with self.assertRaises(ValueError):
            resource_admission_config_from_mapping({"cpu_yellow_percent": "busy"})

    def test_plain_mapping_config_is_validated(self) -> None:
        config = resource_admission_config_from_mapping(
            {"telemetry_stale_after_seconds": 10.0, "recovery_samples": 2}
        )
        self.assertEqual(config.telemetry_stale_after_seconds, 10.0)
        self.assertEqual(config.recovery_samples, 2)


class HysteresisTests(unittest.TestCase):
    def test_worsening_is_immediate_but_recovery_needs_consecutive_samples(self) -> None:
        config = ResourceAdmissionConfig(recovery_samples=3)
        green = decide(healthy_3060(), config=config)
        red = decide(
            healthy_3060(cpu_percent=99),
            previous_state=green.hysteresis_state,
            config=config,
        )
        self.assertEqual(red.tier, "red")

        states = []
        previous = red.hysteresis_state
        for _ in range(3):
            result = decide(healthy_3060(), previous_state=previous, config=config)
            states.append(result)
            previous = result.hysteresis_state

        self.assertEqual([item.tier for item in states], ["red", "red", "green"])
        self.assertFalse(states[0].allow_new_job)
        self.assertFalse(states[1].allow_new_job)
        self.assertTrue(states[2].allow_new_job)
        self.assertIn("hysteresis_recovery_pending", states[0].reason_codes)

    def test_flapping_recovery_samples_reset(self) -> None:
        config = ResourceAdmissionConfig(recovery_samples=2)
        red_state = AdmissionHysteresisState("red")
        first_green = decide(healthy_3060(), previous_state=red_state, config=config)
        yellow = decide(
            healthy_3060(cpu_percent=85),
            previous_state=first_green.hysteresis_state,
            config=config,
        )
        next_green = decide(
            healthy_3060(),
            previous_state=yellow.hysteresis_state,
            config=config,
        )

        self.assertEqual(first_green.hysteresis_state.consecutive_samples, 1)
        self.assertEqual(yellow.tier, "red")
        self.assertEqual(yellow.hysteresis_state.candidate_tier, "yellow")
        self.assertEqual(next_green.tier, "red")
        self.assertEqual(next_green.hysteresis_state.consecutive_samples, 1)

    def test_stale_to_healthy_recovery_is_hysteretic_and_running_job_drains(self) -> None:
        config = ResourceAdmissionConfig(recovery_samples=2)
        stale = decide(healthy_3060(age_seconds=99), running_gpu_jobs=1, config=config)
        first = decide(
            healthy_3060(),
            running_gpu_jobs=1,
            previous_state=stale.hysteresis_state,
            config=config,
        )
        second = decide(
            healthy_3060(),
            running_gpu_jobs=0,
            previous_state=first.hysteresis_state,
            config=config,
        )

        self.assertEqual(stale.tier, "unavailable")
        self.assertTrue(stale.allow_running_job)
        self.assertEqual(first.tier, "unavailable")
        self.assertFalse(first.allow_new_job)
        self.assertEqual(second.tier, "green")
        self.assertTrue(second.allow_new_job)


class CpuStageTests(unittest.TestCase):
    def test_cpu_only_stage_needs_no_asr_profile_and_serializes(self) -> None:
        decision = decide_resource_admission(
            healthy_3060(),
            job_stage="publication",
            primary_model=None,
        )

        self.assertEqual(decision.tier, "green")
        self.assertTrue(decision.allow_new_job)
        self.assertIsNone(decision.asr_model)
        self.assertIsNone(decision.asr_compute_type)
        encoded = json.dumps(decision.to_dict(), sort_keys=True, allow_nan=False)
        self.assertIn('"concurrency_limit": 1', encoded)


if __name__ == "__main__":
    unittest.main()
