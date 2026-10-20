#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
image=${1:?usage: check_smb_batch_container.sh IMAGE}
network="uav-smb-ci-$$"
samba="uav-smb-ci-server-$$"
samba_image="uav-smb-ci:check-$$"
runner="uav-smb-ci-runner-$$"
cleanup() {
  docker rm -fv "$runner" "${runner}-subtitles" "$samba" >/dev/null 2>&1 || true
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
docker run --rm --name "${runner}-subtitles" --network "$network" --user 1000:1000 \
  -e CONFIG_DIR=/tmp/uav-smb-subtitle-check -e APPDATA=/tmp/uav-smb-subtitle-check/appdata \
  --entrypoint python "$image" -c '
import os, tempfile
from types import SimpleNamespace
from uav_downloader.core.storage import storage_fs, storage_open
from uav_downloader.daemon import nas
from uav_downloader.daemon.subtitle_files import SubtitleFileManager
from uav_downloader.subtitles.engine import SrtCue, _format_srt_timestamp, render_srt
from uav_downloader.subtitles.models import SubtitleSegment

nas.save_settings(dict(enabled=True, mode="smb", host="storage-check", share="check", directory="", username="smbtest", password="smb-ci-only"))
settings = nas.load_settings(secret=True)
manager = SubtitleFileManager(state_path="/tmp/uav-smb-subtitle-check/sources.json")
root = nas.smb_destination()
token = os.urandom(6).hex()
paths = []
staging = tempfile.mkdtemp(prefix="uav-smb-subtitle-upload-")
try:
    direct_video = os.path.join(root, "w8-direct-" + token + ".mp4")
    with nas.open_smb_storage(settings, root):
        with storage_open(direct_video, "xb") as stream:
            stream.write(b"synthetic direct SMB video")
        direct = manager.register_smb(direct_video, storage_root=root, nas_settings=settings)
    listed = manager.list_videos()
    assert any(item["video_id"] == direct.video_id and item["source"] == "smb" for item in listed), (
        "real SMB list_videos did not return the newly created direct source", manager.list_errors)
    direct_result = SimpleNamespace(result_id="w8-direct-" + token, video_id=direct.video_id,
        stage="translation", language="en", segments=(SubtitleSegment("w8", 1.0, 2.0, "direct SMB subtitle"),))
    direct_publish = manager.publish_result(direct.video_id, direct_result, "en")
    direct_srt = os.path.join(root, os.path.splitext(os.path.basename(direct_video))[0] + ".en.srt")
    expected = render_srt([SrtCue("1", _format_srt_timestamp(1.0) + " --> " + _format_srt_timestamp(2.0), "direct SMB subtitle")]).encode()
    with nas.open_smb_storage(settings, root):
        assert storage_fs(direct_srt).open(direct_srt, "rb").read() == expected
        names = storage_fs(root).listdir(root)
        assert not any(name.startswith(".uav-subtitle-") for name in names), names
    assert direct_publish["publish_status"] == "published"
    paths.extend([direct_video, direct_srt, os.path.splitext(direct_video)[0] + ".uav-subtitles.json"])

    local_video = os.path.join(staging, "w8-upload-" + token + ".mp4")
    with open(local_video, "xb") as stream:
        stream.write(b"synthetic upload video")
    remote_video = os.path.join(root, os.path.basename(local_video))
    paths.append(remote_video)
    with nas.open_smb_storage(settings, root):
        with storage_open(remote_video, "xb") as stream:
            stream.write(open(local_video, "rb").read())
        upload = manager.register_upload(local_video, allowed_roots=[staging], nas_settings=settings,
            remote_root=root, remote_video_path=remote_video)
    upload_result = SimpleNamespace(result_id="w8-upload-" + token, video_id=upload.video_id,
        stage="translation", language="en", segments=(SubtitleSegment("w8", 1.0, 2.0, "uploaded subtitle"),))
    uploaded = manager.publish_result(upload.video_id, upload_result, "en")
    upload_srt = os.path.join(root, os.path.splitext(os.path.basename(remote_video))[0] + ".en.srt")
    paths.extend([upload_srt, os.path.splitext(remote_video)[0] + ".uav-subtitles.json"])
    upload_expected = render_srt([SrtCue("1", _format_srt_timestamp(1.0) + " --> " + _format_srt_timestamp(2.0), "uploaded subtitle")]).encode()
    with nas.open_smb_storage(settings, root):
        assert storage_fs(upload_srt).open(upload_srt, "rb").read() == upload_expected
        names = storage_fs(root).listdir(root)
        assert not any(name.startswith(".uav-subtitle-") for name in names), names
    assert open(local_video, "rb").read() == b"synthetic upload video", "local staging media is preserved"
    assert uploaded["publish_status"] == "published"
    paths.extend([remote_video, upload_srt, os.path.splitext(remote_video)[0] + ".uav-subtitles.json"])
    print("subtitle SMB source/publication: direct=PASS upload=PASS staging-preserved=PASS")
finally:
    try:
        with nas.open_smb_storage(settings, root):
            filesystem = storage_fs(root)
            for name in paths:
                if filesystem.exists(name):
                    filesystem.remove(name)
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
'
