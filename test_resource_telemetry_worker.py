from __future__ import annotations

import json
import math
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from resource_admission import ResourceTelemetry
from resource_telemetry_worker import (
    HostResourceReading,
    NVIDIA_SMI_QUERY,
    WorkerResourceTelemetrySample,
    WorkerTelemetryConfig,
    _sample_host_resources,
    sample_worker_resource_telemetry,
    validate_worker_telemetry_config,
)


HOST = HostResourceReading(cpu_percent=25.0, ram_available_mib=16_000, ram_total_mib=32_000)


def completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def sample_with_gpu(result, **changes):
    observed: list[tuple[tuple[str, ...], float]] = []

    def runner(command, timeout):
        observed.append((tuple(command), timeout))
        if isinstance(result, BaseException):
            raise result
        return result

    values = {
        "clock": lambda: 1_000.0,
        "host_sampler": lambda _config: HOST,
        "gpu_runner": runner,
    }
    values.update(changes)
    return sample_worker_resource_telemetry(**values), observed


class SuccessfulSamplingTests(unittest.TestCase):
    def test_fixed_query_produces_admission_telemetry(self) -> None:
        sample, observed = sample_with_gpu(completed("12, 11000, 12288\n"))

        self.assertTrue(sample.available)
        self.assertEqual(observed, [(NVIDIA_SMI_QUERY, 2.0)])
        admission = sample.to_resource_telemetry(now_epoch_seconds=1_002.5)
        self.assertIsInstance(admission, ResourceTelemetry)
        self.assertEqual(admission.cpu_percent, 25.0)
        self.assertEqual(admission.ram_available_mib, 16_000.0)
        self.assertEqual(admission.gpu_util_percent, 12.0)
        self.assertEqual(admission.vram_free_mib, 11_000.0)
        self.assertEqual(admission.vram_total_mib, 12_288.0)
        self.assertEqual(admission.age_seconds, 2.5)
        self.assertTrue(admission.available)

    def test_serialized_payload_is_json_safe_and_has_no_process_data(self) -> None:
        sample, _ = sample_with_gpu(completed("12.5, 11000.25, 12288\n"))

        payload = sample.to_dict(now_epoch_seconds=1_001)
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False).casefold()
        self.assertNotIn("pid", encoded)
        self.assertNotIn("cmdline", encoded)
        self.assertNotIn("command", encoded)
        self.assertEqual(payload["error_codes"], [])

    def test_sample_age_clock_regression_fails_closed(self) -> None:
        sample, _ = sample_with_gpu(completed("12, 11000, 12288\n"))
        admission = sample.to_resource_telemetry(now_epoch_seconds=999)

        self.assertFalse(admission.available)
        self.assertIsNone(admission.age_seconds)


