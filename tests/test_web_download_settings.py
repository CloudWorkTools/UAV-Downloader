"""Web preferences must survive restarts and control actual download behavior."""
import json
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from uav_downloader.core import config
from uav_downloader.daemon.server import DaemonServer
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.sites.base import M3U8Crawler


@pytest.fixture
def web_settings(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'videos'))
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setattr('uav_downloader.daemon.task_manager.nas.load_settings', lambda **kw: {'enabled': False})
    history = MagicMock()
    history.find_downloaded.return_value = None
    monkeypatch.setattr('uav_downloader.daemon.task_manager.get_history_manager', lambda: history)
    server = DaemonServer(port=0)
    server.start()

    def request(path, data=None):
        req = Request(f'http://127.0.0.1:{server.server.server_address[1]}{path}',
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    yield request, server, tmp_path
    server.stop()


@pytest.mark.parametrize('mode', ['none', 'ja', 'en', 'zh', 'all'])
def test_saved_preferences_apply_to_web_downloads_and_survive_restart(web_settings, mode):
    request, server, root = web_settings
    preferences = {'subtitle_pref': mode, 'recognition_quality': 'balanced', 'download_cover': False, 'download_concurrency': 7}
    assert request('/api/settings', preferences)[0] == 200
    status, settings = request('/api/settings')
    assert status == 200
    for key, value in preferences.items():
        assert settings[key] == value
    status, response = request('/api/download/start', {'url': 'https://missav.ai/test'})
    assert status == 200
    task = response['task']
    assert task['subtitle_mode'] == mode
    assert task['enable_subtitles'] is (mode != 'none')
    assert task['download_cover'] is False
    saved = json.loads((root / 'config' / 'ui_prefs.json').read_text())
    assert saved['recognition_quality'] == 'balanced'
    server.stop()
    restarted = DaemonServer()
    assert restarted.task_manager.max_concurrent == 7
    restored = restarted.task_manager.get_task(task['task_id'])
    assert restored.subtitle_mode == mode
    assert restored.download_cover is False


def test_explicit_task_options_override_defaults_and_null_uses_defaults(web_settings):
    request, _, _ = web_settings
    request('/api/settings', {'subtitle_pref': 'all', 'download_cover': False})
    _, result = request('/api/download/start', {'url': 'https://missav.ai/first',
                                              'enable_subtitles': False, 'download_cover': True})
    assert result['task']['subtitle_mode'] == 'none'
    assert result['task']['download_cover'] is True
    _, result = request('/api/download/start', {'url': 'https://missav.ai/second',
                                              'enable_subtitles': None, 'subtitle_mode': None,
                                              'download_cover': None})
    assert result['task']['subtitle_mode'] == 'all'
    assert result['task']['download_cover'] is False


@pytest.mark.parametrize('payload', [{'download_cover': 'false'}, {'subtitle_pref': 'invalid'},
                                     {'recognition_quality': 'invalid'},
                                     {'translation_quality': 'invalid'},
                                     {'translation_quality': []}])
def test_invalid_settings_are_rejected(web_settings, payload):
    request, _, _ = web_settings
    assert request('/api/settings', payload)[0] == 400


@pytest.mark.parametrize('enabled', [True, False])
def test_cover_switch_controls_image_download(enabled):
    crawler = object.__new__(M3U8Crawler)
    crawler._cancel_job = False
    crawler.download_cover = enabled
    crawler._create_dest_folder = MagicMock()
    crawler.download_image = MagicMock()
    crawler.is_target_video_exist = lambda: True
    assert crawler.start_download()
    assert crawler.download_image.call_count == int(enabled)


def test_daemon_uses_saved_asr_and_keeps_video_on_failure(tmp_path, monkeypatch):
    from uav_downloader.daemon.task_manager import DownloadTask
    from uav_downloader.subtitles import asr, engine
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    asr.save_settings({'base_url': 'https://asr.example/v1', 'model': 'model', 'api_key': 'private-key'})
    task = DownloadTask(task_id='test', url='https://example.com/video', dest=str(tmp_path))
    task.name = 'video'
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'keep this video')
    job = MagicMock()
    job.dest_folder.return_value = str(tmp_path)
    generate = MagicMock(side_effect=engine.SubtitleError('HTTP 401'))
    monkeypatch.setattr(engine, 'generate_subtitles', generate)
    TaskManager()._run_subtitles(task, job)
    assert generate.call_args.kwargs['asr_settings']['api_key'] == 'private-key'
    assert 'HTTP 401' in task.error
    assert video.read_bytes() == b'keep this video'


