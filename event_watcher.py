from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import logging
import math
import os
import sqlite3
import stat as stat_module
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from scan_state import SQLITE_BUSY_TIMEOUT_SECONDS, ScanStateStore, is_scan_state_transient_error
from video_policy import is_standalone_theme_video


EVENT_DEBOUNCE_SECONDS = 3.0
EVENT_RETRY_SECONDS = 15.0
EVENT_STABILITY_INTERVAL_SECONDS = 2.0
EVENT_QUIET_WINDOW_SECONDS = 5.0
EVENT_STABLE_OBSERVATIONS_REQUIRED = 2
BUSY_LOG_INTERVAL_SECONDS = 60.0
SUCCESS_LOG_INTERVAL_SECONDS = 300.0
PROBE_FAILURE_EVENT_PREFIX = "media_probe_failed_attempt_"
PROBE_EXHAUSTED_EVENT_PREFIX = "media_probe_exhausted_attempt_"
EVENT_QUEUE_STARTUP_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_SECONDS + 5.0
QUEUEABLE_EVENT_TYPES = frozenset({"created", "modified", "moved", "closed"})
INCOMPLETE_VIDEO_STEM_SUFFIXES = (
    ".part",
    ".partial",
    ".tmp",
    ".temp",
    ".download",
    ".crdownload",
    ".incomplete",
    ".filepart",
    ".aria2",
    ".!qb",
)
INCOMPLETE_VIDEO_DIRECTORY_NAMES = frozenset(
    {"@eadir", "#recycle", ".recycle", ".snapshot", ".stfolder", ".trash", ".trash-1000"}
)


@runtime_checkable
class _DurableIngestStore(Protocol):
    """Small compatibility boundary shared with the persistent Job Store."""

    def upsert_ingest_observation(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
        *,
        observed_at: float | None = None,
        event_type: str = "",
        state: str = "stabilizing",
    ) -> Mapping[str, Any]: ...

    def iter_pending_ingest_observations(self) -> Iterable[Mapping[str, Any]]: ...

    def clear_ingest_observation(self, path: Path) -> bool: ...


class _IngestStoreAdapter:
    """Duck-type the M0 API while retaining compatibility with older stores."""

    def __init__(self, state: Any) -> None:
        self.state = state

    @property
    def supported(self) -> bool:
        return bool(
            callable(getattr(self.state, "upsert_ingest_observation", None))
            or callable(getattr(self.state, "observe_ingest", None))
        )

    def observe(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
        *,
        observed_at: float,
        event_type: str,
        state: str,
    ) -> Mapping[str, Any] | None:
        method = getattr(self.state, "upsert_ingest_observation", None)
        if not callable(method):
            method = getattr(self.state, "observe_ingest", None)
        if not callable(method):
            return None
        try:
            return method(
                path,
                size,
                mtime_ns,
                observed_at=observed_at,
                event_type=event_type,
                state=state,
            )
        except TypeError as positional_error:
            try:
                return method(
                    path=path,
                    size=size,
                    mtime_ns=mtime_ns,
                    observed_at=observed_at,
                    event_type=event_type,
                    state=state,
                )
            except TypeError:
                try:
                    return method(path, size, mtime_ns)
                except TypeError:
                    raise positional_error

    def pending(self) -> list[Mapping[str, Any]]:
        method = getattr(self.state, "iter_pending_ingest_observations", None)
        if not callable(method):
            method = getattr(self.state, "list_pending_ingest_observations", None)
        if not callable(method):
            return []
        rows = method()
        try:
            return [row for row in rows if isinstance(row, Mapping)]
        except TypeError:
            return []

    def clear(self, path: Path) -> bool:
        method = getattr(self.state, "clear_ingest_observation", None)
        if not callable(method):
            return False
        try:
            return bool(method(path))
        except TypeError as positional_error:
            try:
                return bool(method(path=path))
            except TypeError:
                raise positional_error


@dataclass(frozen=True)
class _ObservedFile:
    size: int
    mtime_ns: int
    first_seen_at: float
    last_seen_at: float
    stable_observations: int
    event_type: str = ""
    close_observed: bool = False


@dataclass(frozen=True)
class _EventEvidence:
    last_event_at: float
    last_event_monotonic: float
    event_type: str
    close_observed: bool


