"""No mounts: exercise the storage seam, seekable media bridge and NAS isolation."""
import errno
import io
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from uav_downloader.core.smb_storage import SmbStorage
from uav_downloader.core.storage import LocalStorage, SMB_PATH_ROOT, StorageWriteError, registered_storage, storage_fs, storage_open
from uav_downloader.daemon import nas
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.sites import base


@pytest.fixture
def smb(monkeypatch, tmp_path):
    import smbclient
    import smbclient.shutil
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    root = os.path.join(SMB_PATH_ROOT, 'unit-test')
    folder = tmp_path / 'share'
    folder.mkdir()
    def path(remote):
        assert remote.startswith('\\\\nas\\share')
        return folder.joinpath(*remote[len('\\\\nas\\share'):].strip('\\').split('\\'))
    monkeypatch.setattr(smbclient, 'register_session', MagicMock())
    monkeypatch.setattr(smbclient, 'reset_connection_cache', MagicMock())
    def open_file(name, mode, **kwargs):
        if kwargs.get('create_options'):
            from smbprotocol.open import CreateOptions
            assert kwargs['create_options'] == CreateOptions.FILE_DELETE_ON_CLOSE
            assert kwargs.get('share_access') is None
            handle = LocalStorage().lock(path(name))
            class Lock:
                def close(self):
                    path(name).unlink(missing_ok=True)
                    handle.close()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.close()
            return Lock()
        return open(path(name), mode, **{k:v for k,v in kwargs.items() if k not in ('connection_cache', 'share_access')})
    monkeypatch.setattr(smbclient, 'open_file', open_file)
    monkeypatch.setattr(smbclient, 'listdir', lambda name, **kw: os.listdir(path(name)))
    monkeypatch.setattr(smbclient, 'stat', lambda name, **kw: path(name).stat(follow_symlinks=kw.get('follow_symlinks', True)))
    monkeypatch.setattr(smbclient, 'makedirs', lambda name, **kw: path(name).mkdir(parents=True, exist_ok=kw.get('exist_ok', False)))
    monkeypatch.setattr(smbclient, 'mkdir', lambda name, **kw: path(name).mkdir())
    monkeypatch.setattr(smbclient, 'rmdir', lambda name, **kw: path(name).rmdir())
    monkeypatch.setattr(smbclient, 'remove', lambda name, **kw: path(name).unlink())
    monkeypatch.setattr(smbclient, 'replace', lambda src, dst, **kw: path(src).replace(path(dst)))
    def rename(src, dst, **kw):
        if path(dst).exists():
            raise FileExistsError(errno.EEXIST, 'already exists')
        path(src).rename(path(dst))
    monkeypatch.setattr(smbclient, 'rename', rename)
    monkeypatch.setattr(smbclient.shutil, 'rmtree', lambda name, **kw: shutil.rmtree(path(name), ignore_errors=kw.get('ignore_errors', False)))
    settings = dict(host='nas', share='share', directory='', username='test', password='test-only', mode='smb', enabled=True, _smb_path=root)
    backend = SmbStorage(settings, root)
    with registered_storage(root, backend):
        yield SimpleNamespace(root=root, folder=folder, backend=backend, settings=settings)
    backend.close()


def test_virtual_paths_never_fall_back_to_local_disk():
    path = os.path.join(SMB_PATH_ROOT, 'not-connected', 'movie.mp4')
    with pytest.raises(StorageWriteError, match='not active'):
        storage_open(path, 'wb')
    assert not Path(path).exists()


def test_smb_writes_seek_and_exclusive_publish(smb):
    temporary = os.path.join(smb.root, 'video.part')
    target = os.path.join(smb.root, 'video.mp4')
    with storage_open(temporary, 'xb') as stream:
        stream.truncate(10)
        stream.seek(4)
        stream.write(b'abcd')
    smb.backend.publish(temporary, target)
    assert (smb.folder / 'video.mp4').read_bytes() == b'\0\0\0\0abcd\0\0'
    with storage_open(temporary, 'wb') as stream:
        stream.write(b'new')
    with pytest.raises(FileExistsError):
        smb.backend.publish(temporary, target)
    assert (smb.folder / 'video.mp4').read_bytes()[4:8] == b'abcd'