def test_asr_api_settings_round_trip_without_exposing_key(web_settings):
    request, _, root = web_settings
    status, settings = request('/api/settings')
    assert status == 200 and settings['asr']['base_url'] == 'http://uav-speaches:8000/v1'
    assert settings['subtitle_runtime_supported'] is True
    assert request('/api/settings', {'asr': {'base_url': 'https://external.example/v1',
                                         'model': 'whisper', 'api_key': 'private-key'}})[0] == 200
    _, settings = request('/api/settings')
    assert settings['asr']['api_key_saved']
    assert 'private-key' not in json.dumps(settings)
    assert settings['asr']['base_url'] == 'https://external.example/v1'
    assert request('/api/settings', {'asr': {'base_url': 'https://user:pass@host/v1'}})[0] == 400


def test_saved_download_directory_overrides_environment_default(web_settings):
    request, _, root = web_settings
    destination = str(root / 'chosen')
    assert request('/api/settings', {'download_dir': destination})[0] == 200
    assert request('/api/settings')[1]['download_dir'] == destination
    status, response = request('/api/download/start', {'url': 'https://missav.ai/test'})
    assert status == 200
    assert response['task']['dest'] == destination


def test_subtitle_retry_keeps_video_and_does_not_create_download(tmp_path, monkeypatch):
    from uav_downloader.daemon.task_manager import DownloadTask
    from uav_downloader.subtitles import engine
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    manager=TaskManager()
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    video=tmp_path/'video.mp4'
    video.write_bytes(b'original MP4')
    task=DownloadTask(task_id='subtitle-retry', url='https://missav.ai/test', dest=str(tmp_path), name='video',
                      status='completed', enable_subtitles=True, subtitle_mode='zh',
                      error='影片已完成，但字幕生成失敗: 辨識 API 片段文字或時間戳無效')
    old_files=MagicMock()
    task.files=old_files
    manager._tasks[task.task_id]=task
    monkeypatch.setattr('uav_downloader.daemon.task_manager.M3U8Sites.CreateSite', lambda *a,**k: pytest.fail('Redownloaded video'))
    monkeypatch.setattr(engine, 'generate_subtitles', lambda *a,**k: None)
    assert manager.restart_task(task.task_id, subtitles_only=True)
    manager._run_task(task)
    assert task.status=='completed' and task.error is None
    assert video.read_bytes()==b'original MP4'
    old_files.cleanup.assert_not_called()


def test_subtitle_retry_api_and_cancel_preserve_completed_video(web_settings, monkeypatch):
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root=web_settings
    video=root/'video.mp4'
    video.write_bytes(b'keep completed video')
    task=DownloadTask(task_id='retry-api', url='https://missav.ai/test', dest=str(root), name='video',
                      status='completed', enable_subtitles=True, subtitle_mode='zh')
    server.task_manager._tasks[task.task_id]=task
    status,result=request('/api/download/action', {'action':'retry_subtitles', 'task_id':task.task_id})
    assert status==200 and result['success'] and task.subtitles_only
    assert server.task_manager.cancel_task(task.task_id)
    assert task.status=='cancelled' and video.read_bytes()==b'keep completed video'


def test_interrupted_subtitle_retry_can_cancel_after_restart_without_deleting_mp4(tmp_path):
    from uav_downloader.daemon.task_manager import DownloadTask
    state=tmp_path/'tasks.json'
    manager=TaskManager(state_path=state)
    video=tmp_path/'video.mp4'
    video.write_bytes(b'completed video')
    task=DownloadTask(task_id='interrupted-srt', url='https://missav.ai/test', dest=str(tmp_path), name='video',
                      status='subtitles', subtitles_only=True, enable_subtitles=True, subtitle_mode='zh')
    manager._tasks[task.task_id]=task
    manager._save_tasks()
    restored=TaskManager(state_path=state)
    assert restored.get_task(task.task_id).subtitles_only
    assert restored.cancel_task(task.task_id)
    assert restored.get_task(task.task_id).status=='cancelled'
    assert video.read_bytes()==b'completed video'


@pytest.mark.parametrize('quality', ['fast', 'balanced', 'quality'])
def test_translation_quality_persists_and_reaches_job_profile(web_settings, quality):
    from uav_downloader.subtitles.settings import get_translation_settings
    request, _, root = web_settings
    assert request('/api/settings')[1]['translation_quality'] == 'balanced'
    assert request('/api/settings', {'translation_quality': quality})[0] == 200
    assert request('/api/settings')[1]['translation_quality'] == quality
    saved = json.loads((root / 'config' / 'ui_prefs.json').read_text())
    assert saved['translation_quality'] == quality
    assert get_translation_settings().quality == quality


