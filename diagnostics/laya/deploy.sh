#!/bin/bash
# Only the independent advisory service. Never touches Worker admission or Gate.
set -euo pipefail
ROOT=/mnt/user/appdata/anime-subtitle-worker
IMAGE=${1:?Pass the reviewed immutable image ID}
RUN=${2:?Pass a new evidence run ID}
[[ "$IMAGE" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 2
[[ "$RUN" =~ ^laya-[a-zA-Z0-9_-]+$ ]] || exit 2
NAME=subtitle-laya-diagnostics
LOG="$ROOT/logs/laya-20260923/$RUN"
STATE="$ROOT/work/laya-diagnostics"
[[ ! -e "$LOG" ]] || { echo 'Evidence run already exists; refusing overwrite.'; exit 4; }
mkdir -p "$LOG"
exec 9>"$LOG/deploy.lock"
flock -n 9 || exit 3
if docker inspect "$NAME" >/dev/null 2>&1; then
  docker inspect --format '{{.Id}} {{.Image}} {{.State.Status}}' "$NAME"
  echo 'Service already exists; refusing implicit replacement.'
  exit 4
fi
install -d -m 0750 -o 99 -g 100 "$STATE"
docker image inspect "$IMAGE" > "$LOG/sidecar-image.json"
docker run --rm --cidfile "$LOG/unit-container.cid" --name "subtitle-$RUN-unit" \
  --label io.subtitle.project=anime-subtitle-worker --label io.subtitle.temporary=true \
  --label io.subtitle.purpose=isolated-unit-test --label io.subtitle.run-id="$RUN" \
  --label io.subtitle.creator=codex --network none --read-only --cap-drop ALL \
  --runtime runc \
  --security-opt no-new-privileges --cpus 2 --memory 4g --memory-swap 4g --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m "$IMAGE" python -m unittest -v test_sidecar \
  > "$LOG/unit-tests.log" 2>&1
docker run -d --name "$NAME" --restart unless-stopped --init \
  --label io.subtitle.project=anime-subtitle-worker --label io.subtitle.temporary=false \
  --label io.subtitle.purpose=permanent-readonly-diagnostics --label io.subtitle.run-id="$RUN" \
  --label io.subtitle.creator=codex --network none --read-only --cap-drop ALL \
  --runtime runc \
  --security-opt no-new-privileges --cpus 2 --memory 4g --memory-swap 4g --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --mount type=bind,src="$STATE",dst=/diagnostics \
  --mount type=bind,src="$ROOT/work/laya-model-cache",dst=/models,readonly \
  --mount type=bind,src="$ROOT/work",dst=/runtime,readonly \
  --mount type=bind,src="$ROOT/logs",dst=/events,readonly \
  -e LAYA_EVENT_PATH=/events/pipeline-events.jsonl \
  -e LAYA_INFERENCE_ENABLED=0 \
  -e LAYA_RUNTIME_PATH=/runtime/m2_guardrail_runtime.json -e LAYA_IMAGE_ID="$IMAGE" "$IMAGE" > "$LOG/service.cid"
docker inspect "$NAME" > "$LOG/service-inspect.json"
echo 'DEPLOYED_DIAGNOSTIC_ONLY; model health and acceptance still required'