def test_smb_lock_and_existing_file_protection(smb):
    with nas.reserve_direct_target(smb.settings, 'video.mp4'):
        with pytest.raises(nas.NasError, match='任務使用中'):
            with nas.reserve_direct_target(smb.settings, 'video.mp4'):
                pass
    assert not list(smb.folder.glob('*.lock'))
    (smb.folder / 'video.mp4').write_bytes(b'existing')
    with pytest.raises(nas.NasError, match='同名影片'):
        with nas.reserve_direct_target(smb.settings, 'video.mp4'):
            pass
    assert (smb.folder / 'video.mp4').read_bytes() == b'existing'


def test_media_bridge_ranges_and_random_access(smb):
    (smb.folder / 'video.mp4').write_bytes(b'0123456789')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with smb.backend.media_source(os.path.join(smb.root, 'video.mp4')) as url:
        with opener.open(urllib.request.Request(url, headers={'Range':'bytes=3-6'})) as response:
            assert response.status == 206
            assert response.headers['Content-Range'] == 'bytes 3-6/10'
            assert response.read() == b'3456'
        with opener.open(urllib.request.Request(url, headers={'Range':'bytes=-2'})) as response:
            assert response.read() == b'89'
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(url + '/other')
        assert error.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(urllib.request.Request(url, headers={'Range':'bytes=30-40'}))
        assert error.value.code == 416


def test_disconnection_is_storage_failure_not_missing_file(smb, monkeypatch):
    import smbclient
    def disconnected(*args, **kwargs):
        raise RuntimeError('private details must not escape')
    monkeypatch.setattr(smbclient, 'stat', disconnected)
    with pytest.raises(OSError, match='SMB') as error:
        smb.backend.exists(os.path.join(smb.root, 'video.mp4'))
    assert 'private details' not in str(error.value)


def test_smb_subtitle_sidecars_and_identity(smb, tmp_path):
    from uav_downloader.subtitles import engine
    video = os.path.join(smb.root, 'video.mp4')
    content = b'video-bytes' * 100
    (smb.folder / 'video.mp4').write_bytes(content)
    local = tmp_path / 'local.mp4'
    local.write_bytes(content)
    assert engine._media_identity(video) == engine._media_identity(str(local))
    subtitle = os.path.join(smb.root, 'video.ja.srt')
    text = '1\n00:00:00,000 --> 00:00:01,000\n字幕\n'
    engine._atomic_write_text(subtitle, text)
    assert engine._read_srt_text(subtitle) == text
    assert engine._existing(subtitle)
    copied = os.path.join(smb.root, 'video.zh-TW.srt')
    engine._atomic_copy(subtitle, copied)
    assert (smb.folder / 'video.zh-TW.srt').read_text() == text
    with engine._destination_workspace(video) as workspace:
        assert not workspace.startswith(SMB_PATH_ROOT)
    assert not Path(workspace).exists()


def test_smb_settings_and_queue_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    settings = dict(enabled=True, mode='smb', host='smb://nas.local/share/films', username='account', password='secret')
    nas.save_settings(settings)
    assert nas.load_settings()['mode'] == 'smb'
    assert nas.load_settings(secret=True)['password'] == 'secret'
    nas.save_settings(dict(settings, host='nas.local/share/films', password=''))
    monkeypatch.setattr(nas, 'test_connection', lambda settings: None)
    task = TaskManager().add_task('https://jable.tv/videos/example/')
    assert task.dest.startswith(SMB_PATH_ROOT)
    assert task.nas_settings['password'] == 'secret'
    assert task.to_dict()['dest'] == 'smb://nas.local/share/films'
    assert 'secret' not in str(task.to_dict())
    assert not Path(task.dest).exists()


def test_hls_merge_and_ffmpeg_stream_back_to_smb(smb):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg not installed')
    media = subprocess.check_output([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=128x128:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', '-f', 'mpegts', 'pipe:1'])
    job = object.__new__(base.M3U8Crawler)
    job._dest_folder = smb.root
    job._temp_folder = os.path.join(smb.root, 'segments')
    job._targetName = 'merged-video'
    job._tsList = ['first']
    job._cancel_job = False
    smb.backend.makedirs(job._temp_folder)
    with storage_open(job._seg_savename(0), 'wb') as segment:
        segment.write(media)
    assert job._mergeMp4Chunks() > 0
    target = smb.folder / 'merged-video.mp4'
    assert target.read_bytes()[4:8] == b'ftyp'
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(target), '-f', 'null', '-'], check=True)
    assert not list(smb.folder.glob('uav-remux-*'))
    assert not (smb.folder / 'segments').exists()


