"""Exercise SMB batch I/O and MP4 finalization in isolated NAS folders."""
import argparse
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from uav_downloader.core.storage import storage_fs, storage_open
from uav_downloader.daemon import nas
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.sites.base import M3U8Crawler, locate_ffmpeg


def check_batch_smb(settings):
    if not settings.get('enabled') or settings.get('mode') != 'smb':
        return False
    root = nas.smb_destination()
    with nas.open_smb_storage(settings, root):
        filesystem = storage_fs(root)
        temporary = filesystem.mkdtemp(prefix='.uav-batch-check-', dir=root)
        try:
            child = dict(settings, directory='/'.join(filter(None, [settings.get('directory', '').replace('\\', '/'), os.path.basename(temporary)])))
            manager = TaskManager()
            batch_destination = (nas.smb_destination(), child)
            tasks = [manager.add_task(f'https://example.invalid/storage-check/{index}',
                                      batch_destination=batch_destination, folder_policy='new',
                                      subtitle_mode='none', enable_subtitles=False, download_cover=False,
                                      staging_dir='', defer_start=True) for index in range(2)]
            if any(task.status != 'pending' for task in tasks):
                raise ValueError('Batch tasks were not queued independently')
            with ExitStack() as connections:
                for task in tasks:
                    connections.enter_context(nas.open_smb_storage(task.nas_settings, task.dest))
                if tasks[0].to_dict()['final_dest'] != tasks[1].to_dict()['final_dest']:
                    raise ValueError('Batch destinations differ')
                names = [f'check-{task.task_id}.bin' for task in tasks]
                contents = [b'uav-batch-first', b'uav-batch-second']

                def write(index):
                    target = os.path.join(tasks[index].dest, names[index])
                    with storage_fs(target).open(target, 'xb') as stream:
                        if stream.write(contents[index]) != len(contents[index]):
                            raise OSError('Short write')

                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(write, range(2)))
                for index in range(2):
                    target = os.path.join(tasks[1-index].dest, names[index])
                    with storage_fs(target).open(target, 'rb') as stream:
                        if stream.read() != contents[index]:
                            raise OSError('Cross-connection read mismatch')
        finally:
            filesystem.rmtree(temporary)
        if filesystem.exists(temporary):
            raise OSError('Storage check cleanup failed')
    return True


def check_mp4_smb(settings):
    if not settings.get('enabled') or settings.get('mode') != 'smb':
        return False
    ffmpeg, ffprobe = locate_ffmpeg(), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('MP4 check requires FFmpeg and ffprobe')
    root = nas.smb_destination()
    with nas.open_smb_storage(settings, root):
        filesystem = storage_fs(root)
        temporary = filesystem.mkdtemp(prefix='.uav-mp4-check-中文🎬-', dir=root)
        try:
            for source_format in ('mpegts', 'mp4'):
                flags = ['-movflags', 'frag_keyframe+empty_moov'] if source_format == 'mp4' else []
                media = subprocess.check_output([
                    ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=128x128:r=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2',
                    '-c:v', 'libx264', '-c:a', 'aac', *flags, '-f', source_format, 'pipe:1'],
                    stderr=subprocess.PIPE, timeout=30)
                job = object.__new__(M3U8Crawler)
                job._dest_folder = temporary
                job._temp_folder = os.path.join(temporary, '片段-' + source_format)
                job._targetName = '驗證影片🎬-' + source_format
                job._tsList = ['synthetic-test-segment']
                job._cancel_job = False
                filesystem.makedirs(job._temp_folder)
                with storage_open(job._seg_savename(0), 'wb') as stream:
                    stream.write(media)
                if not job._mergeMp4Chunks():
                    raise ValueError('MP4 merge failed')
                output = os.path.join(temporary, job._targetName + '.mp4')
                with storage_open(output, 'rb') as stream:
                    boxes = []
                    while header := stream.read(8):
                        size = int.from_bytes(header[:4], 'big')
                        if len(header) != 8 or size < 8:
                            raise ValueError('Invalid MP4 box')
                        boxes.append(header[4:8])
                        stream.seek(size - 8, 1)
                if (b'moof' in boxes or b'moov' not in boxes or b'mdat' not in boxes
                        or boxes.index(b'moov') > boxes.index(b'mdat')):
                    raise ValueError('MP4 is fragmented or missing faststart')
                with filesystem.media_source(output) as url:
                    metadata = json.loads(subprocess.check_output([
                        ffprobe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', url],
                        stderr=subprocess.PIPE, timeout=30))
                    if ({stream['codec_name'] for stream in metadata['streams']} != {'h264', 'aac'}
                            or not 1.9 < float(metadata['format']['duration']) < 2.3):
                        raise ValueError('MP4 audio/video or duration mismatch')
                    subprocess.run([ffmpeg, '-v', 'error', '-ss', '1', '-i', url, '-f', 'null', '-'],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        finally:
            filesystem.rmtree(temporary)
        if filesystem.exists(temporary):
            raise OSError('MP4 check cleanup failed')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-smb', action='store_true')
    args = parser.parse_args()
    try:
        settings = nas.load_settings(secret=True)
    except Exception:
        print('SMB settings: FAILED', flush=True)
        raise SystemExit(1) from None
    for stage, check in [('SMB concurrent batch write/read/cleanup', check_batch_smb),
                         ('SMB MP4 Unicode remux/faststart/seek/cleanup', check_mp4_smb)]:
        try:
            checked = check(settings)
            if args.require_smb and not checked:
                raise ValueError('SMB is required')
        except Exception:
            print(stage + ': FAILED', flush=True)
            raise SystemExit(1) from None
        print(stage + ': ' + ('OK' if checked else 'SKIPPED (not SMB mode)'), flush=True)


if __name__ == '__main__':
    main()
