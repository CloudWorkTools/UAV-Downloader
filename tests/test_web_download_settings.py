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
    for tid, status in [('pending', 'pending'), ('paused', 'paused'), ('active', 'downloading'), ('other', 'pending'), ('done', 'completed')]:
        manager._tasks[tid] = DownloadTask(tid, 'https://missav.ai/' + tid, str(old), status=status,
                                           enable_subtitles=True, subtitle_mode='zh')
    manager._active['active'] = manager._tasks['active']
    from uav_downloader.daemon.download_files import DownloadFiles
    manager._tasks['paused'].files = DownloadFiles(str(old), 'paused')
    workdir = manager._tasks['paused'].files.workdir
    target = str(root / 'new')
    code, result = request('/api/download/action', {'action': 'update_settings',
        'task_ids': ['pending', 'paused', 'active', 'done', 'missing', 'paused'],
        'storage': 'local', 'dest': target, 'subtitle_mode': 'none'})
    assert code == 200
    assert result['updated'] == ['pending', 'paused']
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
    assert manager.get_task('active').dest == str(old)


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