class FailureSamplingTests(unittest.TestCase):
    def test_gpu_timeout_tool_missing_and_nonzero_exit_are_unavailable(self) -> None:
        cases = (
            (
                subprocess.TimeoutExpired(cmd=list(NVIDIA_SMI_QUERY), timeout=2),
                "gpu_telemetry_timeout",
            ),
            (FileNotFoundError("nvidia-smi"), "gpu_tool_unavailable"),
            (completed("", returncode=7, stderr="secret driver message"), "gpu_telemetry_unavailable"),
        )
        for result, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                sample, _ = sample_with_gpu(result)
                self.assertFalse(sample.available)
                self.assertEqual(sample.error_codes, (expected_reason,))
                self.assertIsNone(sample.cpu_percent)
                self.assertIsNone(sample.vram_free_mib)
                serialized = json.dumps(sample.to_dict(now_epoch_seconds=1_001))
                self.assertNotIn("secret driver message", serialized)

    def test_malformed_gpu_rows_fail_closed(self) -> None:
        rows = (
            "",
            "10, 1000",
            "10, 1000, 12000, extra",
            "10, 1000, 12000\n20, 2000, 12000",
            "NaN, 1000, 12000",
            "-1, 1000, 12000",
            "101, 1000, 12000",
            "10, -1, 12000",
            "10, 13000, 12000",
            "10, 1000, 0",
        )
        for row in rows:
            with self.subTest(row=row):
                sample, _ = sample_with_gpu(completed(row))
                self.assertFalse(sample.available)
                self.assertEqual(sample.error_codes, ("gpu_telemetry_unavailable",))

    def test_invalid_host_values_fail_closed_and_discard_valid_gpu_partial(self) -> None:
        invalid_hosts = (
            HostResourceReading(math.nan, 10_000, 20_000),
            HostResourceReading(-1, 10_000, 20_000),
            HostResourceReading(10, -1, 20_000),
            HostResourceReading(10, 30_000, 20_000),
            HostResourceReading(10, 10_000, 0),
        )
        for host in invalid_hosts:
            with self.subTest(host=host):
                sample, _ = sample_with_gpu(
                    completed("10, 10000, 12288"),
                    host_sampler=lambda _config, value=host: value,
                )
                self.assertFalse(sample.available)
                self.assertIn("host_telemetry_unavailable", sample.error_codes)
                self.assertIsNone(sample.gpu_util_percent)

    def test_host_sampler_hard_timeout(self) -> None:
        config = WorkerTelemetryConfig(
            host_timeout_seconds=0.03,
            gpu_timeout_seconds=0.2,
            cpu_sample_interval_seconds=0.01,
        )

        def slow_host(_config):
            time.sleep(0.2)
            return HOST

        started = time.monotonic()
        sample, _ = sample_with_gpu(
            completed("10, 10000, 12288"),
            config=config,
            host_sampler=slow_host,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertFalse(sample.available)
        self.assertIn("host_telemetry_timeout", sample.error_codes)

    def test_gpu_runner_that_ignores_timeout_is_still_bounded(self) -> None:
        config = WorkerTelemetryConfig(
            host_timeout_seconds=0.2,
            gpu_timeout_seconds=0.03,
            cpu_sample_interval_seconds=0.01,
        )

        def slow_gpu(_command, _timeout):
            time.sleep(0.2)
            return completed("10, 10000, 12288")

        started = time.monotonic()
        sample = sample_worker_resource_telemetry(
            config,
            clock=lambda: 1_000,
            host_sampler=lambda _config: HOST,
            gpu_runner=slow_gpu,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertFalse(sample.available)
        self.assertEqual(sample.error_codes, ("gpu_telemetry_timeout",))


class HostProviderTests(unittest.TestCase):
    def test_psutil_is_optional_and_safe_fallback_is_used(self) -> None:
        expected = HostResourceReading(30, 8_000, 16_000)
        config = WorkerTelemetryConfig()

        with patch("resource_telemetry_worker._load_optional_psutil", return_value=None), patch(
            "resource_telemetry_worker.os.name", "nt"
        ), patch(
            "resource_telemetry_worker._sample_windows_host_resources",
            return_value=expected,
        ) as fallback:
            actual = _sample_host_resources(config)

        self.assertEqual(actual, expected)
        fallback.assert_called_once_with(interval_seconds=config.cpu_sample_interval_seconds)

    def test_psutil_provider_uses_only_aggregate_cpu_and_memory(self) -> None:
        class FakePsutil:
            cpu_intervals: list[float] = []

            @classmethod
            def cpu_percent(cls, *, interval):
                cls.cpu_intervals.append(interval)
                return 42.0

            @staticmethod
            def virtual_memory():
                return SimpleNamespace(available=8_000 * 1024 * 1024, total=16_000 * 1024 * 1024)

        with patch("resource_telemetry_worker._load_optional_psutil", return_value=FakePsutil):
            reading = _sample_host_resources(WorkerTelemetryConfig(cpu_sample_interval_seconds=0.05))

        self.assertEqual(reading, HostResourceReading(42, 8_000, 16_000))
        self.assertEqual(FakePsutil.cpu_intervals, [0.05])


class ConfigurationTests(unittest.TestCase):
    def test_invalid_timeout_configuration_is_rejected(self) -> None:
        cases = (
            WorkerTelemetryConfig(gpu_timeout_seconds=0),
            WorkerTelemetryConfig(host_timeout_seconds=math.nan),
            WorkerTelemetryConfig(host_timeout_seconds=0.1, cpu_sample_interval_seconds=0.1),
        )
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_worker_telemetry_config(config)

    def test_unavailable_sample_converts_without_inventing_zeroes(self) -> None:
        sample = WorkerResourceTelemetrySample(
            None,
            None,
            None,
            None,
            None,
            None,
            sampled_at_epoch_seconds=100,
            available=False,
            error_codes=("gpu_tool_unavailable",),
        )
        telemetry = sample.to_resource_telemetry(now_epoch_seconds=105)

        self.assertFalse(telemetry.available)
        self.assertEqual(telemetry.age_seconds, 5)
        self.assertIsNone(telemetry.cpu_percent)
        self.assertIsNone(telemetry.gpu_util_percent)


if __name__ == "__main__":
    unittest.main()
