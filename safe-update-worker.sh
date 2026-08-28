#!/usr/bin/env sh
set -eu

SERVICE="${SERVICE:-anime-subtitle-worker}"
CONTAINER="${WORKER_CONTAINER_NAME:-anime-subtitle-worker}"
POLL_SECONDS="${POLL_SECONDS:-30}"
WAIT_FOR_IDLE="${WAIT_FOR_IDLE:-1}"
MIKAN_WAIT_SECONDS="${MIKAN_WAIT_SECONDS:-14400}"
IDLE_WAIT_SECONDS="${IDLE_WAIT_SECONDS:-$MIKAN_WAIT_SECONDS}"
MIKAN_LOCK_MAX_AGE_SECONDS="${MIKAN_LOCK_MAX_AGE_SECONDS:-43200}"
MIKAN_ACTIVE_STALE_SECONDS="${MIKAN_ACTIVE_STALE_SECONDS:-900}"
AI_RUNNING_STALE_SECONDS="${AI_RUNNING_STALE_SECONDS:-900}"
AUTO_STATE_BACKUP="${AUTO_STATE_BACKUP:-1}"

case "$POLL_SECONDS" in
  ''|*[!0-9]*|0)
    echo "POLL_SECONDS must be a positive integer; got: ${POLL_SECONDS}" >&2
    exit 2
    ;;
esac
case "$MIKAN_WAIT_SECONDS" in
  ''|*[!0-9]*)
    echo "MIKAN_WAIT_SECONDS must be a non-negative integer; got: ${MIKAN_WAIT_SECONDS}" >&2
    exit 2
    ;;
esac
case "$IDLE_WAIT_SECONDS" in
  ''|*[!0-9]*)
    echo "IDLE_WAIT_SECONDS must be a non-negative integer; got: ${IDLE_WAIT_SECONDS}" >&2
    exit 2
    ;;
esac
case "$MIKAN_LOCK_MAX_AGE_SECONDS" in
  ''|*[!0-9]*)
    echo "MIKAN_LOCK_MAX_AGE_SECONDS must be a non-negative integer; got: ${MIKAN_LOCK_MAX_AGE_SECONDS}" >&2
    exit 2
    ;;
esac
case "$AI_RUNNING_STALE_SECONDS" in
  ''|*[!0-9]*)
    echo "AI_RUNNING_STALE_SECONDS must be a non-negative integer; got: ${AI_RUNNING_STALE_SECONDS}" >&2
    exit 2
    ;;
esac
case "$MIKAN_ACTIVE_STALE_SECONDS" in
  ''|*[!0-9]*)
    echo "MIKAN_ACTIVE_STALE_SECONDS must be a non-negative integer; got: ${MIKAN_ACTIVE_STALE_SECONDS}" >&2
    exit 2
    ;;
esac

echo "[1/2] Building ${SERVICE} image without stopping the running worker..."
docker compose build "$SERVICE"

if [ "$WAIT_FOR_IDLE" != "0" ]; then
  echo "[2/2] Waiting up to ${IDLE_WAIT_SECONDS}s for Mikan operations before gracefully draining active AI work..."
  wait_started_at="$(date +%s)"
  while :; do
    status_info="$(
      docker exec -i \
        -e MIKAN_LOCK_MAX_AGE_SECONDS="$MIKAN_LOCK_MAX_AGE_SECONDS" \
        -e MIKAN_ACTIVE_STALE_SECONDS="$MIKAN_ACTIVE_STALE_SECONDS" \
        -e AI_RUNNING_STALE_SECONDS="$AI_RUNNING_STALE_SECONDS" \
        "$CONTAINER" python - <<'PY' 2>/dev/null || true
from pathlib import Path
import os
import sqlite3
import time

try:
    max_age_seconds = max(0, int(os.environ.get("MIKAN_LOCK_MAX_AGE_SECONDS", "43200")))
except ValueError:
    max_age_seconds = 43200
try:
    ai_stale_seconds = max(0, int(os.environ.get("AI_RUNNING_STALE_SECONDS", "900")))
except ValueError:
    ai_stale_seconds = 900
try:
    mikan_active_stale_seconds = max(0, int(os.environ.get("MIKAN_ACTIVE_STALE_SECONDS", "900")))
except ValueError:
    mikan_active_stale_seconds = 900

lock_specs = [
    ("state", Path("/work/mikan_worker")),
    ("enqueue", Path("/work/mikan_enqueue")),
    ("extract", Path("/work/mikan_extract")),
    ("redownload", Path("/work/mikan_redownload")),
]
fresh_locks = []
old_locks = []
try:
    from lock import VideoLock

    for label, target in lock_specs:
        lock = VideoLock(target)
        if lock.lock_path.exists():
            try:
                age = max(0, int(time.time() - lock.lock_path.stat().st_mtime))
            except OSError:
                age = None
            bucket = old_locks if lock._is_stale_lock() else fresh_locks
            bucket.append((label, lock.lock_path, age))
except Exception:
    for label, target in lock_specs:
        lock_path = target.with_name(f"{target.name}.lock")
        if lock_path.exists():
            try:
                age = max(0, int(time.time() - lock_path.stat().st_mtime))
            except OSError:
                age = None
            bucket = old_locks if age is not None and max_age_seconds > 0 and age >= max_age_seconds else fresh_locks
            bucket.append((label, lock_path, age))