@dataclass(frozen=True)
class _IngestDecision:
    path: Path
    observation: _ObservedFile
    ready_for_probe: bool


def start_event_watcher(config: Any, logger: logging.Logger):
    if not bool(getattr(config, "scanner_event_watch_enabled", False)):
        return None

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        logger.warning("Filesystem event watcher disabled because watchdog is not installed.")
        return None

    try:
        observer, event_queue = _create_event_watcher_components(
            config,
            logger,
            FileSystemEventHandler,
            Observer,
        )
    except Exception as exc:
        logger.warning("Filesystem event watcher could not start: %s", exc)
        return None
    return _RunningEventWatcher(
        observer,
        event_queue,
        config=config,
        logger=logger,
        base_handler_type=FileSystemEventHandler,
        observer_type=Observer,
    )


def _create_event_watcher_components(
    config: Any,
    logger: logging.Logger,
    base_handler_type: type,
    observer_type: type,
) -> tuple[Any, "_FilesystemEventQueue"]:
    input_path = Path(config.input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"input path does not exist: {input_path}")
    event_queue = _FilesystemEventQueue(config, logger)
    try:
        event_queue.start()
        handler = _QueueEventHandler(
            config,
            logger,
            base_handler_type,
            event_queue,
        )
        observer = observer_type()
        observer.schedule(handler, str(input_path), recursive=True)
        observer.daemon = True
        observer.start()
    except Exception:
        event_queue.stop()
        event_queue.join(2)
        raise
    logger.info("Filesystem event watcher started path=%s", input_path)
    return observer, event_queue


class _QueueEventHandler:
    def __init__(
        self,
        config: Any,
        logger: logging.Logger,
        base_handler_type: type,
        event_queue: "_FilesystemEventQueue",
    ) -> None:
        self.config = config
        self.logger = logger
        self.extensions = {str(extension).lower() for extension in config.video_extensions}
        self._delegate = base_handler_type()
        self._event_queue = event_queue

    def dispatch(self, event) -> None:
        self._delegate.dispatch(event)
        event_type = str(getattr(event, "event_type", "") or "").strip().casefold()
        # watchdog also emits opened/closed_no_write events whenever Jellyfin,
        # the scanner, or the AI worker merely reads a video. Treating those as
        # content changes creates a feedback loop that continually requeues the
        # whole library. Only events that can actually change file contents or
        # location are allowed to reach the AI queue.
        if event_type and event_type not in QUEUEABLE_EVENT_TYPES:
            return
        paths: list[str] = []
        src_path = getattr(event, "src_path", None)
        dest_path = getattr(event, "dest_path", None)
        if src_path:
            paths.append(str(src_path))
        if dest_path:
            paths.append(str(dest_path))
        for raw_path in paths:
            self._queue_candidate(
                Path(raw_path),
                is_directory=bool(getattr(event, "is_directory", False)),
                event_type=event_type,
            )

    def _queue_candidate(self, path: Path, *, is_directory: bool, event_type: str = "") -> None:
        if (
            is_directory
            or path.suffix.lower() not in self.extensions
            or _is_incomplete_video_path(path)
        ):
            return
        if is_standalone_theme_video(path, self.config):
            return
        if not path.exists() or not path.is_file():
            return
        self._event_queue.submit(path, event_type=event_type)


