"""Exact video identity checks and files owned by one Web download task."""
import os

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

    def __init__(self, destination, task_id):
        self.destination = destination
        self.filesystem = storage_fs(destination)
        self.filesystem.makedirs(destination, exist_ok=True)
        self.workdir = self.filesystem.mkdtemp(prefix=f'.uav-{task_id}-', dir=destination)
        self.published = []

    def publish(self, cancelled):
        for name in self.filesystem.listdir(self.workdir):
            if cancelled.is_set():
                return
            source = os.path.join(self.workdir, name)
            target = os.path.join(self.destination, name)
            if not self.filesystem.isfile(source):
                continue
            if self.filesystem.exists(target) and os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            self.filesystem.publish(source, target)
            self.published.append(target)

    def cleanup(self, *, cancelled=False):
        if cancelled:
            for target in self.published[:]:
                try:
                    self.filesystem.remove(target)
                except FileNotFoundError:
                    pass
                self.published.remove(target)
        if self.filesystem.exists(self.workdir):
            self.filesystem.rmtree(self.workdir)