def test_remote_existing_output_is_not_deleted_during_merge(smb):
    existing = smb.folder / 'existing.mp4'
    existing.write_bytes(b'preserve')
    job = object.__new__(base.M3U8Crawler)
    job._dest_folder = smb.root
    job._targetName = 'existing'
    with pytest.raises(StorageWriteError):
        job._mergeMp4Chunks()
    assert existing.read_bytes() == b'preserve'


def test_unused_url_credentials_are_not_saved_unencrypted(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    nas.save_settings(dict(enabled=True, mode='smb', host='nas.local/share',
                          username='account', password='secret',
                          direct_path='smb://account:old-private-value@nas.local/share'))
    assert 'old-private-value' not in (tmp_path / 'nas.json').read_text()
    assert nas.load_settings()['direct_path'] == ''


def test_queue_opens_smb_before_downloader_and_closes_after_finish(smb, monkeypatch):
    import uav_downloader.daemon.task_manager as manager_module
    monkeypatch.setattr(nas, 'load_settings', lambda **kwargs: dict(smb.settings))
    monkeypatch.setattr(nas, 'test_connection', lambda settings: None)
    history = MagicMock()
    monkeypatch.setattr(manager_module, 'get_history_manager', lambda: history)
    class Job:
        _cancel_job = False
        def __init__(self, destination):
            self.destination = destination
        def target_name(self):
            return 'queued-video'
        def dest_folder(self):
            return self.destination
        def is_url_vaildate(self):
            return True
        def start_download(self):
            with storage_open(os.path.join(self.destination, 'queued-video.mp4'), 'wb') as stream:
                stream.write(b'queued SMB video')
            return True
    monkeypatch.setattr(manager_module.M3U8Sites, 'CreateSite', lambda url, savepath, **kwargs: Job(savepath))
    manager = TaskManager()
    task = manager.add_task('https://jable.tv/videos/example/')
    manager._run_task(task)
    assert task.status == 'completed', task.error
    assert (smb.folder / 'queued-video.mp4').read_bytes() == b'queued SMB video'
    assert history.record_download.call_args.kwargs['file_path'] == 'smb://nas/share/queued-video.mp4'
    assert not list(smb.folder.glob('*.lock'))
    with pytest.raises(StorageWriteError):
        storage_fs(task.dest)


def test_remote_write_exception_is_fatal_and_redacted(smb, monkeypatch):
    class Broken:
        def write(self, data):
            raise RuntimeError('private account detail')
        def close(self):
            pass
    monkeypatch.setattr(smb.backend.client, 'open_file', lambda *args, **kwargs: Broken())
    with pytest.raises(StorageWriteError) as error:
        with storage_open(os.path.join(smb.root, 'failed.part'), 'wb') as stream:
            stream.write(b'data')
    assert 'private account detail' not in str(error.value)


def test_media_remux_cancellation_stops_process(smb):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg not installed')
    (smb.folder / 'source.ts').write_bytes(b'not-needed-for-cancel')
    processes = []
    assert not smb.backend.remux_to_mp4(os.path.join(smb.root, 'source.ts'), os.path.join(smb.root, 'cancelled.mp4'),
                                       ffmpeg, lambda: True, lambda process: processes.append(process) if process else None)
    assert processes and all(process.poll() is not None for process in processes)
    assert not (smb.folder / 'cancelled.mp4').exists()


def test_native_smb_creates_missing_nested_directory(smb):
    settings = {**smb.settings, 'directory': r'new\nested'}
    destination = nas.smb_destination()
    for _ in range(2):
        with nas.open_smb_storage(settings, destination) as backend:
            assert backend.isdir(destination)
    assert (smb.folder / 'new' / 'nested').is_dir()
    assert not Path(destination).exists()


def test_native_smb_cancel_cleans_task_workspace_and_preserves_originals(smb, monkeypatch):
    import uav_downloader.daemon.task_manager as manager_module
    monkeypatch.setattr(nas, 'load_settings', lambda **kwargs: dict(smb.settings))
    monkeypatch.setattr(nas, 'test_connection', lambda settings: None)
    monkeypatch.setattr(manager_module, 'get_history_manager', MagicMock())
    (smb.folder / 'original.mp4').write_bytes(b'keep')
    manager = TaskManager()
    task = manager.add_task('https://missav.ai/mimk-290')
    class Job:
        _cancel_job = False
        def __init__(self, destination):
            self.destination = destination
        def target_name(self):
            return 'MIMK-290'
        def dest_folder(self):
            return self.destination
        def is_url_vaildate(self):
            return True
        def cancel_download(self, cleanup=True):
            self._cancel_job = True
        def start_download(self):
            with storage_open(os.path.join(self.destination, 'video.part'), 'wb') as stream:
                stream.write(b'partial')
            manager.cancel_task(task.task_id)
            assert task.status == 'cancelling'
            return False
    monkeypatch.setattr(manager_module.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: Job(savepath))
    manager._run_task(task)
    assert task.status == 'cancelled', task.error
    assert [p.name for p in smb.folder.iterdir()] == ['original.mp4']
    assert not Path(task.dest).exists()


def test_native_smb_existing_code_is_skipped(smb, monkeypatch):
    import uav_downloader.daemon.task_manager as manager_module
    monkeypatch.setattr(nas, 'load_settings', lambda **kwargs: dict(smb.settings))
    monkeypatch.setattr(nas, 'test_connection', lambda settings: None)
    monkeypatch.setattr(manager_module, 'get_history_manager', MagicMock())
    (smb.folder / 'MIMK-290 字幕.mp4').write_bytes(b'original')
    monkeypatch.setattr(manager_module.M3U8Sites, 'CreateSite', lambda *a, **kw: pytest.fail('duplicate must not download'))
    manager = TaskManager()
    task = manager.add_task('https://missav.ai/mimk-290-uncensored-leak')
    manager._run_task(task)
    assert task.status == 'skipped', task.error
    assert (smb.folder / 'MIMK-290 字幕.mp4').read_bytes() == b'original'


def test_media_bridge_supports_overlapping_smb_read_handles(smb, monkeypatch):
    from contextlib import contextmanager
    import smbclient
    opened = []

    @contextmanager
    def sharing_open(path, mode, share_access=None, **kwargs):
        if opened and (share_access != 'r' or any(value != 'r' for value in opened)):
            raise PermissionError('SMB sharing violation')
        opened.append(share_access)
        try:
            with open(smb.folder / 'video.ts', mode) as stream:
                yield stream
        finally:
            opened.remove(share_access)

    (smb.folder / 'video.ts').write_bytes(b'0123456789')
    monkeypatch.setattr(smbclient, 'open_file', sharing_open)
    path = os.path.join(smb.root, 'video.ts')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with smb.backend.open(path, 'rb'):
        with smb.backend.media_source(path) as url:
            with opener.open(urllib.request.Request(url, headers={'Range': 'bytes=3-6'})) as response:
                assert response.read() == b'3456'


def test_smb_old_unheld_lock_is_reused_and_removed_on_close(smb):
    lock = smb.folder / nas._target_lock_name('retry.mp4')
    lock.write_bytes(b'')
    with nas.reserve_direct_target(smb.settings, 'retry.mp4'):
        assert lock.exists()
        with pytest.raises(nas.NasError, match='任務使用中'):
            with nas.reserve_direct_target(smb.settings, 'retry.mp4'):
                pytest.fail('Must not steal an active lock')
    assert not lock.exists()


def test_smb_sharing_violation_is_reported_as_busy(monkeypatch):
    from smbprotocol.exceptions import SMBOSError
    from smbprotocol.header import NtStatus
    import smbclient
    def busy(*args, **kwargs):
        raise SMBOSError(NtStatus.STATUS_SHARING_VIOLATION, 'private-host-path')
    monkeypatch.setattr(smbclient, 'open_file', busy)
    from uav_downloader.core.smb_storage import smb_lock
    with pytest.raises(BlockingIOError) as error:
        smb_lock('private-host-path')
    assert error.value.errno == errno.EBUSY
    assert 'private-host-path' not in str(error.value)