def test_batch_task_settings_only_change_selected_idle_tasks_and_persist(web_settings):
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    manager = server.task_manager
    old = root / 'old'
    old.mkdir()
    partial = old / 'partial.ts'
    partial.write_bytes(b'keep')
    for tid, status in [('pending', 'pending'), ('paused', 'paused'), ('failed', 'failed'), ('cancelled', 'cancelled'),
                        ('active', 'downloading'), ('other', 'pending'), ('done', 'completed')]:
        manager._tasks[tid] = DownloadTask(tid, 'https://missav.ai/' + tid, str(old), status=status,
                                           enable_subtitles=True, subtitle_mode='zh')
    manager._active['active'] = manager._tasks['active']
    from uav_downloader.daemon.download_files import DownloadFiles
    manager._tasks['paused'].files = DownloadFiles(str(old), 'paused')
    workdir = manager._tasks['paused'].files.workdir
    target = str(root / 'new')
    code, result = request('/api/download/action', {'action': 'update_settings',
        'task_ids': ['pending', 'paused', 'failed', 'cancelled', 'active', 'done', 'missing', 'paused'],
        'storage': 'local', 'dest': target, 'subtitle_mode': 'none'})
    assert code == 200
    assert result['updated'] == ['pending', 'paused', 'failed', 'cancelled']
    assert result['skipped'] == ['active', 'done', 'missing']
    assert partial.read_bytes() == b'keep'
    assert Path(workdir).is_dir()
    assert manager.get_task('paused').files is None
    assert manager.get_task('paused').status == 'paused'
    assert manager.get_task('other').dest == str(old)
    restored = TaskManager(state_path=manager._state_path)
    for tid in result['updated']:
        task = restored.get_task(tid)
        assert task.dest == target and task.subtitle_mode == 'none' and not task.enable_subtitles
        assert task.status == ('failed' if tid == 'pending' else tid)
        assert restored.restart_task(tid)
        assert restored.get_task(tid).status == 'pending'
    assert manager.get_task('active').dest == str(old)


def test_single_active_task_settings_wait_for_pause_and_keep_partial_files(web_settings, monkeypatch):
    from types import SimpleNamespace
    from uav_downloader.daemon import task_manager as tm

    request, server, root = web_settings
    manager = server.task_manager
    task = manager.add_task('https://missav.ai/mimk-290', subtitle_mode='zh', enable_subtitles=True)
    old_dest = task.dest
    payload = {'action': 'update_settings', 'task_ids': [task.task_id],
               'storage': 'local', 'dest': str(root / 'new'), 'subtitle_mode': 'none'}
    partials = []

    def create_site(url, savepath, **kwargs):
        def download():
            partial = Path(savepath) / 'segment.ts'
            partial.write_bytes(b'keep')
            partials.append(partial)
            assert request('/api/download/action', payload)[1]['skipped'] == [task.task_id]
            code, result = request('/api/download/action', {'action': 'pause', 'task_id': task.task_id})
            assert code == 200 and result['success']
            assert manager.get_task(task.task_id).status == 'pausing'
            assert request('/api/download/action', payload)[1]['updated'] == []
            assert task.dest == old_dest
            return False
        return SimpleNamespace(is_url_vaildate=lambda: True, target_name=lambda: 'MIMK-290',
                               dest_folder=lambda: savepath, start_download=download,
                               cancel_download=lambda cleanup=True: None, _cancel_job=False)

    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', create_site)
    manager._run_task(task)
    assert task.status == 'paused', task.error
    code, result = request('/api/download/action', payload)
    assert code == 200 and result['updated'] == [task.task_id] and result['skipped'] == []
    assert partials[0].read_bytes() == b'keep'
    restored = TaskManager(state_path=manager._state_path).get_task(task.task_id)
    assert restored.status == 'paused' and restored.dest == str(root / 'new')
    assert not restored.enable_subtitles and restored.subtitle_mode == 'none'


@pytest.mark.parametrize('payload', [
    {'task_ids': []}, {'task_ids': 'task'}, {'task_ids': [[]]},
    {'task_ids': ['task'], 'storage': 'local', 'dest': ''},
    {'task_ids': ['task'], 'storage': 'local', 'dest': 'smb://nas/video'},
    {'task_ids': ['task'], 'subtitle_mode': []},
    {'task_ids': ['task'], 'subtitle_mode': 'invalid'},
    {'task_ids': ['task'], 'storage': []},
])
def test_invalid_batch_task_settings(web_settings, payload):
    request, _, _ = web_settings
    assert request('/api/download/action', {'action': 'update_settings', **payload})[0] == 400


