from __future__ import annotations

import logging
import unittest

from gpu_memory import adaptive_whisper_batch_size, cuda_free_memory_mib, is_cuda_oom
from transcriber_whisperx import _transcribe_with_oom_backoff


class _FakeCuda:
    def __init__(self, free_mib: int = 12_000) -> None:
        self.free_mib = free_mib
        self.empty_cache_calls = 0

    def is_available(self) -> bool:
        return True

    def mem_get_info(self):
        return self.free_mib * 1024 * 1024, 12_288 * 1024 * 1024

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


class _FakeTorch:
    def __init__(self, free_mib: int = 12_000) -> None:
        self.cuda = _FakeCuda(free_mib)


class _OomThenSuccessModel:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def transcribe(self, _audio, *, batch_size: int, language: str, task: str):
        self.batch_sizes.append(batch_size)
        if batch_size > 2:
            raise RuntimeError("CUDA out of memory")
        return {"segments": [], "language": language, "task": task}


class GpuMemoryTests(unittest.TestCase):
    def test_reads_free_cuda_memory_without_allocation(self) -> None:
        self.assertEqual(cuda_free_memory_mib(_FakeTorch(11_500)), 11_500)

    def test_large_v3_on_12gb_gpu_caps_batch_conservatively(self) -> None:
        self.assertEqual(
            adaptive_whisper_batch_size(
                16,
                free_memory_mib=12_000,
                model_name="Systran/faster-whisper-large-v3",
                reserve_memory_mib=2048,
            ),
            8,
        )

    def test_low_free_memory_falls_back_to_single_item(self) -> None:
        self.assertEqual(
            adaptive_whisper_batch_size(
                16,
                free_memory_mib=2500,
                model_name="large-v3",
                reserve_memory_mib=2048,
            ),
            1,
        )

    def test_unknown_memory_preserves_configured_batch(self) -> None:
        self.assertEqual(
            adaptive_whisper_batch_size(
                6,
                free_memory_mib=None,
                model_name="large-v3",
            ),
            6,
        )

    def test_cuda_oom_detection_is_specific(self) -> None:
        self.assertTrue(is_cuda_oom(RuntimeError("CUDA out of memory")))
        self.assertTrue(is_cuda_oom(RuntimeError("CUDA failed with error out of memory")))
        self.assertFalse(is_cuda_oom(RuntimeError("network timeout")))

    def test_whisperx_retries_cuda_oom_by_halving_batch(self) -> None:
        model = _OomThenSuccessModel()
        torch_module = _FakeTorch()
        result = _transcribe_with_oom_backoff(
            model,
            object(),
            batch_size=8,
            language="ja",
            task="transcribe",
            torch_module=torch_module,
            logger=logging.getLogger("test"),
        )

        self.assertEqual(model.batch_sizes, [8, 4, 2])
        self.assertEqual(torch_module.cuda.empty_cache_calls, 2)
        self.assertEqual(result["language"], "ja")


if __name__ == "__main__":
    unittest.main()
