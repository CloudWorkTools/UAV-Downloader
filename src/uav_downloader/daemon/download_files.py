"""Exact video identity checks and files owned by one Web download task."""
import os
import uuid

from uav_downloader.core.storage import storage_fs
from uav_downloader.core.video_identity import canonical_code, video_code


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

    def publish(self, cancelled):
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
            if target_fs.exists(target) and os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
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
                    target_fs.publish(temporary, target)
                finally:
                    if target_fs.exists(temporary):
                        target_fs.remove(temporary)
            else:
                self.filesystem.publish(source, target)
            self.published.append(target)

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