@pytest.mark.parametrize('mode', ['upload', 'smb', 'direct'])
def test_batch_task_settings_apply_saved_nas_snapshot(web_settings, monkeypatch, mode):
    from uav_downloader.daemon import nas
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    manager = server.task_manager
    manager._tasks['task'] = DownloadTask('task', 'https://missav.ai/test', str(root / 'old'), status='paused')
    profile = dict(enabled=True, mode=mode, host='nas.local', share='video', directory='new',
                   username='user', password='secret')
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: dict(profile))
    monkeypatch.setattr(nas, 'test_connection', lambda settings: None)
    monkeypatch.setattr(nas, 'direct_destination', lambda settings, **kw: str(root / 'mounted'))
    monkeypatch.setattr('uav_downloader.daemon.task_manager.probe_destination', lambda path: None)
    code, result = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'],
                                                    'storage': 'settings', 'subtitle_mode': 'ja'})
    assert code == 200 and result['updated'] == ['task']
    task = manager.get_task('task')
    assert task.enable_subtitles and task.subtitle_mode == 'ja'
    assert task.nas_settings['host'] == 'nas.local' and task.nas_settings['directory'] == 'new'
    if mode == 'smb':
        assert task.dest.startswith('/__uav_smb__/') and task.dest == task.nas_settings['_smb_path']
    else:
        assert task.dest == str(root / ('mounted' if mode == 'direct' else 'videos'))
    profile['host'] = 'another.local'
    assert task.nas_settings['host'] == 'nas.local'
    assert 'secret' not in manager._state_path.read_text()


def test_subtitle_only_edit_keeps_partial_download_and_original_storage(web_settings):
    from uav_downloader.daemon.download_files import DownloadFiles
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    task = DownloadTask('task', 'https://missav.ai/test', str(root / 'old'), status='paused')
    task.files = DownloadFiles(task.dest, task.task_id)
    files = task.files
    server.task_manager._tasks[task.task_id] = task
    code, result = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'], 'subtitle_mode': 'zh'})
    assert code == 200 and result['updated'] == ['task']
    assert task.files is files and task.dest == str(root / 'old')
    assert task.enable_subtitles and task.subtitle_mode == 'zh'


def test_batch_destination_probe_failure_leaves_tasks_unchanged(web_settings, monkeypatch):
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    task = DownloadTask('task', 'https://missav.ai/test', str(root / 'old'), status='paused')
    server.task_manager._tasks[task.task_id] = task
    def reject(path):
        raise ValueError('目的地無法寫入')
    monkeypatch.setattr('uav_downloader.daemon.task_manager.probe_destination', reject)
    code, _ = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'],
                                              'storage': 'local', 'dest': str(root / 'new'), 'subtitle_mode': 'zh'})
    assert code == 400
    assert task.dest == str(root / 'old') and task.subtitle_mode == 'none'


@pytest.mark.parametrize('mode', ['upload', 'smb', 'direct'])
def test_queue_custom_nas_is_independent_and_credentials_survive_restart(web_settings, monkeypatch, mode):
    from uav_downloader.daemon import nas
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    manager = server.task_manager
    manager._tasks['task'] = DownloadTask('task', 'https://missav.ai/custom', str(root / 'old'), status='paused')
    manager._tasks['other'] = DownloadTask('other', 'https://missav.ai/other', str(root / 'other'), status='paused')
    def no_global_settings(**kwargs):
        raise AssertionError('Independent queue settings must not read global NAS settings')
    monkeypatch.setattr(nas, 'load_settings', no_global_settings)
    monkeypatch.setattr(nas, 'test_connection', lambda settings, **kw: None)
    monkeypatch.setattr(nas, 'direct_destination', lambda settings, **kw: str(root / 'mount'))
    profile = dict(enabled=True, mode=mode, host='another.local', share='videos', directory='chosen',
                   username='custom-user', password='queue-only-secret', direct_path=str(root / 'mount'))
    code, result = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'],
                                                    'storage': 'custom', 'dest': str(root / 'staging'), 'nas': profile})
    assert code == 200 and result['updated'] == ['task']
    task = manager.get_task('task')
    assert task.nas_settings['host'] == 'another.local'
    assert manager.get_task('other').dest == str(root / 'other')
    assert not (root / 'config' / 'nas.json').exists()
    snapshot = manager._state_path.read_text()
    assert 'queue-only-secret' not in snapshot
    assert 'queue-only-secret' not in json.dumps(task.to_dict())
    restored = TaskManager(state_path=manager._state_path)
    assert restored.restart_task('task')
    assert restored.get_task('task').nas_settings['password'] == 'queue-only-secret'
    if mode == 'upload':
        assert task.dest == str(root / 'staging')
        assert task.to_dict()['final_dest'] == 'smb://another.local/videos/chosen'


def test_custom_nas_requires_own_credentials(web_settings):
    request, _, _ = web_settings
    code, _ = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'],
                                              'storage': 'custom', 'nas': {'enabled': True, 'mode': 'smb', 'host': 'nas.local', 'share': 'videos', 'username': 'user'}})
    assert code == 400


