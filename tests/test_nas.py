import io
import json
import ntpath
import threading
from pathlib import Path
from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock
from types import SimpleNamespace

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


@pytest.mark.parametrize('changes', [dict(host='//evil/../share'), dict(share='../video'),
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
        self.directories = set()
        self.fail = False
        self.active_locks = set()
        self.path = self

    @contextmanager
    def open_file(self, path, mode, **kwargs):
        assert mode in ('xb', 'a+b')
        if self.fail:
            raise OSError('private credentials should never escape')
        locking = mode == 'a+b'
        if path in self.active_locks:
            raise BlockingIOError(16, 'active lock')
        if not locking and path in self.files:
            raise FileExistsError(path)
        if locking:
            self.active_locks.add(path)
        self.files[path] = b''
        stream = io.BytesIO()
        try:
            yield stream
        finally:
            if locking:
                self.active_locks.remove(path)
                self.files.pop(path, None)
            else:
                self.files[path] = stream.getvalue()

    def makedirs(self, path, exist_ok=False, **kwargs):
        assert exist_ok
        if self.fail:
            raise PermissionError("private credentials should never escape")
        self.directories.add(path)

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
    monkeypatch.setattr(smbclient, 'stat_volume', lambda *a, **kw: SimpleNamespace(total_size=10**12, actual_available_size=400*10**9, caller_available_size=300*10**9))
    for name in ('open_file', 'rename', 'remove', 'makedirs'):
        monkeypatch.setattr(smbclient, name, getattr(remote, name))
    monkeypatch.setattr(smbclient.path, 'exists', remote.exists)
    return remote


def test_probe_is_removed_and_does_not_save(profile, remote):
    nas.test_connection(profile)
    assert not remote.files
    assert ntpath.join(r'\\192.168.1.10\video', 'UAV') in remote.directories
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
    def create_site(url, savepath, **kwargs):
        job.dest_folder.return_value = savepath
        job.start_download.side_effect = lambda: Path(savepath, 'movie.mp4').write_bytes(b'video')
        return job
    monkeypatch.setattr(task_manager.M3U8Sites, 'CreateSite', create_site)
    monkeypatch.setattr(task_manager, 'existing_video', lambda *a: False)
    monkeypatch.setattr(nas, 'open_smb_storage', lambda *a: nullcontext())
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


@pytest.fixture
def direct_profile(profile, monkeypatch, tmp_path):
    folder = tmp_path / 'nas'
    folder.mkdir()
    monkeypatch.setattr(nas, '_is_network_path', lambda path: path == str(folder))
    return {**profile, 'mode': 'direct', 'direct_path': str(folder), 'host': '',
            'share': '', 'directory': '', 'username': '', 'password': ''}


def test_direct_destination_requires_network_mount(profile, tmp_path):
    with pytest.raises(nas.NasError, match='網路檔案系統'):
        nas.direct_destination({'direct_path': str(tmp_path)}, probe=True)
    with pytest.raises(nas.NasError, match='絕對路徑'):
        nas.direct_destination({'direct_path': 'smb://192.168.1.10/video'})
    with pytest.raises(nas.NasError, match='不存在'):
        nas.direct_destination({'direct_path': str(tmp_path / 'missing')})
    assert not (tmp_path / 'missing').exists()


def test_mount_detection_uses_deepest_mount_and_decodes_spaces():
    mounts = ('1 0 0:1 / / rw - overlay overlay rw\n'
              '2 1 0:2 / /nas\\040videos rw - cifs //server/video rw\n'
              '3 2 0:3 / /nas\\040videos/local rw - ext4 /dev/sda rw\n')
    assert nas._linux_mount_type('/nas videos/movie', mounts) == 'cifs'
    assert nas._linux_mount_type('/nas videos/local/movie', mounts) == 'ext4'
    assert nas._linux_mount_type('/nas videos-other', mounts) == 'overlay'


def test_direct_mode_persistence_keeps_existing_smb_password(profile, direct_profile):
    nas.save_settings(profile)
    direct = {**direct_profile, 'host': profile['host'], 'share': profile['share'],
              'username': profile['username']}
    nas.save_settings(direct)
    saved = nas.load_settings()
    assert saved['mode'] == 'direct'
    assert saved['direct_path'] == direct['direct_path']
    assert nas.load_settings(secret=True)['password'] == profile['password']
    assert 'password' not in saved
    assert not list(Path(direct['direct_path']).iterdir())


def test_direct_queue_overrides_local_default_and_skips_upload(direct_profile, monkeypatch, tmp_path):
    from uav_downloader.daemon import task_manager
    nas.save_settings(direct_profile)
    local = tmp_path / 'local'
    monkeypatch.setenv('DOWNLOAD_DIR', str(local))
    manager = TaskManager()
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    task = manager.add_task('https://jable.tv/videos/test/')
    assert task.dest == direct_profile['direct_path']
    assert 'password' not in task.nas_settings
    with pytest.raises(nas.NasError, match='其他下載目錄'):
        manager.add_task(task.url, dest=str(local))
    job = MagicMock()
    job._cancel_job = False
    job.target_name.return_value = 'movie'
    job.dest_folder.return_value = task.dest
    def download():
        Path(job.dest_folder(), 'movie.mp4').write_bytes(b'video directly on NAS')
        return True
    job.start_download.side_effect = download
    def create_site(url, savepath, max_workers):
        assert Path(savepath).parent == Path(direct_profile['direct_path'])
        job.dest_folder.return_value = savepath
        return job
    monkeypatch.setattr(task_manager.M3U8Sites, 'CreateSite', create_site)
    monkeypatch.setattr(task_manager, 'get_history_manager', MagicMock())
    monkeypatch.setattr(nas, 'upload_video', lambda *args: pytest.fail('direct mode must not upload'))
    manager._run_task(task)
    assert task.status == 'completed'
    assert Path(task.dest, 'movie.mp4').read_bytes() == b'video directly on NAS'
    assert not local.exists()
    assert list(Path(task.dest).glob('.uav-*.lock'))


def test_unmounted_direct_destination_fails_before_downloader(direct_profile, monkeypatch):
    from uav_downloader.daemon import task_manager
    nas.save_settings(direct_profile)
    manager = TaskManager()
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    task = manager.add_task('https://jable.tv/videos/test/')
    monkeypatch.setattr(nas, '_is_network_path', lambda path: False)
    monkeypatch.setattr(task_manager.M3U8Sites, 'CreateSite', lambda *a, **kw: pytest.fail('must stop before crawler'))
    manager._run_task(task)
    assert task.status == 'failed'
    assert '避免誤存至本機' in task.error


def test_direct_reservation_preserves_existing_file_and_blocks_duplicates(direct_profile):
    target = Path(direct_profile['direct_path'], 'movie.mp4')
    with nas.reserve_direct_target(direct_profile, 'movie.mp4'):
        with pytest.raises(nas.NasError, match='已有下載任務'):
            with nas.reserve_direct_target(direct_profile, 'movie.mp4'):
                pytest.fail('duplicate reserved')
    target.write_bytes(b'existing video')
    with pytest.raises(nas.NasError, match='避免覆寫'):
        with nas.reserve_direct_target(direct_profile, 'movie.mp4'):
            pytest.fail('must not replace existing video')
    assert target.read_bytes() == b'existing video'
    assert list(target.parent.glob('.uav-*.lock'))


def test_upload_respects_direct_mode_reservation(profile, remote, tmp_path):
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'local video')
    lock = ntpath.join('\\\\' + profile['host'], profile['share'], profile['directory'], nas._target_lock_name(video.name))
    with nas.smb_lock(lock):
        with pytest.raises(nas.NasError):
            nas.upload_video(profile, video, threading.Event())
        assert lock in remote.files
    assert not remote.files
    assert video.read_bytes() == b'local video'


@pytest.mark.parametrize('address,share,directory', [
    ('nas.local', 'video', 'UAV'),
    ('smb://nas.local/download/', 'download', ''),
    ('nas.local/download/films', 'download', 'films'),
    ('//nas.local/download/films', 'download', 'films'),
    (r'\\nas.local\download\films', 'download', 'films'),
])
def test_nas_address_accepts_hostname_and_smb_url(profile, address, share, directory):
    settings = nas.prepare_settings({**profile, 'host': address})
    assert settings['host'] == 'nas.local'
    assert settings['share'] == share
    assert settings['directory'] == directory


@pytest.mark.parametrize('address', [
    'https://nas.local/download', 'smb://user:example-password@nas.local/download',
    'smb://nas.local/download/../outside', 'smb://nas.local:8080/download',
    'bad host.local', 'smb://nas.local/download?query=1',
    'smb://nas.local/download/%0a', 'smb://nas.local/%00share',
])
def test_nas_address_rejects_invalid_urls_without_exposing_credentials(profile, address):
    with pytest.raises(nas.NasError) as error:
        nas.prepare_settings({**profile, 'host': address})
    assert 'example-password' not in str(error.value)


def test_url_and_hostname_reuse_same_saved_password(profile):
    nas.save_settings({**profile, 'host': 'smb://nas.local/video/UAV'})
    result = nas.prepare_settings({**profile, 'host': 'nas.local', 'password': ''})
    assert result['password'] == profile['password']


@pytest.mark.parametrize('directory', ['', 'new/nested'])
def test_probe_creates_only_requested_subdirectories(profile, remote, directory):
    nas.test_connection({**profile, 'directory': directory})
    expected = {ntpath.join(r'\\192.168.1.10\video', 'new', 'nested')} if directory else set()
    assert remote.directories == expected


def test_cancelling_upload_removes_already_published_video(profile, remote, tmp_path, monkeypatch):
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'video')
    (tmp_path / 'movie.ja.srt').write_text('subtitle')
    cancelled = threading.Event()
    rename = remote.rename
    def cancel_after_publish(src, dst, **options):
        rename(src, dst, **options)
        cancelled.set()
    monkeypatch.setattr('smbclient.rename', cancel_after_publish)
    published = []
    nas.upload_video(profile, video, cancelled, published)
    assert not remote.files
    assert not published
    assert video.exists()


