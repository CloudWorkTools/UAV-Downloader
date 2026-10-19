#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
image=${1:?usage: check_smb_batch_container.sh IMAGE}
network="uav-smb-ci-$$"
samba="uav-smb-ci-server-$$"
samba_image="uav-smb-ci:check-$$"
runner="uav-smb-ci-runner-$$"
cleanup() {
  docker rm -fv "$runner" "$samba" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  docker image rm "$samba_image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build -t "$samba_image" tools/smb-ci
docker network create --internal "$network" >/dev/null
docker run -d --network "$network" --network-alias storage-check --name "$samba" "$samba_image" >/dev/null
ready=false
for attempt in {1..30}; do
  if docker exec "$samba" smbcontrol smbd ping >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
if [ "$ready" != true ]; then docker logs "$samba"; exit 1; fi
docker run --rm --name "$runner" --network "$network" --user 1000:1000 -e CONFIG_DIR=/tmp/uav-smb-check --entrypoint python "$image" -c '
from uav_downloader.daemon import nas
from uav_downloader.daemon.storage_smoke import main
nas.save_settings(dict(enabled=True, mode="smb", host="storage-check", share="check", directory="", username="smbtest", password="smb-ci-only"))
import sys
sys.argv = ["storage_smoke", "--require-smb"]
main()
'