def test_queue_can_import_saved_nas_password_without_coupling_later_changes(web_settings, monkeypatch):
    from uav_downloader.daemon import nas
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    profile = dict(enabled=True, mode='smb', host='saved.local', share='videos', directory='original', username='user', password='saved-private-secret')
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: {**profile, 'password_saved': True})
    monkeypatch.setattr(nas, 'test_connection', lambda settings, **kw: None)
    task = DownloadTask('task', 'https://missav.ai/import', str(root / 'old'), status='paused')
    server.task_manager._tasks[task.task_id] = task
    imported = {**profile, 'directory': 'task-only', 'password': '', 'use_saved_password': True}
    code, result = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'], 'storage': 'custom', 'nas': imported})
    assert code == 200 and result['updated'] == ['task']
    assert task.nas_settings['password'] == 'saved-private-secret'
    assert task.nas_settings['directory'] == 'task-only' and profile['directory'] == 'original'
    assert 'saved-private-secret' not in server.task_manager._state_path.read_text()
    profile.update(host='other.local', password='new-secret')
    restored = TaskManager(state_path=server.task_manager._state_path)
    assert restored.restart_task('task')
    assert restored.get_task('task').nas_settings['password'] == 'saved-private-secret'


def test_queue_cannot_reuse_saved_password_for_another_nas(web_settings, monkeypatch):
    from uav_downloader.daemon import nas
    request, _, _ = web_settings
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: dict(host='saved.local', username='user', password_saved=True, password='secret'))
    code, _ = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'], 'storage': 'custom',
        'nas': dict(enabled=True, mode='smb', host='another.local', share='videos', username='user', password='', use_saved_password=True)})
    assert code == 400


def test_staging_preference_does_not_change_existing_destination_or_nas(web_settings):
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    destination = str(root / 'final')
    staging = str(root / 'staging')
    assert request('/api/settings', {'download_dir': destination})[0] == 200
    assert request('/api/settings', {'staging_dir': staging})[0] == 200
    _, settings = request('/api/settings')
    assert settings['download_dir'] == destination and settings['staging_dir'] == staging
    _, result = request('/api/download/start', {'url': 'https://missav.ai/staging'})
    assert result['task']['dest'] == destination and result['task']['staging_dir'] == staging
    task = DownloadTask('existing', 'https://missav.ai/existing', destination, status='paused', nas_settings={'enabled': True, 'mode': 'smb', 'host': 'original.local', 'share': 'video'})
    server.task_manager._tasks[task.task_id] = task
    original = dict(task.nas_settings)
    code, result = request('/api/download/action', {'action': 'update_settings', 'task_ids': ['existing'], 'staging_dir': staging})
    assert code == 200 and result['updated'] == ['existing']
    assert task.dest == destination and task.nas_settings == original
    restored = TaskManager(state_path=server.task_manager._state_path).get_task('existing')
    assert restored.staging_dir == staging and restored.nas_settings == original


@pytest.mark.parametrize('value', [12, [], 'smb://nas/video', '/tmp/../__uav_smb__/fake', '/tmp/invalid\npath'])
def test_invalid_staging_directory_is_rejected_without_changing_destination(web_settings, value):
    request, _, _ = web_settings
    assert request('/api/settings', {'staging_dir': value})[0] == 400
    assert request('/api/download/action', {'action': 'update_settings', 'task_ids': ['task'], 'staging_dir': value})[0] == 400


def test_disable_staging_for_multiple_tasks_without_accessing_failed_disk(web_settings, monkeypatch):
    from uav_downloader.daemon.task_manager import DownloadTask
    request, server, root = web_settings
    tasks = [DownloadTask(tid, 'https://missav.ai/' + tid, str(root / 'final'),
                          status='failed', staging_dir='/offline/ssd',
                          nas_settings={'enabled': True, 'mode': 'smb', 'host': 'original.local', 'share': 'video'})
             for tid in ('first', 'second')]
    for task in tasks:
        server.task_manager._tasks[task.task_id] = task
    def inaccessible(*args, **kwargs):
        raise OSError(30, 'Read-only file system')
    monkeypatch.setattr('uav_downloader.daemon.task_manager.probe_destination', inaccessible)
    code, result = request('/api/download/action', {'action': 'update_settings',
                          'task_ids': ['first', 'second'], 'staging_dir': ''})
    assert code == 200 and result['updated'] == ['first', 'second']
    restored = TaskManager(state_path=server.task_manager._state_path)
    for task in tasks:
        saved = restored.get_task(task.task_id)
        assert saved.staging_dir == ''
        assert saved.dest == task.dest and saved.nas_settings == task.nas_settings


