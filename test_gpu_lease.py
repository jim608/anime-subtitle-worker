from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest import mock

import gpu_lease
from gpu_lease import (
    GPU_LEASE_CONTRACT,
    GPU_LEASE_SCHEMA_VERSION,
    GpuKernelLease,
    GpuLeasePrimitiveUnavailable,
    read_gpu_lease_state,
)


_GPU_IDENTITY = "GPU-test-00000000-0000-0000-0000-000000000001"
_PROCESS_TIMEOUT_SECONDS = 30.0


def _hold_lease(
    lock_root: str,
    gpu_identity: str,
    ready: multiprocessing.synchronize.Event,
    stop: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Own a lease until stopped; deliberately usable with spawn or fork."""

    lease = GpuKernelLease(lock_root, gpu_identity, timeout_seconds=2.0)
    try:
        acquired = lease.acquire()
        result.put(
            {
                "acquired": acquired,
                "token": lease.token,
                "pid": os.getpid(),
            }
        )
        ready.set()
        if acquired:
            stop.wait(30.0)
    except BaseException as exc:
        result.put({"error": f"{type(exc).__name__}: {exc}"})
        ready.set()
        raise
    finally:
        if lease.acquired:
            lease.release()


def _try_lease_once(
    lock_root: str,
    gpu_identity: str,
    timeout_seconds: float,
    result: multiprocessing.queues.Queue,
) -> None:
    lease = GpuKernelLease(
        lock_root,
        gpu_identity,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=0.02,
    )
    started = time.monotonic()
    try:
        acquired = lease.acquire()
        elapsed = time.monotonic() - started
        token = lease.token
        if acquired:
            lease.release()
        result.put(
            {
                "acquired": acquired,
                "elapsed": elapsed,
                "token": token,
                "pid": os.getpid(),
            }
        )
    except BaseException as exc:
        result.put({"error": f"{type(exc).__name__}: {exc}"})
        raise


def _fork_child_wait(
    ready: multiprocessing.synchronize.Event,
    stop: multiprocessing.synchronize.Event,
) -> None:
    # The module's after-fork hook must already have closed any copied lease
    # descriptor before this target begins executing.
    ready.set()
    stop.wait(30.0)


class GpuKernelLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="gpu-lease-test-")
        self.lock_root = Path(self._temporary_directory.name) / "locks"
        self.spawn = multiprocessing.get_context("spawn")
        self._processes: list[multiprocessing.Process] = []
        self._queues: list[multiprocessing.queues.Queue] = []

    def tearDown(self) -> None:
        for process in self._processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        for result in self._queues:
            result.close()
            result.join_thread()
        self._temporary_directory.cleanup()

    def _new_queue(self, context: multiprocessing.context.BaseContext | None = None):
        selected = context or self.spawn
        result = selected.Queue()
        self._queues.append(result)
        return result

    def _start_holder(self):
        ready = self.spawn.Event()
        stop = self.spawn.Event()
        result = self._new_queue()
        process = self.spawn.Process(
            target=_hold_lease,
            args=(str(self.lock_root), _GPU_IDENTITY, ready, stop, result),
        )
        self._processes.append(process)
        process.start()
        self.assertTrue(ready.wait(_PROCESS_TIMEOUT_SECONDS), "holder did not report readiness")
        message = self._queue_get(result)
        self.assertNotIn("error", message)
        self.assertTrue(message["acquired"])
        self.assertIsInstance(message["token"], str)
        return process, stop, message

    def _run_contender(self, timeout_seconds: float) -> dict[str, object]:
        result = self._new_queue()
        process = self.spawn.Process(
            target=_try_lease_once,
            args=(str(self.lock_root), _GPU_IDENTITY, timeout_seconds, result),
        )
        self._processes.append(process)
        process.start()
        process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        self.assertFalse(process.is_alive(), "contender did not exit within the test bound")
        self.assertEqual(process.exitcode, 0)
        message = self._queue_get(result)
        self.assertNotIn("error", message)
        return message

    def _queue_get(self, result) -> dict[str, object]:
        try:
            return result.get(timeout=_PROCESS_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self.fail(f"child process produced no result: {exc}")

    def _stop_holder(self, process: multiprocessing.Process, stop) -> None:
        stop.set()
        process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        self.assertFalse(process.is_alive(), "holder did not release and exit")
        self.assertEqual(process.exitcode, 0)

    def test_true_multiprocess_contention_and_nonblocking_acquire(self) -> None:
        holder, stop, holder_message = self._start_holder()

        contender = self._run_contender(0.0)

        self.assertFalse(contender["acquired"])
        self.assertIsNone(contender["token"])
        self.assertNotEqual(holder_message["pid"], contender["pid"])
        self.assertLess(contender["elapsed"], 1.0)
        self._stop_holder(holder, stop)

    def test_bounded_wait_expires_without_stealing_live_lock(self) -> None:
        holder, stop, _ = self._start_holder()

        contender = self._run_contender(0.25)

        self.assertFalse(contender["acquired"])
        self.assertGreaterEqual(contender["elapsed"], 0.20)
        self.assertLess(contender["elapsed"], 2.0)
        self._stop_holder(holder, stop)

    def test_process_crash_releases_kernel_lock_and_lockfile_persists(self) -> None:
        holder, _stop, _ = self._start_holder()
        paths = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(paths.lock_path.is_file())

        holder.terminate()
        holder.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        self.assertFalse(holder.is_alive())
        self.assertNotEqual(holder.exitcode, 0)

        recovered = GpuKernelLease(self.lock_root, _GPU_IDENTITY, timeout_seconds=2.0)
        self.assertTrue(recovered.acquire())
        self.assertTrue(paths.lock_path.is_file())
        self.assertTrue(recovered.release())
        self.assertTrue(paths.lock_path.is_file())

    def test_stale_state_can_neither_steal_nor_block_the_kernel_lock(self) -> None:
        holder, stop, _ = self._start_holder()
        paths = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        stale_released = {
            "schema_version": GPU_LEASE_SCHEMA_VERSION,
            "contract": GPU_LEASE_CONTRACT,
            "gpu_identity": _GPU_IDENTITY,
            "lock_path": str(paths.lock_path),
            "lease_token": "0" * 32,
            "owner_pid": 99999999,
            "status": "released",
            "phase": "stale",
            "acquired_at": 1.0,
            "updated_at": 1.0,
        }
        paths.state_path.write_text(
            json.dumps(stale_released, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self.assertFalse(self._run_contender(0.0)["acquired"])
        self._stop_holder(holder, stop)

        stale_held = dict(stale_released, status="held")
        paths.state_path.write_text(
            json.dumps(stale_held, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        recovered = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(recovered.acquire())
        self.assertTrue(recovered.release())

    def test_same_process_instances_are_nonreentrant_and_nonowner_cannot_release(self) -> None:
        owner = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        nonowner = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(owner.acquire())
        owner_token = owner.token

        self.assertFalse(nonowner.acquire())
        self.assertFalse(nonowner.release())
        self.assertEqual(owner.token, owner_token)
        self.assertFalse(self._run_contender(0.0)["acquired"])

        self.assertTrue(owner.release())
        self.assertFalse(owner.release())
        self.assertTrue(nonowner.acquire())
        self.assertTrue(nonowner.release())

    def test_context_manager_releases_in_finally_path(self) -> None:
        lease = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with lease as acquired:
                self.assertIs(acquired, lease)
                self.assertTrue(lease.acquired)
                raise RuntimeError("test failure")

        successor = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(successor.acquire())
        self.assertTrue(successor.release())

    def test_state_is_observational_and_tracks_owner_token(self) -> None:
        lease = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(lease.acquire())
        token = lease.token

        held = read_gpu_lease_state(lease.state_path)
        self.assertIsNotNone(held)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["lease_token"], token)
        self.assertTrue(lease.heartbeat(phase="model-loaded"))
        heartbeat = read_gpu_lease_state(lease.state_path)
        self.assertEqual(heartbeat["phase"], "model-loaded")
        self.assertEqual(heartbeat["lease_token"], token)

        self.assertTrue(lease.release())
        released = read_gpu_lease_state(lease.state_path)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["lease_token"], token)
        self.assertTrue(lease.lock_path.is_file())

        lease.state_path.write_text("{not-json", encoding="utf-8")
        self.assertIsNone(read_gpu_lease_state(lease.state_path))

    def test_state_publication_failure_does_not_change_lock_authority(self) -> None:
        lease = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        with mock.patch.object(gpu_lease, "atomic_write_text", side_effect=OSError("disk unavailable")):
            self.assertTrue(lease.acquire())
            self.assertIn("disk unavailable", lease.state_error)
            self.assertFalse(self._run_contender(0.0)["acquired"])
            self.assertTrue(lease.release())

        successor = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(successor.acquire())
        self.assertTrue(successor.release())

    def test_missing_platform_primitive_fails_closed_without_registry_leak(self) -> None:
        primitive_name = "msvcrt" if os.name == "nt" else "fcntl"
        with mock.patch.object(gpu_lease, primitive_name, None):
            lease = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
            with self.assertRaises(GpuLeasePrimitiveUnavailable):
                lease.acquire()

        successor = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(successor.acquire())
        self.assertTrue(successor.release())

    @unittest.skipUnless(hasattr(os, "fork"), "fork descriptor inheritance is POSIX-only")
    def test_fork_child_does_not_keep_parent_lease_alive(self) -> None:
        owner = GpuKernelLease(self.lock_root, _GPU_IDENTITY)
        self.assertTrue(owner.acquire())
        fork = multiprocessing.get_context("fork")
        ready = fork.Event()
        stop = fork.Event()
        child = fork.Process(target=_fork_child_wait, args=(ready, stop))
        self._processes.append(child)
        child.start()
        self.assertTrue(ready.wait(_PROCESS_TIMEOUT_SECONDS))

        self.assertTrue(owner.release())
        # The child is deliberately still alive.  If its inherited descriptor
        # remained open, this independent spawned contender could not acquire.
        self.assertTrue(self._run_contender(1.0)["acquired"])
        stop.set()
        child.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        self.assertEqual(child.exitcode, 0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
