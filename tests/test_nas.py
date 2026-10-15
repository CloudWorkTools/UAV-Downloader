import io
import json
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from uav_downloader.daemon import nas
from uav_downloader.daemon.task_manager import DownloadTask, TaskManager


@pytest.fixture
def profile(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    return dict(enabled=True, host='192.168.1.10', share='video', directory='UAV',
                username='user', password='nas-secret')


def test_persistence_encryption_and_blank_password(profile):
    nas.save_settings(profile)
    raw = (nas._directory() / 'nas.json').read_text()
    assert 'nas-secret' not in raw
    assert 'password' not in nas.load_settings()
    assert nas.load_settings()['password_saved']
    assert nas.load_settings(secret=True)['password'] == profile['password']
    nas.save_settings({**profile, 'password': '', 'directory': 'other'})
    assert nas.load_settings(secret=True)['password'] == profile['password']
    assert (nas._directory() / 'nas.json').stat().st_mode & 0o777 == 0o600
    assert (nas._directory() / 'nas.key').stat().st_mode & 0o777 == 0o600
    with pytest.raises(nas.NasError):
        nas.save_settings({**profile, 'password': '', 'host': '192.168.1.11'})


@pytest.mark.parametrize('changes', [dict(host='//evil/share'), dict(share='../video'),
    dict(directory='../outside'), dict(directory='x/../../y'), dict(enabled='true'),
    dict(username='user\nsecret'), dict(password=123)])
def test_invalid_settings_do_not_replace_saved_profile(profile, changes):
    nas.save_settings(profile)
    before = (nas._directory() / 'nas.json').read_bytes()
    with pytest.raises(nas.NasError):
        nas.save_settings({**profile, **changes})
    assert (nas._directory() / 'nas.json').read_bytes() == before


def test_replacing_unreadable_password(profile):
    nas.save_settings(profile)
    (nas._directory() / 'nas.key').write_bytes(b'x' * 32)
    with pytest.raises(nas.NasError):
        nas.load_settings(secret=True)
    nas.save_settings({**profile, 'password': 'replacement'})
    assert nas.load_settings(secret=True)['password'] == 'replacement'


class Remote:
    def __init__(self):
        self.files = {}
        self.fail = False
        self.path = self

    @contextmanager
    def open_file(self, path, mode, **kwargs):
        assert mode == 'xb'
        if self.fail:
            raise OSError('private credentials should never escape')
        stream = io.BytesIO()
        try:
            yield stream
        finally:
            self.files[path] = stream.getvalue()

    def rename(self, src, dst, **kwargs):
        if dst in self.files:
            raise FileExistsError(dst)
        self.files[dst] = self.files.pop(src)

    def exists(self, path, **kwargs):
        return path in self.files

    def remove(self, path, **kwargs):
        del self.files[path]


@pytest.fixture
def remote(monkeypatch):
    import smbclient
    remote = Remote()
    monkeypatch.setattr(smbclient, 'register_session', lambda *a, **kw: None)
    monkeypatch.setattr(smbclient, 'reset_connection_cache', lambda **kw: None)
    for name in ('open_file', 'rename', 'remove'):
        monkeypatch.setattr(smbclient, name, getattr(remote, name))
    monkeypatch.setattr(smbclient.path, 'exists', remote.exists)
    return remote


def test_probe_is_removed_and_does_not_save(profile, remote):
    nas.test_connection(profile)
    assert not remote.files
    assert not (nas._directory() / 'nas.json').exists()


def test_upload_sidecars_preserves_local_and_remote_files(profile, remote, tmp_path):
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'video')
    (tmp_path / 'movie.zh-TW.srt').write_text('subtitle')
    (tmp_path / 'other.srt').write_text('other')
    cancelled = threading.Event()
    nas.upload_video(profile, video, cancelled)
    assert sorted(remote.files.values()) == [b'subtitle', b'video']
    assert video.read_bytes() == b'video'
    with pytest.raises(nas.NasError, match='本機檔案已保留'):
        nas.upload_video(profile, video, cancelled)
    assert len(remote.files) == 2
    assert not any(p.endswith('.part') for p in remote.files)


def test_upload_failure_and_cancel_preserve_local(profile, remote, tmp_path):
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'video')
    remote.fail = True
    with pytest.raises(nas.NasError) as error:
        nas.upload_video(profile, video, threading.Event())
    assert 'private credentials' not in str(error.value)
    assert video.exists()
    remote.fail = False
    cancelled = threading.Event()
    cancelled.set()
    nas.upload_video(profile, video, cancelled)
    assert not remote.files


def test_queue_uses_local_default_and_snapshots_nas(profile, monkeypatch, tmp_path):
    nas.save_settings(profile)
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path))
    manager = TaskManager()
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    task = manager.add_task('https://jable.tv/videos/test/')
    assert task.dest == str(tmp_path)
    assert task.nas_settings['password'] == profile['password']
    assert 'nas-secret' not in json.dumps(task.to_dict())
    assert 'nas-secret' not in repr(task)


def test_queue_only_completes_after_upload(profile, monkeypatch, tmp_path):
    from uav_downloader.daemon import task_manager
    job = MagicMock()
    job._cancel_job = False
    job.target_name.return_value = 'movie'
    job.dest_folder.return_value = str(tmp_path)
    (tmp_path / 'movie.mp4').write_bytes(b'video')
    monkeypatch.setattr(task_manager.M3U8Sites, 'CreateSite', lambda *a, **kw: job)
    monkeypatch.setattr(task_manager, 'get_history_manager', MagicMock())
    task = DownloadTask('test', 'https://jable.tv/videos/test/', str(tmp_path), nas_settings=profile)
    def upload(*args):
        assert task.status == 'uploading'
        raise nas.NasError('NAS unavailable; local retained')
    monkeypatch.setattr(nas, 'upload_video', upload)
    TaskManager()._run_task(task)
    assert task.status == 'failed'
    assert 'local retained' in task.error
    assert (tmp_path / 'movie.mp4').exists()