def batch_preview(request, **options):
    body = dict(action='preview', storage='query', name='台北空拍', policy='new', naming='date')
    body.update(options)
    status, result = request('/api/download/batch', body)
    assert status == 200, result
    return body, result


def test_batch_preview_cancel_and_submit_are_one_destination(web_settings):
    request, server, root = web_settings
    body, preview = batch_preview(request)
    folder = root / 'videos' / '台北空拍'
    assert preview['destination'] == str(folder)
    assert preview['base_destination'] == str(root / 'videos')
    assert not folder.exists()
    payload = dict(body, action='submit', expected_destination=preview['destination'], request_id='first',
                   urls=['https://example.com/one', 'https://example.com/two'])
    status, result = request('/api/download/batch', payload)
    assert status == 200
    assert folder.is_dir()
    assert len(result['tasks']) == 2
    assert {task['dest'] for task in result['tasks']} == {str(folder)}
    assert request('/api/download/batch', payload)[1] == result
    assert len(server.task_manager._tasks) == 2
    assert config._load_prefs().get('download_dir') is None


def test_batch_existing_folder_naming_and_race(web_settings):
    request, server, root = web_settings
    folder = root / 'videos' / '台北空拍'
    folder.mkdir(parents=True)
    body, preview = batch_preview(request)
    assert preview['exists']
    assert preview['name'].startswith('台北空拍_')
    assert not Path(preview['destination']).exists()
    Path(preview['destination']).mkdir()
    status, result = request('/api/download/batch', dict(body, action='submit', expected_destination=preview['destination'],
                                                       request_id='race', urls=['https://example.com/one']))
    assert status == 400
    assert not server.task_manager._tasks
    body, preview = batch_preview(request, naming='number')
    assert preview['name'] == '台北空拍_2'
    _, custom = batch_preview(request, naming='custom')
    assert not custom['can_submit']


@pytest.mark.parametrize('name', ['../outside', 'a/b\\c', 'CON', '  ...  ', 'a:*?<>|'])
def test_batch_names_stay_inside_root(web_settings, name):
    request, server, root = web_settings
    _, preview = batch_preview(request, name=name)
    assert Path(preview['destination']).parent == root / 'videos'
    assert preview['name'] not in ('.', '..', 'CON')
    assert not Path(preview['destination']).exists()


def test_batch_merge_busy_and_overwrite_persist(web_settings):
    request, server, root = web_settings
    folder = root / 'videos' / '台北空拍'
    folder.mkdir(parents=True)
    (folder / 'old.mp4').write_bytes(b'old')
    body, preview = batch_preview(request, policy='overwrite')
    assert preview['file_count'] == 1
    status, result = request('/api/download/batch', dict(body, action='submit', expected_destination=preview['destination'],
                                                       request_id='overwrite', urls=['https://example.com/one']))
    assert status == 200
    task = server.task_manager.get_task(result['tasks'][0]['task_id'])
    assert task.folder_policy == 'overwrite'
    _, busy = batch_preview(request, policy='merge')
    assert busy['busy'] and busy['overwrite_busy'] and not busy['can_submit']
    restarted = TaskManager(state_path=server.task_manager._state_path)
    assert restarted.get_task(task.task_id).folder_policy == 'overwrite'
    assert (folder / 'old.mp4').read_bytes() == b'old'


def test_batch_rejects_symlink_and_invalid_urls(web_settings):
    request, server, root = web_settings
    (root / 'videos').mkdir()
    (root / 'videos' / 'link').symlink_to(root, target_is_directory=True)
    assert request('/api/download/batch', dict(action='preview', name='link'))[0] == 400
    assert request('/api/download/batch', dict(action='submit', urls=['file:///etc/passwd']))[0] == 400


@pytest.mark.parametrize('staging', [False, True])
def test_explicit_overwrite_is_atomic_and_cancel_preserves_replacements(tmp_path, staging):
    import threading
    from uav_downloader.daemon.download_files import DownloadFiles
    folder = tmp_path / 'target'
    folder.mkdir()
    (folder / 'old.mp4').write_bytes(b'original')
    (folder / 'unrelated.mp4').write_bytes(b'untouched')
    stage = tmp_path / 'stage'
    stage.mkdir()
    files = DownloadFiles(str(folder), 'test', str(stage) if staging else '')
    Path(files.workdir, 'old.mp4').write_bytes(b'replacement')
    Path(files.workdir, 'new.mp4').write_bytes(b'new')
    files.publish(threading.Event(), overwrite=True)
    files.cleanup(cancelled=True)
    assert (folder / 'old.mp4').read_bytes() == b'replacement'
    assert (folder / 'unrelated.mp4').read_bytes() == b'untouched'
    assert not (folder / 'new.mp4').exists()


