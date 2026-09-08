#!/bin/bash
# UNRAID host adapter; no host Python, Docker socket mount, Queue or recovery writes.
# Intended for the existing host scheduler. Installation is a deployment action.
set -euo pipefail
WORKER=${M3_WORKER_CONTAINER:-anime-subtitle-worker}
PROVIDER=${M3_PROVIDER_CONTAINER:-ollama}
CONFIG=${M3_WORKER_CONFIG:-/app/config.yaml}
MODE=${1:---once}
for NAME in "$WORKER" "$PROVIDER"; do
  [[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || exit 2
done
[[ "$CONFIG" = /* && "$CONFIG" != *$'\n'* ]] || exit 2
[[ "$MODE" = --once || "$MODE" = --scheduled-minute ]] || exit 2
exec 9>"/var/run/m3-provider-observer-$WORKER.lock"
flock -n 9 || exit 0
INSPECT='{"Id":{{json .Id}},"Image":{{json .Image}},"State":{{json .State}},"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}}}'
refresh() {
  local STARTED CONTEXT FIRST SECOND ADDRESSES
  STARTED=$(date +%s.%N)
  CONTEXT=$(timeout 5 docker exec "$WORKER" python /app/m2_guardrail_runtime.py provider-context --config "$CONFIG") || return $?
  FIRST=$(timeout 2 docker inspect --format "$INSPECT" "$PROVIDER") || return $?
  SECOND=$(timeout 2 docker inspect --format "$INSPECT" "$PROVIDER") || return $?
  ADDRESSES=$(hostname -I) || return $?
  printf '%s\n' "$CONTEXT" "$FIRST" "$SECOND" "$ADDRESSES" "$STARTED" |
    timeout 5 docker exec -i "$WORKER" python /app/m2_guardrail_runtime.py provider-refresh-inspected --config "$CONFIG"
}
if [[ "$MODE" = --once ]]; then
  refresh
else
  # One bounded minute invocation. The host scheduler starts the next invocation;
  # this script never installs itself, resumes a job or removes a lock.
  refresh || true
  sleep 20
  refresh
fi
