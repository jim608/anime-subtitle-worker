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
exec 9>"$R/owner.lock"
flock -n 9
P="m3-fixture-provider-$STAMP"
S="m3-fixture-sender-$STAMP"
mkdir "$R/candidate" "$R/fixture"
git -C "$W" archive HEAD | tar -x -C "$R/candidate"
cp "$W/m3_provider_restart_probe.py" "$R/candidate/"
printf '%s\n' "$I" >"$R/image.txt"
cleanup() {
  ORIGINAL_EXIT=$?
  trap - EXIT INT TERM
  set +e  # cleanup warnings must not replace the original test exit status
  printf '%s\n' cleanup_ready >"$R/phase"
  for C in "$S" "$P"; do
    if docker inspect "$C" >/dev/null 2>&1; then
      L=$(docker inspect --format '{{index .Config.Labels "m3.isolated.provider-test"}}' "$C")
      if [ "$L" = "$STAMP" ] &&
         [ "$(docker inspect --format '{{index .Config.Labels "org.anime-subtitle.creator"}}' "$C")" = m3_provider_restart_test.sh ]; then
        ID=$(docker inspect --format '{{.Id}}' "$C")
        docker inspect "$ID" >"$R/$ID-inspect.json"
        # Only this active lease's own test may be stopped. A cleanup never
        # changes Production restart policies or removes Docker volumes.
        docker stop --time 5 "$ID" >>"$R/cleanup.log" 2>&1 || true
        STATE=$(docker inspect --format '{{.State.Status}}' "$ID")
        if docker logs --timestamps "$ID" >"$R/$ID.log" 2>&1 &&
           { [ "$STATE" = exited ] || [ "$STATE" = created ]; }; then
          docker inspect "$ID" >"$R/$ID-final.json"
          if [ "$(docker inspect --format '{{.State.Status}}' "$ID")" = "$STATE" ]; then
            docker rm "$ID" >>"$R/cleanup.log" 2>&1 || printf 'cleanup failed %s\n' "$ID" >>"$R/cleanup-warning.log"
          else
            printf 'state changed; retained %s\n' "$ID" >>"$R/cleanup-warning.log"
          fi
        else
          printf 'evidence export/state check failed %s\n' "$ID" >>"$R/cleanup-warning.log"
        fi
      else
        printf 'cleanup refused label mismatch %s\n' "$C" >>"$R/cleanup.log"
      fi
    fi
  done
  exit "$ORIGINAL_EXIT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Bounded next-entry cleanup. Keep active leases and pending restart evidence.
COUNT=0
for OLD in "$W"/logs/m3-baseline-20260908T070237Z/provider-termination-*; do
  [ "$COUNT" -lt 20 ] || break
  [ -f "$OLD/owner.lock" ] && [ -f "$OLD/phase" ] || continue
  [ "$(cat "$OLD/phase")" = cleanup_ready ] || continue
  COUNT=$((COUNT + 1))
  (
    flock -n 8 || exit 0
    for CIDFILE in "$OLD"/*.cid; do
      [ -f "$CIDFILE" ] || continue
      ID=$(cat "$CIDFILE")
      [[ "$ID" =~ ^[0-9a-f]{64}$ ]] || continue
      docker inspect "$ID" >/dev/null 2>&1 || continue
      [ "$(docker inspect --format '{{index .Config.Labels "org.anime-subtitle.creator"}}' "$ID")" = m3_provider_restart_test.sh ] || continue
      [ "$(docker inspect --format '{{index .Config.Labels "org.anime-subtitle.temporary"}}' "$ID")" = true ] || continue
      [ "$(docker inspect --format '{{index .Config.Labels "org.anime-subtitle.project"}}' "$ID")" = anime-subtitle-platform ] || continue
      [ "$(docker inspect --format '{{index .Config.Labels "m3.isolated.provider-test"}}' "$ID")" = "$(basename "$OLD")" ] || continue
      STATE=$(docker inspect --format '{{.State.Status}}' "$ID")
      [ "$STATE" = exited ] || [ "$STATE" = created ] || continue
      docker inspect "$ID" >"$OLD/$ID-orphan-inspect.json"
      if docker logs --timestamps "$ID" >"$OLD/$ID.log" 2>&1 &&
         [ "$(docker inspect --format '{{.State.Status}}' "$ID")" = "$STATE" ]; then
        docker rm "$ID" >>"$OLD/cleanup.log" 2>&1 || printf 'orphan cleanup failed %s\n' "$ID" >>"$OLD/cleanup-warning.log"
      fi
    done
  ) 8<>"$OLD/owner.lock"
done
printf '%s\n' restart_pending >"$R/phase"
BASE=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --memory 512m --cpus 0.5
      --label org.anime-subtitle.project=anime-subtitle-platform --label org.anime-subtitle.temporary=true
      --label org.anime-subtitle.purpose=provider-restart-validation --label "org.anime-subtitle.run-id=$STAMP"
      --label org.anime-subtitle.creator=m3_provider_restart_test.sh
      -v "$R/candidate:/candidate:ro" -v "$R/fixture:/fixture:rw" -w /candidate
      -e PYTHONDONTWRITEBYTECODE=1 -e M3_ISOLATED_PROVIDER_TEST=1 --entrypoint python)
PORT=$(shuf -i 40000-60000 -n 1)
docker run -d --name "$P" --cidfile "$R/provider.cid" --label "m3.isolated.provider-test=$STAMP" --network bridge \
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
docker create --name "$S" --cidfile "$R/sender.cid" --label "m3.isolated.provider-test=$STAMP" --network bridge \
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