def test_interrupted_upload_cleanup_can_retry_after_nas_reconnect(profile, remote, tmp_path, monkeypatch):
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'video')
    cancelled = threading.Event()
    open_file, remove = remote.open_file, remote.remove
    @contextmanager
    def cancelling_open(path, mode, **options):
        with open_file(path, mode, **options) as stream:
            write = stream.write
            def cancel_on_write(data):
                result = write(data)
                cancelled.set()
                return result
            stream.write = cancel_on_write
            yield stream
    def offline_remove(path, **options):
        if path.endswith('.part'):
            raise OSError('NAS offline')
        remove(path, **options)
    monkeypatch.setattr('smbclient.open_file', cancelling_open)
    monkeypatch.setattr('smbclient.remove', offline_remove)
    owned = []
    with pytest.raises(nas.NasError):
        nas.upload_video(profile, video, cancelled, owned)
    assert owned and all(path.endswith('.part') for path in owned)
    monkeypatch.setattr('smbclient.remove', remove)
    nas.cleanup_upload(profile, owned)
    assert not remote.files
    assert not owned


def test_inactive_lock_from_interrupted_download_does_not_block_retry(direct_profile):
    lock = Path(direct_profile['direct_path'], nas._target_lock_name('retry.mp4'))
    lock.write_bytes(b'')
    with nas.reserve_direct_target(direct_profile, 'retry.mp4'):
        assert not lock.parent.joinpath('retry.mp4').exists()
    with nas.reserve_direct_target(direct_profile, 'retry.mp4'):
        pass


