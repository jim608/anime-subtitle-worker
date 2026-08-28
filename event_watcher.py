from __future__ import annotations

from pathlib import Path
import logging
import sqlite3
import threading
import time
from typing import Any

from scan_state import SQLITE_BUSY_TIMEOUT_SECONDS, ScanStateStore, is_scan_state_transient_error
from video_policy import is_standalone_theme_video


EVENT_DEBOUNCE_SECONDS = 3.0
EVENT_RETRY_SECONDS = 15.0
EVENT_STABILITY_INTERVAL_SECONDS = 2.0
BUSY_LOG_INTERVAL_SECONDS = 60.0
SUCCESS_LOG_INTERVAL_SECONDS = 300.0
EVENT_QUEUE_STARTUP_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_SECONDS + 5.0
QUEUEABLE_EVENT_TYPES = frozenset({"created", "modified", "moved", "closed"})


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
            self._queue_candidate(Path(raw_path), is_directory=bool(getattr(event, "is_directory", False)))

    def _queue_candidate(self, path: Path, *, is_directory: bool) -> None:
        if is_directory or path.suffix.lower() not in self.extensions:
            return
        if is_standalone_theme_video(path, self.config):
            return
        if not path.exists() or not path.is_file():
            return
        self._event_queue.submit(path)


class _FilesystemEventQueue:
    def __init__(
        self,
        config: Any,
        logger: logging.Logger,
        *,
        debounce_seconds: float = EVENT_DEBOUNCE_SECONDS,
        retry_seconds: float = EVENT_RETRY_SECONDS,
        stability_interval_seconds: float = EVENT_STABILITY_INTERVAL_SECONDS,
    ) -> None:
        self.config = config
        self.logger = logger
        self.debounce_seconds = max(0.1, float(debounce_seconds))
        self.retry_seconds = max(1.0, float(retry_seconds))
        configured_stability = float(
            getattr(config, "scanner_event_stability_interval_seconds", stability_interval_seconds)
            or stability_interval_seconds
        )
        self.stability_interval_seconds = max(0.1, configured_stability)
        self.extensions = {str(extension).lower() for extension in config.video_extensions}
        self._condition = threading.Condition()
        self._pending: dict[Path, float] = {}
        self._observations: dict[Path, tuple[int, int]] = {}
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

    def submit(self, path: Path) -> None:
        key = _event_path_key(path)
        due_at = time.monotonic() + self.debounce_seconds
        with self._condition:
            self._pending[key] = due_at
            self._condition.notify()

    def _run(self) -> None:
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
            state.close()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        while True:
            paths = self._wait_for_due_paths()
            if paths is None:
                return
            self._write_batch(paths)

    def _wait_for_due_paths(self) -> list[Path] | None:
        with self._condition:
            while True:
                if self._stopped:
                    if not self._pending:
                        return None
                    paths = list(self._pending)
                    self._pending.clear()
                    return paths

                if not self._pending:
                    self._condition.wait()
                    continue

                now = time.monotonic()
                due_paths = [path for path, due_at in self._pending.items() if due_at <= now]
                if due_paths:
                    for path in due_paths:
                        self._pending.pop(path, None)
                    return due_paths

                next_due_at = min(self._pending.values())
                self._condition.wait(timeout=max(0.1, next_due_at - now))

    def _write_batch(self, paths: list[Path]) -> None:
        candidates: list[Path] = []
        unstable: list[Path] = []
        for path in paths:
            if not _is_existing_video(path, self.extensions):
                self._observations.pop(_event_path_key(path), None)
                continue
            if is_standalone_theme_video(path, self.config):
                self._observations.pop(_event_path_key(path), None)
                continue
            key = _event_path_key(path)
            observation = _safe_size_mtime(path)
            if observation is None or observation[0] <= 0:
                self._observations.pop(key, None)
                unstable.append(path)
                continue
            previous = self._observations.get(key)
            if previous != observation:
                self._observations[key] = observation
                unstable.append(path)
                continue
            self._observations.pop(key, None)
            candidates.append(path)
        if unstable:
            self._requeue(unstable, delay_seconds=self.stability_interval_seconds)
        if not candidates:
            return

        state: ScanStateStore | None = None
        try:
            state = ScanStateStore.from_config(self.config)
            for path in candidates:
                state.upsert_ai_queue_candidate(path, _safe_mtime_ns(path), source="fs_event")
            state.commit()
            self._log_success_periodically(len(candidates))
        except sqlite3.OperationalError as exc:
            if state is not None:
                self._rollback_quietly(state)
            if is_scan_state_transient_error(exc):
                self._requeue(candidates)
                self._log_busy_once(len(candidates), exc)
            else:
                self.logger.warning("Failed to queue filesystem event batch count=%s: %s", len(candidates), exc)
        except Exception as exc:
            if state is not None:
                self._rollback_quietly(state)
            self.logger.warning("Failed to queue filesystem event batch count=%s: %s", len(candidates), exc)
        finally:
            if state is not None:
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


def _event_path_key(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_existing_video(path: Path, extensions: set[str]) -> bool:
    return path.suffix.lower() in extensions and path.exists() and path.is_file()
