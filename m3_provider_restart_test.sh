#!/bin/bash
# Isolated evidence only. Never targets a Production container for mutation.
set -euo pipefail
W=/mnt/user/appdata/anime-subtitle-worker
R=$(mktemp -d "$W/logs/m3-baseline-20260908T070237Z/provider-termination-XXXXXX")
exec >"$R/full.log" 2>&1
printf 'evidence=%s\n' "$R"
I=$(docker inspect --format '{{.Image}}' anime-subtitle-worker)
HOST=192.168.50.204
ADDR=$(hostname -I)
STAMP=$(basename "$R")
P="m3-fixture-provider-$STAMP"
S="m3-fixture-sender-$STAMP"
mkdir "$R/candidate" "$R/fixture"
git -C "$W" archive HEAD | tar -x -C "$R/candidate"
cp "$W/m3_provider_restart_probe.py" "$R/candidate/"
printf '%s\n' "$I" >"$R/image.txt"
cleanup() {
  for C in "$S" "$P"; do
    if docker inspect "$C" >/dev/null 2>&1; then
      L=$(docker inspect --format '{{index .Config.Labels "m3.isolated.provider-test"}}' "$C")
      if [ "$L" = "$STAMP" ]; then
        docker inspect --format '{{json .State}} {{json .Mounts}}' "$C" >>"$R/cleanup.log"
        docker logs --tail 12 "$C" >>"$R/cleanup.log" 2>&1 || true
        docker rm -f "$C" >>"$R/cleanup.log"
      else
        printf 'cleanup refused label mismatch %s\n' "$C" >>"$R/cleanup.log"
      fi
    fi
  done
}
trap cleanup EXIT
BASE=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --memory 512m --cpus 0.5
      -v "$R/candidate:/candidate:ro" -v "$R/fixture:/fixture:rw" -w /candidate
      -e PYTHONDONTWRITEBYTECODE=1 -e M3_ISOLATED_PROVIDER_TEST=1 --entrypoint python)
PORT=$(shuf -i 40000-60000 -n 1)
docker run -d --name "$P" --label "m3.isolated.provider-test=$STAMP" --network bridge \
  "${BASE[@]}" -p "$HOST:$PORT:11434" "$I" -B m3_provider_restart_probe.py provider
docker exec "$P" python -c 'import socket,time
for attempt in range(20):
 try:
  socket.create_connection(("127.0.0.1",11434),timeout=1).close(); break
 except OSError: time.sleep(1)
else: raise RuntimeError("fixture provider startup timeout")'
test "$PORT" = "$(docker inspect --format '{{(index (index .NetworkSettings.Ports "11434/tcp") 0).HostPort}}' "$P")"
EP="http://$HOST:$PORT/v1"
ENV=(-e "M3_FIXTURE_ENDPOINT=$EP" -e "M3_HOST_ADDRESSES=$ADDR")
capture() {
  docker inspect --format '{"Id":{{json .Id}},"Image":{{json .Image}},"State":{{json .State}},"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}}}' "$P" |
    tee "$R/$1-inspect.json" |
    docker run --rm -i --network none "${BASE[@]}" "${ENV[@]}" "$I" -B m3_provider_restart_probe.py "$1"
}
capture bind-old
docker create --name "$S" --label "m3.isolated.provider-test=$STAMP" --network bridge \
  "${BASE[@]}" "${ENV[@]}" "$I" -B m3_provider_restart_probe.py sender
timeout 45 docker start -a "$S"
EXIT_CODE=$(docker wait "$S")
test "$EXIT_CODE" = 0
docker inspect --format '{{json .State}}' "$S" |
  docker run --rm -i --network none "${BASE[@]}" "${ENV[@]}" "$I" -B m3_provider_restart_probe.py sender-exited
# Resolve only after the exact dedicated provider was restarted after sender reap.
test "$(docker inspect --format '{{index .Config.Labels "m3.isolated.provider-test"}}' "$P")" = "$STAMP"
docker restart -t 1 "$P"
capture bind-new
docker run --rm --network none "${BASE[@]}" "${ENV[@]}" "$I" -B m3_provider_restart_probe.py resolve
docker inspect --format '{{json .Id}} {{json .State}} {{json .Mounts}}' "$P" "$S" >"$R/container-evidence.log"
printf 'PASS evidence=%s\n' "$R"