class _FilesystemEventQueue:
    def __init__(
        self,
        config: Any,
        logger: logging.Logger,
        *,
        debounce_seconds: float = EVENT_DEBOUNCE_SECONDS,
        retry_seconds: float = EVENT_RETRY_SECONDS,
        stability_interval_seconds: float = EVENT_STABILITY_INTERVAL_SECONDS,
        quiet_window_seconds: float | None = None,
        stable_observations_required: int | None = None,
        file_complete_probe: Callable[[Path], bool] | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.logger = logger
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self.debounce_seconds = max(0.1, float(debounce_seconds))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.probe_max_attempts = max(
            1, int(getattr(config, "scanner_event_media_probe_max_attempts", 4) or 4)
        )
        self.probe_max_retry_seconds = max(
            self.retry_seconds,
            float(getattr(config, "scanner_event_media_probe_max_retry_seconds", 900.0) or 900.0),
        )
        configured_stability = float(
            getattr(config, "scanner_event_stability_interval_seconds", stability_interval_seconds)
            or stability_interval_seconds
        )
        self.stability_interval_seconds = max(0.1, configured_stability)
        configured_quiet = (
            getattr(config, "scanner_event_quiet_window_seconds", EVENT_QUIET_WINDOW_SECONDS)
            if quiet_window_seconds is None
            else quiet_window_seconds
        )
        self.quiet_window_seconds = max(0.0, float(configured_quiet or 0.0))
        configured_required = (
            getattr(
                config,
                "scanner_event_stable_observations_required",
                EVENT_STABLE_OBSERVATIONS_REQUIRED,
            )
            if stable_observations_required is None
            else stable_observations_required
        )
        self.stable_observations_required = max(2, int(configured_required or 0))
        configured_probe = getattr(config, "scanner_event_file_complete_probe", None)
        self._file_complete_probe = (
            file_complete_probe
            if file_complete_probe is not None
            else configured_probe
            if callable(configured_probe)
            else lambda path: _ffprobe_completed_media(
                path,
                executable=str(
                    getattr(config, "scanner_event_ffprobe_path", "ffprobe")
                    or "ffprobe"
                ),
                timeout_seconds=_media_probe_timeout_seconds(path, config),
            )
        )
        self.extensions = {str(extension).lower() for extension in config.video_extensions}
        self._condition = threading.Condition()
        self._pending: dict[Path, float] = {}
        self._observations: dict[Path, _ObservedFile] = {}
        self._event_evidence: dict[Path, _EventEvidence] = {}
        self._probe_failures: dict[Path, int] = {}
        self._stopped = False
        self._last_busy_log_at = 0.0
        self._last_success_log_at = 0.0
        self._queued_since_success_log = 0
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="anime-subtitle-fs-event-queue",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(EVENT_QUEUE_STARTUP_TIMEOUT_SECONDS):
            self.stop()
            raise TimeoutError("Filesystem event queue did not initialize its scanner state database in time")
        if self._startup_error is not None:
            raise RuntimeError("Filesystem event queue could not initialize its scanner state database") from self._startup_error

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def submit(self, path: Path, *, event_type: str = "") -> None:
        key = _event_path_key(path)
        normalized_event_type = str(event_type or "").strip().casefold()
        now_monotonic = self._monotonic_clock()
        now_wall = self._wall_clock()
        due_at = now_monotonic + self.debounce_seconds
        with self._condition:
            previous = self._event_evidence.get(key)
            self._event_evidence[key] = _EventEvidence(
                last_event_at=now_wall,
                last_event_monotonic=now_monotonic,
                event_type=normalized_event_type,
                close_observed=bool(
                    normalized_event_type == "closed"
                    or (previous is not None and previous.close_observed)
                ),
            )
            self._pending[key] = due_at
            self._condition.notify()

    def _run(self) -> None:
        state: ScanStateStore | None = None
        try:
            # Initialize the schema in the writer thread and do not report the
            # queue as ready until the transaction has committed.  Release the
            # handle immediately afterwards: /work commonly lives on Unraid's
            # FUSE layer, where an externally rotated WAL/SHM file leaves a
            # lifetime connection attached to a hidden, unlinked inode.  A
            # short connection per debounced batch makes the watcher recover
            # automatically on the next batch instead of poisoning every AI
            # queue read until the container is restarted.
            state = ScanStateStore.from_config(self.config)
            adapter = _IngestStoreAdapter(state)
            rehydrated = self._rehydrate_pending_observations(adapter)
            state.commit()
            state.close()
            state = None
            if rehydrated:
                self.logger.info(
                    "Rehydrated pending filesystem ingest observations. count=%s",
                    rehydrated,
                )
        except Exception as exc:
            if state is not None:
                try:
                    state.close()
                except sqlite3.Error:
                    pass
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        while True:
            batch = self._wait_for_due_paths()
            if batch is None:
                return
            paths, stopping = batch
            self._write_batch(paths, allow_promote=not stopping)
            if stopping:
                return

    def _wait_for_due_paths(self) -> tuple[list[Path], bool] | None:
        with self._condition:
            while True:
                if self._stopped:
                    if not self._pending:
                        return None
                    paths = list(self._pending)
                    self._pending.clear()
                    return paths, True

                if not self._pending:
                    self._condition.wait()
                    continue

                now = self._monotonic_clock()
                due_paths = [path for path, due_at in self._pending.items() if due_at <= now]
                if due_paths:
                    for path in due_paths:
                        self._pending.pop(path, None)
                    return due_paths, False

                next_due_at = min(self._pending.values())
                self._condition.wait(timeout=max(0.1, next_due_at - now))

    def _rehydrate_pending_observations(self, adapter: _IngestStoreAdapter) -> int:
        if not adapter.supported:
            return 0
        rows = adapter.pending()
        now_wall = self._wall_clock()
        now_monotonic = self._monotonic_clock()
        rehydrated = 0
        for row in rows:
            raw_path = row.get("canonical_path") or row.get("path")
            if not raw_path:
                continue
            path = Path(str(raw_path))
            key = _event_path_key(path)
            if (
                _is_incomplete_video_path(path)
                or not _is_existing_video(path, self.extensions)
            ):
                adapter.clear(path)
                continue
            try:
                size = int(row.get("size") or 0)
                mtime_ns = int(row.get("mtime_ns") or 0)
                first_seen_at = float(row.get("first_seen_at") or now_wall)
                last_seen_at = float(row.get("last_seen_at") or first_seen_at)
                observation_count = max(1, int(row.get("observation_count") or 1))
            except (TypeError, ValueError):
                adapter.clear(path)
                continue
            if size <= 0 or mtime_ns <= 0:
                adapter.clear(path)
                continue
            current = _safe_size_mtime(path)
            if current is None or current != (size, mtime_ns):
                # Preserve the path as pending but reset the old media revision.
                adapter.clear(path)
                if current is None or current[0] <= 0:
                    continue
                size, mtime_ns = current
                first_seen_at = now_wall
                last_seen_at = now_wall
                observation_count = 1
            event_type = str(row.get("last_event_type") or row.get("event_type") or "")
            if event_type.startswith(PROBE_EXHAUSTED_EVENT_PREFIX):
                # Durable quarantine: restart must not recreate the probe loop.
                continue
            if event_type.startswith(PROBE_FAILURE_EVENT_PREFIX):
                try:
                    self._probe_failures[key] = int(event_type.rsplit("_", 1)[-1])
                except ValueError:
                    self._probe_failures[key] = 1
            close_observed = bool(row.get("close_observed")) or event_type.casefold() == "closed"
            # Require at least one fresh stat after restart even if the durable
            # row had already accumulated many observations before interruption.
            stable_observations = min(
                observation_count,
                self.stable_observations_required - 1,
            )
            self._observations[key] = _ObservedFile(
                size=size,
                mtime_ns=mtime_ns,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                stable_observations=max(1, stable_observations),
                event_type=event_type,
                close_observed=close_observed,
            )
            elapsed = max(0.0, now_wall - last_seen_at)
            self._event_evidence[key] = _EventEvidence(
                last_event_at=last_seen_at,
                last_event_monotonic=now_monotonic - elapsed,
                event_type=event_type,
                close_observed=close_observed,
            )
            self._pending[key] = now_monotonic + self.stability_interval_seconds
            rehydrated += 1
        return rehydrated

    def _write_batch(self, paths: list[Path], *, allow_promote: bool = True) -> None:
        decisions: list[_IngestDecision] = []
        dropped: list[Path] = []
        retry_after_stability: list[Path] = []
        now_wall = self._wall_clock()
        now_monotonic = self._monotonic_clock()

        for path in paths:
            key = _event_path_key(path)
            if (
                _is_incomplete_video_path(path)
                or not _is_existing_video(path, self.extensions)
                or is_standalone_theme_video(path, self.config)
            ):
                self._forget_path(key)
                dropped.append(path)
                continue

            identity = _safe_size_mtime(path)
            if identity is None or identity[0] <= 0 or identity[1] <= 0:
                self._observations.pop(key, None)
                dropped.append(path)
                if path.exists():
                    retry_after_stability.append(path)
                continue

            size, mtime_ns = identity
            previous = self._observations.get(key)
            evidence = self._event_evidence_snapshot(key)
            same_revision = bool(
                previous is not None
                and previous.size == size
                and previous.mtime_ns == mtime_ns
            )
            observation = _ObservedFile(
                size=size,
                mtime_ns=mtime_ns,
                first_seen_at=(previous.first_seen_at if same_revision else now_wall),
                last_seen_at=now_wall,
                stable_observations=(
                    previous.stable_observations + 1 if same_revision else 1
                ),
                event_type=(
                    "closed"
                    if evidence is not None and evidence.close_observed
                    else evidence.event_type
                    if evidence is not None and evidence.event_type
                    else previous.event_type
                    if same_revision and previous is not None
                    else ""
                ),
                close_observed=bool(
                    (evidence is not None and evidence.close_observed)
                    or (same_revision and previous is not None and previous.close_observed)
                ),
            )
            self._observations[key] = observation
            quiet_elapsed = self._quiet_elapsed_seconds(
                observation,
                evidence,
                now_wall=now_wall,
                now_monotonic=now_monotonic,
            )
            ready = bool(
                allow_promote
                and observation.stable_observations >= self.stable_observations_required
                and quiet_elapsed >= self.quiet_window_seconds
            )
            decisions.append(
                _IngestDecision(
                    path=path,
                    observation=observation,
                    ready_for_probe=ready,
                )
            )
            if not ready and allow_promote:
                retry_after_stability.append(path)

        if not self._persist_observation_phase(decisions, dropped):
            self._requeue(
                [decision.path for decision in decisions],
                delay_seconds=self.retry_seconds,
            )
            return

        if not allow_promote:
            return

        ready = [decision for decision in decisions if decision.ready_for_probe]
        passed: list[_IngestDecision] = []
        failed: list[_IngestDecision] = []
        for decision in ready:
            if _file_write_complete(
                decision.path,
                expected=(decision.observation.size, decision.observation.mtime_ns),
                # A real close-write is strong completion evidence. Atomic
                # moves and network shares frequently omit it, so only those
                # candidates pay for the full-container ffprobe fallback.
                probe=(
                    None
                    if decision.observation.close_observed
                    else self._file_complete_probe
                ),
            ):
                passed.append(decision)
            else:
                failed.append(decision)

        promoted, changed_during_publish, retry_after_probe = self._persist_probe_results(passed, failed)
        retry_after_probe.extend(changed_during_publish)
        if retry_after_stability:
            self._requeue(
                retry_after_stability,
                delay_seconds=self.stability_interval_seconds,
            )
        for path in retry_after_probe:
            attempts = max(1, self._probe_failures.get(_event_path_key(path), 1))
            self._requeue(
                [path],
                delay_seconds=min(
                    self.probe_max_retry_seconds,
                    self.retry_seconds * (2 ** (attempts - 1)),
                ),
            )
        if promoted:
            self._log_success_periodically(len(promoted))

    def _persist_observation_phase(
        self,
        decisions: list[_IngestDecision],
        dropped: list[Path],
    ) -> bool:
        if not decisions and not dropped:
            return True
        state: ScanStateStore | None = None
        try:
            state = ScanStateStore.from_config(self.config)
            adapter = _IngestStoreAdapter(state)
            for path in dropped:
                adapter.clear(path)
            for decision in decisions:
                observation = decision.observation
                probe_attempts = self._probe_failures.get(_event_path_key(decision.path), 0)
                adapter.observe(
                    decision.path,
                    observation.size,
                    observation.mtime_ns,
                    observed_at=observation.last_seen_at,
                    event_type=(
                        f"{PROBE_FAILURE_EVENT_PREFIX}{probe_attempts}"
                        if probe_attempts > 0
                        else observation.event_type
                    ),
                    # A quiet/stable stat sample is still only an observation.
                    # Do not advance the durable job until the read-open and
                    # injectable media probe below have both succeeded.
                    state="stabilizing",
                )
            state.commit()
            return True
        except sqlite3.OperationalError as exc:
            if state is not None:
                self._rollback_quietly(state)
            if is_scan_state_transient_error(exc):
                self._log_busy_once(len(decisions), exc)
            else:
                self.logger.warning(
                    "Failed to persist filesystem ingest observations count=%s: %s",
                    len(decisions),
                    exc,
                )
            return False
        except Exception as exc:
            if state is not None:
                self._rollback_quietly(state)
            self.logger.warning(
                "Failed to persist filesystem ingest observations count=%s: %s",
                len(decisions),
                exc,
            )
            return False
        finally:
            self._close_state_quietly(state)

    def _persist_probe_results(
        self,
        passed: list[_IngestDecision],
        failed: list[_IngestDecision],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        if not passed and not failed:
            return [], [], []
        state: ScanStateStore | None = None
        promoted: list[Path] = []
        changed: list[Path] = []
        retryable_failed: list[Path] = []
        try:
            state = ScanStateStore.from_config(self.config)
            adapter = _IngestStoreAdapter(state)
            for decision in failed:
                observation = decision.observation
                key = _event_path_key(decision.path)
                attempts = self._probe_failures.get(key, 0) + 1
                self._probe_failures[key] = attempts
                exhausted = attempts >= self.probe_max_attempts
                adapter.observe(
                    decision.path,
                    observation.size,
                    observation.mtime_ns,
                    observed_at=self._wall_clock(),
                    event_type=(
                        f"{PROBE_EXHAUSTED_EVENT_PREFIX}{attempts}"
                        if exhausted
                        else f"{PROBE_FAILURE_EVENT_PREFIX}{attempts}"
                    ),
                    state="stabilizing",
                )
                if exhausted:
                    self.logger.error(
                        "Quarantined filesystem candidate after bounded media probe failures: "
                        "path=%s attempts=%s reason=media_probe_exhausted size=%s mtime_ns=%s",
                        decision.path,
                        attempts,
                        observation.size,
                        observation.mtime_ns,
                    )
                    self._forget_path(key)
                else:
                    retryable_failed.append(decision.path)
            for decision in passed:
                observation = decision.observation
                current = _safe_size_mtime(decision.path)
                if current != (observation.size, observation.mtime_ns):
                    if current is None or current[0] <= 0 or current[1] <= 0:
                        adapter.clear(decision.path)
                        if decision.path.exists():
                            changed.append(decision.path)
                    else:
                        changed.append(decision.path)
                        adapter.observe(
                            decision.path,
                            int(current[0]),
                            int(current[1]),
                            observed_at=self._wall_clock(),
                            event_type=observation.event_type,
                            state="stabilizing",
                        )
                    continue
                state.upsert_ai_queue_candidate(
                    decision.path,
                    observation.mtime_ns,
                    source="fs_event",
                )
                adapter.observe(
                    decision.path,
                    observation.size,
                    observation.mtime_ns,
                    observed_at=self._wall_clock(),
                    event_type=observation.event_type,
                    state="queued",
                )
                adapter.clear(decision.path)
                promoted.append(decision.path)
                self._probe_failures.pop(_event_path_key(decision.path), None)
            state.commit()
        except sqlite3.OperationalError as exc:
            if state is not None:
                self._rollback_quietly(state)
            self._requeue([decision.path for decision in passed], delay_seconds=self.retry_seconds)
            if is_scan_state_transient_error(exc):
                self._log_busy_once(len(passed), exc)
            else:
                self.logger.warning(
                    "Failed to queue complete filesystem ingest batch count=%s: %s",
                    len(passed),
                    exc,
                )
            return [], [], [decision.path for decision in passed + failed]
        except Exception as exc:
            if state is not None:
                self._rollback_quietly(state)
            self._requeue([decision.path for decision in passed], delay_seconds=self.retry_seconds)
            self.logger.warning(
                "Failed to queue complete filesystem ingest batch count=%s: %s",
                len(passed),
                exc,
            )
            return [], [], [decision.path for decision in passed + failed]
        finally:
            self._close_state_quietly(state)

        for path in promoted:
            self._forget_path(_event_path_key(path))
        return promoted, changed, retryable_failed

    def _quiet_elapsed_seconds(
        self,
        observation: _ObservedFile,
        evidence: _EventEvidence | None,
        *,
        now_wall: float,
        now_monotonic: float,
    ) -> float:
        if evidence is not None:
            return max(0.0, now_monotonic - evidence.last_event_monotonic)
        return max(0.0, now_wall - observation.mtime_ns / 1_000_000_000)

    def _event_evidence_snapshot(self, key: Path) -> _EventEvidence | None:
        with self._condition:
            return self._event_evidence.get(key)

    def _forget_path(self, key: Path) -> None:
        self._observations.pop(key, None)
        with self._condition:
            self._event_evidence.pop(key, None)

    def _close_state_quietly(self, state: ScanStateStore | None) -> None:
        if state is None:
            return
        try:
            state.close()
        except sqlite3.Error as exc:
            self.logger.warning(
                "Failed to close filesystem event scanner state connection; "
                "the next batch will open a fresh handle: %s",
                exc,
            )

    @staticmethod
    def _rollback_quietly(state: ScanStateStore) -> None:
        try:
            if state.in_transaction:
                state.rollback()
        except sqlite3.Error:
            return

    def _requeue(
        self,
        paths: list[Path],
        *,
        delay_seconds: float | None = None,
    ) -> None:
        delay = self.retry_seconds if delay_seconds is None else max(0.1, float(delay_seconds))
        due_at = time.monotonic() + delay
        with self._condition:
            for path in paths:
                self._pending[_event_path_key(path)] = due_at
            self._condition.notify()

    def _log_busy_once(self, count: int, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_busy_log_at < BUSY_LOG_INTERVAL_SECONDS:
            self.logger.debug("Scanner state database is busy; delayed filesystem event batch count=%s: %s", count, exc)
            return
        self._last_busy_log_at = now
        self.logger.warning(
            "Scanner state database is busy; delayed filesystem event batch count=%s retry_in=%ss: %s",
            count,
            int(self.retry_seconds),
            exc,
        )

    def _log_success_periodically(self, count: int) -> None:
        self._queued_since_success_log += max(0, int(count))
        now = time.monotonic()
        if (
            self._last_success_log_at
            and now - self._last_success_log_at < SUCCESS_LOG_INTERVAL_SECONDS
            and self._queued_since_success_log < 100
        ):
            self.logger.debug("Queued filesystem event video batch: count=%s", count)
            return
        self.logger.info(
            "Queued filesystem event videos. count_since_last_log=%s",
            self._queued_since_success_log,
        )
        self._queued_since_success_log = 0
        self._last_success_log_at = now


class _RunningEventWatcher:
    def __init__(
        self,
        observer: Any,
        event_queue: _FilesystemEventQueue,
        *,
        config: Any | None = None,
        logger: logging.Logger | None = None,
        base_handler_type: type | None = None,
        observer_type: type | None = None,
    ) -> None:
        self._observer = observer
        self._event_queue = event_queue
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._base_handler_type = base_handler_type
        self._observer_type = observer_type
        self._component_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        if config is not None and base_handler_type is not None and observer_type is not None:
            self._monitor_thread = threading.Thread(
                target=self._monitor,
                name="anime-subtitle-fs-event-supervisor",
                daemon=True,
            )
            self._monitor_thread.start()

    def stop(self) -> None:
        self._monitor_stop.set()
        with self._component_lock:
            self._observer.stop()
            self._event_queue.stop()

    def join(self, timeout: float | None = None) -> None:
        monitor = self._monitor_thread
        if monitor is not None:
            monitor.join(timeout)
        with self._component_lock:
            self._observer.join(timeout)
            self._event_queue.join(timeout)

    def is_alive(self) -> bool:
        with self._component_lock:
            try:
                return bool(self._observer.is_alive())
            except Exception:
                return False

    def _monitor(self) -> None:
        interval = max(
            1.0,
            float(
                getattr(
                    self._config,
                    "scanner_event_watch_health_interval_seconds",
                    30.0,
                )
                or 30.0
            ),
        )
        while not self._monitor_stop.wait(interval):
            self._restart_if_needed()

    def _restart_if_needed(self) -> bool:
        if self._monitor_stop.is_set() or self.is_alive():
            return False
        if (
            self._config is None
            or self._base_handler_type is None
            or self._observer_type is None
        ):
            return False
        with self._component_lock:
            try:
                self._observer.stop()
            except Exception:
                pass
            self._event_queue.stop()
            try:
                self._observer.join(2)
                self._event_queue.join(2)
            except Exception:
                pass
            try:
                observer, event_queue = _create_event_watcher_components(
                    self._config,
                    self._logger,
                    self._base_handler_type,
                    self._observer_type,
                )
            except Exception as exc:
                self._logger.warning(
                    "Filesystem event watcher is not alive; restart will retry: %s",
                    exc,
                )
                return False
            self._observer = observer
            self._event_queue = event_queue
        self._logger.info("Filesystem event watcher restarted after observer exit.")
        return True

    def __getattr__(self, name: str) -> Any:
        with self._component_lock:
            return getattr(self._observer, name)


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _safe_size_mtime(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_size), int(stat.st_mtime_ns)


def _media_probe_timeout_seconds(path: Path, config: Any) -> float:
    """Budget a full-container probe from size and a conservative read floor."""

    minimum = max(
        1.0,
        float(getattr(config, "scanner_event_media_probe_timeout_seconds", 30.0) or 30.0),
    )
    maximum = max(
        minimum,
        float(getattr(config, "scanner_event_media_probe_max_timeout_seconds", 1800.0) or 1800.0),
    )
    throughput_mib = max(
        0.1,
        float(
            getattr(config, "scanner_event_media_probe_min_throughput_mib_per_second", 8.0)
            or 8.0
        ),
    )
    try:
        size = max(0, int(path.stat().st_size))
    except OSError:
        return minimum
    estimated = math.ceil(size / (throughput_mib * 1024 * 1024))
    return min(maximum, max(minimum, float(estimated)))


def _ffprobe_completed_media(
    path: Path,
    *,
    executable: str = "ffprobe",
    timeout_seconds: float = 30.0,
) -> bool:
    """Parse the complete container when no close-write evidence is available.

    ``-count_packets`` deliberately walks the demuxed file rather than merely
    accepting a readable header. This is only used for quiet events without a
    close-write signal, outside the filesystem callback thread.
    """

    command = [
        str(executable or "ffprobe"),
        "-v",
        "error",
        "-count_packets",
        "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,nb_read_packets",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            ),
        )
        if completed.returncode != 0:
            return False
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    format_payload = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_payload, Mapping) or not str(
        format_payload.get("format_name") or ""
    ).strip():
        return False
    if not isinstance(streams, list):
        return False
    for stream in streams:
        if not isinstance(stream, Mapping) or str(stream.get("codec_type")) != "video":
            continue
        try:
            if int(stream.get("nb_read_packets") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _file_write_complete(
    path: Path,
    *,
    expected: tuple[int, int],
    probe: Callable[[Path], bool] | None,
) -> bool:
    """Verify a quiet candidate without performing work in the event callback.

    A close-write event is durable evidence but is not universally available
    for atomic moves and network-backed filesystems.  The debounce thread always
    applies the portable fallback: read-open the file, compare the opened file's
    stat with path stats before and after the injectable media probe, and reject
    any identity change.
    """

    try:
        before = path.stat()
        if not stat_module.S_ISREG(before.st_mode):
            return False
        if (int(before.st_size), int(before.st_mtime_ns)) != expected:
            return False
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if not _same_open_file_identity(before, opened):
                return False
            first = handle.read(1)
            if expected[0] <= 0 or len(first) != 1:
                return False
            if expected[0] > 1:
                handle.seek(-1, os.SEEK_END)
                if len(handle.read(1)) != 1:
                    return False
        if probe is not None and not bool(probe(path)):
            return False
        after = path.stat()
    except (OSError, ValueError):
        return False
    except Exception:
        # A pluggable parser/probe is a safety gate. Any failure leaves the
        # candidate stabilizing so it can be retried without creating a job.
        return False
    return bool(
        _same_open_file_identity(before, after)
        and (int(after.st_size), int(after.st_mtime_ns)) == expected
    )


def _same_open_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    if int(left.st_size) != int(right.st_size) or int(left.st_mtime_ns) != int(right.st_mtime_ns):
        return False
    left_inode = int(getattr(left, "st_ino", 0) or 0)
    right_inode = int(getattr(right, "st_ino", 0) or 0)
    left_device = int(getattr(left, "st_dev", 0) or 0)
    right_device = int(getattr(right, "st_dev", 0) or 0)
    if left_inode and right_inode and left_inode != right_inode:
        return False
    if left_device and right_device and left_device != right_device:
        return False
    return True


def _event_path_key(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_incomplete_video_path(path: Path) -> bool:
    name = path.name.casefold()
    stem = path.stem.casefold()
    if name.startswith((".", "~")) or stem.endswith("~"):
        return True
    if any(stem.endswith(suffix) for suffix in INCOMPLETE_VIDEO_STEM_SUFFIXES):
        return True
    return any(part.casefold() in INCOMPLETE_VIDEO_DIRECTORY_NAMES for part in path.parts)


def _is_existing_video(path: Path, extensions: set[str]) -> bool:
    return (
        not _is_incomplete_video_path(path)
        and path.suffix.lower() in extensions
        and path.exists()
        and path.is_file()
    )