redownload_active_path = Path("/work/mikan_redownload_all.active.json")
if redownload_active_path.exists():
    try:
        active_age = max(0, int(time.time() - redownload_active_path.stat().st_mtime))
    except OSError:
        active_age = None
    bucket = (
        fresh_locks
        if active_age is not None and active_age <= mikan_active_stale_seconds
        else old_locks
    )
    bucket.append(("redownload-active", redownload_active_path, active_age))

active_ai = []
stale_ai = []
ai_state_error = ""
state_db = Path("/work/scanner_state.sqlite3")
if state_db.exists():
    conn = None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        try:
            rows = conn.execute(
                """
                SELECT q.path,
                       MAX(COALESCE(q.running_at, 0), COALESCE(q.updated_at, 0), COALESCE(j.updated_at, 0))
                FROM ai_candidate_queue q
                LEFT JOIN ai_job_state j ON j.path = q.path
                WHERE q.status = 'running'
                ORDER BY 2 DESC
                LIMIT 5
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                SELECT path, MAX(COALESCE(running_at, 0), COALESCE(updated_at, 0))
                FROM ai_candidate_queue
                WHERE status = 'running'
                ORDER BY 2 DESC
                LIMIT 5
                """
            ).fetchall()
        now = time.time()
        for path, heartbeat in rows:
            age = max(0, int(now - float(heartbeat or 0))) if heartbeat else None
            item = (str(path), age)
            if age is not None and ai_stale_seconds > 0 and age >= ai_stale_seconds:
                stale_ai.append(item)
            else:
                active_ai.append(item)
    except sqlite3.Error as exc:
        ai_state_error = str(exc)
        active_ai.append(("scanner state unavailable", None))
    finally:
        if conn is not None:
            conn.close()

print(f"idle={1 if not fresh_locks and not active_ai else 0}")
print(f"mikan_busy={1 if fresh_locks else 0}")
print(f"ai_busy={1 if active_ai else 0}")
for label, lock_path, age in fresh_locks:
    age_text = "-" if age is None else f"{age // 60}m"
    print(f"mikan {label} {age_text} {lock_path}")
for label, lock_path, age in old_locks:
    age_text = "-" if age is None else f"{age // 60}m"
    print(f"mikan old-{label} {age_text} {lock_path}")
for path, age in active_ai:
    age_text = "-" if age is None else f"{age}s"
    print(f"ai running heartbeat={age_text} {path}")
for path, age in stale_ai:
    age_text = "-" if age is None else f"{age}s"
    print(f"ai stale-running heartbeat={age_text} {path}")
if ai_state_error:
    print(f"ai state-error {ai_state_error}")
PY
    )"
    idle_line="$(printf '%s\n' "$status_info" | sed -n '1p')"
    case "$idle_line" in
      idle=1)
        printf '%s\n' "$status_info" | sed '1d;s/^/  /'
        echo "No fresh Mikan operations detected; requesting a graceful Worker stop."
        break
        ;;
      idle=0)
        now="$(date +%s)"
        waited_seconds=$((now - wait_started_at))
        if [ "$waited_seconds" -ge "$IDLE_WAIT_SECONDS" ]; then
          echo "Idle wait limit reached after ${waited_seconds}s; leaving the current worker running." >&2
          printf '%s\n' "$status_info" | sed '1d;s/^/  /'
          echo "Run with WAIT_FOR_IDLE=0 only if interrupting current work is acceptable." >&2
          exit 3
        fi
        remaining_seconds=$((IDLE_WAIT_SECONDS - waited_seconds))
        sleep_seconds="$POLL_SECONDS"
        if [ "$remaining_seconds" -lt "$sleep_seconds" ]; then
          sleep_seconds="$remaining_seconds"
        fi
        echo "Worker is still busy; checking again in ${sleep_seconds}s."
        printf '%s\n' "$status_info" | sed '1d;s/^/  /'
        sleep "$sleep_seconds"
        ;;
      *)
        echo "Could not read worker state from ${CONTAINER}; continuing with recreate."
        break
        ;;
    esac
  done
  if [ "$AUTO_STATE_BACKUP" != "0" ] && docker exec -i "$CONTAINER" test -f /app/backup_state.py >/dev/null 2>&1; then
    echo "Creating a verified pre-update Worker state backup..."
    if docker exec -i "$CONTAINER" python /app/backup_state.py --config /app/config.yaml; then
      :
    else
      echo "Warning: pre-update state backup failed; update will continue and retry after recreate." >&2
    fi
  fi
  echo "Stopping ${CONTAINER} gracefully; no new AI work will start and the active video may run for up to ${IDLE_WAIT_SECONDS}s."
  docker stop --time "$IDLE_WAIT_SECONDS" "$CONTAINER" >/dev/null
else
  echo "[2/2] WAIT_FOR_IDLE=0, force-stopping ${CONTAINER} before recreate."
  echo "Current worker work may be interrupted; previous running queue items should be requeued on startup."
  docker kill "$CONTAINER" >/dev/null 2>&1 || true
fi

docker compose up -d --no-build --force-recreate "$SERVICE"
if [ "$AUTO_STATE_BACKUP" != "0" ]; then
  echo "Creating a verified post-update state backup with the updated Worker..."
  post_backup_complete=0
  backup_attempt=0
  while [ "$backup_attempt" -lt 12 ]; do
    backup_attempt=$((backup_attempt + 1))
    if docker exec -i "$CONTAINER" python /app/backup_state.py --config /app/config.yaml; then
      post_backup_complete=1
      break
    fi
    sleep 5
  done
  if [ "$post_backup_complete" = "0" ]; then
    echo "Warning: post-update state backup did not complete; run /app/backup_state.py manually." >&2
  fi
fi
echo "Worker update requested. Current container: ${CONTAINER}"