@pytest.mark.parametrize('storage', ['query', 'settings'])
def test_batch_api_append_to_busy_folder(web_settings, storage):
    request, server, _ = web_settings
    body, preview = batch_preview(request, policy='merge', storage=storage)
    first_payload = dict(body, action='submit', expected_destination=preview['destination'],
                         request_id='append-first', urls=['https://missav.ai/mimk-290'])
    status, first = request('/api/download/batch', first_payload)
    assert status == 200
    original = server.task_manager.get_task(first['tasks'][0]['task_id']).to_dict()
    body, preview = batch_preview(request, policy='merge', storage=storage)
    assert preview['busy'] and not preview['overwrite_busy'] and preview['can_submit']
    payload = dict(body, action='submit', expected_destination=preview['destination'], request_id='append-second',
                   urls=['https://jable.tv/videos/mimk-290/', 'https://missav.ai/mimk-291'])
    status, result = request('/api/download/batch', payload)
    assert status == 200, result
    assert [task['status'] for task in result['tasks']] == ['skipped', 'pending']
    assert server.task_manager.get_task(original['task_id']).to_dict() == original
    assert request('/api/download/batch', payload)[1] == result
    restarted = TaskManager(state_path=server.task_manager._state_path)
    assert len(restarted._tasks) == 3
    assert restarted.get_task(result['tasks'][1]['task_id']).dest == original['dest']


def test_batch_dialog_appends_through_real_daemon(web_settings, monkeypatch, tmp_path):
    import os
    import shutil
    import subprocess
    from uav_downloader.daemon.server import DaemonRequestHandler
    chrome = shutil.which(os.environ.get('CHROME_BIN', 'google-chrome'))
    if not chrome:
        pytest.skip('Chrome is required')
    request, server, _ = web_settings
    frontend = Path(__file__).resolve().parents[1] / 'uav-web' / 'frontend'
    html = (frontend / 'index.html').read_text()
    dialog = html[html.index('  <dialog id="download-batch-dialog"'):html.index('  <script src="/download-batch.js"')]
    script = """
    const check = (value, message) => {if (!value) throw Error(message);};
    const until = async (test) => {for(let i=0;i<200;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('wait timed out');};
    const field = id => document.getElementById('batch-'+id);
    let received;
    const batch = createDownloadBatch({getQuery:()=> '追加測試', onTasks:tasks=>{received=tasks;}});
    const selectMerge = async () => {
      await until(()=>document.getElementById('batch-destination').textContent.includes('追加測試'));
      field('policy').value='merge';field('policy').dispatchEvent(new Event('input',{bubbles:true}));
      await until(()=>!field('submit').disabled);
    };
    (async()=>{
      let done=batch.open(['https://missav.ai/mimk-290']);await selectMerge();
      field('submit').click();check(await done,'first submission failed');
      const original=received[0];
      done=batch.open(['https://jable.tv/videos/mimk-290/','https://missav.ai/mimk-291']);await selectMerge();
      check(field('status').textContent.includes('將追加'),'append status missing');
      field('submit').click();check(await done,'append failed');
      check(received[0].status==='skipped' && received[1].status==='pending','wrong duplicate handling');
      check(received.every(task=>task.dest===original.dest),'destination changed');
      check((await fetch('/__batch_publish')).ok,'publication failed');
      done=batch.open(['https://missav.ai/mimk-292']);await selectMerge();
      field('submit').click();check(await done,'third submission failed');
      const response=await fetch('/api/download/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task_id:received[0].task_id})});
      check(response.ok,'cancel failed');
      document.body.innerHTML='<p>REAL_BATCH_PASS</p>';
    })().catch(error=>{document.body.innerHTML='<p>REAL_BATCH_FAIL: '+error.message+'</p>';});
    """
    page = ('<body>' + dialog + '<script>' + (frontend / 'download-batch.js').read_text() + script + '</script>').encode()
    from types import SimpleNamespace
    from uav_downloader.daemon import task_manager as tm
    def media_fixture(url, savepath, **kwargs):
        code = 'MIMK-290' if '290' in url else 'MIMK-291'
        def download():
            Path(savepath, code + '.mp4').write_bytes(code.encode())
            return True
        return SimpleNamespace(dest_folder=lambda: savepath, target_name=lambda: code,
                               is_url_vaildate=lambda: True, start_download=download, _cancel_job=False)
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', media_fixture)
    original_get = DaemonRequestHandler.do_GET
    def serve_page(handler):
        if handler.path == '/__batch_publish':
            for task in list(server.task_manager._tasks.values()):
                if task.status == 'pending':
                    server.task_manager._run_task(task)
                    assert task.status == 'completed', task.error
            return handler._send_json({'success': True})
        if handler.path != '/__batch_test':
            return original_get(handler)
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(page)))
        handler.end_headers()
        handler.wfile.write(page)
    monkeypatch.setattr(DaemonRequestHandler, 'do_GET', serve_page)
    output = subprocess.run([chrome, '--headless', '--no-sandbox', '--disable-gpu', '--no-first-run',
                             '--user-data-dir=' + str(tmp_path / 'chrome'), '--dump-dom', '--virtual-time-budget=15000',
                             f'http://127.0.0.1:{server.server.server_address[1]}/__batch_test'],
                            capture_output=True, text=True, timeout=30, check=True)
    assert '<p>REAL_BATCH_PASS</p>' in output.stdout, output.stdout[-3000:]
    server.stop()
    restarted = DaemonServer(port=0)
    try:
        restarted.start()
        with urlopen(f'http://127.0.0.1:{restarted.server.server_address[1]}/api/download/tasks') as response:
            assert response.status == 200
        assert sorted(task.status for task in restarted.task_manager._tasks.values()) == ['cancelled', 'completed', 'completed', 'skipped']
        for code in ('MIMK-290', 'MIMK-291'):
            assert (tmp_path / 'videos' / '追加測試' / (code + '.mp4')).read_bytes() == code.encode()
    finally:
        restarted.stop()


