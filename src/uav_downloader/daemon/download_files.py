"""Exact video identity checks and files owned by one Web download task."""
import os
import re
import uuid

from uav_downloader.core.storage import storage_fs, storage_open
from uav_downloader.core.video_identity import canonical_code, video_code
from uav_downloader.sites.base import _truncate_target_name, remux_to_mp4


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.ts'}


def video_key(url, title=''):
    return video_code({'url': url, 'title': title}) or url.strip().casefold()


def existing_video(destination, url, title=''):
    filesystem = storage_fs(destination)
    if not filesystem.exists(destination):
        return False
    code = video_code({'url': url, 'title': title})
    for name in filesystem.listdir(destination):
        stem, extension = os.path.splitext(name)
        if extension.casefold() not in VIDEO_EXTENSIONS:
            continue
        if ((code and canonical_code(stem) == code) or (title and stem == title)):
            if filesystem.isfile(os.path.join(destination, name)):
                return True
    return False


class DownloadFiles:
    """Keep partial files private and remove only outputs this task published."""

    def __init__(self, destination, task_id, staging_dir=''):
        self.destination = destination
        self.staging_dir = staging_dir
        workspace = staging_dir or destination
        self.filesystem = storage_fs(workspace)
        self.filesystem.makedirs(workspace, exist_ok=True)
        self.workdir = self.filesystem.mkdtemp(prefix=f'.uav-{task_id}-', dir=workspace)
        self.published = []

    def snapshot(self):
        return {'workdir': self.workdir, 'published': self.published[:]}

    @classmethod
    def restore(cls, destination, task_id, record, staging_dir=''):
        destination = os.path.abspath(destination)
        workdir = os.path.abspath(record['workdir'])
        published = record.get('published', [])
        if (os.path.dirname(workdir) != os.path.abspath(staging_dir or destination) or
                not os.path.basename(workdir).startswith(f'.uav-{task_id}-') or
                any(os.path.dirname(os.path.abspath(path)) != destination for path in published)):
            raise ValueError('下載暫存檔紀錄不屬於此任務')
        files = cls.__new__(cls)
        files.destination, files.workdir = destination, workdir
        files.staging_dir = staging_dir
        files.filesystem = None
        files.published = published
        return files

    def publish(self, cancelled, *, overwrite=False):
        target_fs = storage_fs(self.destination)
        target_fs.makedirs(self.destination, exist_ok=True)
        for name in self.filesystem.listdir(self.workdir):
            if cancelled.is_set():
                return
            source = os.path.join(self.workdir, name)
            target = os.path.join(self.destination, name)
            if not self.filesystem.isfile(source):
                continue
            if self.staging_dir and target in self.published:
                if not target_fs.islink(target) and target_fs.isfile(target) and target_fs.getsize(target) == self.filesystem.getsize(source):
                    continue
                raise OSError('已發布的任務檔案遭到變更，請檢查目的地後重試')
            replacing = target_fs.exists(target)
            if target_fs.islink(target):
                raise ValueError('目的地檔案不可使用符號連結')
            if replacing and not overwrite and os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            if self.staging_dir:
                temporary = os.path.join(self.destination, '.uav-' + uuid.uuid4().hex + '.part')
                try:
                    with self.filesystem.open(source, 'rb') as stream, target_fs.open(temporary, 'xb') as output:
                        while chunk := stream.read(1024 * 1024):
                            if cancelled.is_set():
                                return
                            if output.write(chunk) != len(chunk):
                                raise OSError('完成檔案傳送不完整，暫存檔已保留')
                        output.flush()
                        if not target_fs.remote:
                            os.fsync(output.fileno())
                    if target_fs.getsize(temporary) != self.filesystem.getsize(source):
                        raise OSError('完成檔案大小不符，暫存檔已保留')
                    if cancelled.is_set():
                        return
                    (target_fs.replace if overwrite else target_fs.publish)(temporary, target)
                finally:
                    if target_fs.exists(temporary):
                        target_fs.remove(temporary)
            else:
                (target_fs.replace if overwrite else self.filesystem.publish)(source, target)
            if not replacing:
                self.published.append(target)

    def export_partial(self, destination, name, cancelled, paused, stage):
        filesystem = storage_fs(self.workdir)
        if filesystem.islink(self.workdir) or not filesystem.isdir(self.workdir):
            raise ValueError('找不到此任務的片段暫存目錄')
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('影片檔名無效')

        groups = []
        for directory in filesystem.listdir(self.workdir):
            folder = os.path.join(self.workdir, directory)
            if filesystem.islink(folder) or not filesystem.isdir(folder):
                continue
            segments = []
            for entry in filesystem.listdir(folder):
                if cancelled.is_set() or paused.is_set():
                    return None
                match = re.fullmatch(r'([0-9]{6,})\.mp4', entry)
                if match:
                    path = os.path.join(folder, entry)
                    if not filesystem.islink(path) and filesystem.isfile(path) and filesystem.getsize(path):
                        segments.append((int(match[1]), path))
            if segments:
                groups.append(segments)
        if not groups:
            raise ValueError('找不到已下載的影片片段，請先續傳')
        if len(groups) != 1:
            raise ValueError('找到多份片段目錄，無法確認哪些片段屬於同一份影片')

        target = os.path.join(destination, '不完整影片')
        target_fs = storage_fs(target)
        if target_fs.islink(target):
            raise ValueError('不完整影片的存放目錄不可使用符號連結')
        export = DownloadFiles(target, 'partial', self.workdir)
        try:
            longest_folder = max((target, export.workdir), key=lambda path: len(path.encode('utf-16-le')))
            stem = _truncate_target_name(name, longest_folder, os.path.basename(self.workdir))
            filename = f'{stem}.mp4'
            if target_fs.exists(os.path.join(target, filename)):
                suffix = uuid.uuid4().hex[:8]
                stem = _truncate_target_name(f'{name}_{suffix}', longest_folder, suffix)
                filename = f'{stem}.mp4'
            remux_dir = os.path.join(export.workdir, 'remux')
            filesystem.makedirs(remux_dir)
            merged = os.path.join(remux_dir, 'merged.ts')
            output = os.path.join(remux_dir, 'out.mp4')
            segments = sorted(groups[0])
            with storage_open(merged, 'wb') as out:
                for index, (_, path) in enumerate(segments):
                    stage('強制合併影片片段', index, len(segments))
                    with storage_open(path, 'rb') as source:
                        while chunk := source.read(1024 * 1024):
                            if cancelled.is_set() or paused.is_set():
                                return None
                            out.write(chunk)
            stage('重新封裝不完整影片', len(segments), len(segments))
            if not remux_to_mp4(merged, output, remux_dir,
                                lambda: cancelled.is_set() or paused.is_set()):
                if cancelled.is_set() or paused.is_set():
                    return None
                raise RuntimeError('不完整影片封裝失敗，已保留下載片段')
            if cancelled.is_set() or paused.is_set():
                return None
            filesystem.remove(merged)
            filesystem.replace(output, os.path.join(export.workdir, filename))
            stage('儲存不完整影片')
            export.publish(cancelled)
            if export.published:
                return filename
        finally:
            export.filesystem.rmtree(export.workdir, ignore_errors=True)

    def cleanup(self, *, cancelled=False):
        if cancelled:
            for target in self.published[:]:
                try:
                    storage_fs(self.destination).remove(target)
                except FileNotFoundError:
                    pass
                self.published.remove(target)
        if self.filesystem.exists(self.workdir):
            self.filesystem.rmtree(self.workdir)