def test_upload_recovers_abandoned_lock_without_overwriting_files(profile, remote, tmp_path):
    video = tmp_path / 'recovered.mp4'
    video.write_bytes(b'video')
    root = ntpath.join('\\\\' + profile['host'], profile['share'], profile['directory'])
    lock = ntpath.join(root, nas._target_lock_name(video.name))
    remote.files[lock] = b''
    nas.upload_video(profile, video, threading.Event())
    assert lock not in remote.files
    assert remote.files[ntpath.join(root, video.name)] == b'video'
    assert video.read_bytes() == b'video'


def test_mounted_lock_releases_after_process_exit(direct_profile):
    import subprocess, sys
    folder = direct_profile['direct_path']
    lock = str(Path(folder, nas._target_lock_name('after-crash.mp4')))
    child = subprocess.Popen([sys.executable, '-c',
        'import sys,time; from uav_downloader.core.storage import LocalStorage; '
        'handle=LocalStorage().lock(sys.argv[1]); print("locked",flush=True); time.sleep(60)', lock],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(nas.NasError, match='任務使用中'):
            with nas.reserve_direct_target(direct_profile, 'after-crash.mp4'):
                pytest.fail('Live process lock must not be stolen')
    finally:
        child.kill()
        child.wait(5)
        child.stdout.close()
    with nas.reserve_direct_target(direct_profile, 'after-crash.mp4'):
        pass


def test_nas_space_reports_volume_and_account_quota_without_writes(profile, remote):
    result = nas.storage_space(profile)
    assert result['space'] == {'total_bytes':10**12, 'free_bytes':400*10**9,
                               'available_bytes':300*10**9, 'used_bytes':600*10**9}
    assert not remote.files and not remote.directories
    assert profile['password'] not in json.dumps(result)


def test_nas_connection_success_includes_space(profile, remote):
    result = nas.test_connection(profile)
    assert result['space']['total_bytes'] == 10**12
    assert not remote.files


def test_nas_unavailable_capacity_does_not_invent_zero_or_fail_write_test(profile, remote, monkeypatch):
    import smbclient
    def unavailable(*args, **kwargs):
        raise PermissionError('private-connection-details')
    monkeypatch.setattr(smbclient, 'stat_volume', unavailable)
    for result in (nas.storage_space(profile), nas.test_connection(profile)):
        assert result['space'] is None and result['space_error']
        assert 'private-connection-details' not in json.dumps(result)
    assert not remote.files


def test_mounted_capacity_uses_the_configured_nas_path(direct_profile, monkeypatch):
    inspected = []
    def usage(path):
        inspected.append(path)
        return SimpleNamespace(total=1000, free=400)
    monkeypatch.setattr(nas.shutil, 'disk_usage', usage)
    result = nas.storage_space(direct_profile)
    assert result['space']['total_bytes'] == 1000
    assert result['space']['free_bytes'] == 400
    assert inspected == [direct_profile['direct_path']]


@pytest.mark.parametrize('values', [(0,0,0), (100,101,100), (100,-1,10), (True,0,0)])
def test_invalid_capacity_is_not_displayed(values):
    with pytest.raises(ValueError):
        nas._space_values(*values)


def test_subtitle_only_upload_never_overwrites_existing_mp4(profile, remote, tmp_path):
    video=tmp_path/'movie.mp4'
    video.write_bytes(b'local video')
    target=ntpath.join(r'\\192.168.1.10\video', 'UAV', 'movie.mp4')
    remote.files[target]=b'existing NAS video'
    (tmp_path/'movie.zh-TW.srt').write_text('new subtitles')
    nas.upload_video(profile, video, threading.Event(), subtitles_only=True)
    assert remote.files[target]==b'existing NAS video'
    assert len(remote.files)==2 and b'new subtitles' in remote.files.values()


@pytest.mark.parametrize('mode', ['smb', 'upload', 'direct'])
def test_batch_subfolder_uses_actual_nas_destination(profile, monkeypatch, tmp_path, mode):
    import os
    from uav_downloader.core.storage import LocalStorage
    from uav_downloader.daemon import download_batch
    remote_root = tmp_path / 'remote'
    (remote_root / 'UAV').mkdir(parents=True)
    settings = dict(profile, mode=mode, direct_path=str(remote_root / 'UAV'))
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: dict(settings))
    monkeypatch.setattr(nas, 'direct_destination', lambda value, **kw: value['direct_path'])
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'local'))
    class RemoteFolders(LocalStorage):
        def __init__(self, settings, root):
            self.root, self.directory = root, remote_root / settings['directory']
        def local(self, path):
            return self.directory / os.path.relpath(path, self.root)
        def exists(self, path): return self.local(path).exists()
        def isfile(self, path): return self.local(path).is_file()
        def isdir(self, path): return self.local(path).is_dir()
        def islink(self, path): return self.local(path).is_symlink()
        def listdir(self, path): return os.listdir(self.local(path))
        def makedirs(self, path, exist_ok=False): return self.local(path).mkdir(parents=True, exist_ok=exist_ok)
        def close(self): pass
    monkeypatch.setattr('uav_downloader.core.smb_storage.SmbStorage', RemoteFolders)
    manager = TaskManager()
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    options = dict(name='collection', request_id='nas-batch', urls=['https://example.com/movie'])
    preview = download_batch.destination(manager, options)
    assert not (remote_root / 'UAV' / 'collection').exists()
    assert not (tmp_path / 'local').exists()
    result = download_batch.submit(manager, dict(options, expected_destination=preview['destination']))
    task = manager.get_task(result['tasks'][0]['task_id'])
    assert (remote_root / 'UAV' / 'collection').is_dir()
    assert task.to_dict()['final_dest'] == preview['destination']
    assert 'nas-secret' not in json.dumps(result)
    if mode in ('smb', 'upload'):
        assert task.nas_settings['directory'] == 'UAV/collection'
        assert settings['directory'] == 'UAV'
    else:
        assert task.dest == str(remote_root / 'UAV' / 'collection')


def test_explicit_upload_overwrite_keeps_replaced_file_on_cancel(profile, remote, monkeypatch, tmp_path):
    import smbclient
    target = ntpath.join(r'\\192.168.1.10\video', 'UAV', 'movie.mp4')
    remote.files[target] = b'old'
    untouched = ntpath.join(r'\\192.168.1.10\video', 'UAV', 'other.mp4')
    remote.files[untouched] = b'keep'
    video = tmp_path / 'movie.mp4'
    video.write_bytes(b'new')
    monkeypatch.setattr(smbclient.path, 'islink', lambda *a, **kw: False)
    cancelled = threading.Event()
    def replace(source, destination, **kwargs):
        remote.files[destination] = remote.files.pop(source)
        cancelled.set()
    monkeypatch.setattr(smbclient, 'replace', replace)
    nas.upload_video(dict(profile, _overwrite=True), video, cancelled)
    assert remote.files[target] == b'new'
    assert remote.files[untouched] == b'keep'
    assert not any(path.endswith('.part') for path in remote.files)
