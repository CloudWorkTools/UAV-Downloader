#!/usr/bin/env bash
set -euo pipefail
image=${1:?usage: check_web_container.sh IMAGE}
scratch=$(mktemp -d)
container=$(docker run -d -p 127.0.0.1::8080 "$image")
cleanup() {
  docker logs "$container"
  docker rm -fv "$container" >/dev/null
  rm -rf "$scratch"
}
trap cleanup EXIT
port=$(docker port "$container" 8080/tcp | awk -F: '{print $NF}')
for attempt in {1..30}; do
  if curl --fail --silent "http://127.0.0.1:$port/api/status" > "$scratch/status.json"; then
    break
  fi
  sleep 2
done
curl --fail --silent "http://127.0.0.1:$port/" -o "$scratch/index.html"
grep -q 'select-page-button' "$scratch/index.html"
for asset in mobile-actions.js mobile-actions.css mascot-fab.svg; do
  curl --fail --silent "http://127.0.0.1:$port/$asset" -o "$scratch/$asset"
  test -s "$scratch/$asset"
done
curl --fail --silent "http://127.0.0.1:$port/nas-settings.js" -o "$scratch/nas-settings.js"
grep -q '^window.NasSettings' "$scratch/nas-settings.js"
curl --fail --silent "http://127.0.0.1:$port/video-selection.js" -o "$scratch/video-selection.js"
grep -q '^window.createVideoSelection' "$scratch/video-selection.js"
curl --fail --silent "http://127.0.0.1:$port/api/settings" | python3 -c 'import json,sys; s=json.load(sys.stdin); assert "nas" in s and "password" not in s["nas"]'
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "ok"' "$scratch/status.json"
rm -f "$scratch/status.json"