def test_start_now_api_runs_worker_above_limit_without_duplicate_start(web_settings, monkeypatch):
    import threading
    from types import SimpleNamespace
    from uav_downloader.daemon import task_manager as tm
    request, server, root = web_settings
    manager = server.task_manager
    manager.max_concurrent = 1
    occupied = manager.add_task('https://missav.ai/mimk-290')
    manager._queue.remove(occupied.task_id)
    manager._active[occupied.task_id] = occupied
    task = manager.add_task('https://missav.ai/mimk-291')
    started, release = threading.Event(), threading.Event()
    def create_site(url, savepath, **kwargs):
        def download():
            started.set()
            assert release.wait(3)
            Path(savepath, 'MIMK-291.mp4').write_bytes(b'immediate-download')
            return True
        return SimpleNamespace(dest_folder=lambda: savepath, target_name=lambda: 'MIMK-291',
                               is_url_vaildate=lambda: True, start_download=download, _cancel_job=False)
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', create_site)
    try:
        payload = dict(action='start_now', task_id=task.task_id)
        status, result = request('/api/download/action', payload)
        assert status == 200 and result['success']
        assert started.wait(1)
        assert len(manager._active) == 2 and manager.max_concurrent == 1
        assert request('/api/download/action', payload)[0] == 409
        assert occupied.task_id in manager._active
    finally:
        release.set()
        if task.thread:
            task.thread.join(3)
    assert task.status == 'completed', task.error
    assert (root / 'videos' / 'MIMK-291.mp4').read_bytes() == b'immediate-download'


def test_batch_cancel_api(web_settings):
    request, server, _ = web_settings
    tasks = [server.task_manager.add_task(f'https://missav.ai/cancel-{i}') for i in range(2)]
    ids = [task.task_id for task in tasks]
    status, result = request('/api/download/action', {'action': 'cancel', 'task_ids': ids})
    assert status == 200
    assert result == {'success': True, 'accepted': ids, 'skipped': [], 'errors': []}
    assert all(task.status == 'cancelled' for task in tasks)
    status, _ = request('/api/download/action', {'action': 'cancel', 'task_ids': 'invalid'})
    assert status == 400


def test_batch_cancel_remove_api_finishes_without_browser_followup(web_settings):
    request, server, _ = web_settings
    tasks = [server.task_manager.add_task(f'https://missav.ai/cancel-remove-{i}') for i in range(2)]
    tasks[0].status = 'completed'
    ids = [task.task_id for task in tasks]
    status, result = request('/api/download/action', {'action': 'cancel_remove', 'task_ids': ids})
    assert status == 200 and result['success']
    assert all(server.task_manager.get_task(tid) is None for tid in ids)


def test_batch_cancel_remove_preserves_unverified_files(web_settings):
    request, server, root = web_settings
    task = server.task_manager.add_task('https://missav.ai/unverified-file')
    task.status = 'failed'
    task.cleanup_unverified = True
    workspace = Path(task.dest) / f'.uav-{task.task_id}-unknown'
    workspace.mkdir(parents=True)
    file = workspace / 'fragment.part'
    file.write_bytes(b'preserve')
    status, result = request('/api/download/action', {'action': 'cancel_remove', 'task_ids': [task.task_id]})
    assert status == 200 and result['success']
    assert result['unverified'] == [task.task_id]
    assert server.task_manager.get_task(task.task_id) is None
    assert file.read_bytes() == b'preserve'
